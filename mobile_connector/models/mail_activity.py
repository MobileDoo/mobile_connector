from odoo import api, models
from odoo.tools import html2plaintext


class MailActivity(models.Model):
    _inherit = 'mail.activity'

    @api.model_create_multi
    def create(self, vals_list):
        activities = super().create(vals_list)
        activities._mobile_push_new_activity()
        return activities

    def _mobile_push_new_activity(self):
        """Push "you have a new task" to the assignee.

        Approval flows (time off, expenses, purchase, custom modules) almost
        always schedule an activity for the approver, so this one generic hook
        covers "something is waiting for you" for every model.
        """
        if self.env['ir.config_parameter'].sudo().get_param('mobile_connector.push_activity') != 'True':
            return
        for activity in self:
            user = activity.user_id
            if not user or user == self.env.user:
                continue
            title = activity.summary or activity.activity_type_id.name or activity.res_name
            body = activity.res_name or ''
            note = html2plaintext(activity.note or '').strip()
            if note:
                body = f'{body}\n{note}' if body else note
            self.env['mobile.push.message']._enqueue(user, title, body[:500], {
                'type': 'activity',
                'model': activity.res_model,
                'res_id': activity.res_id,
                'activity_id': activity.id,
            })
