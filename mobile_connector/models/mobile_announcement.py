"""Company announcements on the app's home screen (Mobile App > Announcements).

Targeted by groups and dates. Each user dismisses the ones they have read,
except mandatory ones (e.g. the Tet holiday schedule).
"""
from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

from .mobile_home_item import COLORS


class MobileAnnouncement(models.Model):
    _name = 'mobile.announcement'
    _description = 'Mobile Company Announcement'
    _order = 'sequence, date_start desc, id desc'

    name = fields.Char(string='Title', required=True, translate=True)
    body = fields.Text(translate=True, help='Plain text; web addresses become links in the app.')
    icon = fields.Char(default='bullhorn-outline', help='Material Design Icons name.')
    color = fields.Selection(COLORS, default='indigo', required=True)
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)
    date_start = fields.Date(string='From', default=fields.Date.context_today)
    date_end = fields.Date(string='Until', help='Empty: until archived.')
    group_ids = fields.Many2many('res.groups', string='For', help='Empty: every user of the app.')
    locked = fields.Boolean(string='Mandatory', help='Users cannot dismiss it.')
    dismissed_user_ids = fields.Many2many('res.users', 'mobile_announcement_dismissed_rel', string='Dismissed by',
                                          readonly=True)
    dismissed_count = fields.Integer(compute='_compute_dismissed_count', string='Read')

    @api.depends('dismissed_user_ids')
    def _compute_dismissed_count(self):
        for announcement in self:
            announcement.dismissed_count = len(announcement.dismissed_user_ids)

    @api.constrains('date_start', 'date_end')
    def _check_dates(self):
        for announcement in self:
            if announcement.date_start and announcement.date_end and announcement.date_end < announcement.date_start:
                raise ValidationError(_('The end date is before the start date.'))

    @api.model
    def _for_user(self, user=None):
        """Current announcements for `user` (default: current), not dismissed (mandatory ones always)."""
        user = user or self.env.user
        today = fields.Date.context_today(self.with_user(user))
        announcements = self.sudo().search([
            '|', ('date_start', '=', False), ('date_start', '<=', today),
            '|', ('date_end', '=', False), ('date_end', '>=', today),
        ])
        return announcements.filtered(
            lambda a: (not a.group_ids or a.group_ids & user.groups_id) and (a.locked or user not in a.dismissed_user_ids))

    def _mobile_data(self):
        return [{'id': a.id, 'title': a.name, 'body': a.body or '', 'icon': a.icon or 'bullhorn-outline',
                 'color': a.color, 'locked': a.locked} for a in self]

    def _mobile_dismiss(self, user):
        self.sudo().filtered(lambda a: not a.locked).write({'dismissed_user_ids': [(4, user.id)]})
