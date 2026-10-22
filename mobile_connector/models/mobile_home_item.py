"""Home screen of the app, configured in Odoo (Mobile App > Home Screen): no app release needed.

Three kinds of items, each optionally limited to some groups:
- feature tiles: which native features show on the home screen, in which order, under which label;
- KPIs: a count or a sum over any model and domain, computed with the reader's own rights;
- quick actions: one tap to "New time off", "Check in", an Odoo menu...

Feature modules declare their tiles and quick actions in `mobile.api._mobile_home_tiles()` /
`_mobile_home_shortcuts()`, so only installed features can be picked.
"""
from dateutil.relativedelta import relativedelta

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tools.safe_eval import safe_eval

# Same names as the app's tints (theme/colors.ts).
COLORS = [
    ('indigo', 'Indigo'), ('violet', 'Violet'), ('fuchsia', 'Fuchsia'), ('rose', 'Rose'), ('amber', 'Amber'),
    ('green', 'Green'), ('teal', 'Teal'), ('cyan', 'Cyan'), ('blue', 'Blue'),
]


class MobileHomeItem(models.Model):
    _name = 'mobile.home.item'
    _description = 'Mobile Home Screen Item'
    _order = 'kind, sequence, id'

    name = fields.Char(string='Label', translate=True,
                       help='Text shown in the app. Feature tiles: leave empty to keep the app\'s own label.')
    kind = fields.Selection([('tile', 'Feature tile'), ('kpi', 'KPI'), ('shortcut', 'Quick action'),
                             ('list', 'Record list')], required=True, default='kpi')
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)
    group_ids = fields.Many2many('res.groups', string='Shown by default to',
                                 help='Groups that get the item on their home screen by default. Empty: everyone. '
                                      'Other users can still add it themselves if they may read its records or use '
                                      'the feature (the figures always follow each user\'s rights).')
    default_visible = fields.Boolean(string='Shown by default', default=True,
                                     help='Off: not on the home screen until the user adds it (Customize).')
    locked = fields.Boolean(string='Always shown',
                            help='Users who may use it cannot hide it (e.g. a mandatory KPI).')
    icon = fields.Char(help='Material Design Icons name, e.g. "cart-check" (see pictogrammers.com/library/mdi). '
                            'Empty: a default icon.')
    color = fields.Selection(COLORS, default='indigo', required=True)

    tile_key = fields.Selection(selection='_selection_tile', string='Feature')
    shortcut_key = fields.Selection(selection='_selection_shortcut', string='Action')
    menu_id = fields.Many2one('ir.ui.menu', string='Odoo menu', ondelete='set null',
                              help='KPI: the list opened when the KPI is tapped (same model). '
                                   'Quick action "Open an Odoo menu": the menu to open.')

    model_id = fields.Many2one('ir.model', string='Model', ondelete='cascade')
    model_name = fields.Char(related='model_id.model', string='Model name')
    domain = fields.Char(default='[]', help='Records counted or summed (the user\'s access rights also apply).')
    measure = fields.Selection([('count', 'Number of records'), ('sum', 'Sum of a field')], default='count')
    trend_date_field_id = fields.Many2one(
        'ir.model.fields', string='Trend by', ondelete='set null',
        domain="[('model_id', '=', model_id), ('ttype', 'in', ('date', 'datetime')), ('store', '=', True)]",
        help='KPI: show a small chart of the figure per week/month along this date (large KPI cards).')
    trend_period = fields.Selection([('week', 'Week'), ('month', 'Month')], default='month')
    trend_count = fields.Integer(string='Periods', default=6)
    list_limit = fields.Integer(string='Lines', default=5, help='Record list: how many records the block shows.')
    measure_field_id = fields.Many2one(
        'ir.model.fields', string='Field to sum', ondelete='cascade',
        domain="[('model_id', '=', model_id), ('ttype', 'in', ('integer', 'float', 'monetary')), ('store', '=', True)]")

    @api.onchange('menu_id')
    def _onchange_menu_id(self):
        """A record list shows the records of its menu: same model."""
        action = self.menu_id.action
        if self.kind == 'list' and action and action._name == 'ir.actions.act_window':
            self.model_id = self.env['ir.model']._get(action.res_model)

    @api.model
    def _selection_tile(self):
        return [(t['key'], t['label']) for t in self.env['mobile.api']._mobile_home_tiles()]

    @api.model
    def _selection_shortcut(self):
        return [(s['key'], s['label']) for s in self.env['mobile.api']._mobile_home_shortcuts()]

    @api.constrains('kind', 'tile_key', 'shortcut_key', 'menu_id', 'model_id', 'measure', 'measure_field_id', 'domain')
    def _check_kind(self):
        for item in self:
            if item.kind == 'tile' and not item.tile_key:
                raise ValidationError(_('Choose the feature of the tile.'))
            if item.kind == 'shortcut':
                if not item.shortcut_key:
                    raise ValidationError(_('Choose the action of the quick action.'))
                if item.shortcut_key == 'menu' and not item.menu_id:
                    raise ValidationError(_('Choose the Odoo menu to open.'))
                if not item.name:
                    raise ValidationError(_('A quick action needs a label.'))
            if item.kind == 'list':
                if not item.menu_id or not item.name:
                    raise ValidationError(_('A record list needs a label and an Odoo menu.'))
                if not 1 <= item.list_limit <= 20:
                    raise ValidationError(_('A record list shows 1 to 20 lines.'))
                action = item.menu_id.action
                if not action or action._name != 'ir.actions.act_window':
                    raise ValidationError(_('The menu must open a list of records.'))
                if item.model_name != action.res_model:
                    item.model_id = self.env['ir.model']._get(action.res_model)
            if item.kind == 'kpi' and item.trend_date_field_id and not 2 <= item.trend_count <= 12:
                raise ValidationError(_('A trend shows 2 to 12 periods.'))
            if item.kind in ('kpi', 'list'):
                if not item.model_id or not item.name:
                    raise ValidationError(_('A KPI needs a label and a model.'))
                if item.kind == 'kpi' and item.measure == 'sum' and not item.measure_field_id:
                    raise ValidationError(_('Choose the field to sum.'))
                try:
                    domain = item._eval_domain()
                    self.env[item.model_name].sudo().search_count(domain, limit=1)
                except Exception as exc:  # unknown field, bad syntax...
                    raise ValidationError(_('Invalid filter for %(model)s: %(error)s',
                                            model=item.model_id.name, error=exc)) from exc
                if item.menu_id:
                    action = item.menu_id.action
                    if not action or action._name != 'ir.actions.act_window' or action.res_model != item.model_name:
                        raise ValidationError(_('The menu of a KPI must open a list of %s.', item.model_id.name))

    def _eval_domain(self, env=None):
        """The KPI's domain, evaluated for `env`'s user (uid, company, today...)."""
        self.ensure_one()
        env = env or self.env
        return safe_eval(self.sudo().domain or '[]', env['mobile.menu.service']._eval_context())

    # ------------------------------------------------------------------
    # What the current user gets
    # ------------------------------------------------------------------

    def _default_for_user(self):
        """Shown by default to the current user (the item's role targeting)."""
        self.ensure_one()
        return self.default_visible and (not self.group_ids or bool(self.group_ids & self.env.user.all_group_ids))

    # The configuration (this record, ir.model, ir.model.fields) is read as superuser;
    # the figures are always computed in the user's `env`, with their rights.

    def _openable_menu(self, env):
        """The item's menu if `env`'s user may open it in the app, else False."""
        menu = self.sudo().menu_id
        if not menu:
            return False
        try:
            env['mobile.menu.service']._menu_action(menu.id)
        except (AccessError, UserError):
            return False
        return menu.id

    def _kpi_value(self, env):
        """Count or sum for `env`'s user (record rules apply); None when they cannot read the model."""
        config = self.sudo()
        Model = env[config.model_name]
        if not Model.has_access('read'):
            return None
        domain = self._eval_domain(env)
        if config.measure == 'sum':
            [(total,)] = Model._read_group(domain, aggregates=[f'{config.measure_field_id.name}:sum'])
            return total or 0
        return Model.search_count(domain)

    def _kpi_unit(self, env):
        """Currency of a sum of amounts (the user's company currency)."""
        config = self.sudo()
        if config.measure == 'sum' and config.measure_field_id.ttype == 'monetary':
            currency = env.company.currency_id
            return {'symbol': currency.symbol, 'position': currency.position}
        return False

    def _list_data(self, env):
        """First records of a record list block for `env`'s user (their rights), or None if they may not open it."""
        config = self.sudo()
        menu_id = self._openable_menu(env)
        if not menu_id:
            return None
        data = env['mobile.menu.service']._mobile_list(menu_id, limit=config.list_limit, kpi_id=config.id)
        return {
            'id': config.id, 'label': config.name, 'icon': config.icon or False, 'color': config.color,
            'menu_id': menu_id, 'model': data['menu']['model'], 'total': data['total'], 'columns': data['columns'][:2],
            'records': data['records'], 'state_selection': data['state_selection'],
        }

    def _kpi_trend(self, env):
        """The KPI per week/month over the last periods, for `env`'s user: [{start, value}] or False.

        Grouped by Odoo itself (`_read_group`, the user's time zone), missing periods at 0.
        """
        config = self.sudo()
        if not config.trend_date_field_id:
            return False
        Model = env[config.model_name]
        date_field = config.trend_date_field_id.name
        today = fields.Date.context_today(Model)
        if config.trend_period == 'week':
            first = today - relativedelta(days=today.weekday())
            step = relativedelta(weeks=1)
        else:
            first = today.replace(day=1)
            step = relativedelta(months=1)
        starts = [first - step * i for i in range(config.trend_count - 1, -1, -1)]
        domain = self._eval_domain(env) + [(date_field, '>=', fields.Date.to_string(starts[0]))]
        aggregate = f'{config.measure_field_id.name}:sum' if config.measure == 'sum' else '__count'
        values = {}
        for period, total in Model._read_group(domain, groupby=[f'{date_field}:{config.trend_period}'],
                                               aggregates=[aggregate]):
            if period:
                key = period.date() if hasattr(period, 'date') else period
                values[key] = total or 0
        return [{'start': fields.Date.to_string(start), 'value': values.get(start, 0)} for start in starts]
