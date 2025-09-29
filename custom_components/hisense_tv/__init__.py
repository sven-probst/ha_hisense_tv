"""Component init"""
import asyncio
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.components import mqtt
from homeassistant.helpers import config_validation as cv
import voluptuous as vol

from .const import DOMAIN, SERVICE_SEND_KEY, ATTR_KEY, CONF_MQTT_OUT 

_LOGGER = logging.getLogger(__name__)

PLATFORMS = ["media_player", "switch", "sensor"]

# Define the schema for the send_key service
SEND_KEY_SCHEMA = vol.Schema(
    {
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
        key = call.data[ATTR_KEY]

        # Konstruiere das Topic
        uid = entry.unique_id or entry.entry_id
        mqtt_out_prefix = entry.data.get(CONF_MQTT_OUT)
        if not mqtt_out_prefix:
            _LOGGER.error("CONF_MQTT_OUT Präfix nicht gefunden für Eintrag %s", entry.entry_id)
            return

        formatted_topic = f"{mqtt_out_prefix}/remoteapp/tv/remote_service/{uid}/actions/sendkey"

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
