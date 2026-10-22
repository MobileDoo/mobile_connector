import logging

from odoo import models

_logger = logging.getLogger(__name__)


class ResUsers(models.Model):
    _inherit = 'res.users'

    def _set_password(self):
        """Every password change (own change, admin reset, reset by e-mail) logs the phones out.

        Same idea as Odoo's web sessions, which are invalidated by a password
        change: a lost phone must not stay logged in once the password is changed.
        """
        super()._set_password()
        self._mobile_revoke_devices('password changed')

    def write(self, vals):
        result = super().write(vals)
        if vals.get('active') is False:
            self._mobile_revoke_devices('user archived')
        return result

    def _mobile_revoke_devices(self, reason):
        devices = self.env['mobile.device'].sudo().search([('user_id', 'in', self.ids)])
        if devices:
            devices._mobile_revoke()
            _logger.info('mobile: revoked %d device(s) of user(s) %s: %s', len(devices), self.ids, reason)
