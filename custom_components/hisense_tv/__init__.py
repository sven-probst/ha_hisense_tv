"""Component init"""
import asyncio
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.components import mqtt
from homeassistant.const import CONF_MAC
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er
import voluptuous as vol

from .const import ( 
    DOMAIN,
    SERVICE_SEND_KEY,
    SERVICE_SEND_CHANNEL,
    SERVICE_LAUNCH_APP,
    SERVICE_SEND_TEXT,
    ATTR_KEY,
    ATTR_ENTITY_ID,
    ATTR_CHANNEL,
    ATTR_APP_NAME,
    ATTR_TEXT,
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
        vol.Required(ATTR_ENTITY_ID): cv.entity_id,
        vol.Required(ATTR_KEY): vol.Any(cv.string, vol.All(cv.ensure_list, [cv.string])),
    }
)

SEND_CHANNEL_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_ENTITY_ID): cv.entity_id,
        vol.Required(ATTR_CHANNEL): vol.All(vol.Coerce(int), vol.Range(min=0)),
    }
)

LAUNCH_APP_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_ENTITY_ID): cv.entity_id,
        vol.Required(ATTR_APP_NAME): cv.string,
    }
)

SEND_TEXT_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_ENTITY_ID): cv.entity_id,
        vol.Required(ATTR_TEXT): cv.string,
    }
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
        
        target_entity_id = call.data[ATTR_ENTITY_ID]
        keys_to_send = call.data[ATTR_KEY]

        mqtt_out_prefix, target_config_entry = await _get_target_config_info(target_entity_id)
        if not mqtt_out_prefix:
            return

        client_id_for_topic = target_config_entry.data.get("client_id", DEFAULT_CLIENT_ID)
        formatted_topic = f"{mqtt_out_prefix}/remoteapp/tv/remote_service/{client_id_for_topic}/actions/sendkey"

        if isinstance(keys_to_send, str):
            keys_to_send = [keys_to_send]

        for key in keys_to_send:
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
        
        target_entity_id = call.data[ATTR_ENTITY_ID]
        channel_number = str(call.data[ATTR_CHANNEL])

        mqtt_out_prefix, target_config_entry = await _get_target_config_info(target_entity_id)
        if not mqtt_out_prefix:
            return

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

        target_entity_id = call.data[ATTR_ENTITY_ID]
        app_name = call.data[ATTR_APP_NAME]

        # Get the media_player entity
        media_player_entity = hass.data["media_player"].get_entity(target_entity_id)

        if not media_player_entity:
            _LOGGER.error("Entity %s not found.", target_entity_id)
            return

        await media_player_entity.async_launch_app(app_name)

    hass.services.async_register(
        DOMAIN, SERVICE_LAUNCH_APP, async_launch_app_service, schema=LAUNCH_APP_SCHEMA
    )

    async def async_send_text_service(call: ServiceCall):
        """Handles the send_text service call."""
        _LOGGER.debug("Service hisense_tv.send_text called with data: %s", call.data)
        
        target_entity_id = call.data[ATTR_ENTITY_ID]
        text_to_send = call.data[ATTR_TEXT]

        mqtt_out_prefix, target_config_entry = await _get_target_config_info(target_entity_id)
        if not mqtt_out_prefix:
            return

        client_id_for_topic = target_config_entry.data.get("client_id", DEFAULT_CLIENT_ID)
        formatted_topic = f"{mqtt_out_prefix}remoteapp/tv/remote_service/{client_id_for_topic}$vidaa_common/actions/input"

        for char in text_to_send:
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
