"""Editable generic form: any record of any module, edited in the app like on the web.

The generic viewer (`mobile.record.viewer`) shows a short summary; in full mode it
gives every field of the form view, the lines of one2many fields (quality checks,
checklists, timesheets, components...), the statusbar (stages) and pictures /
signatures, with the same rules as the web client: view modifiers per record and
per line (`parent.x` included), the user's access rights, Odoo's onchange, buttons
of each line. No customer module needs mobile code for its forms.
"""
import logging
from contextlib import contextmanager

import psycopg2

from lxml import etree

from odoo import _, api, models, release
from odoo.exceptions import UserError
from odoo.tools import html2plaintext, plaintext2html

LIST = 'tree' if release.version_info[0] < 18 else 'list'

_logger = logging.getLogger(__name__)

FULL_SKIP_PREFIXES = ('message_', 'activity_', 'website_', 'access_')
MAX_LINES = 200
MAX_STAGES = 30
FALSE_VALUES = ('0', 'false', 'False')


class _Parent:
    """`parent` in the modifiers of a line: the record's values as attributes (parent.state...)."""

    def __init__(self, values):
        self.__dict__.update(values)


def _flag(value, default=True):
    return default if value is None else value not in FALSE_VALUES


class MobileFormSpec(models.AbstractModel):
    _inherit = 'mobile.form.spec'

    # ------------------------------------------------------------------
    # Lines (one2many fields)
    # ------------------------------------------------------------------

    @api.model
    def _line_views(self, model_name, view_id=None):
        """{field: {'columns', 'buttons', 'editable', 'create', 'delete'}} from the list sub-views of the form."""
        arch = etree.fromstring(self.env[model_name].get_view(view_id=view_id, view_type='form')['arch'])
        result = {}
        for node in arch.iter('field'):
            if any(ancestor.tag == 'field' for ancestor in node.iterancestors()):
                continue
            field = self.env[model_name]._fields.get(node.get('name'))
            if not field or field.type != 'one2many':
                continue
            sub = next((child for child in node if child.tag in ('list', 'tree')), None)
            if sub is None:
                # No inline list: the comodel's own list view, read-only like a web list without `editable`.
                sub = etree.fromstring(self.env[field.comodel_name].get_view(view_type=LIST)['arch'])
            def nested(element, sub=sub):
                """Inside a sub-sub-view (a field of the line with its own list): not a column."""
                parent = element.getparent()
                while parent is not None and parent is not sub:
                    if parent.tag == 'field':
                        return True
                    parent = parent.getparent()
                return False

            columns = [dict(c.attrib) for c in sub.iter('field') if not nested(c)]
            def toolbar(element, sub=sub):
                return any(a.tag in ('control', 'header') for a in element.iterancestors() if a is not sub and sub in a.iterancestors())

            buttons = [dict(b.attrib) for b in sub.iter('button') if not nested(b) and not toolbar(b)]
            result[field.name] = {
                'node': dict(node.attrib),
                'columns': [c for c in columns if c.get('name')],
                'buttons': buttons,
                'editable': bool(sub.get('editable')),
                'create': _flag(sub.get('create')),
                'delete': _flag(sub.get('delete')),
            }
        return result

    @api.model
    def _line_ctx(self, line_descs, row_values, row_id, parent_ctx):
        ctx = self._eval_context(line_descs, row_values, row_id)
        ctx['parent'] = _Parent({k: v for k, v in parent_ctx.items() if isinstance(k, str) and not k.startswith('_')})
        return ctx

    @api.model
    def _lines_spec(self, record, fname, view, parent_ctx, parent_meta):
        """Columns, rows with per-cell modifiers and per-row buttons, and what the user may do."""
        field = record._fields[fname]
        Line = self.env[field.comodel_name]
        names = [c['name'] for c in view['columns'] if c['name'] in Line._fields]
        referenced = set(names)
        for attrs in view['columns'] + view['buttons']:
            for key in ('invisible', 'readonly', 'required', 'column_invisible'):
                referenced |= {n for n in self._expr_names(attrs.get(key)) if n in Line._fields}
        descs = self._field_descs(field.comodel_name, referenced)
        column_ctx = {'parent': _Parent(parent_ctx), 'context': dict(self.env.context), 'uid': self.env.uid}
        columns, column_nodes = [], {}
        for attrs in view['columns']:
            name = attrs['name']
            desc = descs.get(name)
            if not desc or name in column_nodes:
                continue
            if self._eval_bool(attrs.get('column_invisible'), column_ctx, False):
                continue
            if desc['type'] == 'binary' and attrs.get('widget') not in ('image', 'contact_image', 'signature'):
                continue
            column_nodes[name] = [attrs]
            meta = self._field_meta(name, desc, [attrs])
            # Column defaults; each row has its own `states` (modifiers may depend on the line).
            meta.update(invisible=False, readonly=bool(desc.get('readonly')), required=bool(desc.get('required')))
            columns.append(meta)
        readonly = bool(parent_meta.get('readonly'))
        editable = not readonly and Line.has_access('write') if hasattr(Line, 'has_access') \
            else not readonly and Line.check_access_rights('write', raise_exception=False)
        can_create = editable and view['create'] and (
            Line.has_access('create') if hasattr(Line, 'has_access') else Line.check_access_rights('create', raise_exception=False))
        can_delete = editable and view['delete'] and (
            Line.has_access('unlink') if hasattr(Line, 'has_access') else Line.check_access_rights('unlink', raise_exception=False))
        rows = []
        lines = record[fname]
        for line in lines[:MAX_LINES]:
            try:
                values = self._read_values(line, descs, list(descs))
            except Exception:  # a line the user may not read (record rules)
                continue
            ctx = self._line_ctx(descs, values, line.id, parent_ctx)
            states = {name: self._field_state(descs[name], column_nodes[name], ctx) for name in column_nodes}
            rows.append({
                'id': line.id,
                'display_name': line.display_name,
                'values': {name: values.get(name, False) for name in column_nodes},
                'states': states,
                # Like the web: buttons of a line work even when its fields are read-only.
                'buttons': [self._public_button(b) for b in self._buttons(view['buttons'], ctx)],
            })
        return {
            'field': fname,
            'string': parent_meta.get('string') or field.string,
            'model': field.comodel_name,
            'columns': columns,
            'rows': rows,
            'total': len(lines),
            'editable': bool(editable),
            'can_create': bool(can_create),
            'can_delete': bool(can_delete),
        }

    @api.model
    def _public_button(self, button):
        return {k: v for k, v in button.items() if not k.startswith('_')}

    @api.model
    def _line_record(self, record, fname, line_id):
        field = record._fields.get(fname)
        if not field or field.type != 'one2many':
            raise UserError(_('%s has no lines %s.', record._name, fname))
        line = record[fname].filtered(lambda l: l.id == int(line_id)) if line_id else self.env[field.comodel_name]
        if line_id and not line:
            raise UserError(_('This line is not on the record any more. Please refresh.'))
        return field, line

    # ------------------------------------------------------------------
    # Statusbar (stages)
    # ------------------------------------------------------------------

    @api.model
    def _statusbar_spec(self, record, parsed, spec, ctx):
        """The header's statusbar: choices in order and whether the user may click them."""
        for fname, nodes in parsed['fields'].items():
            node = next((n for n in nodes if n.get('widget') == 'statusbar'), None)
            if not node or fname not in record._fields:
                continue
            field = record._fields[fname]
            meta = spec['fields'].get(fname) or {}
            current = record[fname]
            options = self._eval(node.get('options') or '{}', ctx, default={}) or {}
            clickable = bool(options.get('clickable')) and not meta.get('readonly', True)
            if field.type == 'selection':
                visible = [s.strip() for s in (node.get('statusbar_visible') or '').split(',') if s.strip()]
                choices = [[key, label] for key, label in field._description_selection(self.env)
                           if not visible or key in visible or key == current]
                value = current or False
            elif field.type == 'many2one':
                Stage = self.env[field.comodel_name]
                domain = self._eval(node['domain'], ctx, default=[]) if node.get('domain') else []
                stages = Stage.search(domain or [], limit=MAX_STAGES)
                fold = options.get('fold_field')
                if fold and fold in Stage._fields:
                    stages = stages.filtered(lambda s: not s[fold] or s == current)
                choices = [[stage.id, stage.display_name] for stage in stages]
                value = current.id or False
            else:
                continue
            return {'field': fname, 'string': meta.get('string') or field.string, 'choices': choices,
                    'value': value, 'clickable': clickable}
        return False


class MobileRecordViewer(models.AbstractModel):
    _inherit = 'mobile.record.viewer'

    @api.model
    def _full_field_names(self, model_name):
        """Every field of the form view (in view order) that the app can show or edit."""
        engine = self.env['mobile.form.spec']
        parsed = engine._parse_view(model_name)
        model_fields = self.env[model_name]._fields
        arch = etree.fromstring(self.env[model_name].get_view(view_type='form')['arch'])
        # Counters of smart buttons (related lists) are not fields of the record to show.
        in_buttons = {n.get('name') for n in arch.xpath('//button//field | //div[@name="button_box"]//field')}
        elsewhere = {n.get('name') for n in arch.iter('field')
                     if not any(a.tag == 'button' or a.get('name') == 'button_box' for a in n.iterancestors())}
        names = []
        for name in parsed['fields']:
            field = model_fields.get(name)
            if not field or name in ('id', 'display_name') or name.startswith(FULL_SKIP_PREFIXES):
                continue
            if name in in_buttons and name not in elsewhere:
                continue
            names.append(name)
        return names

    @api.model
    def _can_write(self, record):
        try:
            if hasattr(record, 'check_access'):
                record.check_access('write')
            else:
                record.check_access_rights('write')
                record.check_access_rule('write')
            return True
        except Exception:
            return False

    @api.model
    def _mobile_full_spec(self, record, values=None):
        engine = self.env['mobile.form.spec']
        names = self._full_field_names(record._name)
        spec = engine._build_spec(record._name, record=record, values=values, field_names=names)
        # Rich text as plain text: the app edits text, not HTML (converted back on save).
        for name, meta in spec['fields'].items():
            if meta['type'] == 'html' and isinstance(spec['values'].get(name), str):
                spec['values'][name] = html2plaintext(spec['values'][name] or '')
        parsed, ctx = spec['_parsed'], spec['_ctx']
        line_views = engine._line_views(record._name)
        spec['lines'] = {}
        parent_ctx = {k: v for k, v in ctx.items() if k in spec['_descs']}
        spec['_parent_ctx'] = parent_ctx
        spec['_line_views'] = line_views
        for name, meta in spec['fields'].items():
            if meta['type'] == 'one2many' and name in line_views and not meta.get('invisible'):
                # The engine shows one2many fields read-only in record services: here, the view's own rule.
                state = engine._field_state(spec['_descs'][name], parsed['fields'].get(name, []), ctx)
                spec['lines'][name] = engine._lines_spec(record, name, line_views[name], parent_ctx,
                                                         dict(meta, readonly=state['readonly']))
        spec['statusbar'] = engine._statusbar_spec(record, parsed, spec, ctx)
        spec['editable'] = self._can_write(record)
        IrModel = self.env['ir.model'].sudo()
        spec['model_name'] = IrModel._get(record._name).name
        state = record._fields.get('state')
        spec['state_selection'] = [list(s) for s in state._description_selection(self.env)] \
            if state and state.type == 'selection' else []
        spec['attachments'] = self.env['mobile.record.service']._attachments(record)
        spec['full'] = True
        return spec

    @api.model
    def _html_in(self, record, values):
        """Plain text from the app -> HTML; unchanged text keeps the record's own formatting."""
        values = dict(values or {})
        for name, value in list(values.items()):
            field = record._fields.get(name)
            if field and field.type == 'html' and isinstance(value, str):
                if value.strip() == html2plaintext(record[name] or '').strip():
                    values.pop(name)
                else:
                    values[name] = plaintext2html(value)
        return values

    @api.model
    def _mobile_full_onchange(self, record, values, changed):
        engine = self.env['mobile.form.spec']
        names = self._full_field_names(record._name)
        new_values, warning = engine._onchange(record._name, record, self._html_in(record, values), changed, names)
        spec = self._mobile_full_spec(record, new_values)
        spec['warning'] = warning
        return spec

    @contextmanager
    def _written(self, model):
        """Write now, in a savepoint: a missing mandatory field becomes a message for the user, not an HTML page
        at commit time."""
        try:
            with self.env.cr.savepoint():
                yield
                self.env.flush_all()
        except psycopg2.errors.NotNullViolation as exc:
            column = exc.diag.column_name
            field = self.env[model]._fields.get(column)
            label = field.get_description(self.env)['string'] if field else column
            raise UserError(_('Please fill in "%s".', label)) from None
        except psycopg2.IntegrityError:
            raise UserError(_('The data is not valid: a required value is missing or a rule is broken.')) from None

    @api.model
    def _mobile_full_save(self, record, values):
        engine = self.env['mobile.form.spec']
        names = self._full_field_names(record._name)
        with self._written(record._name):
            engine._save(record._name, record=record, values=self._html_in(record, values), field_names=names)
        record.invalidate_recordset()
        return self._mobile_full_spec(record)

    @api.model
    def _mobile_set_stage(self, record, value):
        spec = self._mobile_full_spec(record)
        statusbar = spec.get('statusbar')
        if not statusbar or not statusbar['clickable']:
            raise UserError(_('The stage cannot be changed here.'))
        if value not in [choice[0] for choice in statusbar['choices']]:
            raise UserError(_('This stage is not available. Please refresh.'))
        record.write({statusbar['field']: value})
        record.invalidate_recordset()
        return self._mobile_full_spec(record)

    # ------------------------------------------------------------------
    # Lines
    # ------------------------------------------------------------------

    @api.model
    def _line_context(self, record, fname):
        engine = self.env['mobile.form.spec']
        spec = self._mobile_full_spec(record)
        lines = spec['lines'].get(fname)
        if not lines:
            raise UserError(_('These lines cannot be changed here.'))
        view = engine._line_views(record._name)[fname]
        return spec, lines, view

    @api.model
    def _line_defaults_context(self, record, fname, spec, view):
        """The one2many field's own `context` (default_project_id...) + the link to the record, like the web."""
        engine = self.env['mobile.form.spec']
        field = record._fields[fname]
        context = {}
        if view['node'].get('context'):
            context = engine._eval(view['node']['context'], spec['_ctx'], default={}) or {}
        context = {k: v for k, v in dict(context).items() if isinstance(k, str)}
        context[f'default_{field.inverse_name}'] = record.id
        return context

    @api.model
    def _mobile_line_new(self, record, fname):
        """Default values of a new line (Odoo's defaults, linked to the record)."""
        engine = self.env['mobile.form.spec']
        spec, lines, view = self._line_context(record, fname)
        if not lines['can_create']:
            raise UserError(_('You cannot add lines here.'))
        field = record._fields[fname]
        Line = self.env[field.comodel_name].with_context(**self._line_defaults_context(record, fname, spec, view))
        names = [c['name'] for c in lines['columns']]
        nodes = {}
        for attrs in view['columns']:
            if attrs['name'] in names:
                nodes.setdefault(attrs['name'], []).append(attrs)
        referenced = set(names)
        for attrs in view['columns']:
            for key in ('invisible', 'readonly', 'required'):
                referenced |= {n for n in engine._expr_names(attrs.get(key)) if n in Line._fields}
        descs = engine._field_descs(field.comodel_name, referenced)
        new = Line.new(dict(Line.default_get(list(referenced)), **{field.inverse_name: record.id}))
        values = engine._read_values(new, descs, list(descs))
        # Modifiers of the new line's cells, as the web shows them before anything is typed.
        ctx = engine._line_ctx(descs, values, False, spec['_parent_ctx'])
        states = {name: engine._field_state(descs[name], nodes[name], ctx) for name in names if name in nodes}
        return {'values': {name: values.get(name, False) for name in names}, 'states': states,
                'columns': lines['columns']}

    @api.model
    def _mobile_line_save(self, record, fname, line_id, values):
        engine = self.env['mobile.form.spec']
        spec, lines, view = self._line_context(record, fname)
        field, line = engine._line_record(record, fname, line_id)
        if not lines['editable'] or (not line and not lines['can_create']):
            raise UserError(_('These lines cannot be changed here.'))
        names = [c['name'] for c in lines['columns']]
        descs = engine._field_descs(field.comodel_name, names)
        if line:
            row = next(r for r in lines['rows'] if r['id'] == line.id)
            writable = [n for n in names if n in values and not row['states'].get(n, {}).get('readonly')]
        else:
            # Empty cells the user left alone are not sent: Odoo computes them (a sale line's description...).
            new = self._mobile_line_new(record, fname)
            defaults = new['values']
            writable = [n for n in names if n in values and not new['states'].get(n, {}).get('readonly')
                        and not (values[n] in (False, None, '', []) and values[n] == defaults.get(n))]
        orm_values = engine._to_orm(descs, {n: values[n] for n in writable})
        with self._written(field.comodel_name):
            if line:
                if orm_values:
                    line.write(orm_values)
            else:
                self.env[field.comodel_name].with_context(**self._line_defaults_context(record, fname, spec, view)).create(
                    dict(orm_values, **{field.inverse_name: record.id}))
        record.invalidate_recordset()
        return self._mobile_full_spec(record)

    @api.model
    def _mobile_line_name_search(self, record, fname, column, query='', values=None, line_id=None, limit=20):
        """Choices of a many2one/many2many cell, with the column's domain (it may use the line and `parent.`)."""
        engine = self.env['mobile.form.spec']
        spec, _lines, view = self._line_context(record, fname)
        field, line = engine._line_record(record, fname, line_id)
        node = next((c for c in view['columns'] if c.get('name') == column), None)
        Line = self.env[field.comodel_name]
        desc = Line.fields_get([column], attributes=['type', 'relation', 'domain', 'context']).get(column)
        if node is None or not desc or desc['type'] not in ('many2one', 'many2many'):
            raise UserError(_('Field %s is not a relational field.', column))
        domain_expr = node.get('domain') or desc.get('domain') or []
        context_expr = node.get('context') or desc.get('context') or {}
        names = {n for n in engine._expr_names(domain_expr) | engine._expr_names(context_expr) if n in Line._fields}
        descs = engine._field_descs(field.comodel_name, names)
        current = engine._read_values(line, descs, list(descs)) if line else {}
        current.update({k: v for k, v in (values or {}).items() if k in descs})
        ctx = engine._line_ctx(descs, {n: current.get(n, False) for n in descs}, line.id or False, spec['_parent_ctx'])
        domain = engine._eval(domain_expr, ctx, default=[]) if isinstance(domain_expr, str) else domain_expr
        context = engine._eval(context_expr, ctx, default={}) if isinstance(context_expr, str) else context_expr
        comodel = self.env[desc['relation']].with_context(**dict(context or {}))
        return [[rec_id, name] for rec_id, name in comodel.name_search(query or '', list(domain or []), limit=limit)]

    @api.model
    def _mobile_line_delete(self, record, fname, line_id):
        engine = self.env['mobile.form.spec']
        _spec, lines, _view = self._line_context(record, fname)
        _field, line = engine._line_record(record, fname, line_id)
        if not lines['can_delete']:
            raise UserError(_('You cannot remove lines here.'))
        line.unlink()
        record.invalidate_recordset()
        return self._mobile_full_spec(record)

    @api.model
    def _mobile_line_action(self, record, fname, line_id, key):
        engine = self.env['mobile.form.spec']
        spec, lines, view = self._line_context(record, fname)
        _field, line = engine._line_record(record, fname, line_id)
        row = next((r for r in lines['rows'] if r['id'] == line.id), None)
        if not row:
            raise UserError(_('This line is not on the record any more. Please refresh.'))
        # The button as computed for this line (visibility per line and per record), with its context.
        names = {n for a in view['buttons'] for key in ('invisible', 'context') for n in engine._expr_names(a.get(key))}
        line_descs = engine._field_descs(line._name, {n for n in names if n in line._fields})
        ctx = engine._line_ctx(line_descs, engine._read_values(line, line_descs, list(line_descs)), line.id,
                               spec['_parent_ctx'])
        button = engine._find_button(engine._buttons(view['buttons'], ctx), key)
        result = engine._execute_button(line, button)
        record.invalidate_recordset()
        if result.get('type') != 'wizard':
            result['record'] = engine._public(self._mobile_full_spec(record))
        return result
