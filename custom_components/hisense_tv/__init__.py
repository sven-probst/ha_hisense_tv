"""Component init"""
import asyncio
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.components import mqtt
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er # <-- NEU: Import für Entity Registry
import voluptuous as vol

from .const import DOMAIN, SERVICE_SEND_KEY, ATTR_KEY, ATTR_ENTITY_ID,CONF_MQTT_OUT, DEFAULT_CLIENT_ID

_LOGGER = logging.getLogger(__name__)

PLATFORMS = ["media_player", "switch", "sensor"]

# Define the schema for the send_key service
SEND_KEY_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_ENTITY_ID): cv.entity_id, # <-- NEU: entity_id ist jetzt erforderlich
        vol.Required(ATTR_KEY): cv.string,
    }
)

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry):
    """Set up HisenseTV from a config entry."""
    _LOGGER.debug("async_setup_entry")

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = {}

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Registriere den benutzerdefinierten Dienst "send_key"
    async def async_send_key_service(call: ServiceCall):
        """Behandelt den send_key Dienstaufruf."""
        _LOGGER.debug("Dienst hisense_tv.send_key aufgerufen mit Daten: %s", call.data)
        target_entity_id = call.data[ATTR_ENTITY_ID] # <-- NEU: entity_id aus dem Aufruf holen
        key = call.data[ATTR_KEY]

        entity_registry = er.async_get(hass)
        entity_entry = entity_registry.async_get(target_entity_id)

        if not entity_entry:
            _LOGGER.error("Entität %s nicht gefunden.", target_entity_id)
            return
        
        if not entity_entry.config_entry_id:
            _LOGGER.error("Entität %s ist keinem ConfigEntry zugeordnet.", target_entity_id)
            return

        target_config_entry = hass.config_entries.async_get_entry(entity_entry.config_entry_id)

        if not target_config_entry:
            _LOGGER.error("ConfigEntry für Entität %s nicht gefunden.", target_entity_id)
            return

        # Konstruiere das Topic
        client_id_for_topic = DEFAULT_CLIENT_ID 
        mqtt_out_prefix = target_config_entry.data.get(CONF_MQTT_OUT) # <-- NEU: mqtt_out_prefix vom Ziel-TV
        if not mqtt_out_prefix:
            _LOGGER.error("CONF_MQTT_OUT Präfix nicht gefunden für Eintrag %s", entry.entry_id)
            return

        formatted_topic = f"{mqtt_out_prefix}/remoteapp/tv/remote_service/{client_id_for_topic}/actions/sendkey"

        # Verwende den Home Assistant MQTT-Dienst zum Veröffentlichen
        _LOGGER.debug("Publishing to topic: %s with payload: %s", formatted_topic, f"KEY_{key}") # Zusätzliches Debug-Logging
        # Verwende den Home Assistant MQTT-Dienst zum Veröffentlichen
        await mqtt.async_publish(
            hass=hass,
            topic=formatted_topic,
            payload=f"KEY_{key}", # Füge "KEY_" hinzu, wie in der Automatisierung
            retain=False,
        )
        # Die ursprüngliche Automatisierung hatte eine 300ms Verzögerung.
        # Der TV benötigt möglicherweise noch Zeit zur Verarbeitung.
        # Wenn nötig, kann der Benutzer diese Verzögerung in seiner Home Assistant Automatisierung hinzufügen.
        await asyncio.sleep(0.3) # Optional: wenn der TV eine kleine Pause benötigt

    hass.services.async_register(
        DOMAIN, SERVICE_SEND_KEY, async_send_key_service, schema=SEND_KEY_SCHEMA
    )

    return True


async def async_unload_entry(hass, entry):
    """Unload HisenseTV config entry."""
    _LOGGER.debug("async_unload_entry")

    # Entferne den benutzerdefinierten Dienst
    hass.services.async_remove(DOMAIN, SERVICE_SEND_KEY)

    # Keine custom MQTT-Client-Verbindung zum Trennen, da der HA MQTT-Dienst verwendet wird.


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
        if not hass.data[DOMAIN]: # Wenn keine weiteren Einträge vorhanden sind, entferne den Domain-Schlüssel
            hass.data.pop(DOMAIN)        
    return unload_ok


async def async_setup(hass, config):
    """Set up the HisenseTV integration."""
    _LOGGER.debug("async_setup")
    hass.data.setdefault(DOMAIN, {})
    return True
