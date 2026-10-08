#!/usr/bin/env python3
"""One-shot certificate setup for the HA host.

Downloads the current Vidaa app APK (APKMirror by default), extracts the
client certificate + key the TV broker requires (mTLS) and places them where
the integration/bridge expects them:

    /config/certs/vidaa_client.pem
    /config/certs/vidaa_client.key

Usage on the Home Assistant host:

    python3 custom_components/hisense_tv/bridge/setup_certs.py            # APKMirror
    python3 custom_components/hisense_tv/bridge/setup_certs.py -a app.apk  # lokale APK/XAPK
    python3 custom_components/hisense_tv/bridge/setup_certs.py -o /config/certs

Then enable the bridge in the integration options and re-pair with the PIN.
"""

import argparse
import importlib.util
import os
import sys

DEFAULT_APKMIRROR_URL = (
    "https://www.apkmirror.com/apk/v-america-operations-inc/vidaa-smart-tv/"
    "vidaa-smart-tv-1-09-06-002-3-release/"
)
DEFAULT_OUT = "/config/certs"


def _load_extract_certs():
    """Load extract_certs.py from this directory without importing the HA
    integration package (works even on a plain machine without
    homeassistant)."""
    here = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(here, "extract_certs.py")
    spec = importlib.util.spec_from_file_location("hisense_extract_certs", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apkmirror",
        default=DEFAULT_APKMIRROR_URL,
        help="APKMirror release page (default: latest known Vidaa build)",
    )
    parser.add_argument(
        "-a", "--apk", metavar="APK",
        help="use a local APK/XAPK instead of downloading",
    )
    parser.add_argument(
        "-o", "--out", default=DEFAULT_OUT,
        help="output directory (default: %s)" % DEFAULT_OUT,
    )
    args = parser.parse_args(argv)

    os.makedirs(args.out, exist_ok=True)

    extract = _load_extract_certs()
    cmd = ["-o", args.out]
    if args.apk:
        cmd = ["-a", args.apk] + cmd
    else:
        cmd = ["--apkmirror", args.apkmirror] + cmd

    try:
        extract.main(cmd)
    except SystemExit as exc:
        if exc.code:
            return exc.code

    pem = os.path.join(args.out, "vidaa_client.pem")
    key = os.path.join(args.out, "vidaa_client.key")
    for path in (pem, key):
        if not os.path.isfile(path):
            print("ERROR: %s was not created" % path, file=sys.stderr)
            return 1
    try:
        os.chmod(key, 0o600)
    except OSError:
        pass

    print()
    print("OK - certificate and key are in place:")
    print("  %s" % pem)
    print("  %s" % key)
    print()
    print("Next steps in Home Assistant:")
    print("  1. Integration -> Edit (options) -> step 'Bridge':")
    print("       TV IP, certfile=%s, keyfile=%s" % (pem, key))
    print("     Save (starts the supervised bridge).")
    print("  2. Integration -> Reauth -> enter the PIN the TV shows.")
    return 0


if __name__ == "__main__":
    sys.exit(main())