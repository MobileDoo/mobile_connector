import base64
import functools
import hashlib
import io
import json
import os
from urllib.parse import urlencode

from PIL import Image

from odoo import api, models, release
from odoo.fields import Domain
from odoo.tools import SQL

# Bump the minor version for backwards-compatible additions, the major one for breaking changes.
API_VERSION = '1.19'
DEFAULT_POLL_INTERVAL = 20
# App UI strings per Odoo language code: app_i18n/fr_FR.json, app_i18n/ja_JP.json...
APP_I18N_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'app_i18n')


@functools.lru_cache(maxsize=None)
def _read_app_i18n(code):
    with open(os.path.join(APP_I18N_DIR, f'{code}.json'), encoding='utf-8') as handle:
        return json.load(handle)


def _deep_update(target, source):
    for key, value in source.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _deep_update(target[key], value)
        else:
            target[key] = value
    return target


class MobileApi(models.AbstractModel):
    """Site-level information shared by every mobile feature.

    Feature modules (mobile_hr_holidays, ...) `_inherit` this model and extend
    `_mobile_features` so the app knows which native screens to enable, and
    `_mobile_list_versions` so the app can detect changes in their lists.
    """

    _name = 'mobile.api'
    _description = 'Mobile API'

    @api.model
    def _mobile_features(self):
        features = ['core', 'activities', 'notifications']
        # The overview exists once an installed feature module brings cards for it.
        if self.env['mobile.dashboard.service']._card_defs():
            features.append('dashboard')
        return features

    @api.model
    def _mobile_record_services(self):
        """{key: service model} served by the generic `/mobile/api/v1/records/<key>/...` routes."""
        return {}

    @api.model
    def _mobile_push_provider(self):
        """none | firebase (customer's own project: free, Android) | gateway (push gateway: Android + iOS)."""
        icp = self.env['ir.config_parameter'].sudo()
        provider = icp.get_param('mobile_connector.push_provider')
        if provider in ('none', 'firebase', 'gateway'):
            return provider
        # Before API 1.13 the gateway was the only way.
        configured = icp.get_param('mobile_connector.gateway_url') and icp.get_param('mobile_connector.gateway_token')
        return 'gateway' if configured else 'none'

    @api.model
    def _mobile_push_info(self):
        """What the app needs to set push up (public settings only, never the service account).

        `key` identifies the push configuration: a phone receives push from one
        configuration at a time, so switching to a server with another key resets
        the phone's notification setup (the app warns first).
        """
        provider = self._mobile_push_provider()
        if provider == 'firebase':
            fcm = self.env['mobile.push.fcm']
            android = fcm._android_config()
            if android and fcm._service_account():
                return {'provider': 'firebase', 'key': fcm._push_key(), 'android': android, 'ios': False}
            provider = 'none'
        if provider == 'gateway':
            return {'provider': 'gateway', 'key': 'gateway', 'android': False, 'ios': True}
        return {'provider': 'none', 'key': False, 'android': False, 'ios': False}

    @api.model
    def _mobile_site_company(self):
        """The company that names and brands the site in the app (the first one)."""
        return self.env['res.company'].sudo().search([], limit=1, order='id')

    @api.model
    def _mobile_server_url(self):
        """Address the phones use to reach this Odoo (Settings > Mobile), else the web base URL."""
        icp = self.env['ir.config_parameter'].sudo()
        return (icp.get_param('mobile_connector.public_url') or icp.get_param('web.base.url') or '').rstrip('/')

    @api.model
    def _mobile_connect_code(self, url=None):
        """Content of the "connect the app" QR code: server address and database.

        `odoomobile://` opens the app when scanned with the phone's camera too.
        """
        return 'odoomobile://connect?' + urlencode({'url': url or self._mobile_server_url(), 'db': self.env.cr.dbname})

    @api.model
    def _mobile_connect_qr(self, url=None, size=320):
        """PNG (base64) of the connect QR code, made by Odoo's own barcode generator."""
        png = self.env['ir.actions.report'].barcode('QR', self._mobile_connect_code(url), width=size, height=size,
                                                    barLevel='M')
        # Scanners need a white margin (quiet zone) around the code; the generated image has none.
        image = Image.open(io.BytesIO(png)).convert('RGB')
        margin = size // 10
        framed = Image.new('RGB', (image.width + 2 * margin, image.height + 2 * margin), 'white')
        framed.paste(image, (margin, margin))
        output = io.BytesIO()
        framed.save(output, format='PNG')
        return base64.b64encode(output.getvalue())

    @api.model
    def _mobile_logo_version(self, company):
        """Checksum of the mobile logo (False = none): the app downloads it again only when it changes."""
        attachment = self.env['ir.attachment'].sudo().search([
            ('res_model', '=', 'res.company'), ('res_field', '=', 'mobile_logo'), ('res_id', '=', company.id),
        ], limit=1)
        return attachment.checksum or False

    @api.model
    def _mobile_login_background_version(self, company):
        """Checksum of the login background (False = none): the app downloads it again only when it changes."""
        attachment = self.env['ir.attachment'].sudo().search([
            ('res_model', '=', 'res.company'), ('res_field', '=', 'mobile_login_background'),
            ('res_id', '=', company.id),
        ], limit=1)
        return attachment.checksum or False

    @api.model
    def _mobile_info(self):
        icp = self.env['ir.config_parameter'].sudo()
        company = self._mobile_site_company()
        return {
            'api_version': API_VERSION,
            'odoo_version': release.version,
            'odoo_version_info': list(release.version_info[:2]),
            'site_id': icp.get_param('database.uuid'),
            'site_name': company.name,
            # Settings > Mobile > App logo, fetched from /mobile/api/v1/logo (since API 1.6).
            'logo': self._mobile_logo_version(company),
            # Settings > Mobile > Login background, fetched from /mobile/api/v1/login_background (since API 1.17).
            'login_background': self._mobile_login_background_version(company),
            # API 1.13: push provider and the public Firebase settings of the Android app.
            'push': self._mobile_push_info(),
            'features': sorted(set(self._mobile_features())),
            # Languages the app may ask for with X-Mobile-Lang.
            'languages': [[code, name] for code, name in self.env['res.lang'].get_installed()],
            # API 1.16: how each language writes dates, times and numbers (Settings > Translations > Languages).
            'language_formats': self._mobile_language_formats(),
            'policy': self._mobile_policy(),
        }

    @api.model
    def _mobile_language_formats(self):
        """`{code: {...}}` of the enabled languages: the app shows dates and times the way the company set them."""
        formats = {}
        for lang in self.env['res.lang'].sudo().search([('active', '=', True)]):
            formats[lang.code] = {
                # strftime patterns, as in Odoo: '%d/%m/%Y', '%H:%M:%S'.
                'date_format': lang.date_format,
                'time_format': lang.time_format,
                # 1 = Monday ... 7 = Sunday.
                'week_start': int(lang.week_start or 1),
                'decimal_point': lang.decimal_point,
                'thousands_sep': lang.thousands_sep or '',
            }
        return formats

    @api.model
    def _mobile_policy(self):
        """Security rules the app must enforce for this company (Settings > Mobile)."""
        icp = self.env['ir.config_parameter'].sudo()
        return {
            # Unlock the app with Face ID / fingerprint / device passcode.
            'require_app_lock': icp.get_param('mobile_connector.require_app_lock') == 'True',
        }

    @api.model
    def _mobile_user_info(self, user):
        # Evaluate in the user's own env: company-dependent fields such as
        # `employee_id` are empty in the anonymous env of the login route.
        user = user.with_user(user).sudo()
        info = {
            'uid': user.id,
            'name': user.name,
            'login': user.login,
            'lang': user.lang,
            'tz': user.tz,
            'partner_id': user.partner_id.id,
            'company_id': user.company_id.id,
            'companies': [[c.id, c.name] for c in user.company_ids],
        }
        if 'employee_id' in user._fields:
            employee = user.employee_id
            info['employee'] = {
                'id': employee.id,
                'name': employee.name,
                'job_title': employee.job_title or False,
                'department': [employee.department_id.id, employee.department_id.name] if employee.department_id else False,
            } if employee else False
        return info

    # ------------------------------------------------------------------
    # Per-user access
    # ------------------------------------------------------------------

    @api.model
    def _mobile_access(self):
        """What the CURRENT user may do: {feature: {capability: bool}}.

        `_mobile_features` says what is installed on the site; this says what
        this user can use. Feature modules extend it from the user's groups and
        access rights, and only add a feature when at least one capability is
        granted. The app builds its menus from this; controllers enforce it
        with `require_access` (hiding is not security).
        """
        access = {'core': {'use': True}}
        if not self.env.user.share:
            # "My activities" inbox: every internal user has activities; record-level rights still apply.
            access['activities'] = {'view': True}
            # Notification center = the user's Odoo Inbox (since API 1.7).
            access['notifications'] = {'view': True}
            if self.env['mobile.menu.service']._mobile_enabled():
                # Odoo's own menus (any module, custom ones included), as the user sees them on the web.
                access['menus'] = {'view': True}
        return access

    @api.model
    def _mobile_can(self, feature, capability=None):
        capabilities = self._mobile_access().get(feature)
        return bool(capabilities) and (not capability or bool(capabilities.get(capability)))

    @api.model
    def _mobile_access_version(self):
        """Changes whenever the user's access changes (groups, companies, installed features)."""
        payload = json.dumps(
            [
                self._mobile_access(),
                sorted(self._mobile_features()),
                sorted(self.env.user.company_ids.ids),
                # The app follows the user's web language: changing it on Odoo updates the app.
                self.env.user.lang,
            ],
            sort_keys=True,
        )
        return hashlib.sha1(payload.encode()).hexdigest()

    # ------------------------------------------------------------------
    # App UI translations
    # ------------------------------------------------------------------

    @api.model
    def _mobile_app_i18n_code(self, lang):
        """File to use for an Odoo language: exact match (fr_BE.json), else same language (fr_FR.json)."""
        if not lang or not os.path.isdir(APP_I18N_DIR):
            return None
        available = sorted(name[:-5] for name in os.listdir(APP_I18N_DIR) if name.endswith('.json'))
        if lang in available:
            return lang
        base = lang.split('_')[0].split('@')[0]
        return next((code for code in available if code.split('_')[0] == base), None)

    @api.model
    def _mobile_app_translations(self, lang):
        """App UI strings for `lang`, nested like the app's own (missing keys fall back to English in the app).

        Override to add or adjust strings for a customer (call super, then update the dict).
        """
        code = self._mobile_app_i18n_code(lang)
        return json.loads(json.dumps(_read_app_i18n(code))) if code else {}

    @api.model
    def _mobile_i18n(self, lang):
        messages = self._mobile_app_translations(lang)
        return {
            'lang': self._mobile_app_i18n_code(lang) or False,
            'version': hashlib.sha1(json.dumps(messages, sort_keys=True).encode()).hexdigest(),
            'messages': messages,
        }

    @api.model
    def _mobile_text_domain(self, model_name, query):
        """Search-box domain on the fields Odoo's own name search uses (`display_name` is not searchable in 17)."""
        Model = self.env[model_name]
        fnames = Model._rec_names_search or ([Model._rec_name] if Model._rec_name else [])
        fnames = [f for f in fnames if f.split('.')[0] in Model._fields]
        if not fnames:
            return [('id', '=', int(query))] if query.isdigit() else [('id', '=', 0)]
        return Domain.OR([[(fname, 'ilike', query)] for fname in fnames])

    # ------------------------------------------------------------------
    # Change detection
    # ------------------------------------------------------------------

    @api.model
    def _mobile_poll_interval(self):
        value = self.env['ir.config_parameter'].sudo().get_param('mobile_connector.poll_interval')
        try:
            return max(int(value), 5) if value else DEFAULT_POLL_INTERVAL
        except ValueError:
            return DEFAULT_POLL_INTERVAL

    @api.model
    def _mobile_record_versions(self, model_name, ids):
        """{id: version} for the records the user can still read (missing = deleted or no access).

        The version is `write_date` read straight from SQL to keep microseconds:
        the ORM rounds datetimes to the second, which would hide two writes
        made within the same second.
        """
        Model = self.env[model_name].with_context(active_test=False)
        visible = Model.search([('id', 'in', list(ids))]).ids
        if not visible:
            return {}
        Model.flush_model(['write_date'])
        self.env.cr.execute(f'SELECT id, write_date FROM "{Model._table}" WHERE id = ANY(%s)', [visible])
        return {rec_id: write_date.isoformat() if write_date else '' for rec_id, write_date in self.env.cr.fetchall()}

    @api.model
    def _mobile_domain_version(self, model_name, domain):
        """Version of a list: changes when a matching record is created, updated or removed.

        One aggregate query (record rules applied): lists of generic menus can
        cover very large tables, so ids are never loaded.
        """
        Model = self.env[model_name]
        query = Model._search(domain)
        if query.is_empty():
            return '0'
        Model.flush_model(['write_date'])
        self.env.cr.execute(query.select(
            SQL('count(*)'),
            # Odoo 20: `query.table` is an object whose attributes are the columns (SQL.identifier takes a name).
            SQL('max(%s)', query.table.write_date),
            SQL('max(%s)', query.table.id),
        ))
        count, write_date, max_id = self.env.cr.fetchone()
        if not count:
            return '0'
        return f"{count}:{write_date.isoformat() if write_date else ''}:{max_id}"

    @api.model
    def _mobile_list_versions(self, keys):
        """{key: version} for the list keys this module knows; feature modules extend it.

        Unknown keys are simply absent from the result.
        """
        versions = {}
        if 'core.access' in keys:
            versions['core.access'] = self._mobile_access_version()
        if 'activities.mine' in keys and self._mobile_can('activities'):
            versions['activities.mine'] = self.env['mobile.activity.service']._mobile_version()
        if 'notifications.inbox' in keys and self._mobile_can('notifications'):
            versions['notifications.inbox'] = self.env['mobile.notification.service']._mobile_version()
        if self._mobile_can('menus'):
            versions.update(self.env['mobile.menu.service']._mobile_list_versions(
                [key for key in keys if key.startswith(('menu.', 'kpi.'))]))
        services = self._mobile_record_services()
        for key in keys:
            # records.<service>.<scope>: generic list of a record service, only if the user may see it.
            if not key.startswith('records.'):
                continue
            _prefix, service_key, scope = (key.split('.', 2) + ['', ''])[:3]
            service_name = services.get(service_key)
            if not service_name:
                continue
            service = self.env[service_name]
            capability = service._mobile_scope_capabilities.get(scope)
            if capability and self._mobile_can(service._mobile_feature, capability):
                versions[key] = service._mobile_list_version(scope)
        return versions
