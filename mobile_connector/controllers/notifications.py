from odoo import http
from odoo.http import request

from .utils import MobileApiError, mobile_route, require_access


class MobileNotificationController(http.Controller):

    @mobile_route('/mobile/api/v1/notifications/list')
    def notifications_list(self, payload):
        require_access('notifications')
        return request.env['mobile.notification.service']._mobile_list(
            payload.get('filter') or 'unread',
            offset=max(int(payload.get('offset') or 0), 0),
            limit=min(int(payload.get('limit') or 40), 200),
        )

    @mobile_route('/mobile/api/v1/notifications/mark_read')
    def notifications_mark_read(self, payload):
        require_access('notifications')
        ids = payload.get('ids')
        if not payload.get('all') and not (isinstance(ids, list) and ids):
            raise MobileApiError('bad_request', 'ids or all is required')
        return request.env['mobile.notification.service']._mobile_mark_read(ids, all_messages=bool(payload.get('all')))
