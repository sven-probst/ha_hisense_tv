"""Supervisor for the bundled Hisense/VIDAA dynamic MQTT bridge.

On VIDAA 9 firmware the TV rejects the static Mosquitto bridge; the bundled
``bridge`` package (custom_components/hisense_tv/bridge) connects to the TV
broker with dynamic credentials and mirrors the /remoteapp/# topics into Home
Assistant's MQTT broker. This module starts that daemon as a supervised
subprocess for a config entry and restarts it while the entry is loaded.

Enable it in the integration's options flow (step "bridge").
"""

import asyncio
import logging
import os
import sys
import time

import yaml

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_IP_ADDRESS
from homeassistant.core import HomeAssistant

from .const import (
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

DIR_NAME = "hisense_bridge"
CONFIG_FILENAME = "config.yaml"
LOG_FILENAME = "bridge.log"
RESTART_DELAY = 8
MAX_RAPID_RESTARTS = 5


def get_entry_data(entry: ConfigEntry) -> dict:
    """Configuration is stored in entry data (setup) and options (options flow)."""
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
                "host": data.get(CONF_BRIDGE_TV_HOST) or "",
                "port": int(data.get(CONF_BRIDGE_TV_PORT) or 36669),
                "mac": data.get(CONF_BRIDGE_MAC) or None,
                "brand": data.get(CONF_BRIDGE_BRAND) or None,
                "auth_mode": data.get(CONF_BRIDGE_AUTH_MODE) or "auto",
                "certfile": data.get(CONF_BRIDGE_CERTFILE) or None,
                "keyfile": data.get(CONF_BRIDGE_KEYFILE) or None,
                "prefix_in": data.get(CONF_MQTT_IN),
                "prefix_out": data.get(CONF_MQTT_OUT),
                "topic_client_id": DEFAULT_CLIENT_ID,
            }
        ],
    }


def _write_files(hass: HomeAssistant, cfg: dict):
    directory = hass.config.path(DIR_NAME)
    os.makedirs(directory, exist_ok=True)
    config_path = os.path.join(directory, CONFIG_FILENAME)
    with open(config_path, "w", encoding="utf-8") as fh:
        yaml.safe_dump(cfg, fh, default_flow_style=False)
        fh.flush()
        os.fsync(fh.fileno())
    os.chmod(config_path, 0o600)
    return config_path


async def async_setup_bridge(hass: HomeAssistant, entry: ConfigEntry):
    """Start the supervised bridge for the config entry if enabled."""
    store = hass.data.setdefault(DOMAIN, {}).setdefault(entry.entry_id, {})
    await async_unload_bridge(hass, entry)

    data = get_entry_data(entry)
    if not data.get(CONF_BRIDGE_ENABLED):
        return

    mqtt_in = data.get(CONF_MQTT_IN)
    if not mqtt_in:
        _LOGGER.error("Bridge requires MQTT in/out prefixes; check the entry configuration.")
        return
    tv_host = data.get(CONF_BRIDGE_TV_HOST) or data.get(CONF_IP_ADDRESS)
    if not tv_host:
        _LOGGER.error("Bridge is enabled but no TV host is set (options -> Bridge).")
        return

    try:
        cfg = _build_config(entry)
        config_path = _write_files(hass, cfg)
    except Exception as err:  # noqa: BLE001
        _LOGGER.error("Could not write bridge configuration: %s", err)
        return

    log_path = os.path.join(hass.config.path(DIR_NAME), LOG_FILENAME)
    log_handle = open(log_path, "ab")

    try:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "custom_components.hisense_tv.bridge.bridge",
            "-c",
            config_path,
            cwd=hass.config.config_dir,
            stdout=log_handle,
            stderr=asyncio.subprocess.STDOUT,
            stdin=asyncio.subprocess.DEVNULL,
        )
    except Exception as err:  # noqa: BLE001
        log_handle.close()
        _LOGGER.error("Could not start the bridge daemon: %s", err)
        return

    state = {
        "process": process,
        "log": log_handle,
        "restarts": 0,
        "last_start": time.monotonic(),
    }
    task = asyncio.create_task(
        _watch_bridge(hass, entry, state),
        name=f"hisense_tv_bridge_{entry.entry_id}",
    )
    state["task"] = task
    store["bridge"] = state
    _LOGGER.info(
        "Bridge daemon started (PID %s); log: %s", process.pid, log_path
    )


async def _watch_bridge(hass: HomeAssistant, entry: ConfigEntry, state: dict):
    """Restart the daemon until the entry is unloaded."""
    process = state["process"]
    try:
        returncode = await process.wait()
    finally:
        if state["log"]:
            state["log"].close()
            state["log"] = None

    store = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if store is None or store.get("bridge") is not state:
        return

    enabled = get_entry_data(entry).get(CONF_BRIDGE_ENABLED)
    if not enabled:
        return

    _LOGGER.warning(
        "Bridge daemon exited (code %s), restarting in %ss...",
        returncode,
        RESTART_DELAY,
    )
    # Only count as repeated crash if the last run was short; a long-lived
    # daemon that dies once should not accumulate towards the crash limit.
    last_start = state.get("last_start")
    if last_start is None or time.monotonic() - last_start > 300:
        state["restarts"] = 1
    else:
        state["restarts"] += 1
    if state["restarts"] >= MAX_RAPID_RESTARTS:
        _LOGGER.error(
            "Bridge daemon crashed %s times in a row; giving up. Check the "
            "log at %s/%s (e.g. missing paho-mqtt or bad certificates).",
            MAX_RAPID_RESTARTS,
            hass.config.path(DIR_NAME),
            LOG_FILENAME,
        )
        return

    store.pop("bridge", None)
    await asyncio.sleep(RESTART_DELAY)
    if hass.data.get(DOMAIN, {}).get(entry.entry_id) is store:
        await async_setup_bridge(hass, entry)


async def async_unload_bridge(hass: HomeAssistant, entry: ConfigEntry):
    """Stop the supervised bridge for the config entry."""
    store = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
    state = store.pop("bridge", None)
    if not state:
        return

    task = state.pop("task", None)
    if task:
        task.cancel()
    process = state.get("process")
    if process and process.returncode is None:
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=10)
        except asyncio.TimeoutError:
            process.kill()
    log_handle = state.get("log")
    if log_handle:
        try:
            log_handle.close()
        except OSError:
            pass