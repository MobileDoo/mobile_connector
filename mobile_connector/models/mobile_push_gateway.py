"""The push gateway as the customer's Odoo sees it: who the messages are for (without saying who), how many
accounts and phones this Odoo has (heartbeat), and where the subscription stands (status).

Nothing personal leaves this Odoo: `user_ref` is a pseudonym (a keyed hash of the user id, key kept here), and
the heartbeat only holds totals.
"""
import hashlib
import hmac
import json
import logging
import secrets

import requests

from odoo import _, api, fields, models, release

_logger = logging.getLogger(__name__)

TIMEOUT = 15
STATUS_PARAM = 'mobile_connector.gateway_status'
REF_KEY_PARAM = 'mobile_connector.push_ref_key'


class MobilePushGateway(models.AbstractModel):
    _name = 'mobile.push.gateway'
    _description = 'Mobile Push Gateway: pseudonyms, heartbeat, status'

    @api.model
    def _connection(self):
        """(url, token) when push goes through the gateway and it is set up, else None."""
        if self.env['mobile.api']._mobile_push_provider() != 'gateway':
            return None
        icp = self.env['ir.config_parameter'].sudo()
        url = (icp.get_param('mobile_connector.gateway_url') or '').rstrip('/')
        token = icp.get_param('mobile_connector.gateway_token')
        return (url, token) if url and token else None

    # ------------------------------------------------------------------
    # Pseudonyms
    # ------------------------------------------------------------------

    @api.model
    def _ref_key(self):
        icp = self.env['ir.config_parameter'].sudo()
        key = icp.get_param(REF_KEY_PARAM)
        if not key:
            key = secrets.token_hex(32)
            icp.set_param(REF_KEY_PARAM, key)
        return key

    @api.model
    def _user_refs(self, users):
        """{user id: pseudonym}: the same user always gets the same one, nobody can go back to the user without the key."""
        key = self._ref_key().encode()
        return {user.id: hmac.new(key, f'user:{user.id}'.encode(), hashlib.sha256).hexdigest()[:32] for user in users}

    # ------------------------------------------------------------------
    # Heartbeat and status
    # ------------------------------------------------------------------

    @api.model
    def _heartbeat(self):
        """Totals only: no name, no email, no token."""
        devices = self.env['mobile.device'].sudo().search([
            ('push_token', '!=', False), ('push_key', 'in', [False, 'gateway']),
            ('user_id.active', '=', True), ('user_id.share', '=', False)])
        module = self.env['ir.module.module'].sudo().search([('name', '=', 'mobile_connector')], limit=1)
        return {
            'users_total': self.env['res.users'].sudo().search_count([('active', '=', True), ('share', '=', False)]),
            'users_registered': len(devices.user_id),
            'devices': len(devices),
            'odoo_version': release.series,
            'module_version': module.installed_version or '',
        }

    @api.model
    def _store_status(self, status):
        self.env['ir.config_parameter'].sudo().set_param(
            STATUS_PARAM, json.dumps(dict(status, fetched_at=fields.Datetime.to_string(fields.Datetime.now()))))

    @api.model
    def _status(self):
        """Last status the gateway gave ({} when never)."""
        try:
            return json.loads(self.env['ir.config_parameter'].sudo().get_param(STATUS_PARAM) or '{}')
        except ValueError:
            return {}

    @api.model
    def _sync(self):
        """Tell the gateway how big this Odoo is and keep what it answers (subscription status). Returns an error text
        or False. A gateway that does not know the heartbeat yet (older) is not an error."""
        connection = self._connection()
        if not connection:
            return False
        url, token = connection
        try:
            response = requests.post(f'{url}/v1/heartbeat', json=self._heartbeat(), timeout=TIMEOUT,
                                     headers={'Authorization': f'Bearer {token}'})
            if response.status_code == 404:
                return False
            response.raise_for_status()
            self._store_status(response.json())
        except Exception as exc:  # network, HTTP, bad JSON: the next run tries again
            _logger.warning('Mobile push gateway heartbeat failed: %s', exc)
            return str(exc)
        return False

    @api.model
    def _supports(self, capability):
        """Whether the gateway said, in its last answer, that it can do `capability` (an older one says nothing)."""
        return capability in (self._status().get('capabilities') or [])

    @api.model
    def _cron_sync(self):
        self._sync()
