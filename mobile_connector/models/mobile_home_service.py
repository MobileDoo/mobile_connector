import logging

from odoo import _, api, models

_logger = logging.getLogger(__name__)



class MobileApi(models.AbstractModel):
    _inherit = 'mobile.api'

    @api.model
    def _mobile_home_tiles(self):
        """Native feature tiles: [{key, label, feature, capability, sequence}]. Feature modules add theirs.

        `sequence` is the app's own order (whatever the order modules are loaded in).
        """
        return [
            {'key': 'dashboard', 'label': _('Overview'), 'feature': 'dashboard', 'capability': 'view', 'sequence': 1},
        ]

    @api.model
    def _mobile_home_shortcuts(self):
        """Quick actions the app knows: [{key, label, feature, capability}]. Feature modules add theirs."""
        return [
            {'key': 'menu', 'label': _('Open an Odoo menu'), 'feature': 'menus', 'capability': None},
            {'key': 'notifications', 'label': _('Notifications'), 'feature': 'notifications', 'capability': None},
            {'key': 'activities', 'label': _('To do'), 'feature': 'activities', 'capability': None},
        ]

    @api.model
    def _mobile_home_sections(self):
        """Blocks of the home screen: [{key, label, feature, capability, sequence, default_visible, locked, sizes}].

        `sizes`: the layouts the block offers, the first one being the default (e.g. tiles in 3 columns).

        Feature modules add theirs (e.g. the check-in card). Record lists of the admin are blocks too
        (`list:<id>`), added by the catalog.
        """
        return [
            {'key': 'announcements', 'label': _('Announcements'), 'feature': 'core', 'sequence': 0, 'locked': True},
            {'key': 'activities', 'label': _('To do'), 'feature': 'activities', 'sequence': 10},
            {'key': 'shortcuts', 'label': _('Quick actions'), 'feature': 'core', 'sequence': 20,
             'sizes': ['icons', 'buttons']},
            {'key': 'kpis', 'label': _('KPIs'), 'feature': 'core', 'sequence': 30, 'sizes': ['compact', 'large']},
            {'key': 'tiles', 'label': _('Features'), 'feature': 'core', 'sequence': 50,
             'sizes': ['cols3', 'cols2', 'cols4']},
            {'key': 'apps', 'label': _('Odoo apps'), 'feature': 'menus', 'sequence': 60},
        ]

    @api.model
    def _mobile_home_tabs(self):
        """Screens the bottom tab bar can show: [{key, label, feature, capability, sequence, fixed}].

        Home (first) and Settings (last) are always there (`fixed`). Feature modules add theirs.
        """
        return [
            {'key': 'home', 'label': _('Home'), 'feature': 'core', 'capability': None, 'sequence': 0, 'fixed': True},
            {'key': 'dashboard', 'label': _('Overview'), 'feature': 'dashboard', 'capability': 'view', 'sequence': 5},
            {'key': 'activities', 'label': _('To do'), 'feature': 'activities', 'capability': None, 'sequence': 10},
            {'key': 'notifications', 'label': _('Notifications'), 'feature': 'notifications', 'capability': None,
             'sequence': 20},
            {'key': 'settings', 'label': _('Settings'), 'feature': 'core', 'capability': None, 'sequence': 999,
             'fixed': True},
        ]

    @api.model
    def _mobile_home_tab_keys(self, layout):
        """Tabs of a role layout the user may open, between Home and Settings; None: the app's default tabs."""
        lines = layout.tab_line_ids if layout else []
        if not lines:
            return None
        declared = {t['key']: t for t in self._mobile_home_tabs()}
        middle = [line.tab_key for line in lines.sorted('sequence')
                  if line.tab_key in declared and not declared[line.tab_key].get('fixed')
                  and self._mobile_can(declared[line.tab_key]['feature'], declared[line.tab_key].get('capability'))]
        return ['home'] + list(dict.fromkeys(middle)) + ['settings']

    @api.model
    def _safe_trend(self, item):
        try:
            return item._kpi_trend(self.env)
        except Exception:  # never break the home screen for a chart
            _logger.exception('Mobile KPI trend %s could not be computed', item.id)
            return False

    @api.model
    def _layout_defaults(self, entries, lines, ref):
        """Role layout defaults on catalog entries: its lines first (in order, shown or not), others hidden."""
        if not lines:
            return entries
        by_ref = {ref(entry): entry for entry in entries}
        ordered = []
        for line in lines.sorted('sequence'):
            entry = by_ref.pop(line._ref(), None)
            if entry:
                entry = dict(entry, visible=entry['locked'] or line.visible)
                if line.size and line.size in entry.get('sizes', []):
                    entry['size'] = line.size
                ordered.append(entry)
        return ordered + [dict(entry, visible=entry['locked']) for entry in by_ref.values()]

    # ------------------------------------------------------------------
    # Catalog: every block the user may have, with its default visibility
    # ------------------------------------------------------------------

    @api.model
    def _mobile_home_catalog(self):
        """What the current user may put on their home screen, in the default order.

        A block is available when the user may use it: the feature (tiles, quick
        actions), the Odoo menu, or reading the records of a KPI. `visible` is the
        default (admin configuration and role targeting); `locked` ones stay visible.
        """
        items = self.env['mobile.home.item'].sudo().search([])

        def allowed(declared):
            return declared and self._mobile_can(declared['feature'], declared.get('capability'))

        tiles_declared = sorted((t for t in self._mobile_home_tiles() if allowed(t)), key=lambda t: t.get('sequence', 100))
        tile_items = {i.tile_key: i for i in items.filtered(lambda i: i.kind == 'tile')}
        configured = bool(tile_items)
        tiles = []
        # Configured tiles first, in the admin's order; the other allowed tiles can be added.
        for item in tile_items.values():
            if allowed(next((t for t in tiles_declared if t['key'] == item.tile_key), None)):
                tiles.append({'key': item.tile_key, 'label': item.name or False,
                              'visible': item.locked or item._default_for_user(), 'locked': item.locked})
        for declared in tiles_declared:
            if declared['key'] not in tile_items:
                tiles.append({'key': declared['key'], 'label': False, 'visible': not configured, 'locked': False})

        kpis = []
        for item in items.filtered(lambda i: i.kind == 'kpi'):
            try:
                value = item._kpi_value(self.env)  # as the user: their rights and record rules
            except Exception:  # a KPI made invalid by a module update must not break the home screen
                _logger.exception('Mobile KPI %s could not be computed', item.id)
                continue
            if value is None:  # cannot read these records: not available
                continue
            kpis.append({
                'id': item.id, 'label': item.name, 'icon': item.icon or False, 'color': item.color,
                'value': value, 'unit': item._kpi_unit(self.env), 'menu_id': item._openable_menu(self.env),
                'trend': self._safe_trend(item),
                'visible': item.locked or item._default_for_user(), 'locked': item.locked,
            })

        shortcuts_declared = {s['key']: s for s in self._mobile_home_shortcuts()}
        shortcuts = []
        for item in items.filtered(lambda i: i.kind == 'shortcut'):
            if not allowed(shortcuts_declared.get(item.shortcut_key)):
                continue
            menu_id = item._openable_menu(self.env) if item.shortcut_key == 'menu' else False
            if item.shortcut_key == 'menu' and not menu_id:
                continue
            shortcuts.append({'id': item.id, 'label': item.name, 'icon': item.icon or False, 'color': item.color,
                              'key': item.shortcut_key, 'menu_id': menu_id,
                              'visible': item.locked or item._default_for_user(), 'locked': item.locked})

        # Record lists: each one its own block (`list:<id>`), available when the user may open its menu.
        lists = {}
        for item in items.filtered(lambda i: i.kind == 'list'):
            if item._openable_menu(self.env):
                lists[f'list:{item.id}'] = item

        announcements = self.env['mobile.announcement']._for_user()
        content = {'shortcuts': shortcuts, 'kpis': kpis, 'tiles': tiles, 'announcements': announcements}
        declared = sorted(self._mobile_home_sections(), key=lambda s: s.get('sequence', 100))
        sections = [
            {'key': s['key'], 'visible': s.get('locked', False) or s.get('default_visible', True),
             'locked': s.get('locked', False), 'sequence': s.get('sequence', 100),
             **({'size': s['sizes'][0], 'sizes': s['sizes']} if s.get('sizes') else {})}
            for s in declared
            if allowed(s) and (s['key'] not in content or content[s['key']])
        ]
        sections += [{'key': key, 'visible': item.locked or item._default_for_user(), 'locked': item.locked,
                      'label': item.name, 'sequence': 40 + item.sequence / 1000}
                     for key, item in lists.items()]
        sections = [{k: v for k, v in s.items() if k != 'sequence'} for s in sorted(sections, key=lambda s: s['sequence'])]

        # The user's role layout sets the defaults (what is shown, order); items stay those the user may use.
        layout = self.env['mobile.home.layout']._for_user()
        if layout:
            sections = self._layout_defaults(sections, layout.section_line_ids, lambda e: e['key'])
            tiles = self._layout_defaults(tiles, layout.tile_line_ids, lambda e: e['key'])
            kpis = self._layout_defaults(kpis, layout.kpi_line_ids, lambda e: e['id'])
            shortcuts = self._layout_defaults(shortcuts, layout.shortcut_line_ids, lambda e: e['id'])
            configured = configured or bool(layout.tile_line_ids)
        return {'sections': sections, 'tiles': tiles, 'kpis': kpis, 'shortcuts': shortcuts,
                '_tiles_configured': configured, '_layout': layout, '_lists': lists,
                '_announcements': announcements}

    # ------------------------------------------------------------------
    # The user's own arrangement
    # ------------------------------------------------------------------

    @api.model
    def _home_ref(self, kind, entry):
        return entry['key'] if kind in ('sections', 'tiles') else entry['id']

    @api.model
    def _mobile_home_arranged(self):
        """The catalog in the user's order and with their choices (locked blocks stay visible).

        Blocks the user never arranged (added by the admin later) keep their default
        visibility, after the arranged ones.
        """
        catalog = self._mobile_home_catalog()
        preference = self.env['mobile.home.preference'].sudo()._for_user(self.env.user)
        data = (preference.data or {}) if preference else {}
        for kind in ('sections', 'tiles', 'kpis', 'shortcuts'):
            entries = {self._home_ref(kind, entry): entry for entry in catalog[kind]}
            arranged = []
            for choice in data.get(kind) or []:
                entry = entries.pop(choice.get('id'), None)
                if entry:
                    entry = dict(entry, visible=entry['locked'] or bool(choice.get('visible')))
                    if choice.get('size') in entry.get('sizes', []):
                        entry['size'] = choice['size']
                    arranged.append(entry)
            catalog[kind] = arranged + list(entries.values())
        catalog['customized'] = bool(preference)
        return catalog

    @api.model
    def _mobile_home(self):
        """Home screen of the current user (`/mobile/api/v1/home`): visible blocks, in order."""
        arranged = self._mobile_home_arranged()

        def visible(kind, keep=()):
            return [{k: v for k, v in e.items() if k not in ('visible', 'locked') or k in keep}
                    for e in arranged[kind] if e['visible']]

        tiles = visible('tiles')
        if not arranged['customized'] and not arranged['_tiles_configured']:
            tiles = None  # nothing configured: the app shows all the tiles the user may use (API 1.8)
        layout = arranged['_layout']
        sections = [s['key'] for s in arranged['sections'] if s['visible']]
        lists = []
        for key in sections:
            if key in arranged['_lists']:
                try:
                    data = arranged['_lists'][key]._list_data(self.env)
                except Exception:  # a list made invalid by a module update must not break the home screen
                    _logger.exception('Mobile record list %s could not be loaded', key)
                    data = None
                if data:
                    lists.append(data)
        shown = {f"list:{data['id']}" for data in lists}
        sections = [key for key in sections if not key.startswith('list:') or key in shown]
        return {
            'sections': sections,
            # API 1.12: layout of the blocks that offer several (tiles in 2/3/4 columns, large KPIs...).
            'section_sizes': {s['key']: s['size'] for s in arranged['sections'] if s['visible'] and s.get('size')},
            'tiles': tiles,
            'kpis': visible('kpis'),
            'shortcuts': visible('shortcuts'),
            'customized': arranged['customized'],
            # Role layout (API 1.10): its name, and the bottom tabs (None: the app's default ones).
            'layout': {'id': layout.id, 'name': layout.name} if layout else False,
            'tabs': self._mobile_home_tab_keys(layout),
            # API 1.11: record lists (blocks `list:<id>`) and company announcements.
            'lists': lists,
            'announcements': arranged['_announcements']._mobile_data() if 'announcements' in sections else [],
        }

    @api.model
    def _mobile_home_editor(self):
        """Everything the user may show, hide and reorder (`/home/catalog`)."""
        arranged = self._mobile_home_arranged()
        return {kind: arranged[kind] for kind in ('sections', 'tiles', 'kpis', 'shortcuts')} | {
            'customized': arranged['customized']}

    @api.model
    def _mobile_home_save(self, payload):
        """Store the user's arrangement (`/home/preferences`); unknown or unavailable blocks are dropped."""
        Preference = self.env['mobile.home.preference'].sudo()
        if payload.get('reset'):
            Preference._for_user(self.env.user).unlink()
            return self._mobile_home()
        catalog = self._mobile_home_catalog()
        data = {}
        for kind in ('sections', 'tiles', 'kpis', 'shortcuts'):
            known = {self._home_ref(kind, entry) for entry in catalog[kind]}
            data[kind] = [{'id': choice['id'], 'visible': bool(choice.get('visible')),
                           **({'size': choice['size']} if isinstance(choice.get('size'), str) else {})}
                          for choice in payload.get(kind) or []
                          if isinstance(choice, dict) and choice.get('id') in known]
        Preference._store(self.env.user, data)
        return self._mobile_home()

    @api.model
    def _mobile_home_dismiss(self, announcement_id):
        """The user has read an announcement: no longer on their home screen (unless mandatory)."""
        self.env['mobile.announcement'].sudo().browse(int(announcement_id)).exists()._mobile_dismiss(self.env.user)
        return self._mobile_home()
