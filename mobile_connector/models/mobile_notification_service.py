"""Notification center: the user's Odoo Inbox (Discuss), the same one as the bell on the web.

Unread = messages with an unread inbox notification for the user (`needaction`),
read = the Inbox history. Marking read uses Odoo's own methods, so the web
client's counter and inbox update at the same time.
"""
from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.tools import html2plaintext

FILTERS = ('unread', 'all')
BODY_MAX = 400


class MobileNotificationService(models.AbstractModel):
    _name = 'mobile.notification.service'
    _description = 'Mobile Notification Center'

    @api.model
    def _filter_domain(self, name):
        """Messages of the user's Inbox: `unread` = not read yet, `all` = with the read ones (history)."""
        domain = [('res_partner_id', '=', self.env.user.partner_id.id), ('notification_type', '=', 'inbox')]
        if name == 'unread':
            domain.append(('is_read', '=', False))
        return [('notification_ids', 'in', self.env['mail.notification']._search(domain))]

    @api.model
    def _unread_count(self):
        return self.env.user.partner_id._get_needaction_count()

    @api.model
    def _mobile_version(self):
        """Changes when an inbox notification arrives or is read (mail.notification has no write_date)."""
        self.env['mail.notification'].flush_model(['is_read', 'res_partner_id', 'notification_type'])
        self.env.cr.execute("""
            SELECT count(*), count(*) FILTER (WHERE is_read IS NOT TRUE), max(id)
              FROM mail_notification
             WHERE res_partner_id = %s AND notification_type = 'inbox'
        """, [self.env.user.partner_id.id])
        total, unread, max_id = self.env.cr.fetchone()
        return f'{total}:{unread}:{max_id or 0}'

    @api.model
    def _serialize(self, message, unread_ids, model_names):
        author = message.author_id
        body = html2plaintext(message.body or '').strip()
        if not body and message.subtype_id:
            body = message.subtype_id.name or ''
        return {
            'id': message.id,
            'model': message.model or False,
            'model_name': model_names.get(message.model, message.model) if message.model else False,
            'res_id': message.res_id or False,
            'record_name': message.record_name or False,
            'subject': message.subject or False,
            'body': body[:BODY_MAX],
            'author': [author.id, author.name] if author else False,
            'date': fields.Datetime.to_string(message.date),
            'message_type': message.message_type,
            'is_read': message.id not in unread_ids,
        }

    @api.model
    def _mobile_list(self, filter_name='unread', offset=0, limit=40):
        if filter_name not in FILTERS:
            raise UserError(_('Unknown filter %s', filter_name))
        Message = self.env['mail.message']
        messages = Message.search(self._filter_domain(filter_name), offset=offset, limit=limit, order='id desc')
        unread_ids = set(self.env['mail.notification'].sudo().search([
            ('mail_message_id', 'in', messages.ids),
            ('res_partner_id', '=', self.env.user.partner_id.id),
            ('is_read', '=', False),
        ]).mail_message_id.ids)
        # Notified messages are readable by the recipient; sudo only avoids per-record checks on the documents.
        messages = messages.sudo()
        IrModel = self.env['ir.model'].sudo()
        model_names = {model: IrModel._get(model).name for model in set(messages.mapped('model')) if model}
        return {
            'records': [self._serialize(message, unread_ids, model_names) for message in messages],
            'counts': {'unread': self._unread_count()},
            'version': self._mobile_version(),
        }

    @api.model
    def _mobile_mark_read(self, ids=None, all_messages=False):
        """Mark some (or all) inbox messages read, like the web client."""
        Message = self.env['mail.message']
        if all_messages:
            Message.mark_all_as_read()
        elif ids:
            Message.browse([int(i) for i in ids]).set_message_done()
        return {'unread': self._unread_count(), 'version': self._mobile_version()}
