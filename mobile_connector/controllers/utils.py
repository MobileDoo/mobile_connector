"""Shared plumbing for `/mobile/api/v1/*` routes.

Every route is `type='http'` with a JSON body and a JSON envelope:
    success  {"ok": true, "data": ...}
    failure  {"ok": false, "error": {"code": "...", "message": "..."}}
Plain HTTP routes keep the wire format identical on every Odoo version
(the `type='json'` JSON-RPC envelope is version-specific).
"""
import functools
import json
import logging

import psycopg2

from odoo import http
from odoo.exceptions import AccessDenied, AccessError, MissingError, UserError
from odoo.http import request

_logger = logging.getLogger(__name__)


class MobileApiError(Exception):
    def __init__(self, code, message, status=400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def _error_response(code, message, status):
    return request.make_json_response({'ok': False, 'error': {'code': code, 'message': message}}, status=status)


def _rollback():
    if request.env and request.env.cr:
        request.env.cr.rollback()


def _message(exc):
    return exc.args[0] if exc.args else str(exc)


def _once(route, payload, run):
    """Run `run()` once per `client_id` (user + id): a repeated request gets the first answer back, nothing is written twice."""
    client_id = payload.get('client_id')
    if not isinstance(client_id, str) or not client_id or len(client_id) > 64:
        return run()
    log = request.env['mobile.request.log'].sudo()
    done = log.search([('user_id', '=', request.env.uid), ('client_id', '=', client_id)], limit=1)
    if done:
        return json.loads(done.result or 'null')
    data = run()
    log.create({'user_id': request.env.uid, 'client_id': client_id, 'route': route, 'result': json.dumps(data)})
    return data


def mobile_route(route, auth='mobile', methods=('POST',), upload=False, idempotent=False):
    """Declare a mobile endpoint: `handler(self, payload) -> data`.

    Business errors are rolled back and returned as JSON (never an HTML page),
    while concurrency errors are re-raised so Odoo's retry mechanism still works.

    `upload=True`: the body is multipart/form-data (files); the handler gets the text fields as a dict and reads the
    files from `request.httprequest.files`.

    `idempotent=True`: a request with a `client_id` (the phone queues it while offline and may send it twice) runs once;
    the repeat gets the first answer back (see `_once`).
    """
    def decorator(func):
        @http.route(route, type='http', auth=auth, methods=list(methods), csrf=False, save_session=False, cors=False)
        @functools.wraps(func)
        def wrapper(self, **kwargs):
            # Odoo passes query-string AND path parameters as kwargs: keep only the path ones.
            path_args = {k: v for k, v in kwargs.items() if k not in request.httprequest.args}
            try:
                if upload:
                    payload = request.httprequest.form.to_dict()
                else:
                    raw = request.httprequest.get_data(as_text=True)
                    payload = json.loads(raw) if raw and raw.strip() else {}
                if not isinstance(payload, dict):
                    raise MobileApiError('bad_request', 'JSON body must be an object')
                if idempotent:
                    data = _once(route, payload, lambda: func(self, payload, **path_args))
                else:
                    data = func(self, payload, **path_args)
                return request.make_json_response({'ok': True, 'data': data})
            except psycopg2.Error:
                raise
            except MobileApiError as exc:
                _rollback()
                return _error_response(exc.code, exc.message, exc.status)
            except json.JSONDecodeError:
                return _error_response('bad_request', 'Invalid JSON body', 400)
            except AccessDenied as exc:
                _rollback()
                return _error_response('access_denied', _message(exc) or 'Access denied', 401)
            except MissingError as exc:
                _rollback()
                return _error_response('missing', _message(exc), 404)
            except AccessError as exc:
                _rollback()
                return _error_response('access_error', _message(exc), 403)
            except UserError as exc:  # includes ValidationError
                _rollback()
                return _error_response('user_error', _message(exc), 400)
            except Exception:
                _logger.exception('Mobile API error on %s', route)
                _rollback()
                return _error_response('server_error', 'Internal server error', 500)
        return wrapper
    return decorator


def require_access(feature, capability=None):
    """Refuse the call unless the user has `feature` (and `capability`) in `mobile.api._mobile_access`."""
    access = request.env['mobile.api']._mobile_access().get(feature)
    if not access or (capability and not access.get(capability)):
        raise MobileApiError('feature_denied', 'You do not have access to this feature.', 403)


def check_version(record, expected):
    """Optimistic concurrency: refuse to save over a change the user has not seen.

    `expected` is the `version` the app got with the record; no version = no check.
    """
    if not record or not expected:
        return
    current = request.env['mobile.api']._mobile_record_versions(record._name, record.ids).get(record.id)
    if current != expected:
        raise MobileApiError(
            'conflict',
            'This record was changed by someone else meanwhile. Reload it to see the latest version.',
            409,
        )


def get_record(model_name, record_id):
    """Browse a record the current user can read, or raise a clean 404/403."""
    if not record_id:
        return None
    try:
        record_id = int(record_id)
    except (TypeError, ValueError):
        raise MobileApiError('bad_request', 'Invalid id')
    record = request.env[model_name].browse(record_id).exists()
    if not record:
        raise MobileApiError('missing', 'Record not found', 404)
    record.check_access('read')
    return record
