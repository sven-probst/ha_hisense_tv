"""Dynamic MQTT bridge between Home Assistant and a Hisense/VIDAA TV.

Replaces the static Mosquitto bridge (``topic /remoteapp/# both 0 <prefix> ""``)
for firmware that requires timestamp-based authentication (VIDAA 9 /
``transport_protocol`` >= 3000). The bridge maintains two MQTT connections:

- one to Home Assistant's broker, where it looks like the old bridge:
  it subscribes to ``<prefix_out>/remoteapp/tv/#`` and publishes TV traffic
  under ``<prefix_in>/remoteapp/#``;
- one to the TV's embedded broker on port 36669 (TLS + client certificate),
  where it authenticates with freshly generated dynamic credentials and
  mirrors ``/remoteapp/#`` back and forth.

The Home Assistant integration itself is unchanged and still sees the client
id it uses in topics (``HomeAssistant`` by default); the bridge only rewrites
the topic client-id segment if the TV-side id differs.

Run e.g. as ``python -m bridge.bridge -c bridge/config.yaml``.
"""

import argparse
import logging
import os
import signal
import ssl
import sys
import threading
import time

import yaml

import paho.mqtt.client as mqtt

from .credentials import (
    AuthMethod,
    generate_dynamic,
    generate_static,
    method_order,
)
from . import upnp

_LOGGER = logging.getLogger("hisense_bridge")

# MQTT CONNACK return codes for the TV broker
_CONNACK_CODES = {
    0: "connected",
    1: "unacceptable protocol version",
    2: "identifier rejected",
    3: "server unavailable",
    4: "bad user name or password",
    5: "not authorized - check TV credentials, certificates and clock",
}

# VIDAA 9 firmware refuses wildcard subscriptions ("not authorized") and only
# grants the exact topics the client is authorized for once it is paired (and,
# on token-based firmware, has obtained the access token). Older firmware
# grants these exact topics immediately (they are a subset of the wildcards
# the old Mosquitto bridge used), so the same list works for every generation.
# The client id in the topics is the TV-side topic id (`{cid}` placeholder).
TV_SUBSCRIBE_TOPICS = (
    "/remoteapp/mobile/{cid}/ui_service/data/authentication",
    "/remoteapp/mobile/{cid}/ui_service/data/authenticationcode",
    "/remoteapp/mobile/{cid}/ui_service/data/authenticationcodetoast",
    "/remoteapp/mobile/{cid}/ui_service/data/authenticationcodeclose",
    "/remoteapp/mobile/{cid}/ui_service/data/vidaa_app_connect",
    "/remoteapp/mobile/{cid}/ui_service/data/vidaa_app_ble_connect",
    "/remoteapp/mobile/{cid}/ui_service/data/sourcelist",
    "/remoteapp/mobile/{cid}/ui_service/data/applist",
    "/remoteapp/mobile/{cid}/ui_service/data/capability",
    "/remoteapp/mobile/{cid}/ui_service/data/appversion",
    "/remoteapp/mobile/{cid}/ui_service/data/login_each_other_info",
    "/remoteapp/mobile/{cid}/ui_service/data/state",
    "/remoteapp/mobile/{cid}/platform_service/data/tokenissuance",
    "/remoteapp/mobile/{cid}/platform_service/data/gettvinfo",
    "/remoteapp/mobile/{cid}/platform_service/data/getdeviceinfo",
    "/remoteapp/mobile/{cid}/platform_service/data/getplatformcapbility",
    "/remoteapp/mobile/{cid}/platform_service/data/channellist",
    "/remoteapp/mobile/broadcast/ui_service/state",
    "/remoteapp/mobile/broadcast/ui_service/data/hotelmodechange",
    "/remoteapp/mobile/broadcast/platform_service/actions/tvsleep",
    "/remoteapp/mobile/broadcast/platform_service/actions/volumechange",
    "/remoteapp/mobile/broadcast/platform_service/actions/bwsinputdata",
    "/remoteapp/mobile/broadcast/platform_service/data/picturesetting",
)

# How often the exact topics are re-subscribed. VIDAA 9 only grants the data
# topics after pairing + token issuance, which happens via the HA integration
# sometime after the bridge connected; re-subscribing picks the grants up.
RESUBSCRIBE_INTERVAL = 45


def _new_paho_client(client_id: str) -> mqtt.Client:
    """Create a paho client (works with paho-mqtt 1.x and 2.x).

    With paho 2.x the v2 callback API is used so no deprecation warning is
    emitted; the callbacks parse the reason code in a version-agnostic way.
    """
    try:  # paho-mqtt >= 2.0
        from paho.mqtt.client import CallbackAPIVersion

        return mqtt.Client(
            client_id=client_id,
            protocol=mqtt.MQTTv311,
            callback_api_version=CallbackAPIVersion.VERSION2,
        )
    except ImportError:  # paho-mqtt 1.x
        return mqtt.Client(client_id=client_id, protocol=mqtt.MQTTv311)


def _reason_code(positional) -> int:
    """Extract the numeric MQTT reason/return code from callback args.

    paho v1 passes the raw int, v2 passes a ReasonCode object.
    """
    rc = positional
    return rc.value if hasattr(rc, "value") else rc


class TvConnection:
    """Connection to the TV's embedded MQTT broker with dynamic auth."""

    def __init__(
        self,
        cfg: dict,
        prefix_in: str,
        prefix_out: str,
        topic_client_id: str,
        on_message_to_ha,
    ):
        self.host = cfg["host"]
        self.port = int(cfg.get("port", 36669))
        self.certfile = cfg.get("certfile")
        self.keyfile = cfg.get("keyfile")
        self.ca_certs = cfg.get("ca_certs")
        self.keepalive = int(cfg.get("keepalive", 20))
        self.prefix_in = prefix_in.strip("/")
        self.prefix_out = prefix_out.strip("/")
        self.topic_client_id = topic_client_id
        self._on_message_to_ha = on_message_to_ha

        self._lock = threading.RLock()
        self._client: mqtt.Client | None = None
        self._order: list = [AuthMethod.MODERN]
        self._index = 0
        self._method = AuthMethod.MODERN
        self._tv_topic_client_id = topic_client_id
        self._rotate_pending = False
        self._closed = False

        # Set by start() from config/descriptor
        self._auth_mode = "auto"
        self._transport_protocol = None
        self._mac = None
        self._brand = None

    # ------------------------------------------------------------------ API

    def start(self, auth_mode, transport_protocol, mac, brand):
        self._auth_mode = auth_mode
        self._transport_protocol = transport_protocol
        self._mac = mac
        self._brand = brand

        if auth_mode == "static":
            self._order = [AuthMethod.STATIC]
        elif auth_mode == "dynamic":
            self._order = [m for m in method_order(transport_protocol) if m != AuthMethod.STATIC]
        else:
            self._order = method_order(transport_protocol)
        self._index = 0
        self._method = self._order[0]

        _LOGGER.info(
            "TV %s: transport_protocol=%s, mac=%s, brand=%s, method order=%s",
            self.host,
            transport_protocol,
            mac,
            brand or "?",
            [m.value for m in self._order],
        )
        threading.Thread(target=self._resubscribe_loop, daemon=True).start()
        self._connect()

    def publish(self, topic: str, payload, retain: bool):
        client = self._client
        if client is None or not client.is_connected():
            _LOGGER.debug("TV %s: not connected, dropping %s", self.host, topic)
            return
        client.publish(topic, payload, qos=0, retain=retain)

    def close(self):
        with self._lock:
            self._closed = True
            client = self._client
            self._client = None
        if client:
            try:
                client.loop_stop()
                client.disconnect()
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------ credential

    def _creds(self, method: AuthMethod):
        if method == AuthMethod.STATIC:
            return generate_static(self.topic_client_id)
        if not self._mac:
            self._mac, self._brand = self._probe_mac()
        if not self._mac:
            raise ValueError(
                f"{self.host}: {method.value} auth needs the TV's MAC address "
                "(set 'mac' in the config or enable UPnP detection)"
            )
        return generate_dynamic(self._mac, self._brand or "his", method)

    def _probe_mac(self):
        """Re-read the TV descriptor for the auth MAC when it is missing.

        The dynamic-auth MAC is the TV's *wired* / descriptor ``mac`` field,
        which is independent of the WoL MAC (WiFi) the HA config entry uses.
        Tries again on every connect, so a TV that was off at startup is
        picked up as soon as it is reachable.
        """
        try:
            detected = upnp.detect(self.host, mac=None, brand=self._brand)
            mac = detected.get("mac")
            if mac:
                _LOGGER.info(
                    "TV %s: auth MAC auto-detected from descriptor: %s", self.host, mac
                )
            return mac, detected.get("brand") or self._brand
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("TV %s: re-probing descriptor failed: %s", self.host, err)
            return None, self._brand

    # -------------------------------------------------------------- connect

    def _build_client(self, creds) -> mqtt.Client:
        client = _new_paho_client(creds.client_id)
        if self.certfile and self.keyfile:
            for cert_path in (self.certfile, self.keyfile):
                if not cert_path or not os.path.isfile(cert_path):
                    raise FileNotFoundError(
                        f"certificate file not found: {cert_path} - run "
                        "setup_certs.py or fix certfile/keyfile in the config"
                    )
            client.tls_set(
                ca_certs=self.ca_certs,
                certfile=self.certfile,
                keyfile=self.keyfile,
                cert_reqs=ssl.CERT_NONE if not self.ca_certs else ssl.CERT_REQUIRED,
                tls_version=ssl.PROTOCOL_TLSv1_2,
            )
            # TV certificate is self-signed (CN=RemoteCA), never a hostname match.
            client.tls_insecure_set(True)
        else:
            _LOGGER.warning(
                "TV %s: no client certificate configured; if the TV requires "
                "mutual TLS the connection will be refused",
                self.host,
            )
        client.username_pw_set(creds.username, creds.password)
        client.reconnect_delay_set(min_delay=1, max_delay=30)
        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_message = self._on_message
        return client

    def _connect(self):
        with self._lock:
            if self._closed:
                return
            if self._client is not None:
                # A live client already exists (e.g. reconnect retry raced
                # with the running connection); leave it alone.
                return
        try:
            creds = self._creds(self._method)
            client = self._build_client(creds)
        except Exception as err:  # noqa: BLE001
            _LOGGER.error(
                "TV %s: could not build MQTT client (%s); retrying in 10s",
                self.host,
                err,
            )
            self._schedule_reconnect()
            return
        self._tv_topic_client_id = creds.topic_client_id

        with self._lock:
            if self._closed:
                return
            self._client = client
        _LOGGER.info(
            "TV %s: connecting with %s auth (client_id=%s)",
            self.host,
            self._method.value,
            creds.client_id,
        )
        client.connect_async(self.host, self.port, keepalive=self.keepalive)
        client.loop_start()

    def _schedule_reconnect(self):
        if self._closed:
            return
        threading.Timer(10.0, self._connect).start()

    def _rotate_method(self):
        """Move to the next auth method and rebuild the connection."""
        with self._lock:
            if self._closed:
                return
            self._index = min(self._index + 1, len(self._order) - 1)
            self._method = self._order[self._index]
            old = self._client
            self._client = None
        _LOGGER.warning(
            "TV %s: not authorized, falling back to %s auth",
            self.host,
            self._method.value,
        )
        if old:
            try:
                old.loop_stop()
                old.disconnect()
            except Exception:  # noqa: BLE001
                pass
        self._connect()
        self._rotate_pending = False

    # ------------------------------------------------------------ callbacks

    def _on_connect(self, client, userdata, flags, rc, *args):
        rc = _reason_code(rc)
        if rc == 0:
            _LOGGER.info("TV %s: connected to MQTT broker", self.host)
            self._subscribe_topics(client)
            return
        reason = _CONNACK_CODES.get(rc, f"unknown code {rc}")
        _LOGGER.error("TV %s: connection refused: %s", self.host, reason)
        if rc == 5 and not self._rotate_pending and len(self._order) > 1:
            self._rotate_pending = True
            threading.Thread(target=self._rotate_method, daemon=True).start()

    def _on_disconnect(self, client, userdata, rc, *args):
        if self._closed:
            return
        # paho v2 hands the reason code as the 5th positional argument.
        code = args[0] if args else rc
        _LOGGER.warning(
            "TV %s: disconnected (rc=%s); refreshing credentials and reconnecting",
            self.host,
            _reason_code(code),
        )
        try:
            creds = self._creds(self._method)
            client.username_pw_set(creds.username, creds.password)
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("TV %s: could not refresh credentials: %s", self.host, err)

    def _subscribe_topics(self, client, reason=""):
        """Subscribe the exact TV response topics.

        VIDAA 9 refuses subscriptions to topics the client is not yet
        authorized for; older firmware grants them right away. The periodic
        re-subscribe picks up grants that appear after pairing/token issuance.
        """
        cid = self._tv_topic_client_id
        if not cid:
            return
        for template in TV_SUBSCRIBE_TOPICS:
            client.subscribe(template.format(cid=cid), qos=0)
        # Bonus for legacy firmware (which still allows wildcards, like the
        # old Mosquitto bridge did): also try a wildcard so any topic the TV
        # emits is mirrored. VIDAA 9 refuses this ("not authorized") - harmless.
        client.subscribe("/remoteapp/#", qos=0)
        if reason:
            _LOGGER.debug("TV %s: subscribed exact topics + wildcard (%s)", self.host, reason)

    def _resubscribe_loop(self):
        while not self._closed:
            time.sleep(RESUBSCRIBE_INTERVAL)
            if self._closed:
                return
            client = self._client
            if client is not None and client.is_connected():
                try:
                    self._subscribe_topics(client, "periodic")
                except Exception as err:  # noqa: BLE001
                    _LOGGER.debug("TV %s: periodic resubscribe failed: %s", self.host, err)

    def _on_message(self, client, userdata, msg):
        self._on_message_to_ha(self._tv_to_ha(msg.topic), msg.payload, msg.retain)
        # VIDAA 9 unlocks the data topics only after pairing (PIN) + token
        # issuance; grab the new grants as soon as those arrive.
        if "/tokenissuance" in msg.topic or "/data/authenticationcode" in msg.topic:
            try:
                self._subscribe_topics(client, "auth-update")
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("TV %s: resubscribe after auth failed: %s", self.host, err)

    # ---------------------------------------------------------- topic mapping

    @staticmethod
    def _rewrite(topic: str, src: str, dst: str) -> str:
        if src == dst:
            return topic
        parts = topic.split("/")
        for i, part in enumerate(parts):
            if part == src:
                parts[i] = dst
        return "/".join(parts)

    def _is_dynamic(self) -> bool:
        return self._method != AuthMethod.STATIC

    def _tv_to_ha(self, topic: str) -> str:
        t = topic[1:] if topic.startswith("/") else topic
        if self.prefix_in:
            t = f"{self.prefix_in}/{t}"
        if self._is_dynamic():
            t = self._rewrite(t, self._tv_topic_client_id, self.topic_client_id)
        return t

    def ha_to_tv(self, topic: str) -> str:
        parts = topic.split("/")
        if self.prefix_out:
            prefix_parts = [p for p in self.prefix_out.split("/") if p]
            if prefix_parts and parts[: len(prefix_parts)] == prefix_parts:
                parts = parts[len(prefix_parts) :]
        t = "/" + "/".join(parts)
        if self._is_dynamic():
            t = self._rewrite(t, self.topic_client_id, self._tv_topic_client_id)
        return t


class Bridge:
    """One HA-broker connection plus one TV connection."""

    def __init__(self, name: str, tv_cfg: dict, mqtt_cfg: dict):
        self.name = name
        prefix_out = tv_cfg.get("prefix_out") or mqtt_cfg.get("prefix", "hisense")
        prefix_in = tv_cfg.get("prefix_in") or mqtt_cfg.get("prefix", "hisense")
        self.prefix_out = str(prefix_out).strip("/")
        self.prefix_in = str(prefix_in).strip("/")

        self.topic_client_id = tv_cfg.get("topic_client_id", "HomeAssistant")

        # Explicit config overrides the UPnP descriptor.
        self._tv_mac = tv_cfg.get("mac")
        self._tv_brand = tv_cfg.get("brand")
        self._tv_auth_mode = tv_cfg.get("auth_mode", "auto")

        self._tv = TvConnection(
            tv_cfg,
            self.prefix_in,
            self.prefix_out,
            self.topic_client_id,
            self._publish_to_ha,
        )

        client_id = f"hisense-bridge-{self.name.lower()}"
        self._ha = _new_paho_client(client_id)
        host = mqtt_cfg.get("host", "127.0.0.1")
        self._ha_host = host
        self._ha_port = int(mqtt_cfg.get("port", 1883))
        username = mqtt_cfg.get("username")
        if username:
            self._ha.username_pw_set(username, mqtt_cfg.get("password"))
        if mqtt_cfg.get("tls"):
            self._ha.tls_set(ca_certs=mqtt_cfg.get("ca_certs"))
        self._ha.reconnect_delay_set(min_delay=1, max_delay=30)
        self._ha.on_connect = self._on_ha_connect
        self._ha.on_message = self._on_ha_message

    # ------------------------------------------------------------- HA side

    def _ha_sub_topic(self) -> str:
        return f"{self.prefix_out}/remoteapp/tv/#"

    def _on_ha_connect(self, client, userdata, flags, rc, *args):
        rc = _reason_code(rc)
        if rc == 0:
            _LOGGER.info(
                "Broker %s:%s: connected, subscribing to %s",
                self._ha_host,
                self._ha_port,
                self._ha_sub_topic(),
            )
            client.subscribe(self._ha_sub_topic(), qos=0)
        else:
            _LOGGER.error(
                "Broker %s:%s: connection refused (%s)",
                self._ha_host,
                self._ha_port,
                _CONNACK_CODES.get(rc, rc),
            )

    def _on_ha_message(self, client, userdata, msg):
        tv_topic = self._tv.ha_to_tv(msg.topic)
        self._tv.publish(tv_topic, msg.payload, msg.retain)

    def _publish_to_ha(self, topic, payload, retain):
        if self._ha.is_connected():
            self._ha.publish(topic, payload, qos=0, retain=retain)

    # --------------------------------------------------------------- start

    def start(self):
        # Detection is best-effort; explicit config overrides the descriptor.
        detected = {}
        try:
            detected = upnp.detect(
                self._tv.host,
                mac=self._tv_mac,
                brand=self._tv_brand,
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning(
                "TV %s: could not read device descriptor (%s); continuing with "
                "configured values and default auth order",
                self._tv.host,
                err,
            )

        transport_protocol = detected.get("transport_protocol")
        mac = self._tv_mac or detected.get("mac")
        brand = self._tv_brand or detected.get("brand")
        if mac is None and transport_protocol is not None and transport_protocol >= 3000:
            _LOGGER.error(
                "TV %s: dynamic auth requires the TV's MAC address - enable "
                "UPnP detection or set 'mac' in the config",
                self._tv.host,
            )

        self._ha.connect_async(self._ha_host, self._ha_port)
        self._ha.loop_start()
        self._tv.start(self._tv_auth_mode, transport_protocol, mac, brand)

    def close(self):
        self._tv.close()
        try:
            self._ha.loop_stop()
            self._ha.disconnect()
        except Exception:  # noqa: BLE001
            pass


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-c", "--config", required=True, help="YAML config file")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    with open(args.config, encoding="utf-8") as fh:
        config = yaml.safe_load(fh) or {}
    mqtt_cfg = config.get("mqtt", {})
    tv_configs = config.get("tvs") or ([config["tv"]] if config.get("tv") else [])

    if not tv_configs:
        _LOGGER.error("No TVs configured (expected 'tvs:' list or 'tv:')")
        return 1

    bridges = []
    for i, tv_cfg in enumerate(tv_configs, 1):
        name = tv_cfg.get("name", f"tv{i}")
        try:
            bridges.append(Bridge(name, tv_cfg, mqtt_cfg))
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Bridge %s: invalid configuration: %s", name, err)

    running = True

    def shutdown(_sig, _frame):
        nonlocal running
        running = False

    # Install early so a SIGTERM during startup is handled cleanly.
    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    for bridge in bridges:
        try:
            bridge.start()
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Bridge %s: failed to start: %s", bridge.name, err)

    if not bridges:
        return 1

    try:
        while running:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        for bridge in bridges:
            bridge.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())