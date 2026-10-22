import hashlib
import secrets
from datetime import timedelta

from odoo import api, fields, models


def _hash_token(token):
    return hashlib.sha256(token.encode()).hexdigest()


class MobileDevice(models.Model):
    _name = 'mobile.device'
    _description = 'Mobile Device'
    _order = 'last_seen desc, id desc'

    name = fields.Char(string='Device name')
    user_id = fields.Many2one('res.users', required=True, ondelete='cascade', index=True)
    token_hash = fields.Char(required=True, index=True, copy=False, groups='base.group_system')
    device_uid = fields.Char(string='Device UID', index=True, help='Stable identifier sent by the app (one active login per device and user).')
    platform = fields.Selection([('android', 'Android'), ('ios', 'iOS')])
    app_version = fields.Char()
    push_token = fields.Char(copy=False, groups='base.group_system')
    push_key = fields.Char(copy=False, help='Where the push token comes from: "gateway" or "firebase:<project>". '
                                            'Tokens of another Firebase project cannot be used.')
    push_pubkey = fields.Char(copy=False, groups='base.group_system',
                              help='Public key of the phone (base64, 32 bytes) for end-to-end encrypted notifications '
                                   '(iPhone, push gateway). The private key never leaves the phone.')
    last_seen = fields.Datetime()
    active = fields.Boolean(default=True)

    _token_hash_uniq = models.Constraint('unique(token_hash)', 'Device token must be unique.')

    @api.model
    def _find_by_token(self, token):
        return self.search([('token_hash', '=', _hash_token(token))], limit=1)

    @api.model
    def _mobile_register(self, user, info):
        """Create a device login for `user` and return `(device, clear_token)`.

        The clear token is only ever returned here; the database keeps its hash.
        """
        info = info or {}
        device_uid = info.get('device_uid')
        if device_uid:
            self.search([('user_id', '=', user.id), ('device_uid', '=', device_uid)]).write({
                'active': False,
                'push_token': False,
            })
        token = secrets.token_urlsafe(32)
        device = self.create({
            'name': (info.get('name') or '')[:128] or 'Mobile',
            'user_id': user.id,
            'token_hash': _hash_token(token),
            'device_uid': device_uid,
            'platform': info.get('platform') if info.get('platform') in ('android', 'ios') else False,
            'app_version': info.get('app_version'),
            'last_seen': fields.Datetime.now(),
        })
        return device, token

    def _mobile_touch(self):
        """Refresh `last_seen` at most hourly to avoid a write (and lock) on every request."""
        threshold = fields.Datetime.now() - timedelta(hours=1)
        stale = self.filtered(lambda d: not d.last_seen or d.last_seen < threshold)
        if stale:
            stale.write({'last_seen': fields.Datetime.now()})

    def _mobile_revoke(self):
        self.write({'active': False, 'push_token': False})

    def action_revoke(self):
        self._mobile_revoke()
