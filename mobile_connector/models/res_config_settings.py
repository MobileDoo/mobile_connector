import base64
import json

from odoo import _, api, fields, models
from odoo.exceptions import UserError


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    mobile_push_provider = fields.Selection([
        ('none', 'Off'),
        ('firebase', 'Own Firebase project (free, Android only)'),
        ('gateway', 'Push gateway (Android and iPhone, paid service)'),
    ], string='Push notifications', default='none')
    mobile_firebase_google_services = fields.Binary(string='google-services.json', attachment=False)
    mobile_firebase_google_services_name = fields.Char()
    mobile_firebase_service_account = fields.Binary(string='Service account key', attachment=False,
                                                    help='JSON key of a service account of the Firebase project '
                                                         '(Project settings > Service accounts). Stays on the server.')
    mobile_firebase_service_account_name = fields.Char()
    mobile_firebase_status = fields.Char(compute='_compute_mobile_firebase_status', string='Firebase')
    mobile_gateway_url = fields.Char(string='Push gateway URL', config_parameter='mobile_connector.gateway_url')
    mobile_gateway_token = fields.Char(string='Push gateway site token', config_parameter='mobile_connector.gateway_token')
    mobile_push_ios_e2e = fields.Selection([
        ('preferred', 'End-to-end encrypted when the app supports it'),
        ('required', 'Always end-to-end encrypted (no iPhone gets plain text)'),
    ], string='iPhone notifications', default='preferred', config_parameter='mobile_connector.push_ios_e2e',
        help='With the push gateway, the content of a notification for an iPhone can be encrypted so that only that '
             'iPhone reads it: the gateway, Google and Apple only carry a sealed message. Android phones are not concerned.')
    mobile_gateway_notice = fields.Char(compute='_compute_mobile_gateway_notice', string='Subscription')
    mobile_gateway_severity = fields.Selection(
        [('info', 'Info'), ('warning', 'Warning'), ('danger', 'Problem')], compute='_compute_mobile_gateway_notice')
    mobile_push_activity = fields.Boolean(string='Push new activities', config_parameter='mobile_connector.push_activity')
    mobile_push_inbox = fields.Boolean(string='Push Inbox messages', config_parameter='mobile_connector.push_inbox')
    mobile_menu_browser = fields.Boolean(string='Odoo menus in the app', default=True)
    mobile_require_app_lock = fields.Boolean(
        string='Require biometric app lock', config_parameter='mobile_connector.require_app_lock')
    # Site-level like `site_name`: the site's main company, whatever company the admin is working in.
    mobile_site_company_id = fields.Many2one(
        'res.company', default=lambda self: self.env['mobile.api']._mobile_site_company())
    mobile_logo = fields.Image(related='mobile_site_company_id.mobile_logo', readonly=False)
    mobile_login_background = fields.Image(related='mobile_site_company_id.mobile_login_background', readonly=False)
    mobile_public_url = fields.Char(
        string='Server address for the app', config_parameter='mobile_connector.public_url',
        help='Address the phones use to reach this Odoo, e.g. https://erp.example.com. Empty: the web base URL.')
    mobile_connect_qr = fields.Binary(string='Connect QR code', compute='_compute_mobile_connect_qr')

    @api.depends('mobile_public_url')
    def _compute_mobile_connect_qr(self):
        api_model = self.env['mobile.api']
        for settings in self:
            url = (settings.mobile_public_url or '').strip().rstrip('/') or None
            settings.mobile_connect_qr = api_model._mobile_connect_qr(url)

    @api.depends('mobile_push_provider', 'mobile_gateway_url')
    def _compute_mobile_gateway_notice(self):
        """Where the subscription of the push gateway stands, as the gateway last told this Odoo."""
        status = self.env['mobile.push.gateway']._status()
        accounts, seats = status.get('accounts', 0), status.get('seats')
        usage = _('%(accounts)s accounts received a notification in the last 30 days', accounts=accounts)
        if not status:
            notice, severity = _('Not checked yet: the gateway reports once a day, or press "Check now".'), 'info'
        elif status.get('level') == 'suspended':
            notice, severity = _('The subscription of this site is suspended: notifications are not sent. Contact the '
                                 'provider of the push gateway.'), 'danger'
        elif status.get('soft_block'):
            notice, severity = _('%(usage)s, every seat (%(seats)s) is used: accounts that did not receive any yet are '
                                 'refused. Contact the provider to extend the subscription.', usage=usage, seats=seats), 'danger'
        elif status.get('level') == 'over':
            notice, severity = _('%(usage)s: more than the %(seats)s seats of the subscription. Contact the provider to '
                                 'extend it.', usage=usage, seats=seats), 'warning'
        elif status.get('level') == 'near':
            notice, severity = _('%(usage)s, almost all of the %(seats)s seats.', usage=usage, seats=seats), 'warning'
        else:
            notice = (_('%(usage)s (%(seats)s seats).', usage=usage, seats=seats) if seats is not None
                      else _('%(usage)s.', usage=usage))
            severity = 'info'
        if status.get('fetched_at'):
            notice = _('%(notice)s (checked %(when)s UTC)', notice=notice, when=status['fetched_at'][:16])
        for settings in self:
            settings.mobile_gateway_notice = notice
            settings.mobile_gateway_severity = severity

    def action_mobile_gateway_refresh(self):
        """Ask the gateway now (with the address and token typed on screen, saved first)."""
        self.set_values()
        if not self.env['mobile.push.gateway']._connection():
            raise UserError(_('Set the push gateway address and site token first.'))
        error = self.env['mobile.push.gateway']._sync()
        if error:
            raise UserError(_('The push gateway cannot be reached: %s', error))
        return {'type': 'ir.actions.client', 'tag': 'reload'}

    def action_mobile_firebase_test(self):
        """Test the Firebase configuration typed on screen (saved first), with the phones of the admin if any."""
        self.set_values()
        fcm = self.env['mobile.push.fcm']
        tokens = self.env['mobile.device'].sudo().search([
            ('user_id', '=', self.env.user.id), ('push_token', '!=', False), ('platform', '=', 'android'),
            ('push_key', '=', fcm._push_key())]).mapped('push_token')
        ok, message = fcm._test(tokens)
        return {'type': 'ir.actions.client', 'tag': 'display_notification', 'params': {
            'title': _('Firebase'), 'message': message, 'type': 'success' if ok else 'danger', 'sticky': not ok}}

    @api.depends('mobile_push_provider')
    def _compute_mobile_firebase_status(self):
        fcm = self.env['mobile.push.fcm']
        android, account = fcm._android_config(), fcm._service_account()
        if android and account:
            status = _('Project %(project)s · Android app %(app)s · sent as %(email)s',
                       project=android['project_id'], app=android['app_id'], email=account['client_email'])
        else:
            missing = [name for name, value in (('google-services.json', android), (_('service account key'), account))
                       if not value]
            status = _('Not configured: upload %s', _(' and ').join(missing))
        for settings in self:
            settings.mobile_firebase_status = status

    def get_values(self):
        res = super().get_values()
        res['mobile_push_provider'] = self.env['mobile.api']._mobile_push_provider()
        # Stored as 'True'/'False' (unset = on), unlike config_parameter booleans that drop False.
        res['mobile_menu_browser'] = self.env['mobile.menu.service']._mobile_enabled()
        return res

    def set_values(self):
        super().set_values()
        icp = self.env['ir.config_parameter'].sudo()
        icp.set_param('mobile_connector.push_provider', self.mobile_push_provider or 'none')
        fcm = self.env['mobile.push.fcm']
        android = account = None
        if self.mobile_firebase_google_services:
            android = fcm._parse_google_services(base64.b64decode(self.mobile_firebase_google_services))
        if self.mobile_firebase_service_account:
            account = fcm._parse_service_account(base64.b64decode(self.mobile_firebase_service_account))
        project_android = (android or fcm._android_config() or {}).get('project_id')
        project_account = (account or fcm._service_account() or {}).get('project_id')
        if (android or account) and project_android and project_account and project_android != project_account:
            raise UserError(_('google-services.json (project %(a)s) and the service account (project %(b)s) are '
                              'from different Firebase projects.', a=project_android, b=project_account))
        if android:
            icp.set_param('mobile_connector.firebase_android', json.dumps(android))
        if account:
            icp.set_param('mobile_connector.firebase_service_account', json.dumps(account))
        self.env['ir.config_parameter'].sudo().set_param('mobile_connector.menu_browser', str(bool(self.mobile_menu_browser)))
