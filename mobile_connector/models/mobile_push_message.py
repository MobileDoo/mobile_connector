import json
import logging
from datetime import timedelta

import requests

from odoo import api, fields, models

from .mobile_push_e2e import PushE2eError

_logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 3
GATEWAY_TIMEOUT = 15
GATEWAY_BATCH = 100  # messages per request: what the gateway accepts at most
# What the gateway says when it refuses a message, in words for the admin.
GATEWAY_ERRORS = {
    'seat_limit': 'The account is not among the seats of the subscription of the push gateway (soft block): ask the '
                  'provider to extend the subscription.',
}
RECEIPT_PAGE = 200
RECEIPT_WAIT = timedelta(seconds=3)  # first look for the results after handing messages over
NO_RECEIPT_AFTER = timedelta(hours=1)  # the gateway keeps receipts 7 days: after this, nobody is coming


class MobilePushMessage(models.Model):
    """Outgoing push queue.

    Messages are queued inside the business transaction and sent by a cron
    (triggered immediately), so a slow or unreachable gateway never blocks or
    rolls back the user's action.

    With the push gateway the hand-over is only a hand-over: the gateway queues the message (state
    `queued`), sends it to Firebase at its own pace and retries temporary failures; the result comes back
    as a receipt (`_cron_receipts`). A gateway without a queue (older) answers with the results at once.
    """

    _name = 'mobile.push.message'
    _description = 'Mobile Push Message'
    _order = 'id desc'

    user_id = fields.Many2one('res.users', required=True, ondelete='cascade', index=True)
    title = fields.Char(required=True)
    body = fields.Text()
    data = fields.Text(help='JSON payload delivered to the app (all values are sent as strings).')
    state = fields.Selection([
        ('pending', 'Pending'),
        ('queued', 'Queued'),
        ('sent', 'Sent'),
        ('skipped', 'No device'),
        ('failed', 'Failed'),
    ], default='pending', required=True, index=True)
    attempts = fields.Integer()
    error = fields.Text()
    queued_at = fields.Datetime(help='When the gateway took the message: no result after a while means it was lost.')

    @api.model
    def _enqueue(self, users, title, body='', data=None):
        """Queue one push per user. `data` is a flat dict routed by the app (model, res_id, type...)."""
        users = users.filtered(lambda u: u.active and not u.share)
        if users:
            # Only users with a push-enabled device: avoids filling the queue
            # for people who never installed the app.
            users = self.env['mobile.device'].sudo().search([
                ('user_id', 'in', users.ids),
                ('push_token', '!=', False),
            ]).user_id
        if not users:
            return self.browse()
        messages = self.sudo().create([{
            'user_id': user.id,
            'title': title,
            'body': body or '',
            'data': json.dumps(data or {}),
        } for user in users])
        cron = self.env.ref('mobile_connector.ir_cron_mobile_push_send', raise_if_not_found=False)
        if cron:
            cron.sudo()._trigger()
        return messages

    @api.model
    def _cron_send(self, limit=200):
        messages = self.search([('state', '=', 'pending')], limit=limit, order='id')
        if not messages:
            return
        provider = self.env['mobile.api']._mobile_push_provider()
        if provider == 'none':
            messages.write({'state': 'skipped', 'error': 'Push notifications are off (Settings > Mobile > Push).'})
            return
        if provider == 'firebase':
            return self._send_firebase(messages)
        icp = self.env['ir.config_parameter'].sudo()
        url = (icp.get_param('mobile_connector.gateway_url') or '').rstrip('/')
        token = icp.get_param('mobile_connector.gateway_token')
        if not url or not token:
            messages.write({'state': 'failed', 'error': 'Push gateway is not configured (Settings > Mobile).'})
            return

        site_id = icp.get_param('database.uuid')
        gateway = self.env['mobile.push.gateway']
        if not gateway._status():
            gateway._sync()  # what the gateway can do (end-to-end encryption...) is not known yet
        devices_by_user = {}
        for device in self.env['mobile.device'].sudo().search([
            ('user_id', 'in', messages.user_id.ids),
            ('push_token', '!=', False),
            # Tokens registered for the gateway's Firebase project (older apps send no key).
            ('push_key', 'in', [False, 'gateway']),
        ]):
            devices_by_user.setdefault(device.user_id.id, self.env['mobile.device'].sudo())
            devices_by_user[device.user_id.id] |= device

        to_send = []
        for message in messages:
            devices = devices_by_user.get(message.user_id.id)
            if not devices:
                message.state = 'skipped'
                continue
            to_send.append((message, devices))
        if not to_send:
            return

        for start in range(0, len(to_send), GATEWAY_BATCH):
            self._hand_over(url, token, site_id, to_send[start:start + GATEWAY_BATCH])

    @api.model
    def _hand_over(self, url, token, site_id, batch):
        """One request to the gateway for up to GATEWAY_BATCH messages."""
        gateway = self.env['mobile.push.gateway']
        refs = gateway._user_refs(self.env['res.users'].sudo().browse([m.user_id.id for m, _d in batch]))
        e2e = gateway._supports('e2e')
        messages, live = [], []
        for message, devices in batch:
            # A token cleared meanwhile (found dead by the answer to an earlier batch) is not one.
            content, why = self._gateway_message(message, devices.filtered('push_token'), site_id, refs[message.user_id.id], e2e)
            if content:
                messages.append(content)
                live.append((message, devices))
            else:
                message.write({'state': 'skipped', 'error': why or False})
        batch = live
        if not batch:
            return
        payload = {'mode': 'queue', 'messages': messages}

        try:
            response = requests.post(
                f'{url}/v1/send',
                json=payload,
                headers={'Authorization': f'Bearer {token}'},
                timeout=GATEWAY_TIMEOUT,
            )
            response.raise_for_status()
            body = response.json()
        except Exception as exc:  # network errors, HTTP errors, bad JSON
            _logger.warning('Mobile push gateway call failed: %s', exc)
            for message, _devices in batch:
                message.attempts += 1
                message.error = str(exc)
                if message.attempts >= MAX_ATTEMPTS:
                    message.state = 'failed'
            return

        if 'accepted' in body:
            self._queued(batch, body)
        else:  # a gateway without a queue: the results are in the answer
            self._results(batch, body.get('results') or [])

    @api.model
    def _gateway_message(self, message, devices, site_id, user_ref, e2e):
        """What the gateway gets for a message: the phones that get plain text (Android) and, if the gateway can, the
        iPhones that get it end-to-end encrypted (docs/push-e2e.md: the gateway then never sees the text).
        Returns (message, None), or (None, why) when no phone of the user can receive it."""
        required = self.env['ir.config_parameter'].sudo().get_param('mobile_connector.push_ios_e2e') == 'required'
        data = message._gateway_data(site_id)
        plain, encrypted, refused = [], [], False
        for device in devices:
            if device.platform != 'ios':
                plain.append(device.push_token)
                continue
            enc = False
            if e2e:
                try:
                    enc = self.env['mobile.push.e2e']._encrypt(device, site_id, message.title, message.body, data)
                except PushE2eError:
                    enc = False
            if enc:
                encrypted.append({'token': device.push_token, 'enc': enc})
            elif required:
                refused = True  # never plain text to an iPhone
            else:
                plain.append(device.push_token)
        if not plain and not encrypted:
            return None, ('iPhones only receive end-to-end encrypted notifications (Settings > Mobile > Push): update the '
                          'app and the push gateway.' if refused else False)
        content = {'id': message.id, 'user_ref': user_ref, 'tokens': plain}
        if plain:
            content.update(title=message.title, body=message.body or '', data=data)
        else:  # nothing in plain text, only what routes the notification
            content['data'] = {'site_id': site_id, 'message_id': str(message.id)}
        if encrypted:
            content['encrypted'] = encrypted
        return content, None

    @api.model
    def _queued(self, batch, body):
        accepted = {str(i) for i in body.get('accepted') or []}
        rejected = {str(r.get('id')): r.get('error') for r in body.get('rejected') or []}
        now = fields.Datetime.now()
        for message, _devices in batch:
            if str(message.id) in accepted:
                message.write({'state': 'queued', 'queued_at': now, 'error': False})
            else:
                error = rejected.get(str(message.id)) or 'Not accepted by the gateway'
                message.write({'state': 'failed', 'error': GATEWAY_ERRORS.get(error, error)})
        if any(str(message.id) in accepted for message, _devices in batch):
            cron = self.env.ref('mobile_connector.ir_cron_mobile_push_receipts', raise_if_not_found=False)
            if cron:
                cron.sudo()._trigger(at=now + RECEIPT_WAIT)

    @api.model
    def _results(self, batch, results):
        for index, (message, devices) in enumerate(batch):
            result = results[index] if index < len(results) else {}
            invalid = set(result.get('invalid_tokens') or [])
            if invalid:
                devices.filtered(lambda d: d.push_token in invalid).write({'push_token': False})
            message.attempts += 1
            if result.get('success'):
                message.write({'state': 'sent', 'error': False})
            else:
                error = result.get('error') or 'No device accepted the message'
                message.write({'state': 'failed', 'error': GATEWAY_ERRORS.get(error, error)})

    # ------------------------------------------------------------------
    # Results of the messages the gateway queued
    # ------------------------------------------------------------------

    @api.model
    def _cron_receipts(self):
        """Read what became of the queued messages (a safety net too: sending triggers it a few seconds later)."""
        # First what nobody will answer: also when the push settings changed meanwhile (another provider, no gateway).
        lost = self.search([('state', '=', 'queued'), ('queued_at', '<', fields.Datetime.now() - NO_RECEIPT_AFTER)])
        lost.write({'state': 'failed', 'error': 'The gateway gave no result for this message.'})
        if not self.search_count([('state', '=', 'queued')]) or self.env['mobile.api']._mobile_push_provider() != 'gateway':
            return
        icp = self.env['ir.config_parameter'].sudo()
        url = (icp.get_param('mobile_connector.gateway_url') or '').rstrip('/')
        token = icp.get_param('mobile_connector.gateway_token')
        if not url or not token:
            return
        cursor = icp.get_param('mobile_connector.gateway_receipts_cursor') or '0'
        more = True
        while more:
            try:
                response = requests.get(
                    f'{url}/v1/receipts', params={'after': cursor, 'limit': RECEIPT_PAGE},
                    headers={'Authorization': f'Bearer {token}'}, timeout=GATEWAY_TIMEOUT)
                response.raise_for_status()
                page = response.json()
            except Exception as exc:  # the next run asks again from the same cursor
                _logger.warning('Mobile push gateway receipts failed: %s', exc)
                break
            for receipt in page.get('receipts') or []:
                self._apply_receipt(receipt)
            cursor = page.get('next') or cursor
            more = bool(page.get('has_more'))
            icp.set_param('mobile_connector.gateway_receipts_cursor', cursor)

    @api.model
    def _apply_receipt(self, receipt):
        invalid = set(receipt.get('invalid_tokens') or [])
        if invalid:
            self.env['mobile.device'].sudo().search([('push_token', 'in', list(invalid))]).write({'push_token': False})
        try:
            message = self.browse(int(receipt.get('message_id'))).exists()
        except (TypeError, ValueError):
            return
        if not message or message.state != 'queued':
            return
        message.attempts = receipt.get('attempts') or message.attempts
        if receipt.get('status') == 'sent':
            message.write({'state': 'sent', 'error': False})
        else:
            message.write({'state': 'failed', 'error': receipt.get('error') or 'No device accepted the message'})

    def _send_firebase(self, messages):
        """The customer's own Firebase project (free, Android only): straight to FCM, no gateway."""
        fcm = self.env['mobile.push.fcm']
        site_id = self.env['ir.config_parameter'].sudo().get_param('database.uuid')
        devices = self.env['mobile.device'].sudo().search([
            ('user_id', 'in', messages.user_id.ids),
            ('push_token', '!=', False),
            ('platform', '=', 'android'),
            ('push_key', '=', fcm._push_key()),
        ])
        for message in messages:
            # A token cleared meanwhile (a dead one found while sending the previous message of this user) is not one.
            targets = devices.filtered(lambda d: d.user_id == message.user_id and d.push_token)
            if not targets:
                message.write({'state': 'skipped',
                               'error': 'No Android device on this Firebase project (iPhones need the push gateway).'})
                continue
            message.attempts += 1
            try:
                delivered, invalid, error = fcm._send(targets.mapped('push_token'), message.title, message.body or '',
                                                      message._gateway_data(site_id))
            except Exception as exc:  # network, OAuth, HTTP errors: retried by the next cron runs
                _logger.warning('Firebase push failed: %s', exc)
                message.error = str(exc)
                if message.attempts >= MAX_ATTEMPTS:
                    message.state = 'failed'
                continue
            if invalid:
                targets.filtered(lambda d: d.push_token in invalid).write({'push_token': False})
            if delivered:
                message.write({'state': 'sent', 'error': False})
            else:
                message.write({'state': 'failed', 'error': error or 'No device accepted the message'})

    def _gateway_data(self, site_id):
        self.ensure_one()
        try:
            data = json.loads(self.data or '{}')
        except ValueError:
            data = {}
        data['site_id'] = site_id
        data['message_id'] = self.id
        # FCM data payloads only accept string values.
        return {key: '' if value is None or value is False else str(value) for key, value in data.items()}

    @api.autovacuum
    def _gc_old_messages(self):
        limit = fields.Datetime.now() - timedelta(days=30)
        self.search([('state', '!=', 'pending'), ('create_date', '<', limit)]).unlink()
