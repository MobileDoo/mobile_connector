"""Odoo's own menus on mobile: every app and list the user has on the web.

Native screens cover the daily flows (time off, expenses...). Everything else,
including customer modules, is reachable through the user's real Odoo menus:
a menu that opens records (window action) becomes a searchable list whose
cards show the first columns of the action's list view, with the search
view's filters. Records open in the native screen if the app has one, else in
the generic viewer (summary + the record's real buttons).

What is shown follows Odoo exactly: menu groups (`_visible_menu_ids`), model
access rights and record rules of the user, the action's domain and default
filters. Admins hide menus with "Hide in Mobile App" on the menu item.
"""
import hashlib
import json
import logging

from dateutil.relativedelta import relativedelta
from lxml import etree

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError
from odoo.tools.safe_eval import safe_eval

_logger = logging.getLogger(__name__)

LIST_VIEW_TYPES = ('list', 'kanban')
MAX_COLUMNS = 5
COLUMN_SKIP_TYPES = {'one2many', 'many2many', 'html', 'binary', 'text'}
COLUMN_SKIP_WIDGETS = {'handle', 'image', 'many2many_tags', 'list_activity', 'activity_exception', 'badge_html'}


class MobileMenuService(models.AbstractModel):
    _name = 'mobile.menu.service'
    _description = 'Mobile Odoo Menus'

    @api.model
    def _mobile_enabled(self):
        """Settings > Mobile > "Odoo menus in the app" (on by default)."""
        return self.env['ir.config_parameter'].sudo().get_param('mobile_connector.menu_browser', 'True') != 'False'

    # ------------------------------------------------------------------
    # Menu tree
    # ------------------------------------------------------------------

    @api.model
    def _hidden_menu_ids(self):
        return set(self.env['ir.ui.menu'].sudo().with_context(active_test=False).search([('mobile_hide', '=', True)]).ids)

    @api.model
    def _window_action_id(self, action_ref):
        """'ir.actions.act_window,42' -> 42 (other action types have no records to list)."""
        if not action_ref or not isinstance(action_ref, str):
            return None
        action_type, _sep, action_id = action_ref.partition(',')
        return int(action_id) if action_type == 'ir.actions.act_window' and action_id.isdigit() else None

    @api.model
    def _listable(self, action):
        """A window action showing a list of records the user may read."""
        if not action or action.res_model not in self.env or action.target in ('new', 'inline'):
            return False
        Model = self.env[action.res_model]
        if Model._transient or Model._abstract:
            return False
        modes = [mode.strip() for mode in (action.view_mode or '').split(',')]
        if not any(mode in LIST_VIEW_TYPES for mode in modes):
            return False
        return Model.has_access('read')

    @api.model
    def _mobile_menus(self):
        """Apps the user sees on the web, each with sections of list menus.

        {"apps": [{"id", "name", "xmlid", "sections": [{"name", "items": [{"id", "name", "model"}]}]}],
         "version"}   - deeper menu levels are flattened into their section.
        """
        menus = self.env['ir.ui.menu'].load_menus(False)
        hidden = self._hidden_menu_ids()
        action_ids = {self._window_action_id(menu.get('action')) for key, menu in menus.items() if key != 'root'}
        actions = {
            action.id: action
            for action in self.env['ir.actions.act_window'].sudo().browse([a for a in action_ids if a]).exists()
        }

        def item(menu):
            action = actions.get(self._window_action_id(menu.get('action')))
            if menu['id'] in hidden or not self._listable(action):
                return None
            return {'id': menu['id'], 'name': menu['name'], 'model': action.res_model}

        def leaves(menu):
            """The menu itself if it lists records, then its descendants, flattened."""
            if menu['id'] in hidden:
                return []
            own = item(menu)
            result = [own] if own else []
            for child_id in menu['children']:
                result += leaves(menus[child_id])
            return result

        apps = []
        for app_id in menus['root']['children']:
            app = menus[app_id]
            if app_id in hidden:
                continue
            sections = []
            direct = item(app)
            if direct:
                sections.append({'name': False, 'items': [direct]})
            for child_id in app['children']:
                child = menus[child_id]
                if child_id in hidden:
                    continue
                if child['children']:
                    items = []
                    own = item(child)
                    if own:
                        items.append(own)
                    for grandchild_id in child['children']:
                        items += leaves(menus[grandchild_id])
                    if items:
                        sections.append({'name': child['name'], 'items': items})
                else:
                    own = item(child)
                    if own:
                        if sections and sections[-1]['name'] is False:
                            sections[-1]['items'].append(own)
                        else:
                            sections.append({'name': False, 'items': [own]})
            if sections:
                apps.append({'id': app_id, 'name': app['name'], 'xmlid': app.get('xmlid') or False, 'sections': sections})
        version = hashlib.sha1(json.dumps(apps, sort_keys=True).encode()).hexdigest()
        return {'apps': apps, 'version': version}

    # ------------------------------------------------------------------
    # One menu: action, filters, columns
    # ------------------------------------------------------------------

    @api.model
    def _menu_action(self, menu_id):
        """The window action of a menu the user may open, or AccessError."""
        Menu = self.env['ir.ui.menu']
        menu = Menu.browse(int(menu_id)).exists()
        # The menu and all its parents, like the web menu tree (a sub-menu without
        # groups under an admin-only app is not reachable on the web either).
        path = [int(part) for part in (menu.sudo().parent_path or '').strip('/').split('/') if part] if menu else []
        visible = Menu._visible_menu_ids()
        hidden = self._hidden_menu_ids()
        if not path or any(part not in visible or part in hidden for part in path):
            raise AccessError(_('This menu is not available.'))
        action = menu.sudo().action
        if not action or action._name != 'ir.actions.act_window' or not self._listable(action):
            raise UserError(_('This menu does not show a list of records.'))
        return menu, action

    @api.model
    def _eval_context(self):
        """What action domains/contexts and search filters may use, like the web client."""
        ctx = self.env['ir.actions.actions']._get_eval_context()
        ctx.update({
            'context_today': lambda: fields.Date.context_today(self),
            'relativedelta': relativedelta,
            'current_date': fields.Date.to_string(fields.Date.context_today(self)),
            'allowed_company_ids': self.env.companies.ids,
            'current_company_id': self.env.company.id,
            'company_id': self.env.company.id,
            'company_ids': self.env.companies.ids,
            'active_id': False,
            'active_ids': [],
            'context': dict(self.env.context),
        })
        return ctx

    @api.model
    def _eval(self, expr, default):
        if not expr:
            return default
        if not isinstance(expr, str):
            return expr
        try:
            return safe_eval(expr, self._eval_context())
        except Exception:
            _logger.debug('mobile menus: cannot evaluate %r', expr, exc_info=True)
            return default

    @api.model
    def _action_context(self, action):
        context = self._eval(action.context, {})
        return context if isinstance(context, dict) else {}

    @api.model
    def _search_filters(self, action, context):
        """Domain filters of the action's search view: [{name, string, group, domain, default}].

        Filters between two separators form a group (OR-ed, like the web
        client); groups are AND-ed. Date-range and group-by filters are left
        out. `default` comes from the action's `search_default_<name>`.
        """
        Model = self.env[action.res_model].with_context(**context)
        try:
            arch = etree.fromstring(Model.get_view(action.search_view_id.id or None, 'search')['arch'])
        except Exception:
            _logger.debug('mobile menus: no search view for %s', action.res_model, exc_info=True)
            return []
        filters, group = [], 0
        for node in arch.iter('filter', 'separator', 'group'):
            if node.tag != 'filter':
                group += 1
                continue
            name, domain = node.get('name'), node.get('domain')
            if not name or not domain or node.get('date') or node.get('invisible') in ('1', 'True'):
                continue
            if 'group_by' in (node.get('context') or ''):
                continue
            evaluated = self._eval(domain, None)
            if not isinstance(evaluated, list):
                continue
            filters.append({
                'name': name,
                'string': node.get('string') or name,
                'group': group,
                'domain': evaluated,
                'default': bool(context.get(f'search_default_{name}')),
            })
        return filters

    @api.model
    def _columns(self, action, context):
        """First visible columns of the action's list view (the card content)."""
        Model = self.env[action.res_model].with_context(**context)
        # Odoo 18 renamed the "tree" view type "list".
        list_view = next((view.view_id.id for view in action.view_ids if view.view_mode == 'list' and view.view_id), None)
        if not list_view and action.view_id.type == 'list':
            list_view = action.view_id.id
        arch = etree.fromstring(Model.get_view(list_view, 'list')['arch'])
        model_fields = Model._fields
        names, extra = [], set()
        for node in arch.iterchildren('field'):
            name = node.get('name')
            field = model_fields.get(name)
            if not field or name in names or name in ('display_name', Model._rec_name):
                continue
            if node.get('optional') == 'hide' or node.get('widget') in COLUMN_SKIP_WIDGETS:
                continue
            if any((node.get(attr) or '').strip() in ('1', 'True') for attr in ('invisible', 'column_invisible')):
                continue
            if field.type in COLUMN_SKIP_TYPES:
                continue
            names.append(name)
            if field.type == 'monetary' and field.get_currency_field(Model) in model_fields:
                extra.add(field.get_currency_field(Model))
            if len(names) >= MAX_COLUMNS:
                break
        if 'state' in model_fields and 'state' not in names and model_fields['state'].type == 'selection':
            extra.add('state')
        order = arch.get('default_order') or None
        return names, sorted(extra - set(names)), order

    @api.model
    def _domain(self, action, context, filters, active):
        """Action domain AND each group of active filters (OR inside a group)."""
        domain = list(self._eval(action.domain, []) or [])
        groups = {}
        for spec in filters:
            if spec['name'] in active:
                groups.setdefault(spec['group'], []).append(spec['domain'])
        for domains in groups.values():
            if len(domains) == 1:
                domain += domains[0]
            else:
                domain += ['|'] * (len(domains) - 1) + [term for d in domains for term in (d or [(1, '=', 1)])]
        return domain

    @api.model
    def _kpi_domain(self, kpi_id, model_name):
        """Domain of a home screen KPI the user sees, to list exactly the records it counts."""
        item = self.env['mobile.home.item'].sudo().browse(int(kpi_id)).exists()
        if not item or item.kind not in ('kpi', 'list') or item.model_name != model_name:
            raise AccessError(_('This indicator is not available.'))
        return item._eval_domain(self.env)

    @api.model
    def _resolve(self, menu_id, active_filters=None, kpi_id=None):
        menu, action = self._menu_action(menu_id)
        context = self._action_context(action)
        filters = self._search_filters(action, context)
        known = {f['name'] for f in filters}
        if active_filters is None:
            # Opened from a KPI: its records only, not the menu's default filters.
            active = set() if kpi_id else {f['name'] for f in filters if f['default']}
        else:
            active = {name for name in active_filters if name in known}
        domain = self._domain(action, context, filters, active)
        if kpi_id:
            domain = domain + self._kpi_domain(kpi_id, action.res_model)
        return menu, action, context, filters, active, domain

    @api.model
    def _list_key(self, menu_id, active, kpi_id=None):
        filters = '+'.join(sorted(active))
        return f'kpi.{kpi_id}.{menu_id}.{filters}' if kpi_id else f'menu.{menu_id}.{filters}'

    # ------------------------------------------------------------------
    # API
    # ------------------------------------------------------------------

    @api.model
    def _mobile_list(self, menu_id, offset=0, limit=40, query=None, active_filters=None, kpi_id=None):
        menu, action, context, filters, active, domain = self._resolve(menu_id, active_filters, kpi_id)
        Model = self.env[action.res_model].with_context(**{
            k: v for k, v in context.items() if not k.startswith(('search_default_', 'default_'))
        })
        list_domain = domain
        if query and query.strip():
            list_domain = domain + self.env['mobile.api']._mobile_text_domain(action.res_model, query.strip())
        names, extra, order = self._columns(action, context)
        records = Model.search(list_domain, offset=offset, limit=limit, order=order)

        engine = self.env['mobile.form.spec']
        descs = engine._field_descs(action.res_model, names + extra)
        columns = []
        for name in names:
            desc = descs.get(name)
            if desc:
                column = {'name': name, 'string': desc['string'], 'type': desc['type']}
                if desc['type'] == 'monetary':
                    column['currency_field'] = Model._fields[name].get_currency_field(Model)
                if desc.get('selection'):
                    column['selection'] = [list(s) for s in desc['selection']]
                columns.append(column)
        rows = []
        for record in records:
            values = engine._read_values(record, descs, descs)
            rows.append({'id': record.id, 'display_name': record.display_name, 'values': values})
        state = Model._fields.get('state')
        return {
            'menu': {'id': menu.id, 'name': menu.name, 'model': action.res_model,
                     'model_name': self.env['ir.model']._get(action.res_model).name},
            'columns': columns,
            'filters': [{'name': f['name'], 'string': f['string'], 'group': f['group'], 'active': f['name'] in active}
                        for f in filters],
            'records': rows,
            'total': Model.search_count(list_domain),
            'state_selection': [list(s) for s in state._description_selection(self.env)]
            if state and state.type == 'selection' else [],
            'key': self._list_key(menu.id, active, kpi_id),
            'version': self.env['mobile.api']._mobile_domain_version(action.res_model, domain),
        }

    @api.model
    def _mobile_list_versions(self, keys):
        """`menu.<menu_id>.<filter+filter>`: the list without search text, with those filters;
        `kpi.<kpi_id>.<menu_id>.<filter+filter>`: the same, opened from a home screen KPI."""
        versions = {}
        for key in keys:
            kpi_id = None
            if key.startswith('kpi.'):
                _prefix, kpi_id, menu_id, active = (key.split('.', 3) + ['', '', ''])[:4]
                if not kpi_id.isdigit():
                    continue
            else:
                _prefix, menu_id, active = (key.split('.', 2) + ['', ''])[:3]
            if not menu_id.isdigit():
                continue
            try:
                _menu, action, _ctx, _filters, active_set, domain = self._resolve(
                    int(menu_id), [name for name in active.split('+') if name], kpi_id)
            except (AccessError, UserError):
                continue
            versions[key] = self.env['mobile.api']._mobile_domain_version(action.res_model, domain)
        return versions
