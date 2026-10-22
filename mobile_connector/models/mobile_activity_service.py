""""My activities" inbox and a generic record viewer.

Approval flows in Odoo (time off, expenses, purchase, custom modules...)
schedule an activity for the approver. Listing the user's activities and
opening the related record with its real header buttons gives one approval
inbox that works for every module, including ones with no native mobile
screen.
"""
import re

from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.tools import html2plaintext, plaintext2html

NOTE_MAX = 5000
ACTIVITY_TYPES = {
    'call': 'mail.mail_activity_data_call', 'meeting': 'mail.mail_activity_data_meeting',
    'todo': 'mail.mail_activity_data_todo', 'email': 'mail.mail_activity_data_email',
}
FILTERS = ('overdue', 'today', 'upcoming', 'all')
# Buttons of the record that are the answer to an approval: pressed from the inbox without opening the record.
APPROVE_BUTTON = re.compile(r'approve|validate', re.I)
REFUSE_BUTTON = re.compile(r'refuse|reject', re.I)
SUMMARY_MAX_FIELDS = 12
SUMMARY_SKIP_TYPES = {'one2many', 'html', 'binary'}
SUMMARY_SKIP_PREFIXES = ('message_', 'activity_', 'website_', 'access_', 'x_studio_')
MESSAGES_LIMIT = 5


class MobileActivityService(models.AbstractModel):
    _name = 'mobile.activity.service'
    _description = 'Mobile Activities'

    @api.model
    def _base_domain(self):
        # Odoo 19 also has activities on no record (personal to-dos): the app opens records, so they stay on the web.
        return [('user_id', '=', self.env.uid), ('res_model', '!=', False), ('res_id', '!=', False)]

    @api.model
    def _filter_domain(self, name):
        today = fields.Date.context_today(self)
        return {
            'overdue': [('date_deadline', '<', today)],
            'today': [('date_deadline', '=', today)],
            'upcoming': [('date_deadline', '>', today)],
            'all': [],
        }[name]

    @api.model
    def _mobile_version(self):
        return self.env['mobile.api']._mobile_domain_version('mail.activity', self._base_domain())

    @api.model
    def _quick_actions(self, activity):
        """The approve / refuse buttons of the record, if it has them now (an approver answers from the list)."""
        try:
            record = self.env[activity.res_model].browse(activity.res_id).exists()
            if not record:
                return []
            buttons = self.env['mobile.record.viewer']._mobile_spec(record)['buttons']
        except Exception:  # a model the user cannot read, a form that does not build: the card just has no shortcut
            return []
        picked = []
        for kind, pattern in (('approve', APPROVE_BUTTON), ('refuse', REFUSE_BUTTON)):
            button = next((b for b in buttons if b['type'] == 'object' and pattern.search(b['name'])), None)
            if button:
                picked.append({'key': button['key'], 'label': button['label'], 'kind': kind,
                               'confirm': button.get('confirm') or False})
        return picked

    @api.model
    def _serialize(self, activity, model_names, quick=False):
        return {
            'id': activity.id,
            'res_model': activity.res_model,
            'model_name': model_names.get(activity.res_model, activity.res_model),
            'res_id': activity.res_id,
            'res_name': activity.res_name or '',
            'activity_type': [activity.activity_type_id.id, activity.activity_type_id.name]
            if activity.activity_type_id else False,
            'icon': activity.icon or False,
            'summary': activity.summary or '',
            'note': html2plaintext(activity.note or '').strip(),
            'date_deadline': fields.Date.to_string(activity.date_deadline),
            'state': activity.state,
            'actions': self._quick_actions(activity) if quick else [],
        }

    @api.model
    def _mobile_list(self, filter_name='all', offset=0, limit=40, with_actions=False):
        if filter_name not in FILTERS:
            raise UserError(_('Unknown filter %s', filter_name))
        Activity = self.env['mail.activity']
        base = self._base_domain()
        activities = Activity.search(base + self._filter_domain(filter_name), offset=offset, limit=limit,
                                     order='date_deadline asc, id asc')
        IrModel = self.env['ir.model'].sudo()
        model_names = {model: IrModel._get(model).name for model in set(activities.mapped('res_model'))}
        return {
            # `with_actions`: the buttons of each record cost a form view each, so only a screen's worth is asked for.
            'records': [self._serialize(activity, model_names, quick=with_actions) for activity in activities],
            'counts': {name: Activity.search_count(base + self._filter_domain(name)) for name in FILTERS},
            'version': self._mobile_version(),
        }

    @api.model
    def _mobile_done(self, activity, feedback=None):
        """Mark done like the web client: posts the feedback on the record, removes the activity."""
        if activity.user_id != self.env.user:
            raise UserError(_('This activity is assigned to someone else.'))
        activity.action_feedback(feedback=feedback or False)
        return True

    @api.model
    def _mobile_note(self, record, body=None, activity=None):
        """A log note on the record (what a salesperson writes after a meeting), and optionally the next step.

        `activity`: `{type: call|meeting|todo|email, date: YYYY-MM-DD, summary?}` scheduled for the user, like the
        "Schedule activity" button of the chatter. Needs the same rights as the web (write on the record).
        """
        body = (body or '').strip()
        if not body and not activity:
            raise UserError(_('Write a note or schedule the next step.'))
        if len(body) > NOTE_MAX:
            raise UserError(_('The note is too long (%(max)s characters at most).', max=NOTE_MAX))
        if not hasattr(record, 'message_post'):
            raise UserError(_('This record has no notes.'))
        result = {'message_id': False, 'activity_id': False}
        if body:
            message = record.message_post(
                body=plaintext2html(body), message_type='comment', subtype_xmlid='mail.mt_note')
            result['message_id'] = message.id
        if activity:
            xmlid = ACTIVITY_TYPES.get(activity.get('type'))
            if not xmlid or not hasattr(record, 'activity_schedule'):
                raise UserError(_('This kind of next step is not available here.'))
            try:
                deadline = fields.Date.to_date(activity.get('date'))
            except (TypeError, ValueError):
                deadline = None
            if not deadline:
                raise UserError(_('Choose the date of the next step.'))
            scheduled = record.activity_schedule(
                act_type_xmlid=xmlid, date_deadline=deadline, summary=(activity.get('summary') or '')[:200] or False,
                user_id=self.env.uid)
            result['activity_id'] = scheduled.id
        return result


class MobileRecordViewer(models.AbstractModel):
    """Read-only summary + header buttons of any record (target of activities/pushes)."""

    _name = 'mobile.record.viewer'
    _description = 'Mobile Generic Record Viewer'

    @api.model
    def _summary_fields(self, model_name):
        """Fields shown for a model without a native screen.

        Admin-configured fields (Settings > Mobile App > Extra Fields) win;
        otherwise the first visible simple fields of the form view, in order.
        """
        configured = self.env['mobile.field.config']._get_extra_fields(model_name, 'form')
        if configured:
            return [c['name'] for c in sorted(configured, key=lambda c: c['sequence'])]
        engine = self.env['mobile.form.spec']
        parsed = engine._parse_view(model_name)
        model_fields = self.env[model_name]._fields
        names = []
        if 'state' in model_fields:
            names.append('state')
        for name, nodes in parsed['fields'].items():
            field = model_fields.get(name)
            if not field or name in names or field.type in SUMMARY_SKIP_TYPES or name.startswith(SUMMARY_SKIP_PREFIXES):
                continue
            if all((n.get('invisible') or '').strip() in ('1', 'True') for n in nodes):
                continue
            names.append(name)
            if len(names) >= SUMMARY_MAX_FIELDS:
                break
        return names

    @api.model
    def _messages(self, record):
        if 'message_ids' not in record._fields:
            return []
        messages = self.env['mail.message'].search([
            ('model', '=', record._name), ('res_id', '=', record.id),
            ('message_type', 'in', ('comment', 'email')),
        ], limit=MESSAGES_LIMIT, order='id desc')
        return [{
            'id': m.id,
            'author': m.author_id.display_name or '',
            'date': fields.Datetime.to_string(m.date),
            'body': html2plaintext(m.body or '').strip(),
        } for m in messages]

    @api.model
    def _mobile_spec(self, record):
        engine = self.env['mobile.form.spec']
        spec = engine._build_spec(record._name, record=record, field_names=self._summary_fields(record._name))
        IrModel = self.env['ir.model'].sudo()
        spec['model_name'] = IrModel._get(record._name).name
        state = record._fields.get('state')
        spec['state_selection'] = [list(s) for s in state._description_selection(self.env)] \
            if state and state.type == 'selection' else []
        spec['attachments'] = self.env['mobile.record.service']._attachments(record)
        spec['messages'] = self._messages(record)
        return spec

    @api.model
    def _mobile_action(self, record, key):
        engine = self.env['mobile.form.spec']
        button = engine._find_button(self._mobile_spec(record)['buttons'], key)
        return engine._execute_button(record, button)
