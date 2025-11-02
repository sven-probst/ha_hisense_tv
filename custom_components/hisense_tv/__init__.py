"""Component init"""
import asyncio
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall, DOMAIN as HA_DOMAIN
from homeassistant.components import mqtt
from homeassistant.const import CONF_MAC, ATTR_ENTITY_ID
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.service import async_extract_entity_ids
import voluptuous as vol

from homeassistant.components.remote import DOMAIN as REMOTE_DOMAIN
# Import webostv domain to register compatible services
try:
    from homeassistant.components.webostv.const import DOMAIN as WEBOSTV_DOMAIN
except ImportError:
    WEBOSTV_DOMAIN = "webostv"

from .const import ( 
    DOMAIN,
    SERVICE_SEND_KEY,
    SERVICE_SEND_CHANNEL,
    SERVICE_LAUNCH_APP,
    SERVICE_SEND_TEXT,
    SERVICE_SEND_MOUSE_EVENT,
    ATTR_KEY,
    ATTR_CHANNEL,
    ATTR_APP_NAME,
    ATTR_TEXT,
    ATTR_DX,
    ATTR_DY,
    SSDP_ST,
    CONF_MQTT_OUT,
    DEFAULT_CLIENT_ID,
    )

_LOGGER = logging.getLogger(__name__)

# Start with media_player, the others depend on it.
PLATFORMS = ["media_player", "switch", "sensor"]

# Define the schema for the send_key service
SEND_KEY_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_KEY): vol.Any(cv.string, vol.All(cv.ensure_list, [cv.string])),
    },
    extra=vol.ALLOW_EXTRA,
)

SEND_CHANNEL_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_CHANNEL): vol.All(vol.Coerce(int), vol.Range(min=0)),
    },
    extra=vol.ALLOW_EXTRA,
)

LAUNCH_APP_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_APP_NAME): cv.string,
    },
    extra=vol.ALLOW_EXTRA,
)

SEND_TEXT_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_TEXT): cv.string,
    },
    extra=vol.ALLOW_EXTRA,
)

SEND_MOUSE_EVENT_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_DX): vol.Any(str, int, float),
        vol.Required(ATTR_DY): vol.Any(str, int, float),
    },
    extra=vol.ALLOW_EXTRA,
)

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry):
    """Set up HisenseTV from a config entry."""
    _LOGGER.debug("async_setup_entry")

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = {}

    # Add an update listener to reload the entry when options are changed.
    entry.async_on_unload(entry.add_update_listener(async_update_listener))

    # Forward the setup to the platforms.
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Helper function to get the target ConfigEntry and mqtt_out_prefix
    async def _get_target_config_info(target_entity_id: str):
        entity_registry = er.async_get(hass)
        entity_entry = entity_registry.async_get(target_entity_id)

        if not entity_entry:
            _LOGGER.error("Entity %s not found.", target_entity_id)
            return None, None
        
        if not entity_entry.config_entry_id:
            _LOGGER.error("Entity %s is not associated with a config entry.", target_entity_id)
            return None, None

        target_config_entry = hass.config_entries.async_get_entry(entity_entry.config_entry_id)

        if not target_config_entry:
            _LOGGER.error("Config entry for entity %s not found.", target_entity_id)
            return None, None

        mqtt_out_prefix = target_config_entry.data.get(CONF_MQTT_OUT)
        if not mqtt_out_prefix:
            _LOGGER.error("CONF_MQTT_OUT prefix not found for entry %s (entity: %s)", target_config_entry.entry_id, target_entity_id)
            return None, None
        
        return mqtt_out_prefix, target_config_entry

    # Register the custom "send_key" service
    async def async_send_key_service(call: ServiceCall):
        """Handles the send_key service call."""
        _LOGGER.debug("Service hisense_tv.send_key called with data: %s", call.data)
        
        keys_to_send = call.data[ATTR_KEY]
        entity_ids = await async_extract_entity_ids(call)

        for target_entity_id in entity_ids:
            mqtt_out_prefix, target_config_entry = await _get_target_config_info(target_entity_id)
            if not mqtt_out_prefix:
                continue

            client_id_for_topic = target_config_entry.data.get("client_id", DEFAULT_CLIENT_ID)
            formatted_topic = f"{mqtt_out_prefix}/remoteapp/tv/remote_service/{client_id_for_topic}/actions/sendkey"

            keys = keys_to_send
            if isinstance(keys, str):
                keys = [keys]

            for key in keys:
                payload = f"KEY_{key}"
                _LOGGER.debug("Publishing to topic: %s with payload: %s (for entity: %s)", formatted_topic, payload, target_entity_id)
                await mqtt.async_publish(
                    hass=hass,
                    topic=formatted_topic,
                    payload=payload,
                    retain=False,
                )
                await asyncio.sleep(0.5) # A small delay between keys can improve reliability

    hass.services.async_register(
        DOMAIN, SERVICE_SEND_KEY, async_send_key_service, schema=SEND_KEY_SCHEMA
    )

    async def async_send_channel_service(call: ServiceCall):
        """Handles the send_channel service call."""
        _LOGGER.debug("Service hisense_tv.send_channel called with data: %s", call.data)
        
        channel_number = str(call.data[ATTR_CHANNEL])
        entity_ids = await async_extract_entity_ids(call)

        for target_entity_id in entity_ids:
            mqtt_out_prefix, target_config_entry = await _get_target_config_info(target_entity_id)
            if not mqtt_out_prefix:
                continue

            client_id_for_topic = target_config_entry.data.get("client_id", DEFAULT_CLIENT_ID)
            # Use the same sendkey topic, as each digit is a key
            formatted_topic = f"{mqtt_out_prefix}/remoteapp/tv/remote_service/{client_id_for_topic}/actions/sendkey"

            _LOGGER.debug("Sending KEY_EXIT before channel digits for entity: %s", target_entity_id)
            await mqtt.async_publish(
                hass=hass,
                topic=formatted_topic,
                payload="KEY_EXIT",
                retain=False,
            )
            await asyncio.sleep(0.5)

            for digit in channel_number:
                key_payload = f"KEY_{digit}"
                _LOGGER.debug("Publishing to topic: %s with payload: %s (for entity: %s)", formatted_topic, key_payload, target_entity_id)
                await mqtt.async_publish(
                    hass=hass,
                    topic=formatted_topic,
                    payload=key_payload,
                    retain=False,
                )
                await asyncio.sleep(0.5)

    hass.services.async_register(
        DOMAIN, SERVICE_SEND_CHANNEL, async_send_channel_service, schema=SEND_CHANNEL_SCHEMA
    )

    async def async_launch_app_service(call: ServiceCall):
        """Handles the launch_app service call."""
        _LOGGER.debug("Service hisense_tv.launch_app called with data: %s", call.data)

        app_name = call.data[ATTR_APP_NAME]
        entity_ids = await async_extract_entity_ids(call)

        for target_entity_id in entity_ids:
            # Get the media_player entity
            media_player_entity = hass.data["media_player"].get_entity(target_entity_id)

            if not media_player_entity:
                _LOGGER.error("Entity %s not found.", target_entity_id)
                continue

            await media_player_entity.async_launch_app(app_name)

    hass.services.async_register(
        DOMAIN, SERVICE_LAUNCH_APP, async_launch_app_service, schema=LAUNCH_APP_SCHEMA
    )

    async def async_send_text_service(call: ServiceCall):
        """Handles the send_text service call."""
        _LOGGER.debug("Service hisense_tv.send_text called with data: %s", call.data)
        
        text_to_send = call.data[ATTR_TEXT]
        entity_ids = await async_extract_entity_ids(call)

        for target_entity_id in entity_ids:
            mqtt_out_prefix, target_config_entry = await _get_target_config_info(target_entity_id)
            if not mqtt_out_prefix:
                continue

            client_id_for_topic = target_config_entry.data.get("client_id", DEFAULT_CLIENT_ID)
            formatted_topic = f"{mqtt_out_prefix}/remoteapp/tv/remote_service/{client_id_for_topic}/actions/input"

            for char in text_to_send: # pragma: no cover
                if char == ' ':
                    payload = "Lit_SPACE"
                elif char == '\b':
                    payload = "Lit_BACKSPACE"
                else:
                    payload = f"Lit_{char}"
                _LOGGER.debug("Publishing to topic: %s with payload: %s (for entity: %s)", formatted_topic, payload, target_entity_id)
                await mqtt.async_publish(
                    hass=hass,
                    topic=formatted_topic,
                    payload=payload,
                    retain=False,
                )
                await asyncio.sleep(0.1)

    hass.services.async_register(
        DOMAIN, SERVICE_SEND_TEXT, async_send_text_service, schema=SEND_TEXT_SCHEMA
    )

    async def async_send_mouse_event_service(call: ServiceCall):
        """Handles the send_mouse_event service call."""
        _LOGGER.debug("Service hisense_tv.send_mouse_event called with data: %s", call.data)
        
        # Convert dx and dy to integers, rounding from float inputs
        try:
            dx = int(round(float(call.data[ATTR_DX])))
            dy = int(round(float(call.data[ATTR_DY])))
        except (ValueError, TypeError):
            _LOGGER.error("Invalid value for dx or dy: %s", call.data)
            return
        
        entity_ids = await async_extract_entity_ids(call)

        for target_entity_id in entity_ids:
            mqtt_out_prefix, target_config_entry = await _get_target_config_info(target_entity_id)
            if not mqtt_out_prefix:
                continue

            client_id_for_topic = target_config_entry.data.get("client_id", DEFAULT_CLIENT_ID)
            
            # Convert to 16-bit signed hex
            hex_dx = format(dx & 0xFFFF, '04x')
            hex_dy = format(dy & 0xFFFF, '04x')

            formatted_topic = f"{mqtt_out_prefix}/remoteapp/tv/remote_service/{client_id_for_topic}/actions/mouse"
            payload = f"REL_{hex_dx}_{hex_dy}_0000"

            _LOGGER.debug("Publishing to topic: %s with payload: %s (for entity: %s)", formatted_topic, payload, target_entity_id)
            await mqtt.async_publish(
                hass=hass,
                topic=formatted_topic,
                payload=payload,
                retain=False,
            )
            # No sleep needed for mouse events as they are sent in quick succession

    hass.services.async_register(
        DOMAIN, SERVICE_SEND_MOUSE_EVENT, async_send_mouse_event_service, schema=SEND_MOUSE_EVENT_SCHEMA
    )

    # Wrapper service for universal-remote-card compatibility
    async def async_send_command_wrapper_service(call: ServiceCall):
        """
        Handles remote.send_command and dispatches to the correct internal service
        based on a prefix in the 'command' string.
        - "KEY:UP" -> send_key
        - "APP:Netflix" -> launch_app
        - "any other text" -> send_text
        """
        _LOGGER.debug("Wrapper service remote.send_command called with data: %s", call.data)
        
        command_string = call.data.get("command")
        target_entity_id = call.data.get(ATTR_ENTITY_ID)

        if not command_string:
            _LOGGER.warning("remote.send_command called without 'command' data.")
            return

        # Dispatch based on prefix
        if command_string.startswith("KEY:"):
            key = command_string.split(":", 1)[1]
            _LOGGER.debug("Dispatching to send_key with key: %s", key)
            await async_send_key_service(
                ServiceCall(domain=DOMAIN, service=SERVICE_SEND_KEY, data={ATTR_KEY: key, ATTR_ENTITY_ID: target_entity_id})
            )
        elif command_string.startswith("APP:"):
            app_name = command_string.split(":", 1)[1]
            _LOGGER.debug("Dispatching to launch_app with app_name: %s", app_name)
            # launch_app service calls the entity method directly, so we need to do the same
            media_player_entity = hass.data["media_player"].get_entity(target_entity_id)
            if media_player_entity:
                await media_player_entity.async_launch_app(app_name)
            else:
                _LOGGER.error("Could not find media_player entity for dispatching launch_app: %s", target_entity_id)
        else:
            # Default to send_text if no prefix is found
            _LOGGER.debug("Dispatching to send_text with text: %s", command_string)
            await async_send_text_service(
                ServiceCall(
                    domain=DOMAIN, service=SERVICE_SEND_TEXT, data={ATTR_TEXT: command_string, ATTR_ENTITY_ID: target_entity_id}
                )
            )

    # Register the wrapper service under the 'remote' domain
    # This makes the integration natively compatible with the remote card's default keyboard action
    hass.services.async_register(
        REMOTE_DOMAIN, "send_command", async_send_command_wrapper_service
    )

    # --- webOS Compatibility Wrappers ---

    async def async_webostv_button_wrapper(call: ServiceCall):
        """Handles webostv.button and wraps it to remote.send_command."""
        button_pressed = call.data.get("button")
        target_entity_id = call.data.get(ATTR_ENTITY_ID)
        _LOGGER.debug("webOS button wrapper called for button: %s", button_pressed)
        if button_pressed and target_entity_id:
            # Mapping for keys where webOS name differs from Hisense key
            key_map = {
                "LANGUAGE": "LANG",
                "GUIDE":"EPG",
                "BACK":"RETURNS",
                "REWIND":"BACK",
                "FAST_FORWARD":"FORWARDS"
                # Add other special cases here if needed
            }

            # Use the mapped key if it exists, otherwise use the original button name
            hisense_key = key_map.get(button_pressed.upper(), button_pressed.upper())

            # Translate to our KEY: format
            command_string = f"KEY:{hisense_key}"
            
            await async_send_command_wrapper_service(
                ServiceCall(domain=REMOTE_DOMAIN, service="send_command", data={ "command": command_string, ATTR_ENTITY_ID: target_entity_id })
            )

    async def async_webostv_launch_wrapper(call: ServiceCall):
        """Handles webostv.launch and wraps it to remote.send_command."""
        app_id = call.data.get("app_id")
        target_entity_id = call.data.get(ATTR_ENTITY_ID)
        _LOGGER.debug("webOS launch wrapper called for app_id: %s", app_id)
        if app_id and target_entity_id:
            # Translate webOS app_id to our APP: format
            # We capitalize the app name as our dispatcher might expect it
            command_string = f"APP:{app_id.capitalize()}"
            await async_send_command_wrapper_service(
                ServiceCall(domain=REMOTE_DOMAIN, service="send_command", data={ "command": command_string, ATTR_ENTITY_ID: target_entity_id })
            )

    async def async_webostv_command_wrapper(call: ServiceCall):
        """Handles webostv.command (for keyboard) and wraps it to remote.send_command."""
        payload = call.data.get("payload", {})
        text_to_send = payload.get("text")
        target_entity_id = call.data.get(ATTR_ENTITY_ID)
        _LOGGER.debug("webOS command wrapper called for text: %s", text_to_send)
        if text_to_send and target_entity_id:
            # Directly use the text for our send_text dispatcher case
            await async_send_command_wrapper_service(
                ServiceCall(domain=REMOTE_DOMAIN, service="send_command", data={ "command": text_to_send, ATTR_ENTITY_ID: target_entity_id })
            )

    # Register the webOS compatibility services if the domain is available
    if WEBOSTV_DOMAIN:
        _LOGGER.debug("Registering webOS compatibility services.")
        hass.services.async_register(
            WEBOSTV_DOMAIN, "button", async_webostv_button_wrapper
        )
        hass.services.async_register(
            WEBOSTV_DOMAIN, "launch", async_webostv_launch_wrapper
        )
        hass.services.async_register(
            WEBOSTV_DOMAIN, "command", async_webostv_command_wrapper
        )
    else:
        _LOGGER.warning("Could not import webostv domain. Compatibility services will not be available.")


    return True


async def async_update_listener(hass: HomeAssistant, entry: ConfigEntry):
    """Handle options update."""
    _LOGGER.debug("Configuration options for %s have changed, reloading.", entry.title)
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass, entry):
    """Unload HisenseTV config entry."""
    _LOGGER.debug("async_unload_entry")

    # Remove the custom services
    hass.services.async_remove(DOMAIN, SERVICE_SEND_KEY)
    hass.services.async_remove(DOMAIN, SERVICE_SEND_CHANNEL)
    hass.services.async_remove(DOMAIN, SERVICE_LAUNCH_APP)
    hass.services.async_remove(DOMAIN, SERVICE_SEND_TEXT)
    hass.services.async_remove(DOMAIN, SERVICE_SEND_MOUSE_EVENT)
    hass.services.async_remove(REMOTE_DOMAIN, "send_command")
    
    # Unregister webOS compatibility services
    if WEBOSTV_DOMAIN:
        hass.services.async_remove(WEBOSTV_DOMAIN, "button")
        hass.services.async_remove(WEBOSTV_DOMAIN, "launch")
        hass.services.async_remove(WEBOSTV_DOMAIN, "command")

    unload_ok = all(
        await asyncio.gather(
            *[
                hass.config_entries.async_forward_entry_unload(entry, platform)
                for platform in ["media_player", "switch", "sensor"]
            ]
        )
    )
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)
        if not hass.data[DOMAIN]:
            hass.data.pop(DOMAIN)        
    return unload_ok


async def async_setup(hass, config):
    """Set up the HisenseTV integration."""
    _LOGGER.debug("async_setup")
    hass.data.setdefault(DOMAIN, {})
    return True
