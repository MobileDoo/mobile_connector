from odoo import http
from odoo.http import request

from .utils import MobileApiError, mobile_route, require_access

MAX_LIMIT = 200


class MobileMenuController(http.Controller):
    """The user's own Odoo menus: apps -> lists of records (see `mobile.menu.service`)."""

    @mobile_route('/mobile/api/v1/menus')
    def menus(self, payload):
        require_access('menus')
        return request.env['mobile.menu.service']._mobile_menus()

    @mobile_route('/mobile/api/v1/menus/list')
    def menu_list(self, payload):
        require_access('menus')
        menu_id = payload.get('menu_id')
        if not isinstance(menu_id, int):
            raise MobileApiError('bad_request', 'menu_id is required')
        filters = payload.get('filters')
        if filters is not None and not (isinstance(filters, list) and all(isinstance(f, str) for f in filters)):
            raise MobileApiError('bad_request', 'filters must be a list of filter names or null')
        return request.env['mobile.menu.service']._mobile_list(
            menu_id,
            offset=max(int(payload.get('offset') or 0), 0),
            limit=min(int(payload.get('limit') or 40), MAX_LIMIT),
            query=payload.get('query') if isinstance(payload.get('query'), str) else None,
            active_filters=filters,
            kpi_id=payload.get('kpi_id') if isinstance(payload.get('kpi_id'), int) else None,
        )

    @mobile_route('/mobile/api/v1/home')
    def home(self, payload):
        """Home screen of the user: Odoo configuration (Mobile App > Home Screen) + their own arrangement."""
        return request.env['mobile.api']._mobile_home()

    @mobile_route('/mobile/api/v1/home/catalog')
    def home_catalog(self, payload):
        """Every block the user may show/hide/reorder, in their order (Customize screen)."""
        return request.env['mobile.api']._mobile_home_editor()

    @mobile_route('/mobile/api/v1/home/announcements/dismiss')
    def home_dismiss(self, payload):
        """`{"id": <announcement>}`: read, hide it from the user's home screen; returns the new `/home`."""
        if not isinstance(payload.get('id'), int):
            raise MobileApiError('bad_request', 'id is required')
        return request.env['mobile.api']._mobile_home_dismiss(payload['id'])

    @mobile_route('/mobile/api/v1/home/preferences')
    def home_preferences(self, payload):
        """Save the user's arrangement (`{"sections": [{"id", "visible"}], "tiles": ..., "kpis": ..., "shortcuts": ...}`)
        or `{"reset": true}`; returns the new home screen."""
        for kind in ('sections', 'tiles', 'kpis', 'shortcuts'):
            if payload.get(kind) is not None and not isinstance(payload.get(kind), list):
                raise MobileApiError('bad_request', f'{kind} must be a list')
        return request.env['mobile.api']._mobile_home_save(payload)
