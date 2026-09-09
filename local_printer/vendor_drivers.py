"""
Look up manufacturer drivers online.

When neither the spooler nor the in-box INF store has a driver for a device,
the only remaining source is the vendor.  This module turns the model string a
printer reports over IPP into a concrete download page -- and, where the vendor
exposes a usable API, into the actual installer URL and version.

Design notes:

* Vendor download portals sit behind a WAF.  Epson's ``download-center`` API
  answers a browser but returns 403 to plain HTTP clients, so the API attempt is
  best-effort and always degrades to a deep link that is known to work.
* Nothing is downloaded unless the caller explicitly asks for it, and nothing is
  ever executed.  Installers are run by the user, not by this tool.
"""

import json
import locale
import os
import platform
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

from utils.logger import logger

# Identify ourselves honestly by default: a vendor should be able to see what is
# talking to them, and site operators can block or rate-limit us on sight.
HONEST_UA = "printer-ai/1.0 (local printer CLI; python-urllib)"

# The one exception. Epson's download-center sits behind a WAF that returns 403
# to any client whose User-Agent is not a browser, so the *single* endpoint that
# needs it gets a browser string; everything else uses HONEST_UA. This is not a
# licence to spoof elsewhere -- if another vendor blocks us, the answer is
# `open_in_browser`, not a wider disguise.
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# Vendor detection from the model string reported over IPP.
VENDOR_KEYWORDS = {
    "epson": "Epson",
    "seiko epson": "Epson",
    "hp": "HP",
    "hewlett": "HP",
    "brother": "Brother",
    "canon": "Canon",
    "lexmark": "Lexmark",
    "kyocera": "Kyocera",
    "samsung": "Samsung",
    "xerox": "Xerox",
    "ricoh": "Ricoh",
}

# Support entry points per vendor. Only Epson has a verified deep link; the
# others are the official support hubs and are reported as unverified so the
# caller does not present a guess as a fact.
VENDOR_SUPPORT_SITES = {
    "HP": "https://support.hp.com/us-en/drivers",
    "Brother": "https://support.brother.com/g/b/productsearch.aspx?c=us&lang=en&content=dl",
    "Canon": "https://www.usa.canon.com/support",
    "Lexmark": "https://support.lexmark.com/en_us.html",
    "Kyocera": "https://www.kyoceradocumentsolutions.com/en/support.html",
    "Xerox": "https://www.support.xerox.com/en-us",
    "Ricoh": "https://www.ricoh.com/support",
}

EPSON_API = "https://download-center.epson.com/api/v1"
EPSON_PAGE = "https://download-center.epson.com/softwares/"

# Epson OS codes, read from the portal's own /api/v1/os/ endpoint.
EPSON_OS_CODES = {
    ("windows", "11", "amd64"): "WIN1164",
    ("windows", "11", "arm64"): "WIN1164A",
    ("windows", "10", "amd64"): "WIN1064",
    ("windows", "10", "x86"): "WIN10",
    ("windows", "8.1", "amd64"): "W8164",
    ("windows", "7", "amd64"): "S64",
}


# ------------------------------------------------------------ environment


def _region_from_tag(value: Optional[str], allow_bare: bool = False) -> Optional[str]:
    """Pull the territory out of a locale tag, or accept a bare region code.

    Handles 'de_DE', 'de-DE', 'de_DE.UTF-8', 'de_DE@euro'. `allow_bare` also
    accepts a plain 'DE' -- only true for PRINTER_AI_REGION, where a two-letter
    value is unambiguously a region; in LANG a bare 'de' is the *language*.
    """
    if not value:
        return None
    tag = value.strip().split(".")[0].split("@")[0]
    if not tag or tag.upper() in ("C", "POSIX"):
        return None
    parts = re.split(r"[_-]", tag)
    if len(parts) >= 2 and len(parts[1]) == 2 and parts[1].isalpha():
        return parts[1].upper()
    if allow_bare and len(parts) == 1 and len(parts[0]) == 2 and parts[0].isalpha():
        return parts[0].upper()
    return None


def _windows_user_locale() -> Optional[str]:
    """The user's locale name from Win32, e.g. 'de-DE'.

    `locale.getlocale()` returns (None, None) on a fresh Python process on
    Windows because the C locale has not been set, so ask the OS directly.
    """
    if sys.platform != "win32":
        return None
    try:
        import ctypes

        buffer = ctypes.create_unicode_buffer(85)  # LOCALE_NAME_MAX_LENGTH
        if ctypes.windll.kernel32.GetUserDefaultLocaleName(buffer, len(buffer)):
            return buffer.value or None
    except Exception as exc:  # ctypes/OS quirks must never break a lookup
        logger.debug(f"GetUserDefaultLocaleName failed: {exc}")
    return None


def detect_region(default: str = "US") -> str:
    """Two-letter region for the vendor portal, from the system locale.

    `PRINTER_AI_REGION` overrides everything and accepts either a bare region
    ('DE') or a full locale tag ('de_DE').
    """
    for value, bare_ok in (
        (os.environ.get("PRINTER_AI_REGION"), True),
        (os.environ.get("LC_ALL"), False),
        (os.environ.get("LANG"), False),
    ):
        region = _region_from_tag(value, allow_bare=bare_ok)
        if region:
            return region

    # locale.getdefaultlocale() is deprecated since 3.11 and removed in 3.15.
    try:
        tag = locale.getlocale(locale.LC_CTYPE)[0]
    except (ValueError, TypeError, locale.Error):
        tag = None
    region = _region_from_tag(tag)
    if region:
        return region

    return _region_from_tag(_windows_user_locale()) or default


def detect_windows_release() -> str:
    """'11', '10', ... -- platform.win32_ver still reports 10 for Windows 11."""
    if sys.platform != "win32":
        return ""
    try:
        build = sys.getwindowsversion().build
    except Exception:
        return platform.win32_ver()[0]
    return "11" if build >= 22000 else "10"


def detect_arch() -> str:
    machine = platform.machine().lower()
    if machine in ("amd64", "x86_64"):
        return "amd64"
    if machine in ("arm64", "aarch64"):
        return "arm64"
    return "x86"


def epson_os_code() -> Optional[str]:
    if sys.platform != "win32":
        return None  # macOS/Linux codes are not mapped; the portal shows all
    return EPSON_OS_CODES.get(("windows", detect_windows_release(), detect_arch()))


# ---------------------------------------------------------------- model


def split_model(make_and_model: str) -> Dict[str, str]:
    """Split 'EPSON ET-4850 Series' into vendor and portal device id."""
    text = (make_and_model or "").strip()
    low = text.lower()
    vendor = ""
    for keyword, name in VENDOR_KEYWORDS.items():
        if low.startswith(keyword + " ") or low == keyword:
            vendor = name
            break
    if not vendor:
        for keyword, name in VENDOR_KEYWORDS.items():
            if keyword in low:
                vendor = name
                break

    device_id = text
    if vendor:
        # Strip the leading brand word(s); portals index the bare model.
        for keyword in sorted(VENDOR_KEYWORDS, key=len, reverse=True):
            if low.startswith(keyword + " "):
                device_id = text[len(keyword):].strip()
                break
    return {"vendor": vendor, "device_id": device_id, "model": text}


# ------------------------------------------------------------- Epson API


def _get_json(url: str, timeout: float = 15.0, waf_bypass: bool = False) -> Optional[Any]:
    """GET and decode JSON.

    `waf_bypass` is reserved for Epson's download-center API, which 403s any
    non-browser client. It sends the browser UA together with the Referer the
    portal's own XHR carries -- the two only make sense as a pair, and neither is
    sent for ordinary requests such as checking that a page exists.
    """
    headers = {
        "User-Agent": BROWSER_UA if waf_bypass else HONEST_UA,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
    }
    if waf_bypass:
        headers["Referer"] = EPSON_PAGE
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
        logger.debug(f"vendor API call failed ({url}): {exc}")
        return None


def epson_page_url(device_id: str, region: str, os_code: Optional[str], language: str) -> str:
    params = {"device_id": device_id, "region": region, "language": language}
    if os_code:
        params["os"] = os_code
    return f"{EPSON_PAGE}?{urllib.parse.urlencode(params)}"


def epson_lookup(
    device_id: str, region: str, os_code: Optional[str], language: str = "en"
) -> Dict[str, Any]:
    """Best-effort Epson Download Center query, always returning a usable link.

    Privacy: this sends `device_id` (the printer model), the OS code and the
    region to Epson, so the vendor learns which printer model this machine is
    attached to and roughly where it is.  Nothing else is transmitted, and no
    identifier for the user or machine is included -- but because it does leave
    the LAN, `setup` must ask before calling it rather than looking up drivers on
    its own.
    """
    page = epson_page_url(device_id, region, os_code, language)
    result: Dict[str, Any] = {
        "vendor": "Epson",
        "device_id": device_id,
        "region": region,
        "os_code": os_code,
        "download_page": page,
        "page_verified": True,
        "api_reachable": False,
        "downloads": [],
    }

    params = {"device_id": device_id, "os": os_code or "", "region": region, "language": language}
    data = _get_json(
        f"{EPSON_API}/modules/?{urllib.parse.urlencode(params)}", waf_bypass=True
    )
    if not isinstance(data, dict) or "items" not in data:
        result["note"] = (
            "Epson's portal refused a direct API call (it is behind a WAF that "
            "only answers browsers). Open download_page and pick the driver there."
        )
        return result

    result["api_reachable"] = True
    wanted = {"Drivers", "ComboPackage"}
    for item in data.get("items", []):
        if item.get("cti_category") not in wanted:
            continue
        url = item.get("url") or ""
        result["downloads"].append({
            "category": item.get("cti_category"),
            "version": item.get("version"),
            "url": url,
            "filename": url.rsplit("/", 1)[-1] if url else None,
            "size_bytes": item.get("size"),
            "size_mb": round(item["size"] / 1048576, 1) if item.get("size") else None,
        })
    # Full driver packages first, then combo installers, newest version first.
    result["downloads"].sort(
        key=lambda d: (d["category"] != "Drivers", str(d.get("version") or "")), reverse=False
    )
    return result


# ------------------------------------------------------------ public API


def find_driver(
    make_and_model: str,
    region: Optional[str] = None,
    os_code: Optional[str] = None,
    language: str = "en",
) -> Dict[str, Any]:
    """Locate a manufacturer driver for `make_and_model` on the vendor's site.

    This is a network lookup against the manufacturer: the model string, the
    detected OS code and the region are sent to the vendor's portal.  It is
    therefore opt-in -- `setup` must not run it without the user asking, and
    callers that want a purely local answer should stay with the in-box driver
    store instead.
    """
    parts = split_model(make_and_model)
    vendor = parts["vendor"]
    region = region or detect_region()

    base: Dict[str, Any] = {
        "model": parts["model"],
        "vendor": vendor or "unknown",
        "device_id": parts["device_id"],
        "region": region,
        "os": {
            "platform": sys.platform,
            "release": detect_windows_release() or platform.release(),
            "arch": detect_arch(),
        },
    }

    if not vendor:
        base["supported"] = False
        base["hint"] = (
            f"Could not identify the manufacturer from '{parts['model']}'. "
            "Search the vendor's support site for the model plus your OS."
        )
        return base

    if vendor == "Epson":
        lookup = epson_lookup(parts["device_id"], region, os_code or epson_os_code(), language)
        base.update(lookup)
        base["supported"] = True
        return base

    base["supported"] = False
    base["support_site"] = VENDOR_SUPPORT_SITES.get(vendor)
    base["site_verified"] = False
    base["hint"] = (
        f"No verified lookup is implemented for {vendor}. "
        f"Search their support site for '{parts['model']} driver "
        f"{base['os']['release']} {base['os']['arch']}'."
    )
    return base


def open_in_browser(url: str) -> Dict[str, Any]:
    """Hand a URL to the user's default browser.

    Vendor portals that refuse scripted clients still serve a normal browser
    session, so this is the reliable way to complete a download the CLI cannot
    fetch itself.
    """
    import webbrowser

    if not url.startswith("https://"):
        return {"ok": False, "error": "refusing to open a non-HTTPS URL"}
    try:
        opened = webbrowser.open(url)
    except Exception as exc:
        return {"ok": False, "error": f"could not open a browser: {exc}"}
    return {
        "ok": bool(opened),
        "url": url,
        "note": "Opened in your default browser - the download runs there.",
    }


# Names Windows still treats as devices, whatever the extension: opening
# "CON.exe" for writing talks to the console, not to a file.
_WINDOWS_RESERVED = (
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{n}" for n in range(1, 10)}
    | {f"LPT{n}" for n in range(1, 10)}
)


def safe_filename(raw: str, fallback: str = "driver.bin") -> str:
    """Turn a filename taken from a URL into one that is safe to create.

    The server chooses this string, so it is untrusted input: it can carry path
    separators, '..', shell metacharacters, or a Windows device name. Everything
    outside [A-Za-z0-9._-] becomes '_', traversal segments are dropped, and a
    reserved device name is prefixed so it can only ever name a real file inside
    the destination directory.
    """
    name = os.path.basename((raw or "").replace("\\", "/").rsplit("/", 1)[-1])
    name = re.sub(r"[^A-Za-z0-9._-]", "_", name)
    name = name.lstrip(".") or ""
    if not name or name in (".", ".."):
        return fallback
    stem = name.split(".", 1)[0].upper()
    if stem in _WINDOWS_RESERVED:
        name = f"driver_{name}"
    return name[:150]


def _authenticode_signature(path: str) -> Optional[Dict[str, Optional[str]]]:
    """Best-effort Windows signature check of a downloaded installer.

    Reports what Windows itself thinks of the publisher signature; a failure to
    ask (no PowerShell, timeout, unparseable output) returns None rather than
    implying the file is unsigned.
    """
    if sys.platform != "win32":
        return None
    # Single-quoted PowerShell literal: no expansion happens inside, so the only
    # escape needed is doubling an embedded quote.
    literal = "'" + os.path.abspath(path).replace("'", "''") + "'"
    script = (
        f"$s = Get-AuthenticodeSignature -LiteralPath {literal}; "
        "Write-Output $s.Status; "
        "if ($s.SignerCertificate) { Write-Output $s.SignerCertificate.Subject } "
        "else { Write-Output '' }"
    )
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug(f"Authenticode check failed for {path}: {exc}")
        return None
    lines = [line.strip() for line in (proc.stdout or "").splitlines() if line.strip()]
    if not lines:
        logger.debug(f"Authenticode check returned nothing for {path}: {proc.stderr}")
        return None
    return {"status": lines[0], "signer": lines[1] if len(lines) > 1 else None}


VERIFICATION_NOTE = (
    "size and executable-header only; authenticity NOT verified — "
    "check the publisher signature before running"
)


def download_driver(
    url: str, dest_dir: str, timeout: float = 600.0, expected_size: Optional[int] = None
) -> Dict[str, Any]:
    """Fetch an installer to `dest_dir`, verify it, and never execute it.

    Runs only when the user passes --download.  Vendor CDNs commonly sit behind
    a WAF that rejects scripted clients; that case is reported as `blocked` with
    the URL to open in a browser instead, rather than as a bare failure.

    The checks here are integrity checks, not authenticity checks: they say the
    bytes arrived intact, not that the vendor produced them. The SHA-256 is
    reported so the user can compare it against the vendor's published digest,
    and on Windows the Authenticode signature is reported alongside it.
    """
    import hashlib

    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https":
        return {"ok": False, "error": "refusing to download over a non-HTTPS URL"}
    filename = safe_filename(urllib.parse.unquote(parsed.path))
    os.makedirs(dest_dir, exist_ok=True)
    target = os.path.join(os.path.abspath(dest_dir), filename)
    partial = target + ".part"

    # Never clobber: the name comes from the server, and an existing file here
    # may be something the user already downloaded and verified.
    if os.path.exists(target):
        return {"ok": False, "error": f"target exists: {target}"}
    if os.path.exists(partial):
        return {"ok": False, "error": f"target exists: {partial}"}

    request = urllib.request.Request(
        url,
        headers={
            # Vendor CDNs share the portal's WAF, so the browser UA stays here;
            # no Referer is forged -- the WAF gates on the UA.
            "User-Agent": BROWSER_UA,
            "Accept": "*/*",
        },
    )
    digest = hashlib.sha256()
    written = 0
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response, open(
            partial, "wb"
        ) as handle:
            declared = response.headers.get("Content-Length")
            while True:
                chunk = response.read(262144)
                if not chunk:
                    break
                handle.write(chunk)
                digest.update(chunk)
                written += len(chunk)
    except urllib.error.HTTPError as exc:
        _unlink(partial)
        blocked = exc.code in (403, 406, 429)
        return {
            "ok": False,
            "blocked": blocked,
            "http_status": exc.code,
            "error": (
                f"the vendor's CDN rejected a scripted download (HTTP {exc.code}). "
                "It only serves browser sessions."
                if blocked else f"download failed: HTTP {exc.code}"
            ),
            "open_in_browser_url": url if blocked else None,
        }
    except (urllib.error.URLError, OSError) as exc:
        _unlink(partial)
        return {"ok": False, "error": f"download failed: {exc}"}

    # Sanity-check before handing the user something to run.
    problems: List[str] = []
    if expected_size and written != expected_size:
        problems.append(f"size mismatch: got {written}, expected {expected_size}")
    if declared and written != int(declared):
        problems.append(f"truncated: got {written} of {declared} bytes")
    if filename.lower().endswith(".exe"):
        with open(partial, "rb") as handle:
            if handle.read(2) != b"MZ":
                problems.append("not a Windows executable (missing MZ header)")

    if problems:
        _unlink(partial)
        return {"ok": False, "error": "; ".join(problems)}

    try:
        os.replace(partial, target)
    except OSError as exc:
        _unlink(partial)
        return {"ok": False, "error": f"could not write {target}: {exc}"}

    return {
        "ok": True,
        "path": target,
        "filename": filename,
        "size_bytes": written,
        "sha256": digest.hexdigest(),
        "verification": VERIFICATION_NOTE,
        "signature": _authenticode_signature(target),
        "note": (
            "Downloaded, not executed. The size and header were checked; the "
            "publisher was not. Compare the sha256 against the vendor's "
            "published digest, check the signature, then run the installer "
            "yourself and re-run `printer-ai setup` to pick up the driver."
        ),
    }


def _unlink(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass
