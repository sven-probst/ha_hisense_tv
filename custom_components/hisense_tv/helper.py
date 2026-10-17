"""Hisene TV integration helper methods."""
import asyncio
import logging

from homeassistant.components import mqtt
from homeassistant.const import MAJOR_VERSION, MINOR_VERSION

from .const import DEFAULT_CLIENT_ID, DEFAULT_NAME, DOMAIN

_LOGGER = logging.getLogger(__name__)


async def mqtt_pub_sub(hass, pub, sub, payload=""):
    """Wrapper for publishing MQTT topics and receive replies on a subscibed topic.

    The returned generator yields received messages as ``(message,)`` tuples and
    ends (returns) after 10 seconds without a response. Callers must check
    whether the first tuple element is ``None`` (yielded from queued payload)
    before use.
    """
    loop = asyncio.get_event_loop()
    queue = asyncio.Queue()

    _LOGGER.debug("MQTT pub/sub started: pub='%s', sub='%s', payload='%s'", pub, sub, payload)

    def put(*args):
        _LOGGER.debug("MQTT message received (Callback): %s", args)
        loop.call_soon_threadsafe(queue.put_nowait, args)

    async def get():
        try:
            _LOGGER.debug("Waiting for message on topic '%s'...", sub)
            message = await asyncio.wait_for(queue.get(), timeout=10)
            _LOGGER.debug("Message received: %s", message)
            yield message
        except asyncio.TimeoutError:
            _LOGGER.debug("Timeout: no message on topic '%s' within 10 seconds", sub)
            return  # End the generator instead of yielding None.

    _LOGGER.debug("Subscribing to topic: '%s'", sub)
    unsubscribe = await mqtt.async_subscribe(hass=hass, topic=sub, msg_callback=put)
    await asyncio.sleep(0.1)
    _LOGGER.debug("Publishing to topic: '%s' with payload: %s", pub, payload)
    await mqtt.async_publish(hass=hass, topic=pub, payload=payload)
    return get(), unsubscribe

class HisenseTvBase(object):
    """Hisense TV base entity."""

    def __init__(
        self,
        hass,
        name: str,
        mqtt_in: str,
        mqtt_out: str,
        mac: str,
        uid: str,
        ip_address: str,
        client_id: str = None,
    ):
        self._client = client_id or DEFAULT_CLIENT_ID
        self._hass = hass
        self._name = name
        self._mqtt_in = mqtt_in or ""
        self._mqtt_out = mqtt_out or ""
        self._mac = mac
        self._ip_address = ip_address
        self._unique_id = uid
        self._icon = (
            "mdi:television-clean"
            if MAJOR_VERSION <= 2021 and MINOR_VERSION < 11
            else "mdi:television-shimmer"
        )
        self._subscriptions = {
            "tvsleep": lambda: None,
            "state": lambda: None,
            "hotelmode": lambda: None,
            "volume": lambda: None,
            "sourcelist": lambda: None,
            "inputdata": lambda: None,
            "token_refresh": lambda: None,
        }
        # VIDAA 9 detection inputs; subclasses set these when known.
        self._transport_protocol = None
        self._tv_version = None

    def _is_vidaa9(self) -> bool:
        """Return True if the TV runs VIDAA 9 (transport_protocol >= 3000).

        Falls back to the tv_version string ("V0000.09.xx") when the TV does
        not report transport_protocol (VIDAA 9 omits it from getdeviceinfo).
        """
        try:
            if self._transport_protocol is not None:
                return int(str(self._transport_protocol)) >= 3000
        except (ValueError, TypeError):
            pass
        try:
            return str(self._tv_version or "").startswith("V0000.09")
        except (TypeError, AttributeError):
            return False

    def _out_topic(self, topic=""):
        try:
            out_topic = self._mqtt_out + topic % self._client
        except:
            out_topic = self._mqtt_out + topic
        _LOGGER.debug("_out_topic: %s", out_topic)
        return out_topic

    def _in_topic(self, topic=""):
        try:
            in_topic = self._mqtt_in + topic % self._client
        except:
            in_topic = self._mqtt_in + topic
        _LOGGER.debug("_in_topic: %s", in_topic)
        return in_topic
