"""Hisense TV config flow."""
import json
import asyncio
from json.decoder import JSONDecodeError
import logging
import re
import xml.etree.ElementTree as ET

import aiohttp
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.components import mqtt
from homeassistant.components import ssdp as ssdp_component
from homeassistant.const import CONF_IP_ADDRESS, CONF_MAC, CONF_NAME, CONF_PIN, ATTR_FRIENDLY_NAME
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.device_registry import format_mac
from homeassistant.helpers.service_info.ssdp import SsdpServiceInfo

from .const import (
    CONF_ENABLE_POLLING,
    CONF_MQTT_IN,
    CONF_MQTT_OUT,
    DEFAULT_CLIENT_ID,
    DEFAULT_MQTT_PREFIX,
    DEFAULT_NAME,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)


class HisenseTvFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Hisense TV config flow."""

    VERSION = 1

    @staticmethod
    @config_entries.HANDLERS.register("options")
    def async_get_options_flow(config_entry):
        return HisenseTvOptionsFlow(config_entry)

    def __init__(self):
        """Initialize the config flow."""
        self._data: dict[str, any] = {}
        self._discovered_ip = None
        self._discovered_name = None
        self._mac_address = None
        self._client_id = DEFAULT_CLIENT_ID
        self._pin = None

    async def async_step_ssdp(self, discovery_info: SsdpServiceInfo) -> FlowResult:
        """Handle discovery via SSDP."""
        # Fetch and parse the description XML to get detailed device info.
        session = async_get_clientsession(self.hass)
        try:
            async with session.get(discovery_info.ssdp_location) as response:
                response.raise_for_status()
                xml_content = await response.text()
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            _LOGGER.warning("Could not fetch device description at %s: %s", discovery_info.ssdp_location, err)
            return self.async_abort(reason="cannot_connect")

        # The device info is often in a custom <hisense:X_spec> tag.
        # We parse the XML and look for it.
        try:
            root = ET.fromstring(xml_content)
            spec_string = None

            # First, try to find the Hisense-specific 'X_spec' tag.
            for elem in root.iter():
                if elem.tag.endswith("X_spec"):
                    spec_string = elem.text
                    break
            
            # If 'X_spec' is not found, fall back to the standard 'modelDescription' tag.
            if not spec_string:
                # The default namespace makes finding tags tricky. We search for the local name.
                for elem in root.iter():
                    if elem.tag.endswith("modelDescription"):
                        spec_string = elem.text
                        break

            if not spec_string:
                _LOGGER.debug("No X_spec or modelDescription node found in XML, aborting discovery.")
                return self.async_abort(reason="not_hisense_device")
            
        except ET.ParseError as err:
            _LOGGER.warning("Could not parse XML from %s: %s", discovery_info.ssdp_location, err)
            return self.async_abort(reason="cannot_parse_xml")

        # Parse the key-value pairs from the spec string
        device_details = dict(re.findall(r"(\w+)=([^=\s]+)", spec_string))

        # Check if the TV supports VIDAA, which is required for the API this integration uses.
        if device_details.get("vidaa_support") != "1":
            _LOGGER.debug(
                "Device does not report vidaa_support=1, it might not be compatible. Spec: %s",
                device_details,
            )
            return self.async_abort(reason="not_vidaa_device")

        # Prefer MAC, fall back to Ethernet MAC, then Wifi MAC
        mac_address = device_details.get("mac") or device_details.get("macEthernet") or device_details.get("macWifi")
        if not mac_address:
            _LOGGER.debug("No MAC address found in device spec: %s", device_details)
            return self.async_abort(reason="cannot_get_mac")

        ip_address = discovery_info.ssdp_headers["_host"]
        friendly_name = discovery_info.upnp.get(ATTR_FRIENDLY_NAME, f"Hisense TV @ {ip_address}")

        _LOGGER.debug(
            "Hisense TV discovered: IP=%s, Name=%s, MAC=%s", ip_address, friendly_name, mac_address
        )

        self._mac_address = format_mac(mac_address)
        self._discovered_ip = ip_address
        self._discovered_name = friendly_name
        self.context["title_placeholders"] = {"name": self._discovered_name}

        # If MAC was found, set unique_id and check if already configured.
        await self.async_set_unique_id(self._mac_address, raise_on_progress=False)
        self._abort_if_unique_id_configured(updates={CONF_IP_ADDRESS: ip_address})

        # Store discovered device info to be added to the device registry upon creation.
        # This ensures model, manufacturer etc. are available immediately.
        self.context["device_info"] = {
            "manufacturer": "Hisense",
            "model": device_details.get("modelName"),
            "name": friendly_name,
            "sw_version": device_details.get("firmware"), # Assuming firmware is in the spec
        }

        return await self.async_step_user()


    async def async_step_user(self, user_input=None) -> FlowResult:
        """Handle the initial user step."""
        errors = {}
        if user_input is not None:
            self._data.update(user_input)
            if not self.unique_id:
                mac = format_mac(user_input[CONF_MAC])
                await self.async_set_unique_id(mac, raise_on_progress=False)
                self._abort_if_unique_id_configured()
                self._data[CONF_MAC] = mac
            else:
                self._data[CONF_MAC] = self.unique_id

            return await self.async_step_check_auth()

        data_schema = vol.Schema(
            {
                vol.Required(CONF_NAME, default=self._discovered_name or DEFAULT_NAME): str,
                vol.Optional(CONF_IP_ADDRESS, default=self._discovered_ip or ""): str,
                vol.Optional(CONF_MQTT_IN, default=DEFAULT_MQTT_PREFIX): str,
                vol.Optional(CONF_MQTT_OUT, default=DEFAULT_MQTT_PREFIX): str,
                vol.Optional(CONF_ENABLE_POLLING, default=False): bool,
            }
        )

        if not self.unique_id:
            data_schema = data_schema.extend({vol.Required(CONF_MAC): str})

        return self.async_show_form(step_id="user", data_schema=data_schema, errors=errors)

    async def async_step_reauth(self, user_input=None):
        """Reauth handler."""
        _LOGGER.debug("async_step_reauth for entry_id: %s", self.context.get("entry_id"))
        
        # Load the existing entry to get the config data
        entry = self.hass.config_entries.async_get_entry(self.context["entry_id"])
        if entry:
            self._data = dict(entry.data)
            self.unique_id = entry.unique_id

        return await self.async_step_check_auth()

    async def async_step_check_auth(self, user_input=None):
        """Check if TV is already authenticated or needs a PIN."""
        queue = asyncio.Queue()

        async def auth_needed_callback(msg):
            await queue.put("auth_needed")

        async def auth_ok_callback(msg):
            await queue.put("auth_ok")

        mqtt_in = self._data[CONF_MQTT_IN]
        mqtt_out = self._data[CONF_MQTT_OUT]

        # Subscribe to topics that indicate auth status
        unsub_needed = await mqtt.async_subscribe(
            self.hass, f"{mqtt_in}/remoteapp/mobile/{self._client_id}/ui_service/data/authentication", auth_needed_callback
        )
        unsub_ok = await mqtt.async_subscribe(
            self.hass, f"{mqtt_in}/remoteapp/mobile/{self._client_id}/ui_service/data/sourcelist", auth_ok_callback
        )
        # Also subscribe to the general state topic as a fallback for "auth_ok"
        unsub_state = await mqtt.async_subscribe(
            self.hass, f"{mqtt_in}/remoteapp/mobile/broadcast/ui_service/state", auth_ok_callback
        )

        try:
            # Publish message to trigger an auth response
            await mqtt.async_publish(
                self.hass, f"{mqtt_out}/remoteapp/tv/ui_service/{self._client_id}/actions/gettvstate", ""
            )

            # Wait for a response
            result = await asyncio.wait_for(queue.get(), timeout=10)

            if result == "auth_needed":
                return await self.async_step_auth()
            if result == "auth_ok":
                return self.async_create_entry(title=self._data[CONF_NAME], data=self._data)

        except asyncio.TimeoutError:
            return self.async_abort(reason="auth_timeout")
        finally:
            unsub_needed()
            unsub_ok()
            unsub_state()

        return self.async_abort(reason="cannot_connect")

    async def async_step_auth(self, user_input=None):
        """Auth handler."""
        errors = {}
        if user_input is not None:
            queue = asyncio.Queue()

            async def auth_response_callback(msg):
                try:
                    payload = json.loads(msg.payload)
                    await queue.put(payload.get("result") == 1)
                except (JSONDecodeError, AttributeError):
                    await queue.put(False)

            mqtt_in = self._data[CONF_MQTT_IN]
            mqtt_out = self._data[CONF_MQTT_OUT]

            unsub = await mqtt.async_subscribe(
                self.hass, f"{mqtt_in}/remoteapp/mobile/{self._client_id}/ui_service/data/authenticationcode", auth_response_callback
            )

            try:
                payload = json.dumps({"authNum": user_input[CONF_PIN]})
                await mqtt.async_publish(
                    self.hass, f"{mqtt_out}/remoteapp/tv/ui_service/{self._client_id}/actions/authenticationcode", payload
                )

                if await asyncio.wait_for(queue.get(), timeout=10):
                    return self.async_create_entry(title=self._data[CONF_NAME], data=self._data)
                
                errors["base"] = "invalid_auth"

            except asyncio.TimeoutError:
                errors["base"] = "auth_timeout"
            finally:
                unsub()

        return self.async_show_form(
            step_id="auth",
            data_schema=vol.Schema({vol.Required(CONF_PIN): int}),
            errors=errors,
        )

    async def async_step_finish(self, user_input=None):
        """Finish config flow."""
        _LOGGER.debug("async_step_finish")
        _LOGGER.debug("async_step_finish with context: %s", self.context)

        return self.async_create_entry(
            title=self._data[CONF_NAME], data=self._data
        )

    async def async_step_import(self, data):
        """Handle import from YAML."""
        _LOGGER.debug("async_step_import")
        return self.async_create_entry(title=data[CONF_NAME], data=data)


class HisenseTvOptionsFlow(config_entries.OptionsFlow):
    """Handle an options flow for Hisense TV."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        """Initialize options flow."""
        self.config_entry = config_entry

    async def async_step_init(self, user_input=None):
        """Manage the options."""
        if user_input is not None:
            # Create a new dict with updated options, preserving the original data
            self.hass.config_entries.async_update_entry(
                self.config_entry, data=self.config_entry.data, options=user_input
            )
            return self.async_create_entry(title="", data=user_input)

        options_schema = vol.Schema(
            {
                vol.Optional(
                    CONF_IP_ADDRESS,
                    description={"suggested_value": self.config_entry.options.get(CONF_IP_ADDRESS, self.config_entry.data.get(CONF_IP_ADDRESS))},
                ): str,
                vol.Optional(
                    CONF_MQTT_IN, 
                    description={"suggested_value": self.config_entry.options.get(CONF_MQTT_IN, self.config_entry.data.get(CONF_MQTT_IN))},
                ): str,
                vol.Optional(
                    CONF_MQTT_OUT,
                    description={"suggested_value": self.config_entry.data.get(CONF_MQTT_OUT)},
                ): str,
                vol.Optional(
                    CONF_ENABLE_POLLING,
                    default=self.config_entry.options.get(CONF_ENABLE_POLLING, self.config_entry.data.get(CONF_ENABLE_POLLING, False)),
                ): bool,
            }
        )

        return self.async_show_form(step_id="init", data_schema=options_schema, last_step=True)
