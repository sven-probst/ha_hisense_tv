"""Component init"""
import asyncio
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.components import mqtt
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er # <-- NEU: Import für Entity Registry
import voluptuous as vol

from .const import ( 
    DOMAIN,
    SERVICE_SEND_KEY,
    SERVICE_SEND_CHANNEL,
    ATTR_KEY,
    ATTR_ENTITY_ID,
    ATTR_CHANNEL,
    CONF_MQTT_OUT,
    DEFAULT_CLIENT_ID,
    )

_LOGGER = logging.getLogger(__name__)

PLATFORMS = ["media_player", "switch", "sensor"]

# Define the schema for the send_key service
SEND_KEY_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_ENTITY_ID): cv.entity_id, # <-- NEU: entity_id ist jetzt erforderlich
        vol.Required(ATTR_KEY): vol.Any(cv.string, vol.All(cv.ensure_list, [cv.string])),
    }
)

SEND_CHANNEL_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_ENTITY_ID): cv.entity_id,
        vol.Required(ATTR_CHANNEL): vol.All(vol.Coerce(int), vol.Range(min=0)), # Kanal als nicht-negative Ganzzahl
    }
)

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry):
    """Set up HisenseTV from a config entry."""
    _LOGGER.debug("async_setup_entry")

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = {}

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Helferfunktion, um den Ziel-ConfigEntry und mqtt_out_prefix zu erhalten
    async def _get_target_config_info(target_entity_id: str):
        entity_registry = er.async_get(hass)
        entity_entry = entity_registry.async_get(target_entity_id)

        if not entity_entry:
            _LOGGER.error("Entität %s nicht gefunden.", target_entity_id)
            return None, None
        
        if not entity_entry.config_entry_id:
            _LOGGER.error("Entität %s ist keinem ConfigEntry zugeordnet.", target_entity_id)
            return None, None

        target_config_entry = hass.config_entries.async_get_entry(entity_entry.config_entry_id)

        if not target_config_entry:
            _LOGGER.error("ConfigEntry für Entität %s nicht gefunden.", target_entity_id)
            return None, None

        mqtt_out_prefix = target_config_entry.data.get(CONF_MQTT_OUT)
        if not mqtt_out_prefix:
            _LOGGER.error("CONF_MQTT_OUT Präfix nicht gefunden für Eintrag %s (Entität: %s)", target_config_entry.entry_id, target_entity_id)
            return None, None
        
        return mqtt_out_prefix, target_config_entry.entry_id # entry_id für Logging

    # Registriere den benutzerdefinierten Dienst "send_key"
    async def async_send_key_service(call: ServiceCall):
        """Behandelt den send_key Dienstaufruf."""
        _LOGGER.debug("Dienst hisense_tv.send_key aufgerufen mit Daten: %s", call.data)
        
        target_entity_id = call.data[ATTR_ENTITY_ID]
        keys_to_send = call.data[ATTR_KEY] # Dies kann ein String oder eine Liste sein

        mqtt_out_prefix, config_entry_id = await _get_target_config_info(target_entity_id)
        if not mqtt_out_prefix:
            return

        client_id_for_topic = DEFAULT_CLIENT_ID 
        formatted_topic = f"{mqtt_out_prefix}/remoteapp/tv/remote_service/{client_id_for_topic}/actions/sendkey"

        # Sicherstellen, dass keys_to_send eine Liste ist, um darüber zu iterieren
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
            # Eine kleine Verzögerung zwischen den Tasten in einer Sequenz ist wichtig
            await asyncio.sleep(0.3) 

    hass.services.async_register(
        DOMAIN, SERVICE_SEND_KEY, async_send_key_service, schema=SEND_KEY_SCHEMA
    )

    # NEU: Registriere den benutzerdefinierten Dienst "send_channel"
    async def async_send_channel_service(call: ServiceCall):
        """Behandelt den send_channel Dienstaufruf."""
        _LOGGER.debug("Dienst hisense_tv.send_channel aufgerufen mit Daten: %s", call.data)
        
        target_entity_id = call.data[ATTR_ENTITY_ID]
        channel_number = str(call.data[ATTR_CHANNEL]) # In String umwandeln, um Ziffern zu iterieren

        mqtt_out_prefix, config_entry_id = await _get_target_config_info(target_entity_id)
        if not mqtt_out_prefix:
            return

        client_id_for_topic = DEFAULT_CLIENT_ID 
        # Verwendet das gleiche sendkey-Topic, da jede Ziffer eine Taste ist
        formatted_topic = f"{mqtt_out_prefix}/remoteapp/tv/remote_service/{client_id_for_topic}/actions/sendkey" 

        for digit in channel_number:
            key_payload = f"KEY_NUM{digit}" # z.B. KEY_NUM1, KEY_NUM2
            _LOGGER.debug("Publishing to topic: %s with payload: %s (for entity: %s)", formatted_topic, key_payload, target_entity_id)
            await mqtt.async_publish(
                hass=hass,
                topic=formatted_topic,
                payload=key_payload,
                retain=False,
            )
            # Entscheidende Verzögerung zwischen den Ziffern, damit der TV sie verarbeitet
            await asyncio.sleep(0.5) # Etwas längere Verzögerung für Kanalziffern

        # Optional: Nach dem Senden aller Ziffern ein SELECT/ENTER senden (üblich für Kanaleingabe)
        # Dies hängt davon ab, wie der TV die Kanaleingabe handhabt.
        # Wenn der TV nach der letzten Ziffer automatisch umschaltet, ist dies möglicherweise nicht erforderlich.
        # Wenn ein "Enter" oder "OK" erforderlich ist, die folgenden Zeilen einkommentieren:
        # await asyncio.sleep(0.5)
        # await mqtt.async_publish(
        #     hass=hass,
        #     topic=formatted_topic,
        #     payload="KEY_SELECT", # Oder KEY_ENTER, je nach TV
        #     retain=False,
        # )

    hass.services.async_register(
        DOMAIN, SERVICE_SEND_CHANNEL, async_send_channel_service, schema=SEND_CHANNEL_SCHEMA
    )

    return True


async def async_unload_entry(hass, entry):
    """Unload HisenseTV config entry."""
    _LOGGER.debug("async_unload_entry")

    # Entferne die benutzerdefinierten Dienste
    hass.services.async_remove(DOMAIN, SERVICE_SEND_KEY)
    hass.services.async_remove(DOMAIN, SERVICE_SEND_CHANNEL) # NEU: Auch diesen Dienst entfernen

    unload_ok = all(
        await asyncio.gather(
            *[
                hass.config_entries.async_forward_entry_unload(entry, platform)
                for platform in PLATFORMS
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