import logging
import os
import time

import werkzeug.exceptions

from odoo import http, models
from odoo.http import request

_logger = logging.getLogger(__name__)

# Refresh the routing session at most this often (seconds).
SESSION_KEEPALIVE = 3600


class IrHttp(models.AbstractModel):
    _inherit = 'ir.http'

    @classmethod
    def _auth_method_mobile(cls):
        """`auth='mobile'`: authenticate with the device token issued at login.

        The token travels as `Authorization: Bearer <token>`. Session cookies are
        deliberately NOT accepted here, so mobile routes can be `csrf=False`
        without opening a CSRF hole for logged-in browser users.
        """
        header = request.httprequest.headers.get('Authorization') or ''
        scheme, _sep, token = header.partition(' ')
        if scheme.lower() != 'bearer' or not token.strip():
            raise werkzeug.exceptions.Unauthorized('Missing bearer token')

        device = request.env['mobile.device'].sudo()._find_by_token(token.strip())
        if not device or not device.user_id.active:
            raise werkzeug.exceptions.Unauthorized('Invalid or revoked device token')

        user = device.user_id
        request.update_env(user=user.id)
        context = {'lang': cls._mobile_lang(user), 'tz': user.tz}
        companies = cls._mobile_allowed_companies(user)
        if companies:
            context['allowed_company_ids'] = companies
        request.update_context(**context)
        device._mobile_touch()
        cls._mobile_keep_session_alive()

    @classmethod
    def _mobile_keep_session_alive(cls):
        """Keep the `session_id` cookie of multi-database servers valid while the app is used.

        Without dbfilter, that cookie is what routes the app's requests to its
        database. Odoo deletes sessions untouched for `sessions.max_inactivity_seconds`
        (7 days) based on the file's modification time, and mobile routes never
        save the session (the device token authenticates). So bump the file's
        mtime, at most hourly, without rewriting it (no race with the web client).
        """
        sid = request.httprequest.cookies.get('session_id')
        session = request.session
        if not sid or session.is_new or not session.db:
            return  # single database / dbfilter: the app sends no cookie, nothing to keep alive
        store = http.root.session_store
        try:
            path = store.get_session_filename(sid)
            if time.time() - os.path.getmtime(path) > SESSION_KEEPALIVE:
                os.utime(path, None)
        except (AttributeError, OSError, ValueError):
            # Non-filesystem session store or file just vacuumed: nothing we can refresh.
            _logger.debug('mobile: cannot refresh session %s', sid[:8], exc_info=True)

    @classmethod
    def _mobile_lang(cls, user):
        """Language chosen in the app (`X-Mobile-Lang: vi_VN`) if enabled on Odoo, else the user's own.

        Field labels, selections, buttons and translated business messages
        then match the language the app displays.
        """
        wanted = (request.httprequest.headers.get('X-Mobile-Lang') or '').strip()
        if wanted and wanted != user.lang and request.env['res.lang'].sudo()._lang_get(wanted):
            return wanted
        return user.lang

    @classmethod
    def _mobile_allowed_companies(cls, user):
        """Honour `X-Mobile-Company-Ids: 1,3` (first = current company) if the user may access them."""
        raw = request.httprequest.headers.get('X-Mobile-Company-Ids')
        if not raw:
            return []
        try:
            wanted = [int(cid) for cid in raw.split(',') if cid.strip()]
        except ValueError:
            return []
        allowed = set(user.sudo().company_ids.ids)
        return [cid for cid in wanted if cid in allowed]
