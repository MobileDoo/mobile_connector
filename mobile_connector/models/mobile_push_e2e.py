"""End-to-end encryption of push notifications for iPhones: docs/push-e2e.md (the format and its test vectors).

The Odoo seals the content of a notification for one phone with the public key that phone sent; the push gateway, Firebase
and Apple carry an opaque `enc`, and the Notification Service Extension of the app opens it with the private key that never
leaves the phone.
"""
import base64
import json
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from odoo import api, models

VERSION = 1
INFO = b'odoomobile-push-e2e-v1'
KEY_LENGTH = 32
NONCE_LENGTH = 12
TAG_LENGTH = 16
OVERHEAD = 1 + KEY_LENGTH + NONCE_LENGTH + TAG_LENGTH
# APNs allows 4096 bytes for the whole payload (`aps`, site, message and `enc`): `enc` stays under this many characters.
MAX_ENC_LENGTH = 3400
ESSENTIAL_DATA = ('type', 'model', 'res_id', 'message_id')


class PushE2eError(Exception):
    pass


def _aad(site_id):
    return f'om-e2e-1|{site_id}'.encode()


def _raw(public_key):
    return public_key.public_bytes(Encoding.Raw, PublicFormat.Raw)


def _key(shared, ephemeral_public, recipient_public):
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=ephemeral_public + recipient_public, info=INFO).derive(shared)


def seal(plaintext, recipient_public, site_id, ephemeral_private=None, nonce=None):
    """The envelope (bytes) of `plaintext` for the phone with this public key (32 raw bytes)."""
    ephemeral = X25519PrivateKey.from_private_bytes(ephemeral_private) if ephemeral_private else X25519PrivateKey.generate()
    ephemeral_public = _raw(ephemeral.public_key())
    shared = ephemeral.exchange(X25519PublicKey.from_public_bytes(recipient_public))
    nonce = nonce or os.urandom(NONCE_LENGTH)
    body = ChaCha20Poly1305(_key(shared, ephemeral_public, recipient_public)).encrypt(nonce, plaintext, _aad(site_id))
    return bytes([VERSION]) + ephemeral_public + nonce + body


def open_envelope(envelope, recipient_private, site_id):
    """The plaintext (bytes); what the app does. Used by tests: raises PushE2eError when it cannot be opened."""
    if len(envelope) < OVERHEAD:
        raise PushE2eError('envelope too short')
    if envelope[0] != VERSION:
        raise PushE2eError('unsupported version')
    private = X25519PrivateKey.from_private_bytes(recipient_private)
    ephemeral_public = envelope[1:33]
    shared = private.exchange(X25519PublicKey.from_public_bytes(ephemeral_public))
    key = _key(shared, ephemeral_public, _raw(private.public_key()))
    try:
        return ChaCha20Poly1305(key).decrypt(envelope[33:45], envelope[45:], _aad(site_id))
    except InvalidTag as exc:
        raise PushE2eError('cannot be opened') from exc


def public_key(value):
    """A public key given by a phone (base64 of 32 bytes) as raw bytes, or None when it is not one."""
    try:
        raw = base64.b64decode(value or '', validate=True)
    except ValueError:
        return None
    return raw if len(raw) == KEY_LENGTH else None


def _plaintext(title, body, data):
    return json.dumps({'title': title or '', 'body': body or '', 'data': data}, ensure_ascii=False, separators=(',', ':')).encode()


def encrypt_notification(recipient_public, site_id, title, body, data):
    """`enc` (base64) for a notification, shortened to fit a push: the body first, then the details of `data`."""
    data = {key: '' if value is None or value is False else str(value) for key, value in (data or {}).items()}
    budget = MAX_ENC_LENGTH // 4 * 3 - OVERHEAD
    for attempt in (data, {key: data[key] for key in ESSENTIAL_DATA if key in data}):
        body_text = body or ''
        while True:
            plaintext = _plaintext(title, body_text, attempt)
            if len(plaintext) <= budget or not body_text:
                break
            body_text = body_text[: max(int(len(body_text) * 0.8) - 1, 0)].rstrip() + '…' if len(body_text) > 1 else ''
        if len(plaintext) <= budget:
            return base64.b64encode(seal(plaintext, recipient_public, site_id)).decode()
    raise PushE2eError('notification too large')


class MobilePushE2e(models.AbstractModel):
    _name = 'mobile.push.e2e'
    _description = 'Mobile Push end-to-end encryption'

    @api.model
    def _encrypt(self, device, site_id, title, body, data):
        """`enc` for a device, or False when it has no usable key."""
        key = public_key(device.push_pubkey)
        return encrypt_notification(key, site_id, title, body, data) if key else False
