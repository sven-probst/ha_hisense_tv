#!/usr/bin/env python3
"""Extract the current Vidaa/RemoteNOW client certificate from the app APK.

The TV's embedded MQTT broker requires mutual TLS with the client
certificate that ships inside the official RemoteNOW/Vidaa app as a PKCS#12
keystore. After the VIDAA 9 update the old, widely copied certificate files
are rejected, so a fresh pair must be extracted from the current app build.

Sources (either is enough):
  -a/--apk       one or more local APK/XAPK/APKM files (splits are scanned;
                 nested APK bundles are expanded automatically)
  -u/--url       a direct download URL that returns an .apk/.xapk file
  --apkmirror    an APKMirror release page; the variant download links are
                 resolved and fetched automatically (best-effort)
  --adb          pull the app from a connected phone via adb (most reliable)

Output: vidaa_client.pem and vidaa_client.key in -o/--out (default ./certs).

The keystore password is public knowledge from the app's native library
(getNewClientP12Password). The certificate itself belongs to the app vendor.
"""

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile

P12_PASSWORD = "186e990688070325a1c4b0ce275d2388"
CLIENT_SUBJECT_MARKER = "VidaaAppAndroidV01"
URL_DEFAULT_TIMEOUT = 120
BUNDLE_EXTS = (".apk", ".xapk", ".apkm", ".zip")
USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0 Mobile Safari/537.36"
)
# OpenSSL 3+ disables the RC2 legacy ciphers the keystore uses; without
# -legacy even the right password is rejected before it is checked.
OPENSSL_PKCS12_ARGS = ["-legacy"]

ADB_PACKAGES = [
    "com.universal.remote.multi",
    "com.hisense.hitvremoteservice",
    "tv.vidaa.app",
    "com.vidaa.app",
    "com.hisense.vidaa",
]


def log(msg, *args):
    print(msg % args, file=sys.stderr)


def check_openssl():
    if shutil.which("openssl") is None:
        sys.exit("openssl is required but not on PATH")


def handle_zip_errors(func, path, exc_info):
    log("warning: could not clean up %s (%s)", path, exc_info[1])


def collect_files(apk_paths, url, apkmirror_url, adb_pkg):
    files = []
    tmpdirs = []
    if apk_paths:
        for path in apk_paths:
            if not os.path.isfile(path):
                sys.exit(f"APK not found: {path}")
            files.append(path)
    elif url:
        tmpdir = tempfile.mkdtemp(prefix="hisense_apk_")
        target = os.path.join(tmpdir, "downloaded.bin")
        log("Downloading %s ...", url)
        try:
            fetch_to_file(url, target)
        except Exception as err:
            shutil.rmtree(tmpdir, onerror=handle_zip_errors)
            sys.exit(f"Download failed: {err}")
        if not looks_like_zip(target):
            shutil.rmtree(tmpdir, onerror=handle_zip_errors)
            sys.exit(
                "The downloaded file is not an APK/ZIP - the URL probably "
                "returned an HTML page (e.g. a Google Play or APKMirror "
                "download page). A direct .apk file link is required; use "
                "--apkmirror, --adb or a local APK instead."
            )
        files.append(target)
        tmpdirs.append(tmpdir)
    elif apkmirror_url:
        tmpdir = tempfile.mkdtemp(prefix="hisense_apkmirror_")
        files = apkmirror_download(apkmirror_url, tmpdir)
        tmpdirs.append(tmpdir)
    elif adb_pkg is not None:
        files, tmpdir = pull_from_adb(adb_pkg)
        tmpdirs.append(tmpdir)
    else:
        local = sorted(
            f for f in os.listdir(".")
            if f.lower().endswith((".apk", ".xapk", ".apkm"))
        )
        if not local:
            sys.exit(
                "No APK given and none found in this directory. Pass -a/--apk, "
                "-u/--url, --apkmirror or --adb."
            )
        files = local
    return files, tmpdirs


def fetch_to_file(url, target, referer=None):
    headers = {"User-Agent": USER_AGENT}
    if referer:
        headers["Referer"] = referer
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=URL_DEFAULT_TIMEOUT) as resp:
        with open(target, "wb") as fh:
            shutil.copyfileobj(resp, fh)


def apkmirror_download(mirror_url, tmpdir):
    """Fetch an APKMirror release page and download every variant.

    APKMirror serves files through four nested pages:

      1. release page        -> wrapper page links (``...-apk-download/``)
      2. wrapper page        -> variant links (``/download/?key=...``)
      3. variant page        -> the real endpoint embedded as the
                               ``id="download-link"`` anchor
      4. ``download.php?id=...&key=...`` -> the file (redirects to the CDN)

    The downloaded files are usually APKM/XAPK bundles, which the bundle
    scanner expands later.
    """
    release = os.path.join(tmpdir, "release.html")
    log("Fetching %s ...", mirror_url)
    try:
        fetch_to_file(mirror_url, release)
    except Exception as err:
        sys.exit(f"Could not fetch APKMirror page: {err}")

    pages = [release]
    referers = [mirror_url]

    wrappers = extract_wrapper_links(read_page(release), mirror_url)
    for i, wrapper in enumerate(wrappers[:3]):
        page = os.path.join(tmpdir, f"wrapper_{i}.html")
        try:
            fetch_to_file(wrapper, page, referer=mirror_url)
        except Exception as err:
            log("warning: wrapper %s failed: %s", wrapper, err)
            continue
        pages.append(page)
        referers.append(wrapper)

    file_links = set()
    variant_links = []
    for page, referer in zip(pages, referers):
        html = read_page(page)
        file_links.update(extract_file_links(html, referer))
        variant_links.extend(extract_download_links(html, referer))

    if not file_links and variant_links:
        log("No direct file links, descending through %d variant page(s) ...",
            len(variant_links))
        for i, variant in enumerate(dict.fromkeys(variant_links)):
            page = os.path.join(tmpdir, f"variant_{i}.html")
            try:
                fetch_to_file(variant, page, referer=mirror_url)
            except Exception as err:
                log("warning: variant %s failed: %s", variant, err)
                continue
            file_links.update(extract_file_links(read_page(page), variant))
            if file_links:
                break

    if not file_links:
        sys.exit(
            "No download link could be extracted from the APKMirror page. "
            "The page layout may have changed or the download is gated."
        )

    files = []
    for i, link in enumerate(sorted(file_links)):
        target = os.path.join(tmpdir, f"mirror_{i}.apkm")
        log("Downloading %s", link)
        try:
            fetch_to_file(link, target, referer=mirror_url)
        except Exception as err:
            log("warning: download failed: %s", err)
            continue
        if not looks_like_zip(target):
            log("warning: %s is not an APK/ZIP, skipping", target)
            continue
        files.append(target)
        if len(files) >= 8:
            break
    if not files:
        sys.exit("No usable download could be fetched from APKMirror.")
    return files


def read_page(path):
    with open(path, encoding="utf-8", errors="replace") as fh:
        return fh.read()


def resolve_url(href, base):
    if href.startswith("http://") or href.startswith("https://"):
        return href
    if href.startswith("//"):
        return "https:" + href
    if href.startswith("/"):
        from urllib.parse import urlparse

        parsed = urlparse(base)
        return f"{parsed.scheme}://{parsed.netloc}{href}"
    return base.rsplit("/", 1)[0] + "/" + href


def extract_file_links(html, base):
    html = html.replace("&amp;", "&")
    links = set()
    for href in re.findall(
        r'href="([^"]+download\.php\?id=\d+&key=[0-9a-fA-F]+)"', html
    ):
        links.add(resolve_url(href, base))
    for href in re.findall(
        r'id="download-link"[^>]*href="([^"]+)"', html
    ):
        links.add(resolve_url(href.replace("&amp;", "&"), base))
    return links


def extract_download_links(html, base):
    links = []
    for href in re.findall(r'href="([^"]+)"', html):
        href = href.replace("&amp;", "&")
        if "/download/" not in href:
            continue
        if href.startswith("#") or "downloadfile" in href or "download.php" in href:
            continue
        links.append(resolve_url(href, base))
    return list(dict.fromkeys(links))


def extract_wrapper_links(html, base):
    links = []
    for href in re.findall(r'href="([^"]+)"', html):
        href = href.replace("&amp;", "&")
        if href.endswith("/download/") or "/download/?arch=" in href:
            continue
        if href.endswith("-android-apk-download/") or "-apk-download/" in href:
            links.append(resolve_url(href, base))
    return list(dict.fromkeys(links))


def looks_like_zip(path):
    with open(path, "rb") as fh:
        return fh.read(2) == b"PK"


def collect_candidates(files, scan_root):
    """Return [(file_path, p12_entry)] for every keystore found.

    Nested APK bundles (APKMirror .apkm/.xapk, split APKs) are expanded
    recursively by extracting inner .apk/.zip members into scan_root.
    """
    found = []
    counter = [0]

    def scan(path, depth):
        try:
            with zipfile.ZipFile(path) as zf:
                names = zf.namelist()
        except zipfile.BadZipFile as err:
            log("warning: %s is not a valid ZIP: %s", os.path.basename(path), err)
            return
        p12 = [n for n in names if n.lower().endswith(".p12")]
        if p12:
            log("%s: %d .p12 file(s)", os.path.basename(path), len(p12))
            found.extend((path, n) for n in p12)
            return
        if depth >= 3:
            return
        nested = [n for n in names if n.lower().endswith(BUNDLE_EXTS)]
        if nested:
            log(
                "%s: expanding %d nested APK(s)",
                os.path.basename(path),
                len(nested),
            )
            for name in sorted(nested):
                counter[0] += 1
                inner = os.path.join(scan_root, f"nested_{counter[0]}.apk")
                try:
                    with zipfile.ZipFile(path) as zf, zf.open(name) as src, open(
                        inner, "wb"
                    ) as dst:
                        shutil.copyfileobj(src, dst)
                except (KeyError, zipfile.BadZipFile) as err:
                    log("warning: could not extract %s: %s", name, err)
                    continue
                scan(inner, depth + 1)
        else:
            log("%s: no .p12 found", os.path.basename(path))

    for path in files:
        scan(path, 0)
    return found


def pull_from_adb(package):
    candidates = [package] if package else ADB_PACKAGES
    remote_paths = []
    tried = []
    for pkg in candidates:
        pm = subprocess.run(
            f"adb shell pm path {pkg}",
            shell=True,
            capture_output=True,
            text=True,
        )
        tried.append(pkg)
        if pm.returncode == 0 and "package:" in pm.stdout:
            remote_paths = re.findall(r"package:(\S+)", pm.stdout)
            break
    if not remote_paths:
        sys.exit(
            "adb could not locate the app (tried: %s). Install the "
            "RemoteNOW/Vidaa app, connect the phone, and retry."
            % ", ".join(tried)
        )
    tmpdir = tempfile.mkdtemp(prefix="hisense_adb_")
    local = []
    for i, remote in enumerate(remote_paths):
        target = os.path.join(tmpdir, f"apk_{i}.apk")
        pull = subprocess.run(
            f"adb pull {remote} {target}", shell=True, capture_output=True, text=True
        )
        if pull.returncode != 0:
            sys.exit(f"adb pull failed for {remote}: {pull.stderr.strip()}")
        local.append(target)
    return local, tmpdir


def is_client_keystore(p12_path, password):
    result = subprocess.run(
        [
            "openssl", "pkcs12",
            *OPENSSL_PKCS12_ARGS,
            "-in", p12_path,
            "-clcerts", "-nokeys",
            "-passin", f"pass:{password}",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return False
    return CLIENT_SUBJECT_MARKER in result.stdout


def extract_pair(p12_path, outdir, password):
    pem = os.path.join(outdir, "vidaa_client.pem")
    key = os.path.join(outdir, "vidaa_client.key")

    cert = subprocess.run(
        [
            "openssl", "pkcs12",
            *OPENSSL_PKCS12_ARGS,
            "-in", p12_path,
            "-clcerts", "-nokeys",
            "-passin", f"pass:{password}",
        ],
        capture_output=True,
        text=True,
    )
    keydone = subprocess.run(
        [
            "openssl", "pkcs12",
            *OPENSSL_PKCS12_ARGS,
            "-in", p12_path,
            "-nocerts", "-nodes",
            "-passin", f"pass:{password}",
        ],
        capture_output=True,
        text=True,
    )
    if cert.returncode != 0 or keydone.returncode != 0:
        return False

    with open(pem, "w", encoding="utf-8") as fh:
        fh.write(cert.stdout)
    with open(key, "w", encoding="utf-8") as fh:
        fh.write(keydone.stdout)
    os.chmod(key, 0o600)
    try:
        os.chmod(pem, 0o644)
    except OSError:
        pass
    return True


def verify_pair(outdir):
    pem = os.path.join(outdir, "vidaa_client.pem")
    key = os.path.join(outdir, "vidaa_client.key")
    cert_pub = subprocess.run(
        ["openssl", "x509", "-in", pem, "-pubkey", "-noout"],
        capture_output=True,
        text=True,
    )
    key_pub = subprocess.run(
        ["openssl", "pkey", "-in", key, "-pubout"],
        capture_output=True,
        text=True,
    )
    normalise = lambda s: re.sub(r"\s+", "", s)
    if cert_pub.returncode != 0 or key_pub.returncode != 0:
        return False
    return normalise(cert_pub.stdout) == normalise(key_pub.stdout)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-a", "--apk", action="append", metavar="APK",
                        help="local APK file (repeatable; splits are supported)")
    parser.add_argument("-u", "--url", metavar="URL",
                        help="direct download URL returning an .apk/.xapk file")
    parser.add_argument("--apkmirror", metavar="URL",
                        help="APKMirror release page to download from "
                             "(variant links are resolved automatically)")
    parser.add_argument("--adb", nargs="?", const="", metavar="PACKAGE",
                        help="pull the app from a connected device via adb "
                             "(default: common Vidaa/RemoteNOW package names)")
    parser.add_argument("-o", "--out", default="certs",
                        help="output directory (default: ./certs)")
    parser.add_argument("--p12-password", default=P12_PASSWORD,
                        help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    check_openssl()

    files, tmpdirs = collect_files(args.apk, args.url, args.apkmirror, args.adb)
    if not files:
        sys.exit("No APK available to scan.")

    os.makedirs(args.out, exist_ok=True)
    workdir = tempfile.mkdtemp(prefix="hisense_cert_")
    try:
        candidates = collect_candidates(files, workdir)
        if not candidates:
            sys.exit("No .p12 keystore found in the given APKs.")

        def priority(item):
            name = item[1].lower()
            if name == "assets/client_mobile_android.p12":
                return 0
            if name.startswith("res/") and name.endswith(".p12"):
                return 1
            return 2

        candidates.sort(
            key=lambda item: (
                priority(item),
                os.path.basename(item[0]),
                item[1],
            )
        )

        for apk_path, entry in candidates:
            p12_local = os.path.join(workdir, "k.p12")
            with zipfile.ZipFile(apk_path) as zf:
                with zf.open(entry) as src, open(p12_local, "wb") as dst:
                    shutil.copyfileobj(src, dst)
            log("trying %s from %s ...", entry, os.path.basename(apk_path))
            if not is_client_keystore(p12_local, args.p12_password):
                log("  not the client keystore (wrong password or no private key)")
                continue
            if not extract_pair(p12_local, args.out, args.p12_password):
                sys.exit("  could not convert the keystore to PEM")
            if not verify_pair(args.out):
                sys.exit(
                    "certificate and private key do not match in %s; "
                    "the keystore is inconsistent" % args.out
                )
            log("OK: %s/vidaa_client.pem, %s/vidaa_client.key",
                args.out, args.out)
            log("Certificate subject contains %s", CLIENT_SUBJECT_MARKER)
            return 0
        sys.exit(
            "None of the found .p12 files is the client keystore. "
            f"The P12 password '{args.p12_password}' may be outdated for this "
            "app build."
        )
    finally:
        shutil.rmtree(workdir, onerror=handle_zip_errors)
        for tmp in tmpdirs:
            shutil.rmtree(tmp, onerror=handle_zip_errors)


if __name__ == "__main__":
    sys.exit(main())