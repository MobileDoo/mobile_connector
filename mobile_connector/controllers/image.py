from odoo import http
from odoo.exceptions import AccessError, MissingError
from odoo.http import request

# Photos only: the generated SVG avatars (avatar_*) cannot be shown by the app's image component.
IMAGE_FIELDS = ('image_128', 'image_256', 'image_512')


class MobileImageController(http.Controller):

    @http.route('/mobile/api/v1/image/<string:model>/<int:record_id>/<string:field>',
                type='http', auth='mobile', methods=['GET'], csrf=False, save_session=False)
    def image(self, model, record_id, field, **_kwargs):
        """A record's photo (employee, contact, product...) with the user's access rights.

        404 when there is no photo: the app shows initials instead.
        """
        if field not in IMAGE_FIELDS or model not in request.env or field not in request.env[model]._fields:
            return request.not_found()
        try:
            record = request.env[model].browse(record_id).exists()
            if not record:
                return request.not_found()
            record.check_access_rights('read')
            record.check_access_rule('read')
            if not record[field]:
                return request.not_found()
            stream = request.env['ir.binary']._get_image_stream_from(record, field)
        except (AccessError, MissingError):
            return request.not_found()
        if stream.mimetype == 'image/svg+xml':
            return request.not_found()
        # The app sends the record's write_date as `?v=`: cache for a long time, the URL changes with the photo.
        return stream.get_response(max_age=86400 * 30, immutable=bool(request.httprequest.args.get('v')))

    @http.route('/mobile/api/v1/login_background', type='http', auth='none', methods=['GET'], csrf=False,
                save_session=False)
    def login_background(self, **_kwargs):
        """The picture behind the login form (Settings > Mobile). 404 when none is set.

        Public, like `/info`: the login screen shows it before anyone is signed in. It is a picture the company
        chose to show; nothing else is reachable from here. The app sends `?v=<site.login_background>`.
        """
        if not request.db:
            return request.not_found()
        company = request.env['mobile.api'].sudo()._mobile_site_company()
        if not company.mobile_login_background:
            return request.not_found()
        stream = request.env['ir.binary'].sudo()._get_image_stream_from(company, 'mobile_login_background')
        if stream.mimetype == 'image/svg+xml':
            return request.not_found()
        return stream.get_response(max_age=86400 * 30, immutable=bool(request.httprequest.args.get('v')))

    @http.route('/mobile/api/v1/logo', type='http', auth='mobile', methods=['GET'], csrf=False, save_session=False)
    def logo(self, **_kwargs):
        """The site's logo for the app (Settings > Mobile). 404 when none is set.

        Authenticated so it is served from the right database on multi-database servers;
        the app caches it and asks again only when `site.logo` (its checksum) changes.
        """
        company = request.env['mobile.api'].sudo()._mobile_site_company()
        if not company.mobile_logo:
            return request.not_found()
        stream = request.env['ir.binary']._get_image_stream_from(company, 'mobile_logo')
        return stream.get_response(max_age=86400 * 30, immutable=bool(request.httprequest.args.get('v')))
