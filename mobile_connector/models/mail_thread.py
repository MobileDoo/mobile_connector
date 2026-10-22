from odoo import models
from odoo.tools import html2plaintext


class MailThread(models.AbstractModel):
    _inherit = 'mail.thread'

    def _notify_thread_by_inbox(self, message, recipients_data, msg_vals=False, **kwargs):
        super()._notify_thread_by_inbox(message, recipients_data, msg_vals=msg_vals, **kwargs)
        self._mobile_push_inbox(message, [r['id'] for r in recipients_data if r['notif'] == 'inbox'])

    def _mobile_push_inbox(self, message, partner_ids):
        """Push every new Odoo Inbox message (mentions, chatter messages for followers...) to the phone."""
        if not partner_ids or self.env['ir.config_parameter'].sudo().get_param('mobile_connector.push_inbox') != 'True':
            return
        users = self.env['res.users'].sudo().search([('partner_id', 'in', partner_ids)])
        users = users.filtered(lambda u: u.partner_id != message.author_id)
        if not users:
            return
        message = message.sudo()
        title = message.author_id.name or message.record_name or message.subject or ''
        body = html2plaintext(message.body or '').strip() or message.subtype_id.name or ''
        if message.record_name and message.record_name != title:
            body = f'{message.record_name}: {body}' if body else message.record_name
        self.env['mobile.push.message']._enqueue(users, title, body[:500], {
            'type': 'message',
            'model': message.model or '',
            'res_id': message.res_id or '',
            'mail_message_id': message.id,
        })
