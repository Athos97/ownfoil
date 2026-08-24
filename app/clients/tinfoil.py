"""
Tinfoil client implementation.
"""
from flask import Request, Response, jsonify
from typing import Tuple, Optional, Dict, Any
import json
import random
from Crypto.PublicKey import RSA
from Crypto.Cipher import PKCS1_OAEP, AES
from Crypto.Hash import SHA256
import zstandard as zstd

from .client import BaseClient
from constants import APP_TYPE_FILTERS

TINFOIL_HEADERS = [
    'Theme',
    'Uid',
    'Version',
    'Revision',
    'Language',
    'Hauth',
    'Uauth'
]

# https://github.com/blawar/tinfoil/blob/master/docs/files/public.key
TINFOIL_PUBLIC_KEY = '''-----BEGIN PUBLIC KEY-----
MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAvPdrJigQ0rZAy+jla7hS
jwen8gkF0gjtl+lZGY59KatNd9Kj2gfY7dTMM+5M2tU4Wr3nk8KWr5qKm3hzo/2C
Gbc55im3tlRl6yuFxWQ+c/I2SM5L3xp6eiLUcumMsEo0B7ELmtnHTGCCNAIzTFzV
4XcWGVbkZj83rTFxpLsa1oArTdcz5CG6qgyVe7KbPsft76DAEkV8KaWgnQiG0Dps
INFy4vISmf6L1TgAryJ8l2K4y8QbymyLeMsABdlEI3yRHAm78PSezU57XtQpHW5I
aupup8Es6bcDZQKkRsbOeR9T74tkj+k44QrjZo8xpX9tlJAKEEmwDlyAg0O5CLX3
CQIDAQAB
-----END PUBLIC KEY-----'''


class TinfoilClient(BaseClient):
    """Tinfoil client with header-based identification, Hauth verification, and encrypted shop responses."""

    # Class variables
    CLIENT_NAME = "Tinfoil"

    # ==================== Abstract Method Implementations (Required) ====================

    @classmethod
    def identify_client(cls, request: Request) -> bool:
        """Identify Tinfoil client by checking for required headers."""
        return all(header in request.headers for header in TINFOIL_HEADERS) and 'User-Agent' not in request.headers.keys()

    def error_response(self, error_message: str) -> Response:
        """Generate Tinfoil error response in JSON format."""
        return jsonify({'error': error_message})

    def info_response(self, info_message: str) -> Response:
        """Generate Tinfoil info response in JSON format."""
        return jsonify({'success': info_message})

    @BaseClient.authenticate
    @BaseClient.verify_shop_access
    def _handle_get(self, request: Request) -> Response:
        """Handle GET requests for specific paths."""
        # Access auth flags from request object (set by @authenticate decorator)
        if not request.client_auth_success:
            return self.error_response(request.client_auth_error)

        # Get client-specific settings
        client_settings = self.app_settings['shop']['clients']['tinfoil']

        paths = request.path.strip('/').split('/')
        content_filter = paths[0] if paths and paths[0] in APP_TYPE_FILTERS else None
        # Build shop content
        shop = {"success": self.app_settings['shop']['motd']}
        shop["files"] = self._generate_shop_files(content_filter)

        # Get verified_host from auth_data
        verified_host = request.auth_data.get('verified_host')
        if verified_host:
            # Enforce client side host verification
            shop["referrer"] = f"https://{verified_host}"

        # Serve the shop
        if client_settings['encrypt']:
            return Response(self._encrypt_shop(shop), mimetype='application/octet-stream')

        return jsonify(shop)

    # ==================== Private/Helper Methods ====================

    def _client_authenticate(self, request: Request) -> Tuple[bool, Optional[str], Optional[Dict[str, Any]]]:
        """Tinfoil authenticates with the shared Hauth host-verification scheme."""
        return self._client_authenticate_with_host_verification(request)

    def _encrypt_shop(self, shop: dict) -> bytes:
        """Encrypt shop data for Tinfoil using RSA + AES encryption."""
        input_data = json.dumps(shop).encode('utf-8')

        # Random 128-bit AES key (16 bytes), used later for symmetric encryption (AES)
        aes_key = random.randint(0, 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF).to_bytes(0x10, 'big')

        # Zstandard compression
        flag = 0xFD
        cctx = zstd.ZstdCompressor(level=22)
        buf = cctx.compress(input_data)
        sz = len(buf)

        # Encrypt the AES key with RSA, PKCS1_OAEP padding scheme
        pub_key = RSA.importKey(TINFOIL_PUBLIC_KEY)
        cipher = PKCS1_OAEP.new(pub_key, hashAlgo=SHA256, label=b'')
        # Now the AES key can only be decrypted with Tinfoil private key
        session_key = cipher.encrypt(aes_key)

        # Encrypting the Data with AES
        cipher = AES.new(aes_key, AES.MODE_ECB)
        buf = cipher.encrypt(buf + (b'\x00' * (0x10 - (sz % 0x10))))

        binary_data = (
            b'TINFOIL' + 
            flag.to_bytes(1, byteorder='little') + 
            session_key + 
            sz.to_bytes(8, 'little') + 
            buf
        )
        return binary_data
