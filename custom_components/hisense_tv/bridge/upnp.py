"""UPnP descriptor probing for Hisense/VIDAA TVs.

The descriptor at http://<ip>:38400/MediaServer/rendererdevicedesc.xml
(some VIDAA OS versions serve it on 18400) carries the values that decide
how authentication must work:

- ``transport_protocol``  selects the AuthMethod (static vs. dynamic)
- ``mac`` / ``macEthernet`` / ``macWifi``  feed the dynamic credential hash
- ``brand``                is part of the client_id and credential hashes

The MAC in particular matters: the TV recomputes the credential hash with
its own hardware MAC, so using anything but the descriptor's value causes
the connection to be dropped right after CONNACK.
"""

import re
import urllib.request
from typing import Dict, Optional

UPNP_PORTS = (38400, 18400)
DESCRIPTOR_PATH = "/MediaServer/rendererdevicedesc.xml"

_KEYVALUE = re.compile(r"(\w+)=([^=\s]+)")


def fetch_descriptor_text(host: str, ports=UPNP_PORTS, timeout: float = 5.0) -> str:
    """Fetch the device descriptor XML from the TV."""
    last_error: Optional[Exception] = None
    for port in ports:
        url = f"http://{host}:{port}{DESCRIPTOR_PATH}"
        try:
            with urllib.request.urlopen(url, timeout=timeout) as response:
                return response.read().decode("utf-8", "replace")
        except Exception as err:  # noqa: BLE001
            last_error = err
    raise ConnectionError(f"Could not fetch TV descriptor at {host}: {last_error}")


def parse_descriptor(text: str) -> Dict[str, str]:
    """Parse the key=value pairs embedded in the descriptor."""
    if not text:
        return {}
    return dict(_KEYVALUE.findall(text))


def detect(
    host: str,
    ports=UPNP_PORTS,
    timeout: float = 5.0,
    mac: Optional[str] = None,
    brand: Optional[str] = None,
) -> Dict[str, Optional[str]]:
    """Probe the TV descriptor and return auth-relevant device info.

    Explicitly provided ``mac``/``brand`` win over the descriptor.
    """
    fields = parse_descriptor(fetch_descriptor_text(host, ports, timeout))
    resolved_mac = (
        mac
        or fields.get("mac")
        or fields.get("macEthernet")
        or fields.get("macWifi")
    )
    try:
        transport_protocol = (
            int(fields["transport_protocol"])
            if fields.get("transport_protocol") is not None
            else None
        )
    except ValueError:
        transport_protocol = None
    return {
        "transport_protocol": transport_protocol,
        "mac": resolved_mac,
        "brand": brand or fields.get("brand"),
    }