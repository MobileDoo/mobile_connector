from odoo import api, models


class IrConfigParameter(models.Model):
    """Odoo 20 replaced `get_param` / `set_param` by typed accessors (`get_str`, `get_bool`, `set_str`...).

    The mobile modules keep their parameters as strings (`'True'`, `'30'`, a JSON text) and read them in many places
    (`icp.get_param(...) == 'True'`): these two methods keep that working, on top of the new string accessors.
    """
    _inherit = 'ir.config_parameter'

    @api.model
    def get_param(self, key, default=False):
        value = self.get_str(key, None)
        return default if value is None else value

    @api.model
    def set_param(self, key, value):
        """Same as before: a false value removes the setting (it reads back as the default). Returns the old value."""
        return self.set_str(key, value if value not in (None, False, '') else None)
