"""Hisense TV media player entity."""
from datetime import timedelta
import functools
import asyncio
import json
from json.decoder import JSONDecodeError
import logging
import re

import voluptuous as vol
import wakeonlan

from homeassistant.components import mqtt
from homeassistant.util import dt as dt_util
from homeassistant.components.media_player import (
    MediaPlayerDeviceClass,
    PLATFORM_SCHEMA,
    BrowseMedia,
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaType,
    MediaClass
)
from homeassistant.config_entries import SOURCE_IMPORT
from homeassistant.helpers.debounce import Debouncer
from homeassistant.const import (
    CONF_IP_ADDRESS,
    CONF_MAC,
    CONF_NAME,
    STATE_OFF,
    STATE_STANDBY,
    STATE_ON,
    STATE_PLAYING,
) 
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.helpers import config_validation as cv, device_registry as dr, entity_registry as er

from .const import (
    ATTR_CODE,
    CONF_MQTT_IN,
    CONF_MQTT_OUT,
    DEFAULT_NAME,
    DOMAIN,
    CONF_KEY_DELAY,
)
from .helper import HisenseTvBase, mqtt_pub_sub

REQUIREMENTS = []

_LOGGER = logging.getLogger(__name__) 

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend(
    {
        vol.Required(CONF_MAC): cv.string,
        vol.Optional(CONF_NAME, default=DEFAULT_NAME): cv.string,
        vol.Optional(CONF_IP_ADDRESS): cv.string,
        vol.Required(CONF_MQTT_IN): cv.string,
        vol.Required(CONF_MQTT_OUT): cv.string,
    }
)

AUTHENTICATE_SCHEMA = {
    vol.Required(ATTR_CODE): cv.Number,
}


async def async_setup_platform(hass, config, async_add_devices, discovery_info=None):
    """Set up the media player platform."""

    if discovery_info:
        # Now handled by zeroconf in the config flow
        _LOGGER.debug("async_setup_platform with discovery_info")
        return

    mac = config[CONF_MAC]
    for entry in hass.config_entries.async_entries(DOMAIN):
        _LOGGER.debug("entry: %s", entry.data)
        if entry.data[CONF_MAC] == mac:
            return

    entry_data = {
        CONF_NAME: config[CONF_NAME],
        CONF_MAC: config[CONF_MAC],
        CONF_IP_ADDRESS: config.get(CONF_IP_ADDRESS, wakeonlan.BROADCAST_IP),
        CONF_MQTT_IN: config[CONF_MQTT_IN],
        CONF_MQTT_OUT: config[CONF_MQTT_OUT],
    }

    hass.async_create_task(
        hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_IMPORT}, data=entry_data
        )
    )


async def async_setup_entry(hass, config_entry, async_add_entities):
    """Set up the media player entry."""
    _LOGGER.debug("async_setup_entry config: %s", config_entry.data)

    name = config_entry.data[CONF_NAME]
    mac = config_entry.data[CONF_MAC]
    ip_address = config_entry.data.get(CONF_IP_ADDRESS, wakeonlan.BROADCAST_IP)
    mqtt_in = config_entry.data[CONF_MQTT_IN]
    mqtt_out = config_entry.data[CONF_MQTT_OUT]
    uid = config_entry.unique_id
    key_delay = config_entry.options.get(CONF_KEY_DELAY, 0.2)
    if uid is None:
        uid = config_entry.entry_id

    entity = HisenseTvEntity(
        hass=hass,
        name=name,
        mqtt_in=mqtt_in,
        mqtt_out=mqtt_out,
        mac=mac,
        uid=uid,
        ip_address=ip_address,
        key_delay=key_delay,
    )
    async_add_entities([entity])


class HisenseTvEntity(MediaPlayerEntity, HisenseTvBase):
    """HisenseTV Media Player entity."""

    def __init__(
        self,
        hass,
        name: str,
        mqtt_in: str,
        mqtt_out: str,
        mac: str,
        uid: str,
        ip_address: str,
        key_delay: float,
    ):
        super().__init__(
            hass=hass,
            name=name,
            mqtt_in=mqtt_in,
            mqtt_out=mqtt_out,
            mac=mac,
            uid=uid,
            ip_address=ip_address,
        )
        # Set a specific name to avoid conflicts with other integrations like DLNA.
        # This results in a clean entity_id like 'media_player.living_room_tv_control'.
        self._attr_name = f"{name} Control"

        self._muted = False
        self._key_delay = key_delay
        self._attr_unique_id = uid
        self._volume = 0
        self._state = STATE_OFF
        # Request sourcelist on init if TV is already on 
        self._hass.async_create_task(self._check_tv_state())

        self._source_name = None
        self._source_id = None
        self._source_list = {"App": {}}
        self._title = None
        self._channel_name = None
        self._channel_num = None
        self._channel_infos = {}
        self._duration = None
        self._app_list = {}
        self._last_trigger = dt_util.utcnow()
        self._force_trigger = False
        self._endtime = None
        self._starttime = None
        self._position = None
        self._media_position_updated_at = dt_util.utcnow()
        self._pending_poll_response = False
        self._missed_polls = 0
        self._input_text = None  # store "bwsinputdata"

        # Mouse event throttling
        self._mouse_dx_total = 0
        self._mouse_dy_total = 0
        self._mouse_throttle_timer = None
        self._mouse_throttle_interval = 0.1  # Send updates every 100ms

        self._sourcelist_requested = False

    @property
    def should_poll(self):
        """Poll for non media_player updates."""
        return True

    @property
    def media_content_type(self):
        """Content type of current playing media."""
        _LOGGER.debug("media_content_type")
        # return MEDIA_TYPE_CHANNEL
        return MediaType.TVSHOW

    @property
    def device_class(self):
        """Set the device class to TV."""
        _LOGGER.debug("device_class")
        return MediaPlayerDeviceClass.TV

    @property
    def supported_features(self):
        """Flag media player features that are supported."""
        _LOGGER.debug("supported_features")
        return (
            MediaPlayerEntityFeature.SELECT_SOURCE
            | MediaPlayerEntityFeature.TURN_ON
            | MediaPlayerEntityFeature.TURN_OFF 
            | MediaPlayerEntityFeature.VOLUME_MUTE
            | MediaPlayerEntityFeature.VOLUME_STEP
            | MediaPlayerEntityFeature.VOLUME_SET
            | MediaPlayerEntityFeature.BROWSE_MEDIA
            | MediaPlayerEntityFeature.PLAY_MEDIA
            | MediaPlayerEntityFeature.SEEK
        )

    @property
    def state(self):
        """Return the state of the device."""
        _LOGGER.debug("state %s", self._state)
        return self._state

    async def async_update(self):
        """Get the latest data and updates the states."""
        # Standard polling interval check
        if (
            not self._force_trigger
            and dt_util.utcnow() - self._last_trigger < timedelta(minutes=1)
        ):
            _LOGGER.debug("Skip update")
            return

        _LOGGER.debug("Running update. Current state: %s, Pending response: %s, Missed polls: %d", 
                     self._state, self._pending_poll_response, self._missed_polls)

        if self._pending_poll_response:
            self._missed_polls += 1
            # If we miss 2 consecutive polls, we assume the TV is off.
            if self._missed_polls >= 2 and self._state != STATE_OFF:
                _LOGGER.info("TV did not respond to 2 consecutive state requests. Assuming it's off.")
                self._state = STATE_OFF
                self.async_write_ha_state()
        else:
            # If we received a response, reset the missed polls counter.
            self._missed_polls = 0

        self._force_trigger = False
        self._last_trigger = dt_util.utcnow()

        # Set the pending flag before polling.
        self._pending_poll_response = True
        
        # Always poll for the state, regardless of the current state in HA.
        # This is how we detect if the TV was turned on manually.
        await mqtt.async_publish(
            hass=self._hass,
            topic=self._out_topic("/remoteapp/tv/ui_service/%s/actions/gettvstate"),
            payload="",
            retain=False,
        )

    async def async_turn_on(self, **kwargs):
        """Turn the media player on."""
        # If TV is already on, do nothing.
        if self._state not in (STATE_OFF, STATE_STANDBY):
            _LOGGER.debug("TV is already on (state: %s). Doing nothing.", self._state)
            return

        # If the TV is in deep sleep, only WoL will work.
        if self._state == STATE_OFF:
            _LOGGER.debug("Turning on TV from deep sleep with WoL for MAC: %s", self._mac)
            wol_fn = functools.partial(wakeonlan.send_magic_packet, self._mac, ip_address=wakeonlan.BROADCAST_IP)
            await self.hass.async_add_executor_job(wol_fn)
        elif self._state == STATE_STANDBY:
            _LOGGER.debug("Turning on TV from standby. Sending KEY_POWER and WoL as fallback.")
            # Send KEY_POWER first for a quick wake-up from standby
            await mqtt.async_publish(
                hass=self._hass,
                topic=self._out_topic("/remoteapp/tv/remote_service/%s/actions/sendkey"),
                payload="KEY_POWER",
                retain=False,
            )
            # Also send WoL as a fallback in case the TV entered deep sleep from standby
            wol_fn = functools.partial(wakeonlan.send_magic_packet, self._mac, ip_address=wakeonlan.BROADCAST_IP)
            await self.hass.async_add_executor_job(wol_fn)

        # Optimistically update the state to provide immediate feedback.
        self._state = STATE_PLAYING
        self.async_write_ha_state()


    async def async_turn_off(self, **kwargs):
        """Turn off media player."""
        # Do nothing if it's already off.
        if self._state in (STATE_OFF, STATE_STANDBY):
            _LOGGER.debug("TV is already off or in standby. Doing nothing.")
            return

        _LOGGER.debug("Sending KEY_POWER to turn off TV.")
        await mqtt.async_publish(
            hass=self._hass,
            topic=self._out_topic("/remoteapp/tv/remote_service/%s/actions/sendkey"),
            payload="KEY_POWER",
            retain=False,
        )
        # Optimistically set the state to standby and update HA.
        self._state = STATE_STANDBY
        # Clear media-related attributes for a clean UI
        self._title = None
        self._channel_name = None
        self._channel_num = None
        self._starttime = None
        self._endtime = None
        self._position = None
        self.async_write_ha_state()

    @property
    def is_volume_muted(self):
        """Boolean if volume is currently muted."""
        _LOGGER.debug("is_volume_muted %s", self._muted)
        return self._muted

    @property
    def volume_level(self):
        """Volume level of the media player (0..100)."""
        _LOGGER.debug("volume_level %d", self._volume)
        return self._volume / 100

    async def async_set_volume_level(self, volume):
        """Set volume level, range 0..1."""
        _LOGGER.debug("set_volume_level %s", volume)
        # Optimistic update
        self._volume = int(volume * 100)
        await mqtt.async_publish(
            hass=self._hass,
            topic=self._out_topic(
                "/remoteapp/tv/platform_service/%s/actions/changevolume"
            ),
            payload=self._volume,
        )
        self.async_write_ha_state()

    async def async_volume_up(self):
        """Volume up the media player."""
        _LOGGER.debug("volume_up")
        # The TV will send a volumechange message, so we don't need an optimistic update.
        await mqtt.async_publish(
            hass=self._hass,
            topic=self._out_topic("/remoteapp/tv/remote_service/%s/actions/sendkey"),
            payload="KEY_VOLUMEUP",
        )

    async def async_volume_down(self):
        """Volume down media player."""
        _LOGGER.debug("volume_down")
        await mqtt.async_publish(
            hass=self._hass,
            topic=self._out_topic("/remoteapp/tv/remote_service/%s/actions/sendkey"),
            payload="KEY_VOLUMEDOWN",
        )

    async def async_mute_volume(self, mute):
        """Send mute command."""
        _LOGGER.debug("mute_volume %s", mute)
        self._muted = mute
        await mqtt.async_publish(
            hass=self._hass,
            topic=self._out_topic("/remoteapp/tv/remote_service/%s/actions/sendkey"),
            payload="KEY_MUTE",
        )
        self.async_write_ha_state()

    @property
    def source_list(self):
        """List of available input sources."""
        _LOGGER.debug("source_list property accessed.")
        # Always ensure sourcelist is requested if empty and not already requested
        if not self._sourcelist_requested and len(self._source_list) <= 1:
            _LOGGER.debug("Requesting source list from TV (via ensure_sourcelist).")
            self._hass.async_create_task(self._ensure_sourcelist())
        # Return the sources in the order received from the TV
        return list(self._source_list)

    @property
    def source(self):
        """Return the current input source."""
        _LOGGER.debug("source")
        return self._source_name
    
    @property
    def media_title(self):
        """Return the title of current playing media."""
        if self._state == STATE_OFF:
            return None

        _LOGGER.debug("media_title %s", self._title)
        return self._title
    
    @property
    def media_duration(self):
        """Return the duration of current playing media."""
        if self._state == STATE_OFF:
            return None
  
        if self._endtime is not None and self._starttime is not None:
           duration = self._endtime - self._starttime
        else: 
           return None
        _LOGGER.debug("media_duration %s", duration)
        return duration
    
    @property
    def media_position(self):
        """Return the actual position of current playing media."""
        if self._state == STATE_OFF:
            return None

        if self._endtime is not None and dt_util.utcnow().timestamp() > self._endtime:
           self._starttime = None
           self._endtime = None
           self._position = None
           self._media_position_updated_at = dt_util.utcnow() 
           return None
 
        if self._starttime is not None :
           position = int(dt_util.utcnow().timestamp()) - self._starttime
        else: 
           return None
        _LOGGER.debug("media_position %s", position)
        self._media_position_updated_at = dt_util.utcnow() 
        return position
    
    @property
    def media_position_updated_at(self):
        return self._media_position_updated_at

    async def async_media_seek(self, position: float):
        """Fake implementation: log the requested seek but do nothing."""
        _LOGGER.info("media_seek called with position=%s (ignored)", position)
    
    @property
    def media_series_title(self):
        """Return the channel current playing media."""
        if self._state == STATE_OFF:
            return None

        if self._channel_num is not None:
            channel = "%s (%s)" % (self._channel_name, self._channel_num)
        else:
            channel = self._channel_name
        _LOGGER.debug("media_series_title %s", channel)
        return channel

    async def async_select_source(self, source):
        """Select input source."""
        _LOGGER.debug("async_select_source %s", source)

        source_dic = self._source_list.get(source)
        if source_dic and source_dic.get("sourceid") is not None:
            # This is a standard source like HDMI, TV, etc.
            payload = json.dumps(
                {"sourceid": source_dic.get("sourceid"), "sourcename": source_dic.get("sourcename")}
            )
            await mqtt.async_publish(
                hass=self._hass,
                topic=self._out_topic("/remoteapp/tv/ui_service/%s/actions/changesource"),
                payload=payload,
            )
        else:
            # This is likely an app name, so try to launch it.
            _LOGGER.debug("Source '%s' not in standard source list, attempting to launch as app.", source)
            await self.async_launch_app(source)

    async def async_will_remove_from_hass(self):
        for unsubscribe in list(self._subscriptions.values()):
            unsubscribe()

    async def async_added_to_hass(self):
        """Subscribe to MQTT events."""
        # Remove the check for self._state and _request_sourcelist here
        # ...existing code...

        self._subscriptions["tvsleep"] = await mqtt.async_subscribe(
            self._hass,
            self._in_topic(
                "/remoteapp/mobile/broadcast/platform_service/actions/tvsleep"
            ),
            self._message_received_turnoff,
        )

        # Subscribe to TV state changes
        self._subscriptions["state"] = await mqtt.async_subscribe(
            self._hass,
            self._in_topic("/remoteapp/mobile/broadcast/ui_service/state"),
            self._message_received_state,
        )
        
        # Subscribe to hotelmode changes - this is the TV's response to our state requests
        self._subscriptions["hotelmode"] = await mqtt.async_subscribe(
            self._hass,
            self._in_topic("/remoteapp/mobile/broadcast/ui_service/data/hotelmodechange"),
            self._message_received_state,
        )

        self._subscriptions["volume"] = await mqtt.async_subscribe(
            self._hass,
            self._in_topic(
                "/remoteapp/mobile/broadcast/platform_service/actions/volumechange"
            ),
            self._message_received_volume,
        )

        self._subscriptions["sourcelist"] = await mqtt.async_subscribe(
            self._hass,
            self._out_topic("/remoteapp/mobile/%s/ui_service/data/sourcelist"),
            self._message_received_sourcelist,
        )

        # subscribe topic for "bwsinputdata" to get current text field content
        self._subscriptions["inputdata"] = await mqtt.async_subscribe(
            self._hass,
            self._in_topic(
                "/remoteapp/mobile/broadcast/platform_service/actions/bwsinputdata"
            ),
            self._message_received_inputdata,
        )


    async def _message_received_turnoff(self, msg):
        """Run when new MQTT message has been received."""
        self._pending_poll_response = False
        _LOGGER.debug("message_received_turnoff: TV is entering standby or deep sleep.")
        self._sourcelist_requested = False  # Reset flag when TV turns off
        self._state = STATE_OFF  # Assume deep sleep (WoL needed), as standby is also covered.
        self.async_write_ha_state()

    async def _message_received_sourcelist(self, msg):
        """Run when new MQTT message has been received."""
        try:
            payload = json.loads(msg.payload)
        except JSONDecodeError:
            payload = []
        _LOGGER.debug("message_received_sourcelist R(%s):\n%s", msg.retain, payload)
        if len(payload) > 0:
            # Build an ordered dict to preserve order from payload
            from collections import OrderedDict
            new_source_list = OrderedDict()
            for s in payload:
                name = s.get("sourcename")
                if name:
                    new_source_list[name] = s
            # Add "App" only if not present
            if "App" not in new_source_list:
                new_source_list["App"] = {}
            self._source_list = new_source_list
            _LOGGER.debug("Updated source_list: %s", self._source_list)
            self.async_write_ha_state()

    async def _message_received_volume(self, msg):
        """Run when new MQTT message has been received."""
        if msg.retain:
            _LOGGER.debug("_message_received_volume - skip retained message")
            return
        _LOGGER.debug("message_received_volume R(%s)\n%s", msg.retain, msg.payload)
        try:
            payload = json.loads(msg.payload)
            self._state = STATE_PLAYING
        except JSONDecodeError:
            payload = {}
        if payload.get("volume_type") == 0:
            self._volume = payload.get("volume_value")
        elif payload.get("volume_type") == 2:
            self._muted = payload.get("volume_value") == 1
        self.async_write_ha_state()

    async def _message_received_state(self, msg):
        """Run when new MQTT message has been received."""
        _LOGGER.debug("_message_received_state called with payload: %s", msg.payload)

        # Always process payload for UI, but only update state if not retained
        try:
            if msg.payload == "(null)":
                _LOGGER.debug("Got (null) response - TV is responding")
                new_state = STATE_PLAYING
                payload = {}
                statetype = None
            else:
                payload = json.loads(msg.payload)
                statetype = payload.get("statetype")
                if statetype == "fake_sleep_0":
                    _LOGGER.debug("Got fake_sleep_0 - TV going to standby")
                    new_state = STATE_STANDBY
                else:
                    _LOGGER.debug("Got response with statetype %s - TV is on", statetype)
                    new_state = STATE_PLAYING
        except JSONDecodeError:
            _LOGGER.debug("Got non-JSON response - TV is responding")
            new_state = STATE_PLAYING
            payload = {}
            statetype = None

        # Update attributes based on statetype (always, even for retained)
        if statetype == "sourceswitch":
            self._source_name = payload.get("sourcename")
            self._source_id = payload.get("sourceid")
            self._title = payload.get("displayname")
            self._channel_name = payload.get("sourcename")
            self._channel_num = None
            self._starttime = None
            self._endtime = None
            self._position = None
        elif statetype == "livetv":
            self._source_name = "TV"
            self._title = payload.get("progname")
            self._channel_name = payload.get("channel_name")
            self._channel_num = payload.get("channel_num")
            self._starttime = payload.get("starttime")
            self._endtime = payload.get("endtime")
        elif statetype == "remote_launcher":
            self._source_name = "App"
            self._title = "Applications"
            self._channel_name = None
            self._channel_num = None
            self._starttime = None
            self._endtime = None
            self._position = None
        elif statetype == "app":
            self._source_name = "App"
            self._title = payload.get("name")
            self._channel_name = payload.get("url")
            self._channel_num = None
            self._starttime = None
            self._endtime = None
            self._position = None
        elif statetype == "remote_epg":
            pass

        # Only update state if NOT retained
        if not msg.retain:
            self._pending_poll_response = False
            _LOGGER.debug("Setting pending_poll_response to False")
            _LOGGER.debug("State transition: %s -> %s", self._state, new_state)
            state_changed = self._state != new_state
            self._state = new_state
            self.async_write_ha_state()
            # If TV is now running, query sourcelist and volume
            if state_changed and new_state == STATE_PLAYING:
                _LOGGER.debug("TV marked as running in HA, querying sourcelist and volume.")
                await self._request_sourcelist()
                await self._async_update_other_media_players()
            elif state_changed and new_state in (STATE_OFF, STATE_STANDBY):
                _LOGGER.debug("TV marked as off in HA, updating other media players.")
                await self._async_update_other_media_players()
        else:
            # For retained, just update attributes/UI, but not the state
            self.async_write_ha_state()

    async def _async_update_other_media_players(self):
        """Request an update for other media_player entities on the same device."""
        ent_reg = er.async_get(self.hass)
        dev_reg = dr.async_get(self.hass)

        device_entry = dev_reg.async_get_device(identifiers={(DOMAIN, self._unique_id)})
        if not device_entry:
            _LOGGER.debug("Could not find device entry for media_player to sync others.")
            return

        for entity_entry in er.async_entries_for_device(ent_reg, device_entry.id):
            if entity_entry.domain == "media_player" and entity_entry.platform != DOMAIN:
                _LOGGER.info("Requesting update for associated media_player '%s' to sync its state.", entity_entry.entity_id)
                await self.hass.services.async_call('homeassistant', 'update_entity', {ATTR_ENTITY_ID: entity_entry.entity_id}, blocking=False)

    async def _message_received_inputdata(self, msg):
        """received broadcast message 'bwsinputdata'."""
        _LOGGER.debug("Received inputdata broadcast: %s", msg.payload)
        try:
            payload = json.loads(msg.payload)
            # Update the input text if the status indicates an open keyboard (vkbstatus:1)
            if payload.get("vkbstatus") == 1:
                self._input_text = payload.get("inputdata")
            else:
                # Clear the text if the keyboard is closed
                self._input_text = None
            self.async_write_ha_state()
        except (JSONDecodeError, AttributeError):
            _LOGGER.warning("error parsing 'bwsinputdata' broadcast: %s", msg.payload)
    async def _build_library_node(self):
        node = BrowseMedia(
            title="Media Library",
            media_class=MediaClass.DIRECTORY,
            media_content_type="library",
            media_content_id="library",
            can_play=False,
            can_expand=True,
            children=[],
        )

        # Add Applications node first to ensure it's always available
        await self._fetch_app_node(node)
        
        # Try to fetch channel list, but don't wait too long
        try:
            await asyncio.wait_for(self._fetch_channel_list(node), timeout=2.0)
        except asyncio.TimeoutError:
            _LOGGER.debug("Channel list fetch timed out - continuing without channels")
        except Exception as e:
            _LOGGER.debug("Error fetching channel list: %s - continuing without channels", e)

        return node

    async def _fetch_channel_list(self, node):
        """Fetch the channel list and add to node."""
        stream_get, unsubscribe_getchannellistinfo = await mqtt_pub_sub(
            hass=self._hass,
            pub=self._out_topic(
                "/remoteapp/tv/platform_service/%s/actions/getchannellistinfo"
            ),
            sub=self._in_topic(
                "/remoteapp/mobile/%s/platform_service/data/getchannellistinfo"
            ),
            payload="{}",
        )

        try:
            async with asyncio.timeout(2.0):  # Lokales Timeout für die Channel-Liste
                async for msg in stream_get:
                    try:
                        payload_string = msg[0].payload
                        if not payload_string or not isinstance(payload_string, str):
                            _LOGGER.debug("Skipping empty or invalid payload for channellistinfo")
                            break
                        payload = json.loads(payload_string)
                        self._channel_infos = {
                            item.get("list_para"): item for item in payload
                        }
                        for key, item in self._channel_infos.items():
                            node.children.append(
                                BrowseMedia(
                                    title=item.get("list_name"),
                                    media_class=MediaClass.DIRECTORY,
                                )
                            )   
                    except JSONDecodeError as err:
                        _LOGGER.warning(
                            "Could not build Media Library from '%s': %s", msg, err.msg
                        )
                    break
        except asyncio.TimeoutError:
            _LOGGER.debug("timeout error - getchannellistinfo")
        finally:
            unsubscribe_getchannellistinfo()

    async def _fetch_app_node(self, node):
        """Fetch and add the Applications node."""
        node.children.append(
            BrowseMedia(
                title="Applications",
                media_class=MediaClass.APP,
                media_content_type=MediaType.APPS,
                media_content_id="app_list",
                can_play=False,
                can_expand=True,
            )
        )

    async def _build_app_list_node(self):
        node = BrowseMedia(
            title="Applications",
            media_class=MediaClass.APP,
            media_content_type=MediaType.APPS,
            media_content_id="app_list",
            can_play=False,
            can_expand=True,
            children=[],
        )

        #  get getdeviceinfo
        stream_deviceinfo, unsubscribe_deviceinfo = await mqtt_pub_sub(
            hass=self._hass,
            pub=self._out_topic("/remoteapp/tv/platform_service/%s/actions/getdeviceinfo"),
            sub=self._in_topic("/remoteapp/mobile/%s/platform_service/data/getdeviceinfo"),
        )

        transport_protocol = None
        try:
            async for msg in stream_deviceinfo:
                try:
                    payload_string = msg[0].payload
                    if payload_string is None:
                        _LOGGER.debug("Skipping empty device info")
                        break
                    payload = json.loads(payload_string)
                    transport_protocol = payload.get("transport_protocol")
                    _LOGGER.debug("Transport Protocol: %s", transport_protocol)
                except JSONDecodeError as err:
                    _LOGGER.warning(
                        "Could not parse device info from '%s': %s", msg, err.msg
                    )
                break
        except asyncio.TimeoutError:
            _LOGGER.debug("Timeout error - getdeviceinfo")
        finally:
            unsubscribe_deviceinfo()

        # dynamic topic based on available transport_protocol
        vidaaapplist_topic = (
            "/remoteapp/tv/ui_service/%s/actions/vidaaapplist"
            if transport_protocol and str(transport_protocol) != "1140"
            else "/remoteapp/tv/ui_service/%s/actions/applist"
        )

        # get applist
        stream_get, unsubscribe_applist = await mqtt_pub_sub(
            hass=self._hass,
            pub=self._out_topic(vidaaapplist_topic),
            sub=self._in_topic("/remoteapp/mobile/%s/ui_service/data/applist"),
        )

        try:
            async for msg in stream_get:
                try:
                    payload_string = msg[0].payload
                    if not payload_string or not isinstance(payload_string, str):
                        _LOGGER.debug("Skipping empty or invalid payload for applist")
                        break
                    payload = json.loads(payload_string)
                    _LOGGER.debug("payload %s", payload_string)
                    self._app_list = {item.get("appId"): item for item in payload}
                    for nid, item in self._app_list.items():
                        _LOGGER.debug("adding app %s", item.get("name"))
                        # httpIcon must exist and be a string
                        http_icon = item.get("httpIcon")
                        if isinstance(http_icon, str):
                            match = re.search(r'https?://[^\\s]+', http_icon)
                            thumbnail = match.group(0) if match else None
                        else:
                            thumbnail = None
                        if thumbnail:
                            node.children.append(
                                BrowseMedia(
                                    title=item.get("name"),
                                    media_class=MediaClass.APP,
                                    media_content_type=MediaType.APP,
                                    media_content_id=nid,
                                    thumbnail=thumbnail,
                                    can_play=True,
                                    can_expand=False,
                                )
                            )
                        else:
                            node.children.append(
                                BrowseMedia(
                                    title=item.get("name"),
                                    media_class=MediaClass.APP,
                                    media_content_type=MediaType.APP,
                                    media_content_id=nid,
                                    can_play=True,
                                    can_expand=False,
                                )
                            )
                except JSONDecodeError as err:
                    _LOGGER.warning(
                        "Could not parse Application list from '%s': %s", msg, err.msg
                    )
                    _LOGGER.warning(
                        "Could not build Application list from '%s': %s", msg, err.msg
                    )
                break
        except asyncio.TimeoutError:
            _LOGGER.debug("timeout error - applist")
        finally:
            unsubscribe_applist()

        return node

    async def async_browse_media(self, media_content_type=None, media_content_id=None):
        """Implement the websocket media browsing helper."""

        if media_content_id in [None, "library"]:
            return await self._build_library_node()
        if media_content_id == "app_list":
            return await self._build_app_list_node()

        list_name = self._channel_infos.get(media_content_id).get("list_name")
        node = BrowseMedia(
            title=list_name,
            media_class=MediaClass.DIRECTORY,
            media_content_type="channellistinfo",
            media_content_id=media_content_id,
            can_play=False,
            can_expand=True,
            children=[],
        )

        channel_info = json.dumps(
            {"list_para": media_content_id, "list_name": list_name}
        )
        stream_get, unsubscribe_channellist = await mqtt_pub_sub(
            hass=self._hass,
            pub=self._out_topic(
                "/remoteapp/tv/platform_service/%s/actions/channellist"
            ),
            sub=self._in_topic(
                "/remoteapp/mobile/%s/platform_service/data/channellist"
            ),
            payload=channel_info,
        )

        try:
            async for msg in stream_get:
                try:
                    payload_string = msg[0].payload
                    if not payload_string or not isinstance(payload_string, str):
                        _LOGGER.debug("Skipping empty or invalid payload for channellist")
                        break
                    payload = json.loads(payload_string)
                    for item in payload.get("list"):
                        node.children.append(
                            BrowseMedia(
                                title=item.get("channel_name"),
                                media_class=MediaClass.CHANNEL,
                                media_content_type=MediaType.CHANNEL,
                                media_content_id=item.get("channel_param"),
                                can_play=True,
                                can_expand=False,
                            )
                        )
                except JSONDecodeError as err:
                    _LOGGER.warning(
                        "Could not build channel list from '%s': %s", msg, err.msg
                    )
                break
        except asyncio.TimeoutError:
            _LOGGER.debug("timeout error - channellist")

        unsubscribe_channellist()
        return node

    async def async_play_media(self, media_type, media_id, **kwargs):
        """Send the play_media command to the media player."""
        _LOGGER.debug("async_play_media %s\n%s", media_id, kwargs)

        if media_type == MediaType.CHANNEL:
            channel = json.dumps({"channel_param": media_id})
            await mqtt.async_publish(
                hass=self._hass,
                topic=self._out_topic(
                    "/remoteapp/tv/ui_service/%s/actions/changechannel"
                ),
                payload=channel,
            )
        elif media_type == MediaType.APP:
            app = self._app_list.get(media_id)
            payload = json.dumps(
                {
                    "appId": media_id,
                    "name": app.get("name"),
                    "url": app.get("url"),
                    "storeType": app.get("storeType"),
                }
            )
            await mqtt.async_publish(
                hass=self._hass,
                topic=self._out_topic("/remoteapp/tv/ui_service/%s/actions/launchapp"),
                payload=payload,
            )

    async def async_launch_app(self, app_name: str):
        """Launch an application by name."""
        _LOGGER.debug("Launching app: %s", app_name)

        if not self._app_list:
            _LOGGER.debug("App list is empty, fetching it now.")
            await self._build_app_list_node()

        app_id_to_launch = None
        app_to_launch = None

        # Search for the app in the app list
        for app_id, app_info in self._app_list.items():
            if app_info.get("name", "").lower() == app_name.lower():
                app_id_to_launch = app_id
                app_to_launch = app_info
                break

        if app_id_to_launch and app_to_launch:
            await self.async_play_media(media_type=MediaType.APP, media_id=app_id_to_launch)
        else:
            # Fallback for apps that are not in the list from the TV, but can be started.
            # e.g. "hbbtv"
            _LOGGER.info(
                "App '%s' not found in the cached app list. Trying to launch by name.",
                app_name,
            )
            payload = json.dumps(
                {"appId": app_name, "name": app_name, "url": app_name, "storeType": ""}
            )
            await mqtt.async_publish(
                hass=self._hass,
                topic=self._out_topic("/remoteapp/tv/ui_service/%s/actions/launchapp"),
                payload=payload,
            )

    async def _check_tv_state(self):
        """Check TV state during initialization and request sourcelist if on."""
        await mqtt.async_publish(
            hass=self._hass,
            topic=self._out_topic("/remoteapp/tv/ui_service/%s/actions/gettvstate"),
            payload="",
            retain=False,
        )

    async def _ensure_sourcelist(self):
        """Make sure we have the sourcelist if TV is on."""
        if not self._sourcelist_requested and len(self._source_list) <= 1:
            _LOGGER.debug("TV is on but sourcelist not requested yet - requesting now")
            await self._request_sourcelist()

    async def _request_sourcelist(self):
        """Helper method to request the sourcelist."""
        _LOGGER.debug("Requesting sourcelist from TV")
        self._sourcelist_requested = True
        await mqtt.async_publish(
            hass=self._hass,
            topic=self._out_topic("/remoteapp/tv/ui_service/%s/actions/sourcelist"),
            payload="",
            retain=False
        )
        # Also request volume to ensure we have current state
        await mqtt.async_publish(
            hass=self._hass,
            topic=self._out_topic("/remoteapp/tv/platform_service/%s/actions/getvolume"),
            payload="",
            retain=False
        )

    @property
    def extra_state_attributes(self):
        """Return the state attributes."""
        # Start with the base class attributes
        attributes = super().extra_state_attributes or {}
        # Add our custom attribute
        attributes["input_text"] = self._input_text
        return attributes

    async def async_send_key(self, key: str):
        """Send a single key to the TV."""
        payload = f"KEY_{key}"
        _LOGGER.debug("Publishing key: %s", payload)
        await mqtt.async_publish(
            hass=self._hass,
            topic=self._out_topic("/remoteapp/tv/remote_service/%s/actions/sendkey"),
            payload=payload,
            retain=False,
        )

    async def async_send_keys(self, keys: list[str]):
        """Send a sequence of keys with delays."""
        for key in keys:
            await self.async_send_key(key)
            await asyncio.sleep(self._key_delay)

    async def async_send_channel(self, channel: str):
        """Send a channel number to the TV."""
        await self.async_send_key("EXIT")
        await asyncio.sleep(self._key_delay)
        for digit in channel:
            await self.async_send_key(digit)
            await asyncio.sleep(self._key_delay)

    async def async_send_text(self, text: str):
        """Send a text string to the TV's input field."""
        # Use a shorter, fixed delay for text input as it's more sensitive
        text_delay = 0.1
        formatted_topic = self._out_topic("/remoteapp/tv/remote_service/%s/actions/input")

        for char in text:
            if char == '\b':
                payload = "Lit_BACKSPACE"
            elif char == '\n':
                payload = "Lit_ENTER"
            elif char == ' ':
                payload = "Lit_SPACE"
            else:
                payload = f"Lit_{char}"

            _LOGGER.debug("Publishing to topic: %s with payload: %s", formatted_topic, payload)
            await mqtt.async_publish(
                hass=self._hass,
                topic=formatted_topic,
                payload=payload,
                retain=False,
            )
            await asyncio.sleep(text_delay)

    async def async_send_mouse_event(self, dx: int, dy: int):
        """Accumulate and throttle mouse movement events."""
        self._mouse_dx_total += dx
        self._mouse_dy_total += dy

        if not self._mouse_throttle_timer:
            self._mouse_throttle_timer = self.hass.loop.call_later(
                self._mouse_throttle_interval, self.hass.async_create_task, self._async_send_throttled_mouse_event()
            ) 

    async def _async_send_throttled_mouse_event(self, _=None):
        """Send the accumulated mouse movement event to the Hisense TV."""
        if self._mouse_dx_total == 0 and self._mouse_dy_total == 0:
            self._mouse_throttle_timer = None
            return

        dx_to_send = self._mouse_dx_total
        dy_to_send = self._mouse_dy_total

        # Reset accumulators
        self._mouse_dx_total = 0
        self._mouse_dy_total = 0

        _LOGGER.debug("Sending throttled mouse event: dx=%d, dy=%d", dx_to_send, dy_to_send)
        payload = json.dumps({"dx": dx_to_send, "dy": dy_to_send})
        await mqtt.async_publish(
            hass=self._hass,
            topic=self._out_topic(
                "/remoteapp/tv/remote_service/%s$vidaa_common/actions/mousemove"
            ),
            payload=payload,
            retain=False,
        )

        # Reset the timer so the next movement will start a new one
        self._mouse_throttle_timer = None