"""Push through the customer's own Firebase project (FCM HTTP v1), without the push gateway.

Free for the customer (FCM costs nothing) and no third party in between, but
Android only: iPhones need an APNs key of the app publisher's Apple account,
so iOS push goes through the (paid) push gateway.

The service account (private key) never leaves the server; the app only gets
the public Firebase settings of the Android app (`mobile.api._mobile_push_info`).
"""
import base64
import hashlib
import json
import logging
import time

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from odoo import _, api, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

SCOPE = 'https://www.googleapis.com/auth/firebase.messaging'
FCM_URL = 'https://fcm.googleapis.com'
TIMEOUT = 15
ANDROID_PACKAGE = 'com.mobiledoo'
# Access tokens per (database, service account key): valid 1 hour, reused until 5 minutes before.
_TOKENS = {}


def _b64(data):
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode()


class MobilePushFcm(models.AbstractModel):
    _name = 'mobile.push.fcm'
    _description = 'Mobile Push through Firebase (FCM HTTP v1)'

    # ------------------------------------------------------------------
    # Configuration (Settings > Mobile > Push)
    # ------------------------------------------------------------------

    @api.model
    def _service_account(self):
        raw = self.env['ir.config_parameter'].sudo().get_param('mobile_connector.firebase_service_account')
        return json.loads(raw) if raw else None

    @api.model
    def _android_config(self):
        raw = self.env['ir.config_parameter'].sudo().get_param('mobile_connector.firebase_android')
        return json.loads(raw) if raw else None

    @api.model
    def _parse_service_account(self, content):
        """Validate an uploaded service account JSON; returns the dict to store."""
        try:
            data = json.loads(content)
        except ValueError as exc:
            raise UserError(_('The service account file is not valid JSON.')) from exc
        missing = [k for k in ('type', 'project_id', 'private_key', 'client_email', 'token_uri') if not data.get(k)]
        if data.get('type') != 'service_account' or missing:
            raise UserError(_('This is not a Firebase service account key (missing: %s).', ', '.join(missing) or 'type'))
        try:
            serialization.load_pem_private_key(data['private_key'].encode(), password=None)
        except ValueError as exc:
            raise UserError(_('The private key of the service account cannot be read.')) from exc
        return {k: data[k] for k in ('project_id', 'private_key', 'client_email', 'token_uri')}

    @api.model
    def _parse_google_services(self, content):
        """Public settings of the Android app from `google-services.json`."""
        try:
            data = json.loads(content)
            project = data['project_info']
            package = self.env['ir.config_parameter'].sudo().get_param('mobile_connector.android_package') \
                or ANDROID_PACKAGE
            client = next(c for c in data['client']
                          if c['client_info']['android_client_info']['package_name'] == package)
        except (ValueError, KeyError, TypeError) as exc:
            raise UserError(_('This is not a google-services.json file.')) from exc
        except StopIteration as exc:
            raise UserError(_('google-services.json has no Android app "%s": add it in the Firebase console.',
                              package)) from exc
        return {
            'project_id': project['project_id'],
            'sender_id': project['project_number'],
            'app_id': client['client_info']['mobilesdk_app_id'],
            'api_key': client['api_key'][0]['current_key'],
            'storage_bucket': project.get('storage_bucket') or False,
        }

    @api.model
    def _push_key(self):
        """Identifies where device tokens come from: tokens of another Firebase project cannot be used."""
        android = self._android_config()
        return f"firebase:{android['project_id']}" if android else False

    # ------------------------------------------------------------------
    # Sending
    # ------------------------------------------------------------------

    @api.model
    def _access_token(self, account):
        # Per key, not per account: a new key uploaded for the same account gets a new token.
        key = (self.env.cr.dbname, account['client_email'], hashlib.sha256(account['private_key'].encode()).hexdigest())
        cached = _TOKENS.get(key)
        if cached and cached[1] > time.time() + 300:
            return cached[0]
        now = int(time.time())
        header = _b64(json.dumps({'alg': 'RS256', 'typ': 'JWT'}).encode())
        claims = _b64(json.dumps({'iss': account['client_email'], 'scope': SCOPE, 'aud': account['token_uri'],
                                  'iat': now, 'exp': now + 3600}).encode())
        private_key = serialization.load_pem_private_key(account['private_key'].encode(), password=None)
        signature = _b64(private_key.sign(f'{header}.{claims}'.encode(), padding.PKCS1v15(), hashes.SHA256()))
        response = requests.post(account['token_uri'], timeout=TIMEOUT, data={
            'grant_type': 'urn:ietf:params:oauth:grant-type:jwt-bearer',
            'assertion': f'{header}.{claims}.{signature}',
        })
        response.raise_for_status()
        token = response.json()
        _TOKENS[key] = (token['access_token'], now + int(token.get('expires_in', 3600)))
        return token['access_token']

    @api.model
    def _send(self, tokens, title, body, data):
        """Send one notification to device tokens. Returns (delivered, invalid_tokens, error)."""
        account = self._service_account()
        if not account:
            return 0, [], 'Firebase is not configured (Settings > Mobile > Push).'
        base = (self.env['ir.config_parameter'].sudo().get_param('mobile_connector.fcm_url') or FCM_URL).rstrip('/')
        url = f"{base}/v1/projects/{account['project_id']}/messages:send"
        headers = {'Authorization': f'Bearer {self._access_token(account)}'}
        delivered, invalid, error = 0, [], False
        for token in tokens:
            message = {
                'token': token,
                'notification': {'title': title, 'body': body},
                'data': data,
                'android': {'priority': 'HIGH', 'notification': {'channel_id': 'default'}},
            }
            response = requests.post(url, json={'message': message}, headers=headers, timeout=TIMEOUT)
            if response.ok:
                delivered += 1
                continue
            status = ''
            try:
                status = response.json().get('error', {}).get('status', '')
            except ValueError:
                pass
            if response.status_code == 404 or status in ('NOT_FOUND', 'UNREGISTERED', 'INVALID_ARGUMENT'):
                invalid.append(token)  # uninstalled app, token of another project...
            else:
                error = f'FCM {response.status_code} {status}'.strip()
        return delivered, invalid, error

    @api.model
    def _test(self, tokens=()):
        """Try the configuration without waiting for a real notification. Returns (ok, message).

        With device tokens: a real test notification to them. Without: Google checks a message that is never
        delivered (`validate_only`), enough to tell a good key and project from a bad one.
        """
        android, account = self._android_config(), self._service_account()
        if not (android and account):
            return False, _('Upload google-services.json and the service account key first.')
        try:
            self._access_token(account)
        except requests.RequestException as exc:
            detail = ''
            try:
                detail = exc.response.json().get('error_description') or exc.response.json().get('error') or ''
            except (AttributeError, ValueError):
                detail = str(exc)
            return False, _('Google refused the service account key (%s). Generate a new private key in the Firebase '
                            'console (Project settings > Service accounts).', detail)
        if tokens:
            delivered, invalid, error = self._send(
                tokens, _('Test notification'), _('Firebase is set up correctly.'), {'type': 'test'})
            if delivered:
                return True, _('Test notification sent to %s device(s).', delivered)
            return False, _('Firebase accepted no device (%s). Open the app on the phone once more so it registers '
                            'again.', error or _('dead tokens: %s', len(invalid)))
        base = (self.env['ir.config_parameter'].sudo().get_param('mobile_connector.fcm_url') or FCM_URL).rstrip('/')
        response = requests.post(
            f"{base}/v1/projects/{account['project_id']}/messages:send", timeout=TIMEOUT,
            headers={'Authorization': f'Bearer {self._access_token(account)}'},
            json={'validate_only': True, 'message': {'token': 'odoo-mobile-check', 'data': {'type': 'test'}}})
        status = ''
        try:
            status = response.json().get('error', {}).get('status', '')
        except ValueError:
            pass
        if response.ok or status == 'INVALID_ARGUMENT':  # the fake token is refused, the key and project are fine
            return True, _('Firebase accepts this configuration (project %s). No Android phone is registered on it '
                           'yet: open the app on one to receive a real test.', account['project_id'])
        if response.status_code in (401, 403):
            return False, _('The service account may not send messages: give it the role "Firebase Cloud Messaging '
                            'API Admin" and enable the "Firebase Cloud Messaging API" in the Google Cloud console.')
        if response.status_code == 404:
            return False, _('Firebase project "%s" not found: check that the service account belongs to it.',
                            account['project_id'])
        return False, _('Firebase answered %(code)s %(status)s.', code=response.status_code, status=status)
