"""Hisense TV switch entity"""
import logging

import wakeonlan

from homeassistant.components import mqtt, switch
from homeassistant.components.switch import SwitchDeviceClass, SwitchEntity
from homeassistant.const import (CONF_IP_ADDRESS, CONF_MAC, CONF_NAME, STATE_OFF,
                                 STATE_STANDBY)

from .const import CONF_MQTT_IN, CONF_MQTT_OUT, DEFAULT_NAME, DOMAIN
from .helper import HisenseTvBase

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass, config_entry, async_add_entities):
    """Start HisenseTV switch setup process."""
    _LOGGER.debug("async_setup_entry config: %s", config_entry.data)

    name = config_entry.data[CONF_NAME]
    mac = config_entry.data[CONF_MAC]
    ip_address = config_entry.data.get(CONF_IP_ADDRESS, wakeonlan.BROADCAST_IP)
    mqtt_in = config_entry.data[CONF_MQTT_IN]
    mqtt_out = config_entry.data[CONF_MQTT_OUT]
    uid = config_entry.unique_id
    if uid is None:
        uid = config_entry.entry_id

    entity = HisenseTvSwitch(
        hass=hass,
        name=name,
        mqtt_in=mqtt_in,
        mqtt_out=mqtt_out,
        mac=mac,
        uid=uid,
        ip_address=ip_address,
    )
    async_add_entities([entity])


class HisenseTvSwitch(SwitchEntity, HisenseTvBase):
    """Hisense TV switch entity."""

    def __init__(self, hass, name, mqtt_in, mqtt_out, mac, uid, ip_address):
        HisenseTvBase.__init__(
            self=self,
            hass=hass,
            name=name,
            mqtt_in=mqtt_in,
            mqtt_out=mqtt_out,
            mac=mac,
            uid=uid,
            ip_address=ip_address,
        )
        self._is_on = False

    @property
    def is_on(self):
        """Return true if switch is on."""
        return self._is_on

    def _get_media_player_state(self):
        """Get the state of the associated media_player entity."""
        entity_registry = self.hass.helpers.entity_registry.async_get(self.hass)
        media_player_entity_id = entity_registry.async_get_entity_id(
            "media_player", DOMAIN, self.unique_id
        )
        if media_player_entity_id:
            if (state := self.hass.states.get(media_player_entity_id)):
                return state.state
        return STATE_OFF  # Fallback if state cannot be determined

    async def async_turn_on(self, **kwargs):
        """Turn the entity on."""
        current_state = self._get_media_player_state()

        if current_state not in (STATE_OFF, STATE_STANDBY):
            _LOGGER.debug("Switch is already on, not sending any command.")
            return

        if current_state == STATE_STANDBY:
            _LOGGER.debug("Sending KEY_POWER to turn on TV from standby.")
            await mqtt.async_publish(
                hass=self._hass,
                topic=self._out_topic("/remoteapp/tv/remote_service/%s/actions/sendkey"),
                payload="KEY_POWER",
                retain=False,
            )
        else:  # current_state is STATE_OFF
            if not self._mac:
                _LOGGER.error("MAC address is not set. Cannot send magic packet.")
                return

            _LOGGER.debug("Sending WoL packet to turn on TV from deep sleep.")
            if self._ip_address:
                wakeonlan.send_magic_packet(self._mac, ip_address=self._ip_address)
            else:
                wakeonlan.send_magic_packet(self._mac)

    async def async_toggle(self, **kwargs):
        """Toggle the entity."""
        if self.is_on:
            await self.async_turn_off(**kwargs)
        else:
            await self.async_turn_on(**kwargs)

    async def async_turn_off(self, **kwargs):
        """Turn the entity off."""
        if not self.is_on:
            _LOGGER.debug("Switch is already off/standby, not sending power key.")
            return

        _LOGGER.debug("Sending KEY_POWER to turn off TV.")
        await mqtt.async_publish(
            hass=self._hass,
            topic=self._out_topic("/remoteapp/tv/remote_service/%s/actions/sendkey"),
            payload="KEY_POWER",
            retain=False,
        )

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, self._unique_id)},
            "name": self._name,
            "manufacturer": DEFAULT_NAME,
        }

    @property
    def unique_id(self):
        """Return the unique id of the device."""
        return self._unique_id

    @property
    def name(self):
        return self._name

    @property
    def icon(self):
        return self._icon

    @property
    def device_class(self):
        _LOGGER.debug("device_class")
        return SwitchDeviceClass.SWITCH

    @property
    def should_poll(self):
        """No polling needed."""
        return False

    async def async_will_remove_from_hass(self):
        for unsubscribe in list(self._subscriptions.values()):
            unsubscribe()

    async def async_added_to_hass(self):
        """Subscribe to MQTT events."""
        self._subscriptions["tvsleep"] = await mqtt.async_subscribe(
            self._hass,
            self._in_topic(
                "/remoteapp/mobile/broadcast/platform_service/actions/tvsleep"
            ),
            self.async_update_state,
        )

        self._subscriptions["state"] = await mqtt.async_subscribe(
            self._hass,
            self._in_topic("/remoteapp/mobile/broadcast/ui_service/state"),
            self.async_update_state,
        )

        # Schedule a state update on startup to get the initial state
        self.async_schedule_update_ha_state(force_refresh=True)

    async def async_update_state(self, msg=None):
        """Update the state of the switch and request a HA state update."""
        new_state = self._get_media_player_state()
        self._is_on = new_state not in (STATE_OFF, STATE_STANDBY)
        _LOGGER.debug("Switch state updated to '%s' based on media_player state '%s'", self._is_on, new_state)
        self.async_write_ha_state()
