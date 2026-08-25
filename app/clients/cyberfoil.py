"""
CyberFoil client implementation.
"""
from flask import Request, Response, jsonify
from typing import Tuple, Optional, Dict, Any

from .client import BaseClient
from constants import APP_TYPE_FILTERS

CYBERFOIL_HEADERS = [
    'Theme',
    'Uid',
    'Version',
    'Revision',
    'Language',
    'Hauth',
    'Uauth'
]

class CyberFoilClient(BaseClient):
    """CyberFoil client with header-based identification, Hauth verification."""

    # Class variables
    CLIENT_NAME = "CyberFoil"

    # ==================== Abstract Method Implementations (Required) ====================

    @classmethod
    def identify_client(cls, request: Request) -> bool:
        """Identify CyberFoil client by checking for required headers."""
        return all(header in request.headers for header in CYBERFOIL_HEADERS) and request.headers.get('User-Agent') == 'cyberfoil'

    def error_response(self, error_message: str) -> Response:
        """Generate CyberFoil error response in JSON format."""
        return jsonify({'error': error_message})

    def info_response(self, info_message: str) -> Response:
        """Generate CyberFoil info response in JSON format."""
        return jsonify({'success': info_message})

    @BaseClient.authenticate
    @BaseClient.verify_shop_access
    def _handle_get(self, request: Request) -> Response:
        """Handle GET requests for specific paths."""
        # Access auth flags from request object (set by @authenticate decorator)
        if not request.client_auth_success:
            return self.error_response(request.client_auth_error)

        # Get client-specific settings
        client_settings = self.app_settings['shop']['clients']['cyberfoil']

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
        return jsonify(shop)

    # ==================== Private/Helper Methods ====================

    def _client_authenticate(self, request: Request) -> Tuple[bool, Optional[str], Optional[Dict[str, Any]]]:
        """CyberFoil authenticates with the shared Hauth host-verification scheme."""
        return self._client_authenticate_with_host_verification(request)
