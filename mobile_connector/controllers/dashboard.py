from odoo import http
from odoo.http import request

from .utils import mobile_route, require_access


class MobileDashboardController(http.Controller):

    @mobile_route('/mobile/api/v1/dashboard')
    def dashboard(self, payload):
        """The overview cards (module-declared, computed with the user's rights)."""
        require_access('dashboard', 'view')
        return request.env['mobile.dashboard.service']._mobile_dashboard()
