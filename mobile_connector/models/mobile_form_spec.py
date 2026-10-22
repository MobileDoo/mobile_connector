"""Form spec engine: turns an Odoo form view + record into JSON the app can render.

Why server-side: view modifiers differ between versions (`attrs` domains in 16,
Python expressions in 17+) and customer modules extend views freely. Evaluating
them here means the app never re-implements Odoo's view language, and every
inherited view, onchange, constraint and button of a custom module just works.

Value format exchanged with the app ("mobile format"):
    many2one            [id, display_name] | false
    many2many/one2many  [[id, display_name], ...]
    date                "YYYY-MM-DD" | false
    datetime            "YYYY-MM-DD HH:MM:SS" (UTC) | false
    others              plain JSON value
"""
import ast
import json
import logging
import re

from dateutil.relativedelta import relativedelta
from lxml import etree

from odoo import _, api, fields, models, Command
from odoo.exceptions import UserError
from odoo.tools.safe_eval import safe_eval, datetime as safe_datetime, time as safe_time

_logger = logging.getLogger(__name__)

SUPPORTED_TYPES = {
    'char', 'text', 'html', 'integer', 'float', 'monetary', 'boolean',
    'date', 'datetime', 'selection', 'many2one', 'many2many', 'one2many',
    # Pictures and signatures only (widget image / signature): the app gets true/false, never the data.
    'binary',
}
# Odoo widget -> what the app shows (a photo taken with the camera, or a pad to sign on).
BINARY_WIDGETS = {'image': 'image', 'contact_image': 'image', 'signature': 'signature'}
SIGNATURE_SIZE = (600, 240)
RELATIONAL_TYPES = {'many2one', 'many2many', 'one2many'}
FIELD_ATTRIBUTES = ['type', 'string', 'help', 'relation', 'selection', 'required', 'readonly', 'domain', 'context']
DANGER_HINTS = ('refuse', 'reject', 'cancel', 'delete', 'unlink')


class MobileFormSpec(models.AbstractModel):
    _name = 'mobile.form.spec'
    _description = 'Mobile Form Spec Engine'

    # ------------------------------------------------------------------
    # View parsing
    # ------------------------------------------------------------------

    @api.model
    def _parse_view(self, model_name, view_type='form', view_id=None):
        """Top-level field nodes (not inside x2many sub-views) and buttons: header, footer, form body.

        Body buttons (banners, inline actions of customer modules, e.g. OCA tier validation's
        Validate / Reject) count too, smart buttons excepted (they open related lists). A field or
        button inside an element with its own `invisible` is hidden with it.

        `get_view` already applies view inheritance and strips nodes the user
        cannot see because of `groups`.
        """
        view = self.env[model_name].get_view(view_id=view_id, view_type=view_type)
        arch = etree.fromstring(view['arch'])

        def top_level(node):
            return not any(ancestor.tag == 'field' for ancestor in node.iterancestors())

        def with_parents(node):
            """Its attributes, hidden too when an enclosing element (alert, group, page...) is hidden."""
            attrs = dict(node.attrib)
            hidden_by = [a.get('invisible') for a in node.iterancestors() if a.tag != 'form' and a.get('invisible')]
            conditions = [c for c in [attrs.get('invisible')] + hidden_by if c]
            if hidden_by:
                attrs['invisible'] = ' or '.join(f'({c})' for c in conditions)
            return attrs

        field_nodes = {}
        for node in arch.iter('field'):
            if top_level(node) and node.get('name'):
                field_nodes.setdefault(node.get('name'), []).append(with_parents(node))
        for node in arch.iter('field'):
            if top_level(node) and node.get('widget') == 'daterange':
                end_field = self._daterange_end_field(node)
                if end_field:
                    field_nodes.setdefault(end_field, []).append(self._daterange_end_node(node))

        def smart_button(node):
            """Smart buttons (related lists) and link-style helpers next to a field (reports, recompute)."""
            css = node.get('class') or ''
            return ('oe_stat_button' in css or 'oe_link' in css or 'btn-link' in css
                    or bool(node.xpath('ancestor::div[@name="button_box"]')))

        header = [with_parents(node) for node in arch.xpath('//header//button') if top_level(node)]
        footer = [with_parents(node) for node in arch.xpath('//footer//button') if top_level(node)]
        # Anywhere else in the form: in the sheet, or banners modules put around it.
        body = [with_parents(node) for node in arch.xpath('//button[not(ancestor::header) and not(ancestor::footer)]')
                if top_level(node) and not smart_button(node)]
        return {'fields': field_nodes, 'header': header, 'footer': footer, 'body': body}

    @api.model
    def _expr_names(self, expr):
        if not expr or not isinstance(expr, str):
            return set()
        try:
            tree = ast.parse(expr.strip(), mode='eval')
        except SyntaxError:
            return set()
        return {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}

    @api.model
    def _needed_fields(self, model_name, parsed, field_names):
        """Minimal set of fields to read for `field_names`.

        Objects often have hundreds of fields while mobile shows a handful:
        read only the shown fields, the fields their own modifiers refer to,
        and the fields the buttons' visibility refers to, never the whole
        desktop form (which may hold dozens of costly computed fields).
        """
        names = set(field_names)
        for fname in field_names:
            for attrs in parsed['fields'].get(fname, []):
                for key in ('invisible', 'readonly', 'required'):
                    names |= self._expr_names(attrs.get(key))
        for attrs in parsed['header'] + parsed['footer'] + parsed.get('body', []):
            for key in ('invisible', 'context'):
                names |= self._expr_names(attrs.get(key))
        model_fields = self.env[model_name]._fields
        return {name for name in names if name in model_fields and name != 'id'}

    @api.model
    def _referenced_fields(self, parsed):
        """Every field of the form view: what the web client sends to onchange() for a new record."""
        names = set(parsed['fields'])
        for nodes in parsed['fields'].values():
            for attrs in nodes:
                for key in ('invisible', 'readonly', 'required', 'domain', 'context'):
                    names |= self._expr_names(attrs.get(key))
        for attrs in parsed['header'] + parsed['footer'] + parsed.get('body', []):
            for key in ('invisible', 'context'):
                names |= self._expr_names(attrs.get(key))
        return names

    # ------------------------------------------------------------------
    # Values
    # ------------------------------------------------------------------

    @api.model
    def _field_descs(self, model_name, fnames):
        descs = self.env[model_name].fields_get(list(fnames), attributes=FIELD_ATTRIBUTES)
        return {name: desc for name, desc in descs.items() if desc.get('type') in SUPPORTED_TYPES}

    @api.model
    def _to_mobile(self, desc, value):
        ftype = desc['type']
        if ftype == 'many2one':
            if not value:
                return False
            rec_id = value.id if isinstance(value.id, int) else value._origin.id
            return [rec_id, value.display_name] if rec_id else False
        if ftype in ('many2many', 'one2many'):
            result = []
            for rec in value:
                rec_id = rec.id if isinstance(rec.id, int) else rec._origin.id
                if rec_id:
                    result.append([rec_id, rec.display_name])
            return result
        if ftype == 'date':
            return fields.Date.to_string(value) if value else False
        if ftype == 'datetime':
            return fields.Datetime.to_string(value) if value else False
        if ftype in ('float', 'monetary'):
            return value or 0.0
        if ftype == 'binary':
            return bool(value)  # read with bin_size: a size, never the picture itself
        return value

    @api.model
    def _signature_png(self, signature):
        """Strokes drawn on the phone -> PNG (base64): {'strokes': [[[x, y], ...], ...], 'width', 'height'}."""
        import base64
        import io
        from PIL import Image, ImageDraw
        width, height = SIGNATURE_SIZE
        scale_x = width / max(float(signature.get('width') or width), 1)
        scale_y = height / max(float(signature.get('height') or height), 1)
        image = Image.new('RGBA', (width, height), (255, 255, 255, 0))
        draw = ImageDraw.Draw(image)
        strokes = signature.get('strokes') or []
        if not any(len(stroke) for stroke in strokes):
            raise UserError(_('The signature is empty.'))
        for stroke in strokes[:500]:
            points = [(float(x) * scale_x, float(y) * scale_y) for x, y in stroke[:2000]]
            if len(points) == 1:
                x, y = points[0]
                draw.ellipse([x - 2, y - 2, x + 2, y + 2], fill=(20, 20, 60, 255))
            elif points:
                draw.line(points, fill=(20, 20, 60, 255), width=4, joint='curve')
        buffer = io.BytesIO()
        image.save(buffer, format='PNG')
        return base64.b64encode(buffer.getvalue()).decode()

    @api.model
    def _mobile_widget(self, desc, widget):
        return BINARY_WIDGETS.get(widget, widget) if desc['type'] == 'binary' else widget

    @api.model
    def _binary_to_orm(self, value):
        """True = unchanged (the app only knows a value exists); False = cleared; a photo or a drawn signature."""
        if value is True or value is None:
            return None
        if not value:
            return False
        if isinstance(value, dict):
            if value.get('strokes') is not None:
                return self._signature_png(value)
            return value.get('datas') or False
        return value if isinstance(value, str) else None

    @api.model
    def _read_values(self, record, descs, fnames):
        values = {}
        for fname in fnames:
            desc = descs.get(fname)
            if not desc:
                continue
            try:
                source = record.with_context(bin_size=True) if desc['type'] == 'binary' else record
                values[fname] = self._to_mobile(desc, source[fname])
            except Exception:  # field-level access rights, broken computes in custom code...
                _logger.debug('mobile spec: cannot read %s.%s', record._name, fname, exc_info=True)
                values[fname] = False
        return values

    @api.model
    def _ids_of(self, value):
        if not value:
            return []
        ids = []
        for item in value:
            if isinstance(item, (list, tuple)) and item:
                ids.append(item[0])
            elif isinstance(item, dict) and item.get('id'):
                ids.append(item['id'])
            elif isinstance(item, int):
                ids.append(item)
        return [i for i in ids if isinstance(i, int)]

    @api.model
    def _id_of(self, value):
        if isinstance(value, (list, tuple)):
            return value[0] if value else False
        if isinstance(value, dict):
            return value.get('id') or False
        return value or False

    @api.model
    def _to_orm(self, descs, values):
        """Mobile format -> values accepted by create/write/onchange."""
        result = {}
        for fname, value in values.items():
            desc = descs.get(fname)
            if not desc or desc['type'] == 'one2many':
                continue
            if desc['type'] == 'many2one':
                result[fname] = self._id_of(value)
            elif desc['type'] == 'many2many':
                result[fname] = [Command.set(self._ids_of(value))]
            elif desc['type'] == 'binary':
                binary = self._binary_to_orm(value)
                if binary is not None:
                    result[fname] = binary
            else:
                result[fname] = value
        return result

    @api.model
    def _from_onchange(self, desc, value, previous):
        ftype = desc['type']
        if ftype == 'many2one':
            if not value:
                return False
            if isinstance(value, dict):
                return [value['id'], value.get('display_name') or '']
            if isinstance(value, (list, tuple)):
                return [value[0], value[1] if len(value) > 1 else '']
            record = self.env[desc['relation']].browse(value)
            return [record.id, record.display_name]
        if ftype in ('many2many', 'one2many'):
            ids = self._ids_of(previous)
            if value and all(isinstance(item, int) for item in value):
                ids, value = list(value), []  # plain ids (no sub-fields requested)
            for command in value or []:
                if not isinstance(command, (list, tuple)) or not command:
                    continue
                op = command[0]
                if op == Command.SET:
                    ids = list(command[2] or [])
                elif op == Command.CLEAR:
                    ids = []
                elif op == Command.LINK and command[1] not in ids:
                    ids.append(command[1])
                elif op in (Command.UNLINK, Command.DELETE):
                    ids = [i for i in ids if i != command[1]]
            records = self.env[desc['relation']].browse([i for i in ids if isinstance(i, int)]).exists()
            return [[rec.id, rec.display_name] for rec in records]
        if ftype == 'binary':
            return bool(value)
        return value

    # ------------------------------------------------------------------
    # Expression evaluation
    # ------------------------------------------------------------------

    @api.model
    def _eval_context(self, descs, values, record_id=False):
        ctx = {}
        for fname, value in values.items():
            ftype = descs.get(fname, {}).get('type')
            if ftype == 'many2one':
                ctx[fname] = self._id_of(value)
            elif ftype in ('many2many', 'one2many'):
                ctx[fname] = self._ids_of(value)
            elif ftype == 'date':
                ctx[fname] = fields.Date.to_date(value) if value else False
            elif ftype == 'datetime':
                ctx[fname] = fields.Datetime.to_datetime(value) if value else False
            else:
                ctx[fname] = value
        today = fields.Date.context_today(self)
        ctx.update({
            'id': record_id or False,
            'uid': self.env.uid,
            'context': dict(self.env.context),
            'current_company_id': self.env.company.id,
            'allowed_company_ids': self.env.companies.ids,
            'today': fields.Date.to_string(today),
            'now': fields.Datetime.to_string(fields.Datetime.now()),
            'context_today': lambda: today,
            'datetime': safe_datetime,
            'time': safe_time,
            'relativedelta': relativedelta,
        })
        return ctx

    @api.model
    def _eval(self, expr, ctx, default=None):
        try:
            return safe_eval(expr, dict(ctx))
        except Exception:
            _logger.debug('mobile spec: cannot evaluate %r', expr, exc_info=True)
            return default

    @api.model
    def _eval_bool(self, expr, ctx, default=False):
        if expr is None:
            return default
        expr = expr.strip()
        if expr in ('', '0', 'False', 'false'):
            return False
        if expr in ('1', 'True', 'true'):
            return True
        result = self._eval(expr, ctx, default=None)
        return default if result is None else bool(result)

    # ------------------------------------------------------------------
    # Spec
    # ------------------------------------------------------------------

    @api.model
    def _field_state(self, desc, nodes, ctx):
        """Merge modifiers of every node showing the field (a view may show a field twice)."""
        if not nodes:
            return {'invisible': False, 'readonly': bool(desc.get('readonly')), 'required': bool(desc.get('required'))}
        states = []
        for attrs in nodes:
            states.append({
                'invisible': self._eval_bool(attrs.get('invisible'), ctx, False),
                # Unparseable readonly -> readonly: safer, and the server re-checks on save anyway.
                'readonly': self._eval_bool(attrs['readonly'], ctx, True) if 'readonly' in attrs else bool(desc.get('readonly')),
                'required': self._eval_bool(attrs['required'], ctx, False) if 'required' in attrs else bool(desc.get('required')),
            })
        visible = [s for s in states if not s['invisible']]
        if not visible:
            return {'invisible': True, 'readonly': all(s['readonly'] for s in states), 'required': False}
        return {
            'invisible': False,
            'readonly': all(s['readonly'] for s in visible),
            'required': any(s['required'] for s in visible),
        }

    @api.model
    def _field_meta(self, fname, desc, nodes):
        meta = {
            'name': fname,
            'type': desc['type'],
            'string': next((n['string'] for n in nodes if n.get('string')), None) or desc.get('string') or fname,
            'help': desc.get('help') or False,
            'widget': self._mobile_widget(desc, next((n['widget'] for n in nodes if n.get('widget')), None) or False),
            'placeholder': next((n['placeholder'] for n in nodes if n.get('placeholder')), None) or False,
        }
        if desc.get('selection'):
            meta['selection'] = [[key, label] for key, label in desc['selection']]
        if desc.get('relation'):
            meta['relation'] = desc['relation']
        return meta

    @api.model
    def _buttons(self, button_nodes, ctx):
        buttons, seen = [], set()
        for attrs in button_nodes:
            label = attrs.get('string') or attrs.get('title') or attrs.get('name') or ''
            if attrs.get('special'):
                buttons.append({
                    'key': f"special:{attrs['special']}", 'type': 'special', 'name': attrs['special'],
                    'label': label, 'style': 'secondary', 'confirm': False,
                })
                continue
            btype, name = attrs.get('type'), attrs.get('name')
            if btype not in ('object', 'action') or not name:
                continue
            if btype == 'object' and name.startswith('_'):
                continue
            if self._eval_bool(attrs.get('invisible'), ctx, False):
                continue
            css = attrs.get('class') or ''
            if 'btn-danger' in css or any(hint in name.lower() for hint in DANGER_HINTS):
                style = 'danger'
            elif 'btn-primary' in css or 'oe_highlight' in css:
                style = 'primary'
            else:
                style = 'secondary'
            button_ctx = self._eval(attrs['context'], ctx, default={}) if attrs.get('context') else {}
            if f'{btype}:{name}' in seen:
                continue  # same button declared twice with complementary conditions
            seen.add(f'{btype}:{name}')
            buttons.append({
                'key': f'{btype}:{name}', 'type': btype, 'name': name, 'label': label, 'style': style,
                'confirm': attrs.get('confirm') or False,
                '_context': button_ctx if isinstance(button_ctx, dict) else {},
            })
        return buttons

    @api.model
    def _daterange_end_field(self, node):
        """`options="{'end_date_field': 'request_date_to'}"` of a daterange widget."""
        match = re.search(r"""['"]end_date_field['"]\s*:\s*['"](\w+)['"]""", node.get('options') or '')
        return match.group(1) if match else None

    @api.model
    def _daterange_end_node(self, node):
        """The end date is edited inside the start date's daterange widget on the web, the field itself
        being `invisible="1"`. The app shows it as its own field, visible and editable like the start date
        (including the conditions of the containers hiding the widget, e.g. half-day time off)."""
        conditions = [f'({expr})' for expr in [node.get('invisible')] + [
            ancestor.get('invisible') for ancestor in node.iterancestors()] if expr]
        attrs = {key: node.get(key) for key in ('readonly', 'required') if node.get(key)}
        if conditions:
            attrs['invisible'] = ' or '.join(conditions)
        return attrs

    @api.model
    def _build_spec(self, model_name, record=None, values=None, field_names=None,
                    view_type='form', view_id=None, buttons_from='header'):
        """Spec for `record` (existing) or a new record described by `values` (mobile format).

        `values` override the record's stored values, so the spec reflects unsaved edits.
        Keys starting with `_` are internal; use `_public` before sending to the app.
        """
        parsed = self._parse_view(model_name, view_type, view_id)
        if field_names is None:
            field_names = list(parsed['fields'])
        descs = self._field_descs(model_name, self._needed_fields(model_name, parsed, field_names))

        current = self._read_values(record, descs, descs) if record else {}
        if values:
            current.update({k: v for k, v in values.items() if k in descs})
        ctx = self._eval_context(descs, {f: current.get(f, False) for f in descs}, record.id if record else False)

        spec_fields = {}
        for fname in field_names:
            desc = descs.get(fname)
            if not desc:
                continue
            nodes = parsed['fields'].get(fname, [])
            if desc['type'] == 'binary' and not any(n.get('widget') in BINARY_WIDGETS for n in nodes):
                continue  # file fields, attachments: not part of a mobile form
            meta = self._field_meta(fname, desc, nodes)
            meta.update(self._field_state(desc, nodes, ctx))
            if desc['type'] == 'one2many':
                meta['readonly'] = True
            spec_fields[fname] = meta

        version = False
        if record and not record._transient:
            version = self.env['mobile.api']._mobile_record_versions(model_name, record.ids).get(record.id, False)
        return {
            'model': model_name,
            'id': record.id if record else False,
            'version': version,
            'display_name': record.display_name if record else False,
            'fields': spec_fields,
            'order': [f for f in field_names if f in spec_fields],
            'values': self._spec_values(model_name, record, values, current, descs),
            # Form: header then body buttons; wizards: their footer.
            'buttons': self._buttons(parsed[buttons_from] + (parsed.get('body', []) if buttons_from == 'header' else []), ctx),
            '_descs': descs,
            '_parsed': parsed,
            '_ctx': ctx,
        }

    @api.model
    def _spec_values(self, model_name, record, values, current, descs):
        """Values sent to the app.

        Existing record: only the needed fields. New record: also round-trip
        the hidden form fields the app got from onchange (defaults such as a
        `holiday_type`), because the next onchange() of a new record has no
        stored record to fall back on, exactly like the web client.
        """
        result = {f: current.get(f, False) for f in descs}
        if not record and values:
            model_fields = self.env[model_name]._fields
            result.update({k: v for k, v in values.items() if k not in result and k in model_fields})
        return result

    @api.model
    def _public(self, data):
        """Strip internal (`_`-prefixed) keys recursively before returning JSON."""
        if isinstance(data, dict):
            return {k: self._public(v) for k, v in data.items() if not (isinstance(k, str) and k.startswith('_'))}
        if isinstance(data, list):
            return [self._public(v) for v in data]
        return data

    # ------------------------------------------------------------------
    # Onchange / save / relational search
    # ------------------------------------------------------------------

    @api.model
    def _onchange(self, model_name, record=None, values=None, changed=None, field_names=None, view_id=None):
        """Run Odoo's real onchange, so computes and @api.onchange of custom modules apply.

        Returns `(values, warning)` in mobile format. With no record and no
        `changed` field this is the "new record" call that fills defaults.
        """
        parsed = self._parse_view(model_name, 'form', view_id)
        if field_names is None:
            field_names = list(parsed['fields'])
        if record:
            # Fields not sent are taken from the stored record: the minimal set is enough.
            names = self._needed_fields(model_name, parsed, field_names)
        else:
            model_fields = self.env[model_name]._fields
            names = {f for f in set(field_names) | self._referenced_fields(parsed) if f in model_fields}
        descs = self._field_descs(model_name, names)
        descs = {f: d for f, d in descs.items() if d['type'] != 'one2many'}

        current = self._read_values(record, descs, descs) if record else {}
        current.update({k: v for k, v in (values or {}).items() if k in descs})
        # x2many without sub-fields, like the web client does for fields it does not render as lines:
        # with sub-fields, Odoo expects commands even for defaults coming from the context (a list
        # of ids, e.g. `default_sheet_ids` of wizards) and crashes. Names are read in _from_onchange.
        fields_spec = {
            fname: ({'fields': {'display_name': {}}} if desc['type'] == 'many2one' else {})
            for fname, desc in descs.items()
        }
        target = record if record else self.env[model_name]
        if changed:
            result = target.onchange(self._to_orm(descs, current), [changed], fields_spec)
        else:
            result = target.onchange(self._to_orm(descs, values or {}), [], fields_spec)

        new_values = dict(current)
        for fname, value in (result.get('value') or {}).items():
            if fname in descs:
                new_values[fname] = self._from_onchange(descs[fname], value, current.get(fname))
        warning = result.get('warning') or False
        if warning:
            warning = {'title': warning.get('title') or '', 'message': warning.get('message') or ''}
        return new_values, warning

    @api.model
    def _save(self, model_name, record=None, values=None, field_names=None, view_id=None):
        """Create/write only fields that are part of the mobile form and editable right now."""
        values = values or {}
        spec = self._build_spec(model_name, record=record, values=values, field_names=field_names, view_id=view_id)
        missing = []
        for fname in spec['order']:
            meta = spec['fields'][fname]
            if meta['required'] and not meta['invisible'] and spec['values'].get(fname) in (False, None, '', []):
                missing.append(meta['string'])
        if missing:
            raise UserError(_('Please fill in: %s', ', '.join(missing)))

        writable = {
            fname: values[fname] for fname in spec['order']
            if fname in values and not spec['fields'][fname]['readonly']
        }
        orm_values = self._to_orm(spec['_descs'], writable)
        if record:
            if orm_values:
                record.write(orm_values)
            return record
        return self.env[model_name].create(orm_values)

    @api.model
    def _field_domain_context(self, model_name, fname, record=None, values=None, view_id=None):
        parsed = self._parse_view(model_name, 'form', view_id)
        desc = self.env[model_name].fields_get([fname], attributes=FIELD_ATTRIBUTES).get(fname)
        if not desc or desc.get('type') not in ('many2one', 'many2many'):
            raise UserError(_('Field %s is not a relational field.', fname))
        nodes = parsed['fields'].get(fname, [])
        node = next((n for n in nodes if n.get('domain') or n.get('context')), nodes[0] if nodes else {})
        domain_expr = node.get('domain') or desc.get('domain') or []
        context_expr = node.get('context') or desc.get('context') or {}

        names = self._expr_names(domain_expr) | self._expr_names(context_expr)
        model_fields = self.env[model_name]._fields
        descs = self._field_descs(model_name, [f for f in names if f in model_fields])
        current = self._read_values(record, descs, descs) if record else {}
        current.update({k: v for k, v in (values or {}).items() if k in descs})
        ctx = self._eval_context(descs, {f: current.get(f, False) for f in descs}, record.id if record else False)

        domain = self._eval(domain_expr, ctx, default=[]) if isinstance(domain_expr, str) else domain_expr
        context = self._eval(context_expr, ctx, default={}) if isinstance(context_expr, str) else context_expr
        return desc, list(domain or []), dict(context or {})

    @api.model
    def _name_search(self, model_name, fname, query='', record=None, values=None, limit=20, view_id=None):
        desc, domain, context = self._field_domain_context(model_name, fname, record, values, view_id)
        comodel = self.env[desc['relation']].with_context(**context)
        return [[rec_id, name] for rec_id, name in comodel.name_search(query or '', domain, limit=limit)]

    # ------------------------------------------------------------------
    # Buttons & actions
    # ------------------------------------------------------------------

    @api.model
    def _find_button(self, buttons, key):
        button = next((b for b in buttons if b['key'] == key), None)
        if not button or button['type'] == 'special':  # 'mobile' buttons are run by the record service
            raise UserError(_('This action is not available for this record any more. Please refresh.'))
        return button

    @api.model
    def _execute_button(self, record, button):
        """Run a button the spec currently exposes (never an arbitrary method name)."""
        context = dict(record.env.context, **(button.get('_context') or {}))
        if button['type'] == 'object':
            method = getattr(record.with_context(context), button['name'], None)
            if not callable(method):
                raise UserError(_('Unknown action %s.', button['name']))
            result = method()
        else:
            context.update(active_id=record.id, active_ids=record.ids, active_model=record._name)
            action_id = int(button['name'])
            action_type = self.env['ir.actions.actions'].sudo().browse(action_id).exists().type
            if action_type == 'ir.actions.server':
                # Same as the web client's /web/action/run: executed as the user (groups are checked).
                result = self.env['ir.actions.server'].browse(action_id).with_context(context).run()
                return self._normalize_action(result, record)
            result = self._load_action(action_id)
            if isinstance(result, dict):
                action_ctx = result.get('context') or {}
                if isinstance(action_ctx, str):
                    action_ctx = self._eval(action_ctx, self._action_eval_context(record, context), default={}) or {}
                result['context'] = dict(context, **action_ctx)
        return self._normalize_action(result, record)

    @api.model
    def _load_action(self, action_id):
        action = self.env['ir.actions.actions'].sudo().browse(action_id).exists()
        if not action:
            raise UserError(_('Action not found.'))
        real = self.env[action.type].sudo().browse(action_id)
        if hasattr(real, '_get_action_dict'):
            return real._get_action_dict()
        return real.read()[0]

    @api.model
    def _action_eval_context(self, record, context):
        return {
            'active_id': record.id, 'active_ids': record.ids, 'active_model': record._name,
            'uid': self.env.uid, 'context': context,
            'allowed_company_ids': self.env.companies.ids, 'current_company_id': self.env.company.id,
        }

    @api.model
    def _jsonable(self, data):
        try:
            return json.loads(json.dumps(data, default=str))
        except (TypeError, ValueError):
            return {}

    @api.model
    def _normalize_action(self, result, record):
        """Map whatever a button returned to one of: done | wizard | notification | open."""
        if not isinstance(result, dict):
            return {'type': 'done'}
        atype = result.get('type')
        if atype == 'ir.actions.act_window':
            context = result.get('context') or {}
            if isinstance(context, str):
                context = self._eval(context, self._action_eval_context(record, dict(record.env.context)), default={}) or {}
            context = dict(context)
            context.setdefault('active_id', record.id)
            context.setdefault('active_ids', record.ids)
            context.setdefault('active_model', record._name)
            view_id = result.get('view_id')
            if isinstance(view_id, (list, tuple)):
                view_id = view_id[0] if view_id else False
            if not view_id:
                view_id = next((v[0] for v in result.get('views') or [] if v and v[1] == 'form' and v[0]), False)
            if result.get('target') == 'new':
                return {
                    'type': 'wizard',
                    'model': result.get('res_model'),
                    'res_id': result.get('res_id') or False,
                    'view_id': view_id or False,
                    'title': result.get('name') or '',
                    'context': self._jsonable(context),
                }
            return {'type': 'open', 'model': result.get('res_model'), 'res_id': result.get('res_id') or False}
        if atype == 'ir.actions.client' and result.get('tag') == 'display_notification':
            params = result.get('params') or {}
            return {
                'type': 'notification',
                'title': params.get('title') or '',
                'message': params.get('message') or '',
                'level': params.get('type') or 'info',
            }
        return {'type': 'done'}

    # ------------------------------------------------------------------
    # Wizards (any transient model opened by a button)
    # ------------------------------------------------------------------

    @api.model
    def _wizard_spec(self, model_name, context=None, res_id=False, view_id=False, values=None, changed=None):
        Model = self.env[model_name].with_context(**(context or {}))
        if not Model._transient:
            raise UserError(_('%s is not a wizard.', model_name))
        record = Model.browse(res_id).exists() if res_id else None
        spec_self = self.with_context(**(context or {}))
        if record:
            if changed:
                values, warning = spec_self._onchange(model_name, record, values, changed, view_id=view_id)
            else:
                warning = False
        else:
            values, warning = spec_self._onchange(model_name, None, values, changed, view_id=view_id)
        spec = spec_self._build_spec(model_name, record=record, values=values, view_id=view_id, buttons_from='footer')
        spec['warning'] = warning
        return spec

    @api.model
    def _wizard_execute(self, model_name, key, context=None, res_id=False, view_id=False, values=None):
        context = context or {}
        spec_self = self.with_context(**context)
        Model = self.env[model_name].with_context(**context)
        if not Model._transient:
            raise UserError(_('%s is not a wizard.', model_name))
        record = Model.browse(res_id).exists() if res_id else None
        record = spec_self._save(model_name, record=record, values=values or {}, view_id=view_id)
        spec = spec_self._build_spec(model_name, record=record, view_id=view_id, buttons_from='footer')
        button = self._find_button(spec['buttons'], key)
        return spec_self._execute_button(record.with_context(**context), button)
