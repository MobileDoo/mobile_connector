"""Base service for a native mobile feature built on one model (time off, expenses...).

A feature module declares a service:

    class MobileExpenseService(models.AbstractModel):
        _name = 'mobile.expense.service'
        _inherit = 'mobile.record.service'
        _mobile_model = 'hr.expense'
        _mobile_feature = 'expense'                     # key in mobile.api._mobile_access()
        _mobile_create_capability = 'submit'            # None = records cannot be created on mobile
        _mobile_scope_capabilities = {'mine': 'submit'} # list scopes and the capability each needs
        _mobile_core_field_names = [...]                # laid out natively by the app
        _mobile_list_field_names = [...]                # shown on list cards
        _mobile_line_field = 'order_line'               # optional: one2many shown as a table
        _mobile_line_field_names = [...]                #   and its columns

registers it in `mobile.api._mobile_record_services()`, and gets list/spec/
onchange/save/action/name_search/attachments through the generic routes
`/mobile/api/v1/records/<key>/...` with change detection and access checks.

Customer modules `_inherit` the feature service and override the `_mobile_*`
hooks (extra fields, buttons, domains, defaults, push texts) — never the
controllers. Fields, modifiers, onchanges, constraints and header buttons of
customer modules are picked up from the model's form view automatically.
"""
from odoo import _, api, models
from odoo.fields import Domain
from odoo.exceptions import UserError

ATTACHMENT_FIELDS = ('id', 'name', 'mimetype', 'file_size')
MAX_LINES = 200


class MobileRecordService(models.AbstractModel):
    _name = 'mobile.record.service'
    _description = 'Mobile Record Service (base)'

    _mobile_model = None
    _mobile_feature = None
    _mobile_create_capability = None
    _mobile_scope_capabilities = {}
    _mobile_core_field_names = []
    _mobile_list_field_names = []
    _mobile_line_field = None
    _mobile_line_field_names = []

    # ------------------------------------------------------------------
    # Hooks
    # ------------------------------------------------------------------

    @api.model
    def _mobile_core_fields(self):
        model_fields = self.env[self._mobile_model]._fields
        return [f for f in self._mobile_core_field_names if f in model_fields]

    @api.model
    def _mobile_extra_fields(self):
        """Extra form fields as dicts: {'name', 'sequence', 'string'?, 'widget'?}.

        Defaults to what admins configured in Settings > Mobile App > Extra Fields.
        """
        return self.env['mobile.field.config']._get_extra_fields(self._mobile_model, 'form')

    @api.model
    def _mobile_list_extra_fields(self):
        """Extra fields shown on list cards, same format as `_mobile_extra_fields`."""
        return self.env['mobile.field.config']._get_extra_fields(self._mobile_model, 'list')

    @api.model
    def _mobile_scope_domain(self, scope, params):
        """The scope's domain, plus records waiting for the user's review in approval modules.

        OCA tier validation (`base_tier_validation`, used by purchase/sale/expense... approval
        modules) keeps the record in its usual state while reviewers decide, and exposes a
        searchable `can_review`: those records belong to "to approve" too.
        """
        domain = self._mobile_domain(scope, params)
        if scope == 'to_approve' and 'can_review' in self.env[self._mobile_model]._fields:
            domain = Domain.OR([domain, [('can_review', '=', True)]])
        return domain

    @api.model
    def _mobile_domain(self, scope, params):
        """Domain of a list scope (must be one of `_mobile_scope_capabilities`)."""
        raise UserError(_('Unknown scope %s', scope))

    @api.model
    def _mobile_search_domain(self, query):
        """Domain of the list search box (`query` of the list call)."""
        return self.env['mobile.api']._mobile_text_domain(self._mobile_model, query)

    @api.model
    def _mobile_order(self, scope):
        return 'id desc'

    @api.model
    def _mobile_new_context(self):
        """Context used to create records (defaults such as `default_employee_id`)."""
        return {}

    @api.model
    def _mobile_buttons(self, record, buttons):
        """Filter/relabel/add buttons. Buttons not returned here cannot be executed.

        Add mobile-only buttons with `'type': 'mobile'` and handle them in
        `_mobile_run_button`.
        """
        return buttons

    @api.model
    def _mobile_run_button(self, record, name):
        """Execute a `type: 'mobile'` button; return what an Odoo button would."""
        raise UserError(_('Unknown action %s.', name))

    # ------------------------------------------------------------------
    # Lists
    # ------------------------------------------------------------------

    @api.model
    def _state_selection(self):
        field = self.env[self._mobile_model]._fields.get('state')
        if not field or field.type != 'selection':
            return []
        return [[key, label] for key, label in field._description_selection(self.env)]

    @api.model
    def _serialize(self, record, extra=None):
        engine = self.env['mobile.form.spec']
        names = [f for f in self._mobile_list_field_names if f in record._fields]
        extra = extra if extra is not None else self._mobile_list_extra_fields()
        extra_names = [e['name'] for e in extra if e['name'] in record._fields]
        descs = engine._field_descs(self._mobile_model, names + extra_names)
        values = engine._read_values(record, descs, names + extra_names)
        values['id'] = record.id
        values['display_name'] = record.display_name
        values['extra'] = [{
            'name': e['name'],
            'string': e.get('string') or descs.get(e['name'], {}).get('string') or e['name'],
            'type': descs.get(e['name'], {}).get('type'),
            'value': values.pop(e['name'], False),
        } for e in sorted(extra, key=lambda e: e.get('sequence', 100)) if e['name'] in descs]
        return values

    @api.model
    def _mobile_list(self, scope, offset=0, limit=40, params=None):
        params = params or {}
        scope_domain = self._mobile_scope_domain(scope, params)
        domain = scope_domain
        query = params.get('query')
        if isinstance(query, str) and query.strip():
            domain = domain + self._mobile_search_domain(query.strip())
        Model = self.env[self._mobile_model]
        records = Model.search(domain, offset=offset, limit=limit, order=self._mobile_order(scope))
        extra = self._mobile_list_extra_fields()
        return {
            'records': [self._serialize(record, extra) for record in records],
            'total': Model.search_count(domain),
            'state_selection': self._state_selection(),
            # Version of the scope, not of the search: what `records.<service>.<scope>` watches.
            'version': self.env['mobile.api']._mobile_domain_version(self._mobile_model, scope_domain),
        }

    @api.model
    def _mobile_list_version(self, scope):
        return self.env['mobile.api']._mobile_domain_version(self._mobile_model, self._mobile_scope_domain(scope, {}))

    # ------------------------------------------------------------------
    # Form
    # ------------------------------------------------------------------

    @api.model
    def _extra_fields_sorted(self):
        model_fields = self.env[self._mobile_model]._fields
        core = set(self._mobile_core_fields())
        seen, result = set(), []
        for extra in sorted(self._mobile_extra_fields(), key=lambda e: e.get('sequence', 100)):
            name = extra.get('name')
            if name in model_fields and name not in core and name not in seen:
                seen.add(name)
                result.append(extra)
        return result

    @api.model
    def _field_names(self):
        return self._mobile_core_fields() + [e['name'] for e in self._extra_fields_sorted()]

    @api.model
    def _engine(self, record=None):
        engine = self.env['mobile.form.spec']
        return engine if record else engine.with_context(**self._mobile_new_context())

    @api.model
    def _mobile_spec(self, record=None, values=None):
        spec = self._engine(record)._build_spec(
            self._mobile_model, record=record, values=values, field_names=self._field_names())
        extras = self._extra_fields_sorted()
        for extra in extras:
            meta = spec['fields'].get(extra['name'])
            if not meta:
                continue
            meta['extra'] = True
            meta['sequence'] = extra.get('sequence', 100)
            if extra.get('widget'):
                meta['widget'] = extra['widget']
            if extra.get('string'):
                meta['string'] = extra['string']
        spec['core'] = [f for f in self._mobile_core_fields() if f in spec['fields']]
        spec['extra'] = [e['name'] for e in extras if e['name'] in spec['fields']]
        spec['buttons'] = self._mobile_buttons(record, spec['buttons']) if record else []
        spec['state_selection'] = self._state_selection()
        spec['attachments'] = self._attachments(record) if record else []
        spec['lines'] = self._mobile_lines(record) if record else False
        return spec

    @api.model
    def _mobile_lines(self, record):
        """The `_mobile_line_field` one2many as a read-only table (order lines, expense lines...).

        `rows[].values` use the mobile format; rows with a `display_type`
        (sections, notes) carry only their label.
        """
        fname = self._mobile_line_field
        if not fname or fname not in record._fields:
            return False
        engine = self.env['mobile.form.spec']
        lines = record[fname]
        comodel = lines._name
        names = [f for f in self._mobile_line_field_names if f in self.env[comodel]._fields]
        descs = engine._field_descs(comodel, names)
        columns = [{'name': f, 'string': descs[f]['string'], 'type': descs[f]['type']} for f in names if f in descs]
        if 'display_type' in names:
            columns = [c for c in columns if c['name'] != 'display_type']
        rows = []
        for line in lines[:MAX_LINES]:
            values = engine._read_values(line, descs, descs)
            rows.append({'id': line.id, 'display_type': values.pop('display_type', False) or False, 'values': values})
        return {
            'field': fname,
            'string': record._fields[fname].get_description(self.env)['string'],
            'columns': columns,
            'rows': rows,
            'total': len(lines),
        }

    @api.model
    def _mobile_new(self, values=None):
        new_values, warning = self._engine()._onchange(self._mobile_model, None, values or {}, None, self._field_names())
        spec = self.with_context(**self._mobile_new_context())._mobile_spec(None, new_values)
        spec['warning'] = warning
        return spec

    @api.model
    def _mobile_onchange(self, record, values, changed):
        new_values, warning = self._engine(record)._onchange(
            self._mobile_model, record, values, changed, self._field_names())
        spec = self._mobile_spec(record, new_values)
        spec['warning'] = warning
        return spec

    @api.model
    def _mobile_save(self, record, values):
        return self._engine(record)._save(self._mobile_model, record=record, values=values, field_names=self._field_names())

    @api.model
    def _mobile_action(self, record, key):
        engine = self.env['mobile.form.spec']
        button = engine._find_button(self._mobile_spec(record)['buttons'], key)
        if button['type'] == 'mobile':
            return engine._normalize_action(self._mobile_run_button(record, button['name']), record)
        return engine._execute_button(record, button)

    @api.model
    def _mobile_name_search(self, record, field, query, values, limit=20):
        if field not in self._field_names():
            raise UserError(_('Field %s is not available on mobile.', field))
        return self._engine(record)._name_search(
            self._mobile_model, field, query, record=record, values=values, limit=limit)

    # ------------------------------------------------------------------
    # Attachments
    # ------------------------------------------------------------------

    @api.model
    def _attachments(self, record):
        attachments = self.env['ir.attachment'].search(
            [('res_model', '=', record._name), ('res_id', '=', record.id)], order='id')
        return attachments.read(list(ATTACHMENT_FIELDS))

    @api.model
    def _mobile_add_attachment(self, record, name, mimetype, datas):
        record.check_access('write')
        attachment = self.env['ir.attachment'].create({
            'name': name or 'attachment',
            'mimetype': mimetype or 'application/octet-stream',
            'datas': datas,
            'res_model': record._name,
            'res_id': record.id,
        })
        return attachment.read(list(ATTACHMENT_FIELDS))[0]
