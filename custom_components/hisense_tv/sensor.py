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
        self.has_entity_name = True
        # This ensures the entity has a unique ID within the device.
        self._attr_unique_id = f"{uid}_picturesettings"

        super().__init__(
            hass=hass,
            name=None,  # This is a child entity; its name is set via _attr_name below.
            mqtt_in=mqtt_in,
            mqtt_out=mqtt_out,
            mac=mac,
            uid=uid,
            ip_address=ip_address,
        )
        # This will be the name of the sensor entity.
        self._attr_name = "Picture Settings"
        # Store the device's unique_id for the device_info property.
        self._device_unique_id = uid
        self._is_available = False
        self._state = {}
        self._device_info = {}  # store "getdeviceinfo"
        self._tv_info = {}  # store "gettvinfo"
        self._last_trigger = dt_util.utcnow()
        self._force_trigger = False

    async def async_will_remove_from_hass(self):
        for unsubscribe in list(self._subscriptions.values()):
            unsubscribe()

    async def async_added_to_hass(self):
        self._subscriptions = {}
        self._subscriptions["tvsleep"] = await mqtt.async_subscribe(
            self._hass,
            self._in_topic("/remoteapp/broadcast/platform_service/actions/tvsleep"),
            self._message_received_turnoff,
        )

        self._subscriptions["state"] = await mqtt.async_subscribe(
            self._hass,
            self._in_topic("/remoteapp/broadcast/ui_service/state"),
            self._message_received_turnon,
        )

        self._subscriptions["picturesettings"] = await mqtt.async_subscribe(
            self._hass,
            self._in_topic("/remoteapp/%s/platform_service/data/picturesetting"),
            self._message_received,
        )

        self._subscriptions["picturesettings_value"] = await mqtt.async_subscribe(
            self._hass,
            self._in_topic(
                "/remoteapp/broadcast/platform_service/data/picturesetting"
            ),
            self._message_received_value,
        )

        # subscribe topic for "gettvinfo"
        self._subscriptions["tvinfo"] = await mqtt.async_subscribe(
            self._hass,
            self._in_topic("/remoteapp/%s/platform_service/data/gettvinfo"),
            self._message_received_tvinfo,
        )

        # subscribe topic for "getdeviceinfo"
        self._subscriptions["deviceinfo"] = await mqtt.async_subscribe(
            self._hass,
            self._in_topic("/remoteapp/%s/platform_service/data/getdeviceinfo"),
            self._message_received_deviceinfo,
        )

        # Proactively request picture settings when the entity is added.
        # This ensures the sensor becomes available if the TV is already on.
        _LOGGER.debug("Proactively requesting picture settings for sensor.")
        await mqtt.async_publish(
            hass=self._hass,
            topic=self._out_topic(
                "/remoteapp/tv/platform_service/%s/actions/picturesetting"
            ),
            payload="",
            retain=False,
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


    async def _message_received(self, msg):
        self._is_available = True
        try:
            payload = json.loads(msg.payload)
        except JSONDecodeError:
            payload = {}
        _LOGGER.debug("_message_received R(%s):\n%s", msg.retain, payload)
        self._state = {
            s.get("menu_id"): {"name": s.get("menu_name"), "value": s.get("menu_value")}
            for s in payload.get("menu_info", [])
        }
        self.async_write_ha_state()

    async def _message_received_value(self, msg):
        self._is_available = True
        self._force_trigger = True
        try:
            payload = json.loads(msg.payload)
        except JSONDecodeError:
            payload = {}
        _LOGGER.debug("_message_received_value R(%s):\n%s", msg.retain, payload)
        if "notify_value_changed" == payload.get("action"):
            menu_id = payload.get("menu_id")
            entry = self._state.get(menu_id)
            if entry is not None:
                entry["value"] = payload.get("menu_value")
            else:
                _LOGGER.debug("_message_received_value menu_id not found: %s", menu_id)

        self.async_write_ha_state()

    @property
    def native_value(self):
        """Return the number of picture settings found as the state."""
        return len(self._state) if self._is_available else None

    @property
    def available(self):
        """Return True if entity is available."""
        return self._is_available

    @property
    def extra_state_attributes(self):
        """Return the state attributes of the sensor."""
        attributes = {v["name"]: v["value"] for v in self._state.values()}
        attributes["device_info"] = self._device_info
        attributes["ip_address"] = self._ip_address
        attributes["tv_info"] = self._tv_info
        return attributes

    @property
    def device_info(self):
        """Return the device info for the sensor."""
        # This links the sensor to the main media_player device.
        return {
            "identifiers": {(DOMAIN, self._device_unique_id)},
        }

    async def async_update(self):
        """Update is handled by MQTT subscriptions, not polling. But we can request an update."""
        _LOGGER.debug("async_update called for sensor")
        self._force_trigger = True
