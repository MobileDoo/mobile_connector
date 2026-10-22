from odoo import fields, models


class ResCompany(models.Model):
    _inherit = 'res.company'

    # Shown by the app on login, home and the site switcher (Settings > Mobile).
    mobile_logo = fields.Image(string='Mobile app logo', max_width=512, max_height=512)
    # Behind the login form, before anyone is signed in (Settings > Mobile > Branding).
    mobile_login_background = fields.Image(string='Mobile app login background', max_width=1600, max_height=1600)
