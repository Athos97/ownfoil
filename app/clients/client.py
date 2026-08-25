"""
Base client class for shop clients.
All client implementations must inherit from this class and implement the required methods.
"""
from abc import ABC, abstractmethod
from flask import Request, Response
from typing import Tuple, Optional, Dict, Any
from functools import wraps
from db import get_filtered_files
from auth import basic_auth
from settings import set_shop_settings
import activity
import logging

logger = logging.getLogger('main')


class BaseClient(ABC):
    """Base class for shop clients implementing common interface for authentication, shop serving, and file delivery."""

    # Class variables - should be overridden by subclasses
    CLIENT_NAME = "BaseClient"

    # ==================== Initialization ====================

    def __init__(self, app_settings: dict):
        """Initialize the client with application settings and database."""
        self.app_settings = app_settings
        logger.debug(f"Initialized {self.CLIENT_NAME} client")

    # ==================== Authentication Decorator ====================

    @staticmethod
    def authenticate(handler):
        """Decorator that handles authentication for handle_<method> functions."""
        @wraps(handler)
        def wrapper(self, request: Request) -> Response:
            # Initialize auth flags on request object
            request.basic_auth_success = False
            request.basic_auth_error = None
            request.client_auth_success = False
            request.client_auth_error = None
            request.user = None
            request.auth_data = {}

            # Perform host verification only for HTTPS requests
            if request.is_secure or request.headers.get("X-Forwarded-Proto") == "https":
                shop_host = self.app_settings["shop"].get("host")
                if not shop_host:
                    self.log_error("Missing shop host configuration, Host verification is disabled.")
                elif request.host != shop_host:
                    return self.error_response(f"Incorrect URL referrer detected: {request.host}.")

            # Generic Basic Auth
            request.basic_auth_success, request.basic_auth_error, request.user = basic_auth(request)
            if request.basic_auth_success:
                self.log_info(f"Basic authentication successful for user: {request.user.user}")
            else:
                self.log_warning(f"Authentication failed: {request.basic_auth_error}")

            # Audit the visit (throttled per user/device inside) - after basic auth
            # so the event carries who it was.
            activity.record_shop_connect(request, self.CLIENT_NAME)

            # Client-specific authentication
            request.client_auth_success, request.client_auth_error, client_auth_data = self._client_authenticate(request)
            if request.client_auth_success:
                self.log_info("Client-specific authentication successful.")
                if client_auth_data:
                    request.auth_data.update(client_auth_data)

            else:
                self.log_warning(f"Client-specific auth failed: {request.client_auth_error}")

            # Call the actual handler
            return handler(self, request)

        return wrapper

    @staticmethod
    def verify_shop_access(handler):
        """Decorator that enforces authenticated access to the shop."""
        @wraps(handler)
        def wrapper(self, request: Request) -> Response:
            # Check if shop requires authentication
            if not self.app_settings['shop']['public']:
                if not request.basic_auth_success:
                    return self.error_response("Shop requires authentication.\n" + (request.basic_auth_error))
                # Check if user has shop access
                if request.user and not request.user.has_shop_access():
                    return self.error_response(f'User {request.user.user} does not have access to the shop.')

            # Call the actual handler
            return handler(self, request)

        return wrapper

    def _client_authenticate(self, request: Request) -> Tuple[bool, Optional[str], Optional[Dict[str, Any]]]:
        """
        Client-specific authentication logic. Override in subclasses for custom behavior.

        Returns:
            Tuple of (success: bool, error_message: Optional[str], auth_data: Optional[Dict])
            - success: True if authentication passed
            - error_message: Error message if authentication failed
            - auth_data: Additional authentication data to be stored in request.auth_data
        """
        # Default implementation: no additional authentication required
        return True, None, {}

    # ==================== Shared Hauth Host-Verification (Opt-In) ====================
    #
    # CyberFoil and Tinfoil both authenticate with the same Hauth-based host-verification
    # scheme; Sphaira has no such concept and must keep using the no-op default above, so
    # this isn't wired in as _client_authenticate's default - a subclass that wants it
    # delegates to _client_authenticate_with_host_verification from its own override.

    def _client_authenticate_with_host_verification(self, request: Request) -> Tuple[bool, Optional[str], Optional[Dict[str, Any]]]:
        """Host verification for HTTPS requests, keyed by self.CLIENT_NAME.lower()."""
        success = True
        error = None
        verified_host = None

        if request.is_secure or request.headers.get("X-Forwarded-Proto") == "https":
            success, error, verified_host = self._verify_host(request)

        return success, error, {'verified_host': verified_host}

    def _verify_host(self, request: Request) -> Tuple[bool, Optional[str], Optional[str]]:
        """Verify Hauth to prevent hotlinking."""
        client_key = self.CLIENT_NAME.lower()
        request_host = request.host
        request_hauth = request.headers.get('Hauth')
        shop_host = self.app_settings["shop"].get("host")
        client_settings = self.app_settings["shop"]["clients"][client_key]
        hauth_dict = client_settings.get("hauth", {})

        # Get hauth for this specific host
        shop_hauth = hauth_dict.get(request_host)

        self.log_info(f"Secure request from remote host {request_host}, proceeding with host verification.")

        if not shop_host:
            self.log_error("Missing shop host configuration, Host verification is disabled.")
            return True, None, None

        if not shop_hauth:
            return self._handle_missing_hauth(request, request_host, request_hauth)

        if request_hauth != shop_hauth:
            self.log_warning(f"Incorrect Hauth detected for host: {request_host}.")
            return False, f"Incorrect Hauth for URL `{request_host}`.", None

        return True, None, shop_host

    def _handle_missing_hauth(self, request: Request, request_host: str, request_hauth: str) -> Tuple[bool, Optional[str], Optional[str]]:
        """Handle case when Hauth is not configured."""
        client_key = self.CLIENT_NAME.lower()
        basic_auth_success = request.basic_auth_success
        user_is_admin = request.user.has_admin_access() if request.user else False

        if basic_auth_success and user_is_admin:
            # Save hauth to client-specific settings as a dict with host as key
            shop_settings = self.app_settings['shop']
            hauth_dict = shop_settings['clients'][client_key].get('hauth', {})

            # Set hauth for this specific host
            hauth_dict[request_host] = request_hauth
            shop_settings['clients'][client_key]['hauth'] = hauth_dict
            set_shop_settings(shop_settings)
            self.log_info(f"Successfully set Hauth value for host {request_host}.")
            return True, None, request_host

        self.log_warning(
            f"Hauth value not set for host {request_host}, Host verification is disabled. "
            f"Connect to the shop from {self.CLIENT_NAME} with an admin account to set it."
        )
        return True, None, None

    def _generate_shop_files(self, content_filter: Optional[str] = None) -> list:
        """Generate the files list for the shop with optional content type filtering.

        The URL carries this client's name as a query param: the file-download
        request that actually follows it doesn't resend the shop-protocol
        identification headers (Theme/Uid/Version/etc. - confirmed against a real
        Tinfoil download, which arrives as a bare authenticated GET), so
        serve_game() can't otherwise tell who is downloading. This is the one
        place that genuinely knows."""
        files = self.get_filtered_files(content_filter)
        client_param = self.CLIENT_NAME.lower()
        return [{'url': f'/api/get_game/{f.id}?client={client_param}#{f.filename}', 'size': f.size}
                for f in files]

    # ==================== Abstract Methods (Required) ====================

    @classmethod
    @abstractmethod
    def identify_client(cls, request: Request) -> bool:
        """Identify if the request is from this client type."""
        pass

    @abstractmethod
    def error_response(self, error_message: str) -> Response:
        """Generate an error response in the format expected by the client."""
        pass

    @abstractmethod
    def info_response(self, info_message: str) -> Response:
        """Generate an info response in the format expected by the client."""
        pass

    @abstractmethod
    def _handle_get(self, request: Request) -> Response:
        """Handle GET requests for specific paths."""
        pass

    # ==================== Public Methods ====================

    def handle_request(self, request: Request) -> Response:
        """Handle an incoming HTTP request and route to appropriate handler."""
        method = request.method
        path = request.path
        headers = request.headers

        # Route request based on method and path
        if method == "OPTIONS":
            return self._handle_options(path, headers)
        elif method == "HEAD":
            return self._handle_head(request)
        elif method == "GET":
            return self._handle_get(request)

    def get_filtered_files(self, content_filter: Optional[str] = None) -> list:
        """Get filtered files from the database based on content type."""
        return get_filtered_files(content_filter)

    def log_info(self, message: str):
        """Log an info message with client context."""
        logger.info(f"({self.CLIENT_NAME}) {message}")

    def log_warning(self, message: str):
        """Log a warning message with client context."""
        logger.warning(f"({self.CLIENT_NAME}) {message}")

    def log_error(self, message: str):
        """Log an error message with client context."""
        logger.error(f"({self.CLIENT_NAME}) {message}")

    # ==================== Private/Helper Methods ====================

    def _handle_options(self, path: str, headers: dict) -> Response:
        """Handle OPTIONS requests for CORS preflight."""
        pass

    def _handle_head(self, request: Request) -> Response:
        """
        Handle HEAD requests. Override in subclasses if needed.
        Default implementation returns 404.
        """
        return self.error_response("HEAD method not implemented"), 404
