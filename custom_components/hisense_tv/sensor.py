"""Support for Picture Settings sensors."""
from datetime import timedelta
import json
from json.decoder import JSONDecodeError
import logging
from wakeonlan import BROADCAST_IP

from homeassistant.components import mqtt
from homeassistant.components.sensor import SensorEntity
from homeassistant.const import CONF_IP_ADDRESS, CONF_MAC, CONF_NAME
from homeassistant.util import dt as dt_util

from .const import CONF_MQTT_IN, CONF_MQTT_OUT, DEFAULT_NAME, DOMAIN
from .helper import HisenseTvBase

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass, config_entry, async_add_entities):
    """Set up MQTT sensors dynamically through MQTT discovery."""
    _LOGGER.debug("async_setup_entry config: %s", config_entry.data)

    name = config_entry.data[CONF_NAME]
    mac = config_entry.data[CONF_MAC]
    ip_address = config_entry.data.get(CONF_IP_ADDRESS, BROADCAST_IP)
    mqtt_in = config_entry.data[CONF_MQTT_IN]
    mqtt_out = config_entry.data[CONF_MQTT_OUT]
    uid = config_entry.unique_id
    if uid is None:
        uid = config_entry.entry_id

    entity = HisenseTvSensor(
        hass=hass,
        # The name will be set by the entity itself.
        # We pass the base name from the config.
        name=name,
        mqtt_in=mqtt_in,
        mqtt_out=mqtt_out,
        mac=mac,
        # Use the same unique_id as the media_player to link them to the same device.
        # The sensor's unique identity will be defined by its name/object_id.
        uid=uid,
        ip_address=ip_address,
    )
    async_add_entities([entity])


class HisenseTvSensor(SensorEntity, HisenseTvBase):
    """Representation of a sensor that can be updated using MQTT."""

    def __init__(self, hass, name, mqtt_in, mqtt_out, mac, uid, ip_address):
        super().__init__(
            hass=hass,
            name=name,
            mqtt_in=mqtt_in,
            mqtt_out=mqtt_out,
            mac=mac,
            uid=uid,
            ip_address=ip_address,
        )
        self.has_entity_name = True
        # This will be the name of the sensor entity.
        self._attr_name = "TV Info"
        # This ensures the entity has a unique ID within the device.
        self._attr_unique_id = f"{uid}_info"
        self._is_available = False
        self._state = {}
        self._device_info = {}  # store "getdeviceinfo"
        self._tv_info = {}  # store "gettvinfo"
        self._platform_capability = {}  # store "getplatformcapbility"
        self._ui_capability = {}  # store "capability"
        self._picture_settings = {}  # store "picturesetting"
        self._last_trigger = dt_util.utcnow()
        self._force_trigger = False

    async def async_will_remove_from_hass(self):
        for unsubscribe in list(self._subscriptions.values()):
            unsubscribe()

    async def async_added_to_hass(self):
        self._subscriptions["tvsleep"] = await mqtt.async_subscribe(
            self._hass,
            self._in_topic(
                "/remoteapp/mobile/broadcast/platform_service/actions/tvsleep"
            ),
            self._message_received_turnoff,
        )

        self._subscriptions["state"] = await mqtt.async_subscribe(
            self._hass,
            self._in_topic("/remoteapp/mobile/broadcast/ui_service/state"),
            self._message_received_turnon,
        )

        # subscribe topic for "gettvinfo"
        self._subscriptions["tvinfo"] = await mqtt.async_subscribe(
            self._hass,
            self._in_topic("/remoteapp/mobile/%s/platform_service/data/gettvinfo"),
            self._message_received_tvinfo,
        )

        # subscribe topic for "getdeviceinfo"
        self._subscriptions["deviceinfo"] = await mqtt.async_subscribe(
            self._hass,
            self._in_topic("/remoteapp/mobile/%s/platform_service/data/getdeviceinfo"),
            self._message_received_deviceinfo,
        )

        # subscribe topic for "getplatformcapbility"
        self._subscriptions["platformcapability"] = await mqtt.async_subscribe(
            self._hass,
            self._in_topic("/remoteapp/mobile/%s/platform_service/data/getplatformcapbility"),
            self._message_received_platformcapability,
        )

        # subscribe topic for "capability"
        self._subscriptions["uicapability"] = await mqtt.async_subscribe(
            self._hass,
            self._in_topic("/remoteapp/mobile/%s/ui_service/data/capability"),
            self._message_received_uicapability,
        )

        # subscribe topic for "picturesetting"
        self._subscriptions["picturesetting_broadcast"] = await mqtt.async_subscribe(
            self._hass,
            self._in_topic(
                "/remoteapp/mobile/broadcast/platform_service/data/picturesetting"
            ),
            self._message_received_picturesetting_broadcast,
        )

    async def _message_received_turnoff(self, msg):
        _LOGGER.debug("message_received_turnoff")
        self._is_available = False
        self.async_write_ha_state()

    async def _message_received_turnon(self, msg):
        _LOGGER.debug("message_received_turnon")
        if msg.retain:
            _LOGGER.debug("message_received_turnon - skip retained message")
            return

        try:
            payload = json.loads(msg.payload)
            statetype = payload.get("statetype")
        except (JSONDecodeError, AttributeError):
            # If payload is not JSON or not a dict, assume TV is on
            statetype = None

        # The "state" topic is also used to signal the TV is going to standby.
        # We only want to query the TV if it's not turning off.
        if statetype == "fake_sleep_0":
            _LOGGER.debug("Sensor received fake_sleep_0, marking as unavailable.")
            self._is_available = False
            self.async_write_ha_state()
            return

        _LOGGER.debug(
            "Sensor received state update, marking as available and refreshing info."
        )
        self._is_available = True
        self._force_trigger = True
        self.async_write_ha_state()

        # publish "getdeviceinfo"-Topic
        await mqtt.async_publish(
            hass=self._hass,
            topic=self._out_topic(
                "/remoteapp/tv/platform_service/%s/actions/getdeviceinfo"
            ),
            payload="",
            retain=False,
        )
        # publish "gettvinfo"-Topic
        await mqtt.async_publish(
            hass=self._hass,
            topic=self._out_topic(
                "/remoteapp/tv/platform_service/%s/actions/gettvinfo"
            ),
            payload="",
            retain=False,
        )
        # publish "getplatformcapbility"-Topic
        await mqtt.async_publish(
            hass=self._hass,
            topic=self._out_topic(
                "/remoteapp/tv/platform_service/%s/actions/getplatformcapbility"
            ),
            payload="",
            retain=False,
        )
        # publish "capability"-Topic
        await mqtt.async_publish(
            hass=self._hass,
            topic=self._out_topic(
                "/remoteapp/tv/ui_service/%s/actions/capability"
            ),
            payload="",
            retain=False,
        )

    async def _message_received_deviceinfo(self, msg):
        """received message 'getdeviceinfo'."""
        try:
            payload = json.loads(msg.payload)
        except JSONDecodeError:
            _LOGGER.warning("error parsing 'getdeviceinfo': %s", msg.payload)
            return

        _LOGGER.debug("Received deviceinfo: %s", payload)
        self._device_info = payload
        self.async_write_ha_state()

    async def _message_received_tvinfo(self, msg):
        """received message 'gettvinfo'."""
        try:
            payload = json.loads(msg.payload)
        except JSONDecodeError:
            _LOGGER.warning("error parsing 'gettvinfo': %s", msg.payload)
            return

        _LOGGER.debug("Received tvinfo: %s", payload)
        self._tv_info = payload
        self.async_write_ha_state()

    async def _message_received_platformcapability(self, msg):
        """received message 'getplatformcapbility'."""
        try:
            payload = json.loads(msg.payload)
        except JSONDecodeError:
            _LOGGER.warning("error parsing 'getplatformcapbility': %s", msg.payload)
            return
        _LOGGER.debug("Received platformcapability: %s", payload)
        self._platform_capability = payload
        self.async_write_ha_state()

    async def _message_received_uicapability(self, msg):
        """received message 'capability'."""
        try:
            payload = json.loads(msg.payload)
        except JSONDecodeError:
            _LOGGER.warning("error parsing 'capability': %s", msg.payload)
            return
        _LOGGER.debug("Received uicapability: %s", payload)
        self._ui_capability = payload
        self.async_write_ha_state()

    async def _message_received_picturesetting_broadcast(self, msg):
        """received broadcast message 'picturesetting'."""
        _LOGGER.info("Received picturesetting broadcast: %s", msg.payload)
        try:
            payload = json.loads(msg.payload)
            self._picture_settings = payload
            self.async_write_ha_state()
        except JSONDecodeError:
            _LOGGER.warning("error parsing 'picturesetting' broadcast: %s", msg.payload)

    @property
    def native_value(self):
        """Return the firmware version as the state."""
        return self._device_info.get("tv_version") if self._is_available else None

    @property
    def available(self):
        """Return True if entity is available."""
        return self._is_available

    @property
    def extra_state_attributes(self):
        """Return the state attributes of the sensor."""
        attributes = {}
        attributes["device_info"] = self._device_info
        attributes["tv_info"] = self._tv_info
        attributes["platform_capability"] = self._platform_capability
        attributes["ui_capability"] = self._ui_capability
        attributes["picture_settings"] = self._picture_settings
        attributes["ip_address"] = self._ip_address
        return attributes

    @property
    def device_info(self):
        """Return the device info for the sensor."""
        # This links the sensor to the main media_player device.
        return {
            "identifiers": {(DOMAIN, self._unique_id)},
        }

    async def async_update(self):
        """Update is handled by MQTT subscriptions, not polling. But we can request an update."""
        _LOGGER.debug("async_update called for sensor")
        self._force_trigger = True