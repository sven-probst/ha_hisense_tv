"""Authentication and Token Manager for Hisense TV."""
import asyncio
import json
import logging
import hashlib
import secrets
from datetime import datetime, timedelta
from typing import Optional
from dataclasses import dataclass

from homeassistant.components import mqtt
from homeassistant.core import HomeAssistant

from .const import (
    DEFAULT_CLIENT_ID,
    TOKEN_VALIDITY_HOURS,
    TOKEN_REFRESH_BUFFER_HOURS,
)

_LOGGER = logging.getLogger(__name__)

# Topic constants for authentication flow
# These match the Hisense VIDAA MQTT protocol
TOKEN_TOPIC_PUBLISH = "/remoteapp/tv/platform_service/{client_id}/actions/gettoken"
TOKEN_TOPIC_RESPONSE = "/remoteapp/mobile/{client_id}/platform_service/data/tokenissuance"
AUTH_TOPIC_PUBLISH = "/remoteapp/tv/ui_service/{client_id}/actions/vidaa_app_connect"
AUTH_TOPIC_RESPONSE = "/remoteapp/mobile/{client_id}/ui_service/data/vidaa_app_connect"
LOGIN_INFO_TOPIC_PUBLISH = "/remoteapp/tv/ui_service/{client_id}/actions/login_each_other_info"
LOGIN_INFO_TOPIC_RESPONSE = "/remoteapp/mobile/{client_id}/ui_service/data/login_each_other_info"


@dataclass
class AuthToken:
    """Represents an authentication token."""
    token: str
    created_at: datetime
    expires_at: datetime
    device_id: Optional[str] = None
    session_id: Optional[str] = None
    
    @property
    def is_expired(self) -> bool:
        """Check if the token has expired."""
        return datetime.now() >= self.expires_at
    
    @property
    def needs_refresh(self) -> bool:
        """Check if the token needs refreshing."""
        refresh_time = self.expires_at - timedelta(hours=TOKEN_REFRESH_BUFFER_HOURS)
        return datetime.now() >= refresh_time
    
    def to_dict(self) -> dict:
        """Convert token to dictionary for storage."""
        return {
            "token": self.token,
            "created_at": self.created_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "device_id": self.device_id,
            "session_id": self.session_id,
        }
    
    @classmethod
    def from_dict(cls, data: dict) -> "AuthToken":
        """Create token from dictionary."""
        return cls(
            token=data["token"],
            created_at=datetime.fromisoformat(data["created_at"]),
            expires_at=datetime.fromisoformat(data["expires_at"]),
            device_id=data.get("device_id"),
            session_id=data.get("session_id"),
        )


class HisenseAuthManager:
    """Manages authentication and token lifecycle for Hisense TV."""
    
    def __init__(
        self,
        hass: HomeAssistant,
        client_id: str,
        mqtt_in: str,
        mqtt_out: str,
        mac_address: str,
        stored_token: Optional[dict] = None,
    ):
        self._hass = hass
        self._client_id = client_id or DEFAULT_CLIENT_ID
        self._mqtt_in = mqtt_in
        self._mqtt_out = mqtt_out
        self._mac_address = mac_address
        
        # Load stored token if available
        self._token: Optional[AuthToken] = None
        if stored_token:
            try:
                self._token = AuthToken.from_dict(stored_token)
                _LOGGER.debug("Loaded stored token, expires at: %s", self._token.expires_at)
            except (KeyError, ValueError) as e:
                _LOGGER.warning("Could not load stored token: %s", e)
                self._token = None
        
        # Generate consistent device identifiers
        self._device_id = self._generate_device_id()
        self._session_id = secrets.token_hex(16)
        
        # Lock for token operations (prevent concurrent auth attempts)
        self._token_lock = asyncio.Lock()
        
        # Track authentication state
        self._is_authenticated = False
    
    def _generate_device_id(self) -> str:
        """Generate a consistent device ID based on MAC address."""
        mac_clean = self._mac_address.replace(":", "").replace("-", "").upper()
        hash_input = f"HisenseTV_{mac_clean}".encode()
        return hashlib.sha256(hash_input).hexdigest()[:32]
    
    @property
    def is_authenticated(self) -> bool:
        """Check if currently authenticated."""
        if not self._is_authenticated or not self._token:
            return False
        return not self._token.is_expired
    
    @property
    def current_token(self) -> Optional[AuthToken]:
        """Get the current token."""
        return self._token
    
    def get_token_dict(self) -> Optional[dict]:
        """Get token as dictionary for storage."""
        if self._token:
            return self._token.to_dict()
        return None
    
    async def ensure_authenticated(self) -> bool:
        """Ensure we have a valid authentication, refresh if needed."""
        async with self._token_lock:
            # If we have a valid token that doesn't need refresh, return True
            if self._token and not self._token.needs_refresh and not self._token.is_expired:
                _LOGGER.debug("Token is valid, no refresh needed")
                return True
            
            # If token needs refresh, try to refresh
            if self._token and self._token.needs_refresh and not self._token.is_expired:
                _LOGGER.debug("Token needs refresh")
                if await self._refresh_token():
                    return True
            
            # Otherwise, get a new token
            _LOGGER.debug("Getting new authentication token")
            return await self._get_new_token()
    
    async def _get_new_token(self) -> bool:
        """Get a new authentication token from the TV."""
        queue = asyncio.Queue()
        
        async def connect_response_callback(msg):
            try:
                payload = json.loads(msg.payload)
                connect_result = payload.get("connect_result")
                _LOGGER.debug("vidaa_app_connect response: %s", payload)
                # TV responds with connect_result: 1 for success
                await queue.put({"success": connect_result == 1, "payload": payload})
            except (json.JSONDecodeError, AttributeError) as e:
                _LOGGER.error("Failed to parse connect response: %s", e)
                await queue.put({"success": False, "payload": {}})
        
        # Build topics
        topic_response = self._mqtt_in + AUTH_TOPIC_RESPONSE.format(client_id=self._client_id)
        topic_publish = self._mqtt_out + AUTH_TOPIC_PUBLISH.format(client_id=self._client_id)
        
        # Subscribe to connect response
        unsub = await mqtt.async_subscribe(
            self._hass, topic_response, connect_response_callback
        )
        
        try:
            # Send vidaa_app_connect (matching the official app format)
            connect_payload = json.dumps({
                "app_version": 2,
                "device_type": "Mobile App",
                "device_id": self._device_id,
                "mac_address": self._mac_address,
            })
            
            await mqtt.async_publish(
                self._hass, topic_publish, connect_payload
            )
            _LOGGER.debug("Sent vidaa_app_connect request")
            
            # Wait for response
            result = await asyncio.wait_for(queue.get(), timeout=10)
            
            if result["success"]:
                _LOGGER.info("Successfully authenticated with TV")
                self._is_authenticated = True
                # Now request a token
                return await self._request_token()
            else:
                _LOGGER.warning("Authentication failed: %s", result["payload"])
                return False
                
        except asyncio.TimeoutError:
            _LOGGER.error("Timeout waiting for authentication response")
            return False
        finally:
            unsub()
    
    async def _request_token(self) -> bool:
        """Request a token from the TV after successful authentication."""
        queue = asyncio.Queue()
        
        async def token_response_callback(msg):
            try:
                payload = json.loads(msg.payload) if msg.payload != "(null)" else {}
                _LOGGER.debug("Token issuance response: %s", payload)
                await queue.put(payload)
            except json.JSONDecodeError:
                _LOGGER.debug("Token response: null or invalid")
                await queue.put({})
        
        # Build topics
        topic_response = self._mqtt_in + TOKEN_TOPIC_RESPONSE.format(client_id=self._client_id)
        topic_publish = self._mqtt_out + TOKEN_TOPIC_PUBLISH.format(client_id=self._client_id)
        
        # Subscribe to token response
        unsub = await mqtt.async_subscribe(
            self._hass, topic_response, token_response_callback
        )
        
        try:
            # Request token
            await mqtt.async_publish(self._hass, topic_publish, "")
            _LOGGER.debug("Requested token from TV")
            
            # Wait for response
            response = await asyncio.wait_for(queue.get(), timeout=10)
            
            if response:
                # Extract token from response
                token_value = response.get("token") or response.get("token_value")
                if token_value:
                    now = datetime.now()
                    self._token = AuthToken(
                        token=token_value,
                        created_at=now,
                        expires_at=now + timedelta(hours=TOKEN_VALIDITY_HOURS),
                        device_id=self._device_id,
                        session_id=self._session_id,
                    )
                    _LOGGER.info("Token received and stored, expires at: %s", self._token.expires_at)
                    return True
                else:
                    # TV might not require token (older firmware)
                    _LOGGER.info("No token in response - TV may not require token auth")
                    now = datetime.now()
                    self._token = AuthToken(
                        token="no-token-required",
                        created_at=now,
                        expires_at=now + timedelta(hours=TOKEN_VALIDITY_HOURS),
                        device_id=self._device_id,
                        session_id=self._session_id,
                    )
                    return True
            
            return False
            
        except asyncio.TimeoutError:
            _LOGGER.error("Timeout waiting for token response")
            return False
        finally:
            unsub()
    
    async def _refresh_token(self) -> bool:
        """Refresh the existing token."""
        _LOGGER.debug("Attempting to refresh token")
        return await self._request_token()
    
    async def send_login_info(self) -> bool:
        """Send login_each_other_info to establish session."""
        queue = asyncio.Queue()
        
        async def login_response_callback(msg):
            try:
                payload = json.loads(msg.payload) if msg.payload != "(null)" else {}
                _LOGGER.debug("Login info response: %s", payload)
                await queue.put(payload)
            except json.JSONDecodeError:
                await queue.put({})
        
        # Build topics
        topic_response = self._mqtt_in + LOGIN_INFO_TOPIC_RESPONSE.format(client_id=self._client_id)
        topic_publish = self._mqtt_out + LOGIN_INFO_TOPIC_PUBLISH.format(client_id=self._client_id)
        
        # Subscribe to login response
        unsub = await mqtt.async_subscribe(
            self._hass, topic_response, login_response_callback
        )
        
        try:
            login_payload = json.dumps({
                "device_id": self._device_id,
                "device_type": "Mobile App",
                "app_version": 2,
                "token": self._token.token if self._token else "",
            })
            
            await mqtt.async_publish(self._hass, topic_publish, login_payload)
            
            response = await asyncio.wait_for(queue.get(), timeout=10)
            return bool(response)
            
        except asyncio.TimeoutError:
            _LOGGER.debug("Timeout waiting for login info response")
            return False
        finally:
            unsub()
    
    async def validate_session(self) -> bool:
        """Validate the current session with the TV."""
        if not self._is_authenticated:
            return await self.ensure_authenticated()
        
        if self._token and self._token.is_expired:
            _LOGGER.info("Token expired, re-authenticating")
            return await self.ensure_authenticated()
        
        return True
