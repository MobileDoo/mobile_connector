"""API documentation for integrators: OpenAPI description + Swagger UI.

    GET /mobile/api/v1/openapi.json   OpenAPI 3.1 (built by docs/openapi/build.py)
    GET /mobile/api/docs              Swagger UI ("Try it out" calls this very server)

Public on purpose: the description contains no data, and every operation
still requires a device token.
"""
import functools
import json
import os

from odoo import http
from odoo.http import request

SPEC_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'static', 'api', 'openapi.json')
SWAGGER_UI = 'https://cdn.jsdelivr.net/npm/swagger-ui-dist@5'

DOCS_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>MobileDoo API</title>
  <link rel="stylesheet" href="{ui}/swagger-ui.css">
</head>
<body>
  <div id="swagger-ui"></div>
  <script src="{ui}/swagger-ui-bundle.js"></script>
  <script>
    window.ui = SwaggerUIBundle({{
      url: 'v1/openapi.json',
      dom_id: '#swagger-ui',
      deepLinking: true,
      persistAuthorization: true,
      displayRequestDuration: true,
    }});
  </script>
</body>
</html>
"""


@functools.lru_cache(maxsize=1)
def _spec_text():
    with open(SPEC_PATH, encoding='utf-8') as handle:
        return handle.read()


class MobileApiDocsController(http.Controller):

    @http.route('/mobile/api/v1/openapi.json', type='http', auth='none', methods=['GET'], csrf=False, save_session=False)
    def openapi(self, **_kwargs):
        spec = json.loads(_spec_text())
        # Point "Try it out" and generated clients at this server (proxy_mode gives the public URL).
        spec['servers'] = [{'url': request.httprequest.url_root.rstrip('/') + '/mobile/api/v1', 'description': 'This Odoo'}]
        return request.make_json_response(spec)

    @http.route('/mobile/api/docs', type='http', auth='none', methods=['GET'], csrf=False, save_session=False)
    def docs(self, **_kwargs):
        return request.make_response(DOCS_PAGE.format(ui=SWAGGER_UI), headers=[('Content-Type', 'text/html; charset=utf-8')])
