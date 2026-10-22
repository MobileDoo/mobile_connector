from datetime import timedelta

from odoo import api, fields, models


class MobileRequestLog(models.Model):
    """Answers kept for requests the phone may send twice (queued while offline, sent again after a timeout).

    A route declared `idempotent=True` runs a request with a `client_id` once per user: the second time it gets the
    stored answer back and writes nothing. The answer is stored in the transaction of the request, so both succeed or
    neither does.
    """

    _name = 'mobile.request.log'
    _description = 'Mobile Request Answer (replay guard)'
    _order = 'id desc'

    user_id = fields.Many2one('res.users', required=True, ondelete='cascade', index=True)
    client_id = fields.Char(required=True, index=True)
    route = fields.Char()
    result = fields.Text()

    _client_unique = models.Constraint('unique(user_id, client_id)', 'Request already answered.')

    @api.autovacuum
    def _gc_old_requests(self):
        limit = fields.Datetime.now() - timedelta(days=30)
        self.search([('create_date', '<', limit)]).unlink()
