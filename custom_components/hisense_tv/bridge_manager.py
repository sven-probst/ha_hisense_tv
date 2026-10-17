"""In-process supervisor for the bundled Hisense/VIDAA dynamic MQTT bridge.

On VIDAA 9 firmware the TV rejects the static Mosquitto bridge; the bundled
``bridge`` package connects to the TV broker with dynamic credentials and
mirrors the ``/remoteapp/#`` topics into Home Assistant's MQTT broker.

The bridge runs as a daemon thread inside the Home Assistant process (no
separate process): HA starts and stops it together with the config entry.
Enable it in the integration's options flow (step "bridge").
"""

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_IP_ADDRESS
from homeassistant.core import HomeAssistant

from .const import (
    CONF_CLIENT_ID,
    CONF_MQTT_IN,
    CONF_MQTT_OUT,
    DEFAULT_CLIENT_ID,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

CONF_BRIDGE_ENABLED = "bridge_enabled"
CONF_BRIDGE_HA_HOST = "bridge_mqtt_host"
CONF_BRIDGE_HA_PORT = "bridge_mqtt_port"
CONF_BRIDGE_HA_USER = "bridge_mqtt_user"
CONF_BRIDGE_HA_PASS = "bridge_mqtt_pass"
CONF_BRIDGE_TV_HOST = "bridge_tv_host"
CONF_BRIDGE_TV_PORT = "bridge_tv_port"
CONF_BRIDGE_CERTFILE = "bridge_certfile"
CONF_BRIDGE_KEYFILE = "bridge_keyfile"
CONF_BRIDGE_MAC = "bridge_mac"
CONF_BRIDGE_BRAND = "bridge_brand"
CONF_BRIDGE_AUTH_MODE = "bridge_auth_mode"


def get_entry_data(entry: ConfigEntry) -> dict:
    """Configuration lives in entry data (setup) and options (options flow)."""
    return {**entry.data, **entry.options}


def _build_config(entry: ConfigEntry) -> dict:
    data = get_entry_data(entry)
    return {
        "mqtt": {
            "host": data.get(CONF_BRIDGE_HA_HOST) or "127.0.0.1",
            "port": int(data.get(CONF_BRIDGE_HA_PORT) or 1883),
            "username": data.get(CONF_BRIDGE_HA_USER) or None,
            "password": data.get(CONF_BRIDGE_HA_PASS) or None,
        },
        "tvs": [
            {
                "name": entry.title,
                "host": data.get(CONF_BRIDGE_TV_HOST) or data.get(CONF_IP_ADDRESS) or "",
                "port": int(data.get(CONF_BRIDGE_TV_PORT) or 36669),
                "mac": data.get(CONF_BRIDGE_MAC) or None,
                "brand": data.get(CONF_BRIDGE_BRAND) or None,
                "auth_mode": data.get(CONF_BRIDGE_AUTH_MODE) or "auto",
                "certfile": data.get(CONF_BRIDGE_CERTFILE) or None,
                "keyfile": data.get(CONF_BRIDGE_KEYFILE) or None,
                "prefix_in": data.get(CONF_MQTT_IN),
                "prefix_out": data.get(CONF_MQTT_OUT),
                "topic_client_id": data.get(CONF_CLIENT_ID) or DEFAULT_CLIENT_ID,
            }
        ],
    }


async def async_setup_bridge(hass: HomeAssistant, entry: ConfigEntry):
    """Start the in-process bridge for the config entry if enabled."""
    store = hass.data.setdefault(DOMAIN, {}).setdefault(entry.entry_id, {})
    await async_unload_bridge(hass, entry)

    data = get_entry_data(entry)
    if not data.get(CONF_BRIDGE_ENABLED):
        return

    if not data.get(CONF_MQTT_IN):
        _LOGGER.error(
            "Bridge requires MQTT in/out prefixes; check the entry configuration."
        )
        return
    if not (data.get(CONF_BRIDGE_TV_HOST) or data.get(CONF_IP_ADDRESS)):
        _LOGGER.error("Bridge is enabled but no TV host is set (options -> Bridge).")
        return

    try:
        from .bridge.bridge import Bridge
    except Exception as err:  # noqa: BLE001
        _LOGGER.error("Bundled bridge could not be imported: %s", err)
        return

    cfg = _build_config(entry)
    try:
        bridge = Bridge(entry.title, cfg["tvs"][0], cfg["mqtt"])
        await hass.async_add_executor_job(bridge.start)
    except Exception as err:  # noqa: BLE001
        _LOGGER.error("Bridge failed to start: %s", err)
        return

    store["bridge"] = bridge
    _LOGGER.info(
        "In-process bridge started for %s (TV %s). If the connection flaps "
        "(repeated connect/disconnect), a leftover bridge process from an "
        "older version may be sharing the client id - clean it up with "
        "'pkill -f hisense_tv.bridge.bridge' and restart Home Assistant.",
        entry.title,
        cfg["tvs"][0]["host"],
    )


async def async_unload_bridge(hass: HomeAssistant, entry: ConfigEntry):
    """Stop the in-process bridge for the config entry."""
    store = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
    bridge = store.pop("bridge", None)
    if bridge is None:
        return
    try:
        await hass.async_add_executor_job(bridge.close)
        _LOGGER.info("In-process bridge stopped for %s", entry.title)
    except Exception as err:  # noqa: BLE001
        _LOGGER.error("Error stopping the bridge: %s", err)