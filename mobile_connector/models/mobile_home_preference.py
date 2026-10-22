from odoo import api, fields, models


class MobileHomePreference(models.Model):
    """A user's own home screen: order and visibility of the blocks they may use (set in the app)."""

    _name = 'mobile.home.preference'
    _description = 'Mobile Home Screen of a User'
    _order = 'write_date desc'
    _rec_name = 'user_id'

    user_id = fields.Many2one('res.users', required=True, ondelete='cascade', index=True)
    data = fields.Json(help='{"sections"|"tiles"|"kpis"|"shortcuts": [{"id": ..., "visible": bool}, ...]}')
    summary = fields.Char(compute='_compute_summary')

    _user_unique = models.Constraint('unique(user_id)', 'One home screen per user.')

    @api.depends('data')
    def _compute_summary(self):
        for preference in self:
            data = preference.data or {}
            shown = sum(1 for kind in ('tiles', 'kpis', 'shortcuts') for c in data.get(kind) or [] if c.get('visible'))
            hidden = sum(1 for kind in ('tiles', 'kpis', 'shortcuts') for c in data.get(kind) or [] if not c.get('visible'))
            preference.summary = f'{shown} shown, {hidden} hidden'

    @api.model
    def _for_user(self, user):
        return self.search([('user_id', '=', user.id)], limit=1)

    @api.model
    def _store(self, user, data):
        preference = self._for_user(user)
        if preference:
            preference.data = data
        else:
            self.create({'user_id': user.id, 'data': data})
