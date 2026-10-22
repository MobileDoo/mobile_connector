"""1.1.0 adds Odoo menus to the app: hide administration menus on existing databases too.

(data/ir_ui_menu_data.xml is `noupdate`, so it only applies on new installs.)
"""
from odoo import SUPERUSER_ID, api

DEFAULT_HIDDEN = ('base.menu_administration', 'base.menu_management')


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    for xmlid in DEFAULT_HIDDEN:
        menu = env.ref(xmlid, raise_if_not_found=False)
        if menu:
            menu.mobile_hide = True
