from odoo import api, fields, models


class MobileFieldConfig(models.Model):
    """No-code way for admins to show extra (custom) fields in a native mobile screen."""

    _name = 'mobile.field.config'
    _description = 'Mobile Extra Field'
    _order = 'model_id, sequence, id'

    model_id = fields.Many2one('ir.model', required=True, ondelete='cascade')
    model = fields.Char(string='Model name', related='model_id.model', store=True, index=True)
    field_id = fields.Many2one(
        'ir.model.fields', required=True, ondelete='cascade',
        domain="[('model_id', '=', model_id), ('ttype', 'not in', ('one2many', 'binary', 'reference', 'properties', 'properties_definition', 'json'))]",
    )
    field_name = fields.Char(related='field_id.name', store=True)
    scope = fields.Selection([('form', 'Form'), ('list', 'List card')], default='form', required=True)
    sequence = fields.Integer(default=100)
    label = fields.Char(help='Overrides the field label on mobile.')
    widget = fields.Char(help='Mobile widget key (leave empty to pick one from the field type).')
    active = fields.Boolean(default=True)

    @api.model
    def _get_extra_fields(self, model_name, scope='form'):
        configs = self.sudo().search([('model', '=', model_name), ('scope', '=', scope)])
        return [{
            'name': config.field_name,
            'sequence': config.sequence,
            'string': config.label or False,
            'widget': config.widget or False,
        } for config in configs]
