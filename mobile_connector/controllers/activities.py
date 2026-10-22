from odoo import http
from odoo.http import request

from .utils import MobileApiError, check_version, get_record, mobile_route, require_access


class MobileActivityController(http.Controller):

    @mobile_route('/mobile/api/v1/activities/list')
    def activities_list(self, payload):
        require_access('activities')
        return request.env['mobile.activity.service']._mobile_list(
            payload.get('filter') or 'all',
            offset=max(int(payload.get('offset') or 0), 0),
            limit=min(int(payload.get('limit') or 40), 200),
            with_actions=bool(payload.get('with_actions')),
        )

    @mobile_route('/mobile/api/v1/activities/done')
    def activities_done(self, payload):
        require_access('activities')
        activity = get_record('mail.activity', payload.get('id'))
        if not activity:
            raise MobileApiError('bad_request', 'id is required')
        return request.env['mobile.activity.service']._mobile_done(activity, payload.get('feedback'))

    # ------------------------------------------------------------------
    # Generic record viewer: any model, user's own access rights apply
    # ------------------------------------------------------------------

    def _record(self, payload):
        model = payload.get('model')
        if not model or model not in request.env:
            raise MobileApiError('bad_request', 'Unknown model')
        record = get_record(model, payload.get('id'))
        if not record:
            raise MobileApiError('bad_request', 'id is required')
        return record

    @mobile_route('/mobile/api/v1/form/spec')
    def form_spec(self, payload):
        """Summary (default) or the whole editable form (`full: true`)."""
        record = self._record(payload)
        viewer = request.env['mobile.record.viewer']
        spec = viewer._mobile_full_spec(record) if payload.get('full') else viewer._mobile_spec(record)
        return request.env['mobile.form.spec']._public(spec)

    @mobile_route('/mobile/api/v1/form/action')
    def form_action(self, payload):
        record = self._record(payload)
        if not payload.get('key'):
            raise MobileApiError('bad_request', 'key is required')
        viewer = request.env['mobile.record.viewer']
        result = viewer._mobile_action(record, payload['key'])
        record.invalidate_recordset()
        if result.get('type') != 'wizard' and record.exists():
            spec = viewer._mobile_full_spec(record) if payload.get('full') else viewer._mobile_spec(record)
            result['record'] = request.env['mobile.form.spec']._public(spec)
        return result

    # ------------------------------------------------------------------
    # Editable generic form (full mode): any record, every field of its form
    # ------------------------------------------------------------------

    def _public_full(self, spec):
        return request.env['mobile.form.spec']._public(spec)

    @mobile_route('/mobile/api/v1/form/onchange')
    def form_onchange(self, payload):
        record = self._record(payload)
        if not payload.get('changed'):
            raise MobileApiError('bad_request', 'changed is required')
        viewer = request.env['mobile.record.viewer']
        return self._public_full(viewer._mobile_full_onchange(record, payload.get('values') or {}, payload['changed']))

    @mobile_route('/mobile/api/v1/form/note', idempotent=True)
    def form_note(self, payload):
        """Log note on any record that has a chatter, and optionally the next step (an activity for the user)."""
        record = self._record(payload)
        activity = payload.get('activity')
        if activity is not None and not isinstance(activity, dict):
            raise MobileApiError('bad_request', 'activity must be an object')
        return request.env['mobile.activity.service']._mobile_note(record, payload.get('body'), activity)

    @mobile_route('/mobile/api/v1/form/save', idempotent=True)
    def form_save(self, payload):
        record = self._record(payload)
        check_version(record, payload.get('version'))
        viewer = request.env['mobile.record.viewer']
        return self._public_full(viewer._mobile_full_save(record, payload.get('values') or {}))

    @mobile_route('/mobile/api/v1/form/stage')
    def form_stage(self, payload):
        """Click on the statusbar (stage / state) when the form allows it."""
        record = self._record(payload)
        viewer = request.env['mobile.record.viewer']
        return self._public_full(viewer._mobile_set_stage(record, payload.get('value')))

    def _line_payload(self, payload):
        if not payload.get('field'):
            raise MobileApiError('bad_request', 'field is required')
        return self._record(payload), payload['field']

    @mobile_route('/mobile/api/v1/form/line/new')
    def form_line_new(self, payload):
        record, field = self._line_payload(payload)
        return request.env['mobile.record.viewer']._mobile_line_new(record, field)

    @mobile_route('/mobile/api/v1/form/line/name_search')
    def form_line_name_search(self, payload):
        record, field = self._line_payload(payload)
        return request.env['mobile.record.viewer']._mobile_line_name_search(
            record, field, payload.get('column'), payload.get('query') or '', payload.get('values'),
            payload.get('line_id'), limit=min(int(payload.get('limit') or 20), 80))

    @mobile_route('/mobile/api/v1/form/line/save')
    def form_line_save(self, payload):
        record, field = self._line_payload(payload)
        viewer = request.env['mobile.record.viewer']
        return self._public_full(viewer._mobile_line_save(record, field, payload.get('line_id'), payload.get('values') or {}))

    @mobile_route('/mobile/api/v1/form/line/delete')
    def form_line_delete(self, payload):
        record, field = self._line_payload(payload)
        if not payload.get('line_id'):
            raise MobileApiError('bad_request', 'line_id is required')
        viewer = request.env['mobile.record.viewer']
        return self._public_full(viewer._mobile_line_delete(record, field, payload['line_id']))

    @mobile_route('/mobile/api/v1/form/line/action')
    def form_line_action(self, payload):
        record, field = self._line_payload(payload)
        if not payload.get('line_id') or not payload.get('key'):
            raise MobileApiError('bad_request', 'line_id and key are required')
        return request.env['mobile.record.viewer']._mobile_line_action(record, field, payload['line_id'], payload['key'])
