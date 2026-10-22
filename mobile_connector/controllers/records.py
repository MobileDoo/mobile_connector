from odoo import http
from odoo.http import request

from .utils import MobileApiError, check_version, get_record, mobile_route, require_access

MAX_LIMIT = 200


class MobileRecordController(http.Controller):
    """Generic routes for every `mobile.record.service` registered in `mobile.api._mobile_record_services`."""

    def _service(self, key):
        name = request.env['mobile.api']._mobile_record_services().get(key)
        if not name:
            raise MobileApiError('missing', f'Unknown record service {key}', 404)
        service = request.env[name]
        require_access(service._mobile_feature)
        return service

    def _record(self, service, payload):
        """Existing record (ACL/record rules apply) or, without id, check the right to create."""
        record = get_record(service._mobile_model, payload.get('id'))
        if not record:
            if not service._mobile_create_capability:
                raise MobileApiError('feature_denied', 'Records cannot be created from the mobile app here.', 403)
            require_access(service._mobile_feature, service._mobile_create_capability)
        return record

    def _public(self, data):
        return request.env['mobile.form.spec']._public(data)

    @mobile_route('/mobile/api/v1/records/<string:service_key>/list')
    def records_list(self, payload, service_key):
        service = self._service(service_key)
        scope = payload.get('scope')
        capability = service._mobile_scope_capabilities.get(scope)
        if not capability:
            raise MobileApiError('bad_request', f'scope must be one of {", ".join(service._mobile_scope_capabilities)}')
        require_access(service._mobile_feature, capability)
        return service._mobile_list(
            scope,
            offset=max(int(payload.get('offset') or 0), 0),
            limit=min(int(payload.get('limit') or 40), MAX_LIMIT),
            params=payload,
        )

    @mobile_route('/mobile/api/v1/records/<string:service_key>/spec')
    def records_spec(self, payload, service_key):
        """Spec of an existing record (`id`) or of a new one (defaults + optional `values`)."""
        service = self._service(service_key)
        record = self._record(service, payload)
        return self._public(service._mobile_spec(record) if record else service._mobile_new(payload.get('values')))

    @mobile_route('/mobile/api/v1/records/<string:service_key>/onchange')
    def records_onchange(self, payload, service_key):
        if not payload.get('changed'):
            raise MobileApiError('bad_request', 'changed is required')
        service = self._service(service_key)
        record = self._record(service, payload)
        return self._public(service._mobile_onchange(record, payload.get('values') or {}, payload['changed']))

    @mobile_route('/mobile/api/v1/records/<string:service_key>/save', idempotent=True)
    def records_save(self, payload, service_key):
        service = self._service(service_key)
        record = self._record(service, payload)
        check_version(record, payload.get('version'))
        record = service._mobile_save(record, payload.get('values') or {})
        return self._public(service._mobile_spec(record))

    @mobile_route('/mobile/api/v1/records/<string:service_key>/action')
    def records_action(self, payload, service_key):
        service = self._service(service_key)
        record = get_record(service._mobile_model, payload.get('id'))
        if not record or not payload.get('key'):
            raise MobileApiError('bad_request', 'id and key are required')
        result = service._mobile_action(record, payload['key'])
        record.invalidate_recordset()
        if result.get('type') != 'wizard' and record.exists():
            result['record'] = self._public(service._mobile_spec(record))
        return result

    @mobile_route('/mobile/api/v1/records/<string:service_key>/name_search')
    def records_name_search(self, payload, service_key):
        service = self._service(service_key)
        record = get_record(service._mobile_model, payload.get('id'))
        return service._mobile_name_search(
            record, payload.get('field'), payload.get('query') or '', payload.get('values'),
            limit=min(int(payload.get('limit') or 20), 80),
        )

    @mobile_route('/mobile/api/v1/records/<string:service_key>/attachment/add')
    def records_add_attachment(self, payload, service_key):
        service = self._service(service_key)
        record = get_record(service._mobile_model, payload.get('id'))
        if not record or not payload.get('datas'):
            raise MobileApiError('bad_request', 'id and datas (base64) are required')
        return service._mobile_add_attachment(record, payload.get('name'), payload.get('mimetype'), payload['datas'])
