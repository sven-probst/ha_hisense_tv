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
    CONF_KEY_DELAY as KEY_DELAY,
    DEFAULT_KEY_DELAY,
    DEFAULT_CLIENT_ID,
    )
from .webos_compatibility import async_setup_webos_compatibility, async_unload_webos_compatibility

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

    # Helper function to get the media_player entity
    def _get_media_player_entity(target_entity_id: str):
        """Get the media_player entity object."""
        media_player_entity = hass.data["media_player"].get_entity(target_entity_id)
        if not media_player_entity:
            _LOGGER.error("Could not find the hisense_tv media_player for entity_id '%s'.", target_entity_id)
            return None
        return media_player_entity

    # Register the custom "send_key" service
    async def async_send_key_service(call: ServiceCall):
        """Handles the send_key service call."""
        _LOGGER.debug("Service hisense_tv.send_key called with data: %s", call.data)
        
        keys_to_send = call.data[ATTR_KEY]
        entity_ids = await async_extract_entity_ids(call)

        for entity_id in entity_ids:
            media_player = _get_media_player_entity(entity_id)
            if not media_player:
                continue
            keys = keys_to_send
            await media_player.async_send_keys(keys if isinstance(keys, list) else [keys])

    hass.services.async_register(
        DOMAIN, SERVICE_SEND_KEY, async_send_key_service, schema=SEND_KEY_SCHEMA
    )

    async def async_send_channel_service(call: ServiceCall):
        """Handles the send_channel service call."""
        _LOGGER.debug("Service hisense_tv.send_channel called with data: %s", call.data)
        
        channel_number = str(call.data[ATTR_CHANNEL])
        entity_ids = await async_extract_entity_ids(call)

        for entity_id in entity_ids:
            media_player = _get_media_player_entity(entity_id)
            if media_player:
                await media_player.async_send_channel(channel_number)

    hass.services.async_register(
        DOMAIN, SERVICE_SEND_CHANNEL, async_send_channel_service, schema=SEND_CHANNEL_SCHEMA
    )

    async def async_launch_app_service(call: ServiceCall):
        """Handles the launch_app service call."""
        _LOGGER.debug("Service hisense_tv.launch_app called with data: %s", call.data)

        app_name = call.data[ATTR_APP_NAME]
        entity_ids = await async_extract_entity_ids(call)

        for target_entity_id in entity_ids:
            media_player_entity = _get_media_player_entity(target_entity_id)
            if not media_player_entity:
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

        for entity_id in entity_ids:
            media_player = _get_media_player_entity(entity_id)
            if media_player:
                await media_player.async_send_text(text_to_send)

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
            media_player = _get_media_player_entity(target_entity_id)
            if not media_player:
                continue
            # Use the throttled method on the media_player entity
            await media_player.async_send_mouse_event(dx, dy)

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
                ServiceCall(hass, domain=DOMAIN, service=SERVICE_SEND_KEY, data={ATTR_KEY: key, ATTR_ENTITY_ID: target_entity_id})
            )
        elif command_string.startswith("APP:"):
            app_name = command_string.split(":", 1)[1]
            _LOGGER.debug("Dispatching to launch_app with app_name: %s", app_name)
            # launch_app service calls the entity method directly, so we need to do the same
            media_player_entity = _get_media_player_entity(target_entity_id)
            if media_player_entity:
                await media_player_entity.async_launch_app(app_name)
            else:
                _LOGGER.error("Could not find media_player entity for dispatching launch_app: %s", target_entity_id)
        else:
            # Default to send_text if no prefix is found
            _LOGGER.debug("Dispatching to send_text with text: %s", command_string)
            await async_send_text_service(
                ServiceCall(
                    hass, domain=DOMAIN, service=SERVICE_SEND_TEXT, data={ATTR_TEXT: command_string, ATTR_ENTITY_ID: target_entity_id}
                )
            )

    # Register the wrapper service under the 'remote' domain
    # This makes the integration natively compatible with the remote card's default keyboard action
    hass.services.async_register(
        REMOTE_DOMAIN, "send_command", async_send_command_wrapper_service
    )

    # Setup the WebOS compatibility layer, passing the helper function
    await async_setup_webos_compatibility(hass, _get_media_player_entity)

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

    # Unload the WebOS compatibility layer
    await async_unload_webos_compatibility(hass)

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
