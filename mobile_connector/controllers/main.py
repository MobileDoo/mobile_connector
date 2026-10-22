import re
from collections import defaultdict

from odoo import http
from odoo.exceptions import AccessDenied
from odoo.http import request

from ..models.mobile_push_e2e import public_key
from .utils import MobileApiError, get_record, mobile_route

MAX_WATCHED = 100


class MobileConnectorController(http.Controller):

    # ------------------------------------------------------------------
    # Site & auth
    # ------------------------------------------------------------------

    @mobile_route('/mobile/api/v1/info', auth='none', methods=('GET', 'POST'))
    def info(self, payload):
        return request.env['mobile.api'].sudo()._mobile_info()

    @mobile_route('/mobile/api/v1/i18n', auth='none', methods=('GET', 'POST'))
    def i18n(self, payload):
        """App UI strings for an Odoo language code (`{"lang": "fr_FR"}`); public, they are not data."""
        lang = payload.get('lang') or request.httprequest.args.get('lang')
        if not isinstance(lang, str):
            raise MobileApiError('bad_request', 'lang is required')
        return request.env['mobile.api'].sudo()._mobile_i18n(lang)

    @mobile_route('/mobile/api/v1/auth/login', auth='none')
    def login(self, payload):
        """Exchange credentials for a long-lived, revocable device token.

        The database is the one this request is routed to (single database,
        dbfilter, or the `session_id` cookie bound by /web/session/authenticate
        on multi-database servers).
        """
        if not request.db:
            raise MobileApiError('database_required', 'No database selected on this server', 400)
        if payload.get('db') and payload['db'] != request.db:
            raise MobileApiError('database_mismatch', f'This request is bound to database {request.db}', 400)
        login, password = payload.get('login'), payload.get('password')
        if not login or not password:
            raise MobileApiError('bad_request', 'login and password are required')

        user_agent_env = {
            'interactive': True,
            'base_location': request.httprequest.url_root.rstrip('/'),
            'HTTP_HOST': request.httprequest.environ.get('HTTP_HOST', ''),
            'REMOTE_ADDR': request.httprequest.environ.get('REMOTE_ADDR'),
        }
        try:
            uid = request.env['res.users'].authenticate(request.db, login, password, user_agent_env)
        except AccessDenied:
            raise MobileApiError('invalid_credentials', 'Wrong login or password', 401)

        user = request.env['res.users'].sudo().browse(uid)
        if user.share:
            raise MobileApiError('not_internal_user', 'Only internal users can use the mobile app', 403)
        self._check_totp(user, payload.get('totp_code'))

        device, token = request.env['mobile.device'].sudo()._mobile_register(user, payload.get('device'))
        api = request.env['mobile.api'].sudo()
        # Access is computed as the user (not sudo): that user's groups and ACLs.
        user_api = request.env['mobile.api'].with_user(user)
        return {
            'device_token': token,
            'device_id': device.id,
            'user': api._mobile_user_info(user),
            'site': api._mobile_info(),
            'access': user_api._mobile_access(),
            'access_version': user_api._mobile_access_version(),
        }

    def _check_totp(self, user, code):
        if 'totp_enabled' not in user._fields or not user.totp_enabled:
            return
        if not code:
            raise MobileApiError('totp_required', 'Two-factor authentication code required', 401)
        try:
            user._totp_check(int(re.sub(r'\s', '', str(code))))
        except (AccessDenied, ValueError):
            raise MobileApiError('totp_invalid', 'Invalid two-factor authentication code', 401)

    @mobile_route('/mobile/api/v1/auth/logout')
    def logout(self, payload):
        self._current_device()._mobile_revoke()
        return True

    @mobile_route('/mobile/api/v1/me')
    def me(self, payload):
        api = request.env['mobile.api']
        return {
            'user': api._mobile_user_info(request.env.user),
            'site': api.sudo()._mobile_info(),
            'access': api._mobile_access(),
            'access_version': api._mobile_access_version(),
        }

    @mobile_route('/mobile/api/v1/device/push_token')
    def push_token(self, payload):
        """Store (or clear with null) the FCM token of the calling device, and the public key of the phone for end-to-end
        encrypted notifications (iPhone)."""
        device = self._current_device()
        values = {'push_token': payload.get('push_token') or False}
        if 'push_key' in payload:
            values['push_key'] = payload['push_key'] if isinstance(payload['push_key'], str) else False
        if 'push_pubkey' in payload:
            pubkey = payload['push_pubkey']
            if pubkey and (not isinstance(pubkey, str) or public_key(pubkey) is None):
                raise MobileApiError('bad_request', 'push_pubkey must be a public key: 32 bytes in base64')
            values['push_pubkey'] = pubkey or False
        if not values['push_token']:
            values['push_pubkey'] = False  # no token, nobody to encrypt for
        if payload.get('platform') in ('android', 'ios'):
            values['platform'] = payload['platform']
        if payload.get('app_version'):
            values['app_version'] = payload['app_version']
        device.write(values)
        return True

    def _current_device(self):
        token = (request.httprequest.headers.get('Authorization') or '').partition(' ')[2].strip()
        device = request.env['mobile.device'].sudo()._find_by_token(token)
        if not device:
            raise MobileApiError('unauthorized', 'Unknown device', 401)
        return device

    # ------------------------------------------------------------------
    # Change detection
    # ------------------------------------------------------------------

    @mobile_route('/mobile/api/v1/changes')
    def changes(self, payload):
        """Which of the watched records/lists changed since the versions the app holds.

        Request:  {"records": [{"model", "id", "version"}], "lists": [{"key", "version"}]}
        Response: only what changed, plus the polling interval the app should use.
        A record the user can no longer read comes back with `deleted: true`.
        """
        api = request.env['mobile.api']
        by_model = defaultdict(dict)
        for item in (payload.get('records') or [])[:MAX_WATCHED]:
            model, rec_id = item.get('model'), item.get('id')
            if model in request.env and isinstance(rec_id, int):
                by_model[model][rec_id] = item.get('version')

        changed_records = []
        for model, watched in by_model.items():
            versions = api._mobile_record_versions(model, list(watched))
            for rec_id, known in watched.items():
                current = versions.get(rec_id)
                if current != known:
                    changed_records.append({
                        'model': model, 'id': rec_id, 'version': current or False, 'deleted': current is None,
                    })

        watched_lists = {
            item['key']: item.get('version')
            for item in (payload.get('lists') or [])[:MAX_WATCHED]
            if isinstance(item.get('key'), str)
        }
        list_versions = api._mobile_list_versions(list(watched_lists))
        changed_lists = [
            {'key': key, 'version': version}
            for key, version in list_versions.items()
            if key in watched_lists and watched_lists[key] != version
        ]
        return {
            'records': changed_records,
            'lists': changed_lists,
            'poll_interval': api._mobile_poll_interval(),
        }

    # ------------------------------------------------------------------
    # Generic forms (used for wizards and as a fallback for any model)
    # ------------------------------------------------------------------

    @mobile_route('/mobile/api/v1/form/name_search')
    def form_name_search(self, payload):
        model = self._model(payload)
        record = get_record(model, payload.get('id'))
        return request.env['mobile.form.spec'].with_context(**(payload.get('context') or {}))._name_search(
            model, payload.get('field'), payload.get('query') or '',
            record=record, values=payload.get('values'),
            limit=min(int(payload.get('limit') or 20), 80), view_id=payload.get('view_id') or None,
        )

    @mobile_route('/mobile/api/v1/wizard/spec')
    def wizard_spec(self, payload):
        engine = request.env['mobile.form.spec']
        spec = engine._wizard_spec(
            self._model(payload), context=payload.get('context') or {}, res_id=payload.get('res_id'),
            view_id=payload.get('view_id') or None, values=payload.get('values'), changed=payload.get('changed'),
        )
        return engine._public(spec)

    @mobile_route('/mobile/api/v1/wizard/execute')
    def wizard_execute(self, payload):
        return request.env['mobile.form.spec']._wizard_execute(
            self._model(payload), payload.get('key'), context=payload.get('context') or {},
            res_id=payload.get('res_id'), view_id=payload.get('view_id') or None, values=payload.get('values'),
        )

    def _model(self, payload):
        model = payload.get('model')
        if not model or model not in request.env:
            raise MobileApiError('bad_request', 'Unknown model')
        return model
