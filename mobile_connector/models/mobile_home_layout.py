"""Home screen and bottom tabs per role (Mobile App > Role Layouts).

A layout targets groups; a user gets the first active layout (by sequence) of
one of their groups, else the layout for everyone (no group), else the plain
company configuration. The layout only sets the DEFAULTS: what is shown, in
which order, and the tabs. Users still arrange their own home screen within
what they may use, and "Always shown" items stay locked.
"""
from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

MAX_TABS = 5


class MobileHomeLayout(models.Model):
    _name = 'mobile.home.layout'
    _description = 'Mobile Home Layout per Role'
    _order = 'sequence, id'

    name = fields.Char(required=True, translate=True)
    sequence = fields.Integer(default=10, help='When a user is in several targeted groups, the first layout wins.')
    active = fields.Boolean(default=True)
    group_ids = fields.Many2many('res.groups', string='Roles',
                                 help='Users of any of these groups get this layout. Empty: everyone else.')
    section_line_ids = fields.One2many('mobile.home.layout.line', 'layout_id', string='Blocks',
                                       domain=[('kind', '=', 'section')], context={'default_kind': 'section'})
    tile_line_ids = fields.One2many('mobile.home.layout.line', 'layout_id', string='Features',
                                    domain=[('kind', '=', 'tile')], context={'default_kind': 'tile'})
    kpi_line_ids = fields.One2many('mobile.home.layout.line', 'layout_id', string='KPIs',
                                   domain=[('kind', '=', 'kpi')], context={'default_kind': 'kpi'})
    shortcut_line_ids = fields.One2many('mobile.home.layout.line', 'layout_id', string='Quick actions',
                                        domain=[('kind', '=', 'shortcut')], context={'default_kind': 'shortcut'})
    tab_line_ids = fields.One2many('mobile.home.layout.line', 'layout_id', string='Bottom tabs',
                                   domain=[('kind', '=', 'tab')], context={'default_kind': 'tab'})
    user_count = fields.Integer(compute='_compute_user_count', string='Users')

    @api.depends('group_ids')
    def _compute_user_count(self):
        for layout in self:
            layout.user_count = len(self.env['res.users'].search(
                [('share', '=', False)] + ([('groups_id', 'in', layout.group_ids.ids)] if layout.group_ids else [])))

    @api.constrains('tab_line_ids')
    def _check_tabs(self):
        for layout in self:
            if len(layout.tab_line_ids) > MAX_TABS - 2:
                raise ValidationError(_('At most %s tabs between Home and Settings (they are always there).',
                                        MAX_TABS - 2))

    @api.model
    def _for_user(self, user=None):
        """The layout of `user` (default: current), or an empty recordset."""
        user = user or self.env.user
        layouts = self.sudo().search([])
        return next((layout for layout in layouts if layout.group_ids & user.groups_id), None) \
            or next((layout for layout in layouts if not layout.group_ids), self.browse())


class MobileHomeLayoutLine(models.Model):
    _name = 'mobile.home.layout.line'
    _description = 'Mobile Home Layout Line'
    _order = 'sequence, id'

    layout_id = fields.Many2one('mobile.home.layout', required=True, ondelete='cascade', index=True)
    kind = fields.Selection([('section', 'Block'), ('tile', 'Feature'), ('kpi', 'KPI'), ('shortcut', 'Quick action'),
                             ('tab', 'Tab')], required=True)
    sequence = fields.Integer(default=10)
    visible = fields.Boolean(string='Shown', default=True,
                             help='Off: in the layout\'s order but hidden until the user shows it.')
    section_key = fields.Selection(selection='_selection_section', string='Block')
    tile_key = fields.Selection(selection='_selection_tile', string='Feature')
    tab_key = fields.Selection(selection='_selection_tab', string='Tab')
    size = fields.Selection([('cols2', 'Features: 2 columns'), ('cols3', 'Features: 3 columns'),
                             ('cols4', 'Features: 4 columns'), ('compact', 'KPIs: compact'),
                             ('large', 'KPIs: large with chart'), ('icons', 'Quick actions: icons'),
                             ('buttons', 'Quick actions: buttons')],
                            help='Blocks: how the block is laid out for this role (users can change it).')
    item_id = fields.Many2one('mobile.home.item', string='Item', ondelete='cascade',
                              domain="[('kind', '=', kind)]")

    @api.model
    def _selection_section(self):
        """Declared blocks + one per record list of Mobile App > Home Screen."""
        declared = [(s['key'], s['label']) for s in self.env['mobile.api']._mobile_home_sections()]
        lists = self.env['mobile.home.item'].sudo().with_context(active_test=False).search([('kind', '=', 'list')])
        return declared + [(f'list:{item.id}', _('List: %s', item.name)) for item in lists]

    @api.model
    def _selection_tile(self):
        return [(t['key'], t['label']) for t in self.env['mobile.api']._mobile_home_tiles()]

    @api.model
    def _selection_tab(self):
        return [(t['key'], t['label']) for t in self.env['mobile.api']._mobile_home_tabs() if not t.get('fixed')]

    @api.constrains('kind', 'section_key', 'tile_key', 'tab_key', 'item_id')
    def _check_ref(self):
        for line in self:
            field = {'section': 'section_key', 'tile': 'tile_key', 'tab': 'tab_key'}.get(line.kind, 'item_id')
            if not line[field]:
                raise ValidationError(_('Choose what this line shows.'))
            if line.kind in ('kpi', 'shortcut') and line.item_id.kind != line.kind:
                raise ValidationError(_('The item must be a %s.', dict(self._fields['kind'].selection)[line.kind]))

    def _ref(self):
        self.ensure_one()
        return {'section': self.section_key, 'tile': self.tile_key, 'tab': self.tab_key}.get(self.kind) or self.item_id.id
