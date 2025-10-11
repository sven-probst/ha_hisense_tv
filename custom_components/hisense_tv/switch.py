"""Hisense TV switch entity"""
import logging

from homeassistant.components.media_player import SERVICE_TURN_OFF

from homeassistant.components import mqtt, switch
from homeassistant.components.media_player import DOMAIN as MEDIA_PLAYER_DOMAIN, SERVICE_TURN_ON
from homeassistant.components.switch import SwitchDeviceClass, SwitchEntity
from homeassistant.const import (CONF_IP_ADDRESS, CONF_MAC, CONF_NAME, STATE_OFF,
                                 STATE_STANDBY, ATTR_ENTITY_ID, EVENT_HOMEASSISTANT_START)
from homeassistant.core import callback, Event
from homeassistant.helpers import device_registry as dr, entity_registry as er, event

from .const import CONF_MQTT_IN, CONF_MQTT_OUT, DEFAULT_NAME, DOMAIN

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass, config_entry, async_add_entities):
    """Start HisenseTV switch setup process."""
    _LOGGER.debug("async_setup_entry config: %s", config_entry.data)

    uid = config_entry.unique_id
    if uid is None:
        uid = config_entry.entry_id

    entity = HisenseTvSwitch(
        hass=hass,        
        name=config_entry.data[CONF_NAME],
        uid=uid,
    )
    async_add_entities([entity])


class HisenseTvSwitch(SwitchEntity):
    """Hisense TV switch entity."""

    def __init__(self, hass, name, uid):
        # The switch entity is simpler and primarily delegates to the media_player.
        # It doesn't need its own MQTT subscriptions or a full HisenseTvBase initialization.
        self._hass = hass
        self._name = name
        self._device_unique_id = uid # Store the device's unique_id for lookups
        self._attr_unique_id = f"{uid}_power"
        self._is_on = False
        self._attr_icon = "mdi:power"
        self._media_player_entity_id = None
        # Set the entity name, which will be prefixed by the device name.
        self._attr_name = "Power"

    @property
    def is_on(self):
        """Return true if switch is on."""
        return self._is_on

    def _get_media_player_state(self):
        """Get the state of the associated media_player entity."""
        entity_registry = er.async_get(self.hass)
        if not self._media_player_entity_id:
            self._media_player_entity_id = entity_registry.async_get_entity_id("media_player", DOMAIN, self._device_unique_id)

        if self._media_player_entity_id and (state := self.hass.states.get(self._media_player_entity_id)):
            return state.state
        return STATE_OFF  # Fallback if state cannot be determined

    def _get_media_player_entity(self):
        """Get the media_player entity object."""
        entity_registry = er.async_get(self.hass)
        if not self._media_player_entity_id:
            self._media_player_entity_id = entity_registry.async_get_entity_id("media_player", DOMAIN, self._device_unique_id)

        if self._media_player_entity_id:
            return self.hass.data["media_player"].get_entity(self._media_player_entity_id)
        
        _LOGGER.error("Could not find the hisense_tv media_player for device with unique_id '%s'.", self.unique_id)
        return None

    async def async_turn_on(self, **kwargs):
        """Turn the entity on."""
        media_player = self._get_media_player_entity()
        if not media_player:
            _LOGGER.error("Could not find media_player entity to turn on.")
            return

        _LOGGER.debug("Calling async_turn_on for media_player: %s", media_player.entity_id)
        await media_player.async_turn_on()

        # Optimistically set the state to on.
        self._is_on = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs):
        """Turn the entity off."""
        media_player = self._get_media_player_entity()
        if not media_player:
            _LOGGER.error("Could not find media_player entity to turn off.")
            return

        _LOGGER.debug("Calling async_turn_off for media_player: %s", media_player.entity_id)
        await media_player.async_turn_off()

        # Optimistically set the state to off.
        self._is_on = False
        self.async_write_ha_state()

    @property
    def device_info(self):
        # The switch entity shares device info with the media_player
        return {
            "identifiers": {(DOMAIN, self._device_unique_id)},
        }

    @property
    def device_class(self):
        _LOGGER.debug("device_class")
        return SwitchDeviceClass.SWITCH

    @property
    def should_poll(self):
        """No polling needed."""
        return False

    @callback
    def _async_update_state(self, event: Event | None = None) -> None:
        """Update the switch's state based on the media_player's state."""
        media_player_state = self._get_media_player_state()
        new_state = media_player_state not in (STATE_OFF, STATE_STANDBY, None)
        
        if self._is_on != new_state:
            _LOGGER.debug("Switch state updated to '%s' based on media_player state '%s'", new_state, media_player_state)
            self._is_on = new_state
            self.async_write_ha_state()

    async def async_added_to_hass(self):
        """Run when entity about to be added to hass, and register state change listener."""
        await super().async_added_to_hass()

        # Get the media_player entity_id
        entity_registry = er.async_get(self.hass)
        self._media_player_entity_id = entity_registry.async_get_entity_id("media_player", DOMAIN, self._device_unique_id)

        if not self._media_player_entity_id:
            _LOGGER.warning("Could not find the hisense_tv media_player for device with unique_id '%s'.", self._device_unique_id)
            return

        # Listen for state changes of the media_player
        self.async_on_remove(
            event.async_track_state_change_event(
                self.hass, self._media_player_entity_id, self._async_update_state
            )
        )

        # Update state on startup
        # This listener cleans itself up, so we don't need to track it for removal
        # in the same way as persistent listeners.
        if self.hass.is_running:
            self._async_update_state()
        else:
            self.hass.bus.async_listen_once(EVENT_HOMEASSISTANT_START, self._async_update_state)
