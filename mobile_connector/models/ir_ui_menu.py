from odoo import fields, models


class IrUiMenu(models.Model):
    _inherit = 'ir.ui.menu'

    mobile_hide = fields.Boolean(
        string='Hide in Mobile App',
        help='Do not show this menu (and its sub-menus) in the "Odoo apps" section of the mobile app.')
