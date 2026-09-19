"""Credential generation for the Hisense/VIDAA TV MQTT broker.

With the firmware move to VIDAA 9 (transport_protocol >= 3000) the TV no
longer accepts the static ``hisenseservice``/``multimqttservice`` login that
the classic Mosquitto bridge configuration used. Connections now require a
timestamp-based username/password and a MAC-derived MQTT client id.

The algorithm was reverse engineered from the official Vidaa app's
``libmqttcrypt.so``. The reference implementation lives in
warrenrees/pyvidaa (MIT license); this module is a self-contained port of
the parts the bridge needs.
"""

import hashlib
import time
from dataclasses import dataclass
from enum import Enum
from typing import Optional

# Credential constants (from libmqttcrypt.so, see pyvidaa/config/constants.py)
PATTERN = "38D65DC30F45109A369A86FCE866A85B"
VALUE_SUFFIX_LEGACY = "h*i&s%e!r^v0i1c9"  # transport_protocol < 3290
VALUE_SUFFIX_MODERN = "h!i@s#$v%i^d&a*a"  # transport_protocol >= 3290
TIME_XOR_CONSTANT = 0x569814772B03A968

# Fixed login for pre-dynamic firmware (transport_protocol < 3000)
STATIC_USERNAME = "hisenseservice"
STATIC_PASSWORD = "multimqttservice"

# transport_protocol thresholds defining the authentication scheme
PROTOCOL_MIDDLE = 3000
PROTOCOL_MODERN = 3290


class AuthMethod(Enum):
    """Authentication scheme selected by the TV's transport_protocol."""

    STATIC = "static"  # pre-dynamic firmware: fixed login, no MAC/timestamp
    LEGACY = "legacy"  # dynamic, no XOR username, legacy suffix
    MIDDLE = "middle"  # dynamic, XOR username, legacy suffix
    MODERN = "modern"  # dynamic, XOR username, modern suffix


def method_order(transport_protocol: Optional[int] = None) -> list:
    """Most likely authentication methods first, covering every method.

    A TV below the middle threshold predates the dynamic algorithm entirely,
    so the fixed static login leads there. Newer firmware rejects it, so
    static trails the dynamic methods rather than being dropped: a TV that
    misreports its version still gets every method tried.
    """
    if transport_protocol is not None and transport_protocol < PROTOCOL_MIDDLE:
        return [
            AuthMethod.STATIC,
            AuthMethod.LEGACY,
            AuthMethod.MIDDLE,
            AuthMethod.MODERN,
        ]
    if transport_protocol is not None and transport_protocol < PROTOCOL_MODERN:
        return [
            AuthMethod.MIDDLE,
            AuthMethod.MODERN,
            AuthMethod.LEGACY,
            AuthMethod.STATIC,
        ]
    # >= PROTOCOL_MODERN, or unknown: modern is the best guess.
    return [
        AuthMethod.MODERN,
        AuthMethod.MIDDLE,
        AuthMethod.LEGACY,
        AuthMethod.STATIC,
    ]


@dataclass
class MQTTCredentials:
    """MQTT connect credentials."""

    client_id: str  # MQTT CONNECT client id (and the TV-side topic id)
    topic_client_id: str  # client id used inside the /remoteapp/# topics
    username: str
    password: str


def _md5(value: str) -> str:
    """MD5 hex digest, uppercase (matches the official app)."""
    return hashlib.md5(value.encode("utf-8")).hexdigest().upper()


def _digit_sum(value: int) -> int:
    return sum(int(d) for d in str(abs(value)))


def generate_dynamic(
    mac_or_uuid: str,
    brand: str = "his",
    method: AuthMethod = AuthMethod.MODERN,
    operation: str = "vidaacommon",
    timestamp: Optional[int] = None,
) -> MQTTCredentials:
    """Build MQTT connect credentials for a dynamic-auth TV.

    Args:
        mac_or_uuid: The TV's real MAC address (e.g. "AA:BB:CC:DD:EE:FF").
            Dynamic credentials are a hash of it, so a wrong MAC makes the TV
            reject the connection despite a successful CONNACK.
        brand: Brand string used in client_id/username ("his", "tve", ...).
            Must be the value the TV itself uses or authentication fails.
        method: AuthMethod.LEGACY/MIDDLE/MODERN.
        operation: "vidaacommon" (remote control) or "vidaavoice".
        timestamp: Unix timestamp, defaults to now.
    """
    uuid = mac_or_uuid
    if ":" not in uuid and "-" not in uuid and len(uuid) == 12:
        uuid = ":".join(uuid[i : i + 2] for i in range(0, 12, 2))

    race_md5 = _md5(f"{PATTERN}${uuid}")[:6]
    client_id = f"{uuid}${brand}${race_md5}_{operation}_001"

    ts = int(time.time()) if timestamp is None else int(timestamp)

    if method == AuthMethod.LEGACY:
        username = f"{brand}${ts}"
    else:
        username = f"{brand}${ts ^ TIME_XOR_CONSTANT}"

    suffix = VALUE_SUFFIX_MODERN if method == AuthMethod.MODERN else VALUE_SUFFIX_LEGACY
    remainder = _digit_sum(ts) % 10
    value_md5 = _md5(f"{brand}{remainder}{suffix}")[:6]
    password = _md5(f"{ts}${value_md5}")

    return MQTTCredentials(
        client_id=client_id,
        topic_client_id=client_id,
        username=username,
        password=password,
    )


def generate_static(client_id: str) -> MQTTCredentials:
    """Credentials for pre-dynamic firmware (old Mosquitto bridge behavior)."""
    return MQTTCredentials(
        client_id=client_id,
        topic_client_id=client_id,
        username=STATIC_USERNAME,
        password=STATIC_PASSWORD,
    )