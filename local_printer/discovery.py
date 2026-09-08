"""
Cross-platform network printer discovery.

Windows' spooler reports the *cached* state of a print queue, so a printer that
is switched off, or that moved to a different subnet, still shows up as
``idle``.  The helpers here talk to the device itself instead: a TCP probe of
the usual printing ports plus a real IPP Get-Printer-Attributes request, which
returns the model and live state straight from the hardware.

Pure stdlib, no third-party dependencies, works on Windows/macOS/Linux.
"""

import re
import socket
import ssl
import struct
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional, Tuple

from utils.logger import logger

# Ports that indicate "something here speaks a printing protocol"
PRINTER_PORTS: Dict[int, str] = {
    9100: "raw",   # JetDirect / AppSocket
    631: "ipp",    # IPP / IPPS
    515: "lpd",    # LPR
}

# Probed for extra context, but not treated as proof of a printer
EXTRA_PORTS: Dict[int, str] = {80: "http", 443: "https"}

# Best-effort vendor hint from the MAC prefix. Only a fallback for devices that
# refuse IPP -- `printer-make-and-model` from IPP is authoritative when present.
OUI_HINTS: Dict[str, str] = {
    "6855D4": "Seiko Epson",
    "0026AB": "Seiko Epson",
    "A4EE57": "Seiko Epson",
    "008077": "Brother",
    "001B78": "HP",
    "001E8F": "Canon",
}


# ---------------------------------------------------------------- TCP probing


def probe_ports(
    host: str, ports: Optional[List[int]] = None, timeout: float = 0.6
) -> Dict[int, bool]:
    """Check which TCP ports on `host` accept a connection."""
    if ports is None:
        ports = list(PRINTER_PORTS) + list(EXTRA_PORTS)

    def _one(port: int) -> Tuple[int, bool]:
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return port, True
        except OSError:
            return port, False

    with ThreadPoolExecutor(max_workers=len(ports)) as pool:
        return dict(pool.map(_one, ports))


def is_printer_host(open_ports: Dict[int, bool]) -> bool:
    """True if any port specific to printing is open."""
    return any(open_ports.get(p) for p in PRINTER_PORTS)


def open_port_names(open_ports: Dict[int, bool]) -> List[str]:
    """Readable list of the open ports, e.g. ['ipp:631', 'raw:9100']."""
    names = {**PRINTER_PORTS, **EXTRA_PORTS}
    return [f"{names.get(p, 'tcp')}:{p}" for p, is_open in sorted(open_ports.items()) if is_open]


# ----------------------------------------------------------------------- IPP

IPP_STATES = {3: "idle", 4: "processing", 5: "stopped"}

# IPP value tags carrying human-readable text
_TEXT_TAGS = {0x41, 0x42, 0x44, 0x45, 0x47, 0x48, 0x49}
_INT_TAGS = {0x21, 0x23}  # integer, enum
_BOOL_TAG = 0x22

# Vendors disagree on the queue path; these cover the common cases.
DEFAULT_IPP_PATHS = ["/ipp/print", "/ipp/printer", "/printers/ipp", "/"]

_WANTED_ATTRS = [
    b"printer-name",
    b"printer-make-and-model",
    b"printer-state",
    b"printer-state-reasons",
    b"printer-is-accepting-jobs",
    b"printer-location",
    b"printer-info",
    b"printer-uri-supported",
    b"media-default",
    b"media-supported",
    b"sides-supported",
    b"print-color-mode-supported",
    b"printer-resolution-supported",
    b"copies-supported",
    b"media-source-supported",
    b"media-type-supported",
]


def _ipp_attr(tag: int, name: bytes, value: bytes) -> bytes:
    return struct.pack(">BH", tag, len(name)) + name + struct.pack(">H", len(value)) + value


def _ipp_build_request(host: str, path: str, request_id: int = 1) -> bytes:
    """Encode a minimal IPP/1.1 Get-Printer-Attributes operation."""
    body = struct.pack(">BBHI", 1, 1, 0x000B, request_id)  # version, op-id, request-id
    body += b"\x01"  # operation-attributes-tag
    body += _ipp_attr(0x47, b"attributes-charset", b"utf-8")
    body += _ipp_attr(0x48, b"attributes-natural-language", b"en")
    body += _ipp_attr(0x45, b"printer-uri", f"ipp://{host}{path}".encode())
    body += _ipp_attr(0x44, b"requested-attributes", _WANTED_ATTRS[0])
    for attr in _WANTED_ATTRS[1:]:
        # Additional values of one attribute are sent with a zero-length name.
        body += _ipp_attr(0x44, b"", attr)
    body += b"\x03"  # end-of-attributes-tag
    return body


def _ipp_parse_response(data: bytes) -> Dict[str, Any]:
    """Decode the attribute groups of an IPP response into a plain dict."""
    if len(data) < 9:
        return {}
    attrs: Dict[str, Any] = {}
    pos = 8  # skip version(2) + status-code(2) + request-id(4)
    current = None
    while pos < len(data):
        tag = data[pos]
        pos += 1
        if tag == 0x03:  # end-of-attributes
            break
        if tag < 0x10:  # delimiter starting a new attribute group
            current = None
            continue
        if pos + 2 > len(data):
            break
        (name_len,) = struct.unpack(">H", data[pos : pos + 2])
        pos += 2
        name = data[pos : pos + name_len].decode("utf-8", "replace")
        pos += name_len
        if pos + 2 > len(data):
            break
        (val_len,) = struct.unpack(">H", data[pos : pos + 2])
        pos += 2
        raw = data[pos : pos + val_len]
        pos += val_len

        if tag in _TEXT_TAGS:
            value: Any = raw.decode("utf-8", "replace")
        elif tag in _INT_TAGS and val_len == 4:
            value = struct.unpack(">i", raw)[0]
        elif tag == _BOOL_TAG and val_len == 1:
            value = bool(raw[0])
        else:
            continue

        if name:  # a new attribute
            current = name
            attrs[name] = value
        elif current:  # another value belonging to the previous attribute
            existing = attrs[current]
            if isinstance(existing, list):
                existing.append(value)
            else:
                attrs[current] = [existing, value]
    return attrs


def _dechunk(payload: bytes) -> bytes:
    """Reassemble an HTTP chunked body."""
    out, pos = b"", 0
    while pos < len(payload):
        end = payload.find(b"\r\n", pos)
        if end == -1:
            break
        try:
            size = int(payload[pos:end].split(b";")[0], 16)
        except ValueError:
            break
        if size == 0:
            break
        out += payload[end + 2 : end + 2 + size]
        pos = end + 2 + size + 2
    return out


def _as_list(value: Any) -> List[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _ipp_exchange(
    host: str, port: int, path: str, use_tls: bool, timeout: float
) -> Optional[bytes]:
    """POST one IPP request and return the raw HTTP response."""
    body = _ipp_build_request(host, path)
    request = (
        f"POST {path} HTTP/1.1\r\n"
        f"Host: {host}:{port}\r\n"
        f"Content-Type: application/ipp\r\n"
        f"Content-Length: {len(body)}\r\n"
        f"Connection: close\r\n\r\n"
    ).encode() + body

    try:
        raw_sock = socket.create_connection((host, port), timeout=timeout)
        if use_tls:
            # Printers ship self-signed certificates; the transport is only
            # used to satisfy the device, we are not authenticating it.
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            sock = ctx.wrap_socket(raw_sock, server_hostname=host)
        else:
            sock = raw_sock
        with sock:
            sock.settimeout(timeout)
            sock.sendall(request)
            chunks = []
            while True:
                chunk = sock.recv(8192)
                if not chunk:
                    break
                chunks.append(chunk)
        return b"".join(chunks)
    except (OSError, ssl.SSLError) as exc:
        logger.debug(f"IPP exchange {host}:{port}{path} (tls={use_tls}) failed: {exc}")
        return None


def ipp_query(
    host: str,
    port: Optional[int] = None,
    timeout: float = 3.0,
    paths: Optional[List[str]] = None,
) -> Optional[Dict[str, Any]]:
    """Ask a device for its own attributes over IPP.

    Tries plain IPP first, then IPPS -- many printers (Epson among them) accept
    connections on 631 but reset anything that is not TLS.  Returns a normalised
    dict, or None if the host answers no variant.
    """
    if port is not None:
        transports = [(port, False), (port, True)]
    else:
        transports = [(631, False), (631, True), (443, True)]

    for use_port, use_tls in transports:
        for path in paths or DEFAULT_IPP_PATHS:
            raw = _ipp_exchange(host, use_port, path, use_tls, timeout)
            if not raw:
                continue

            head, _, payload = raw.partition(b"\r\n\r\n")
            if not head or b" 200 " not in head.split(b"\r\n")[0]:
                continue
            if b"chunked" in head.lower():
                payload = _dechunk(payload)
            attrs = _ipp_parse_response(payload)
            if not attrs:
                continue
            return _normalise_identity(host, use_port, use_tls, path, attrs)
    return None


def _normalise_identity(
    host: str, port: int, use_tls: bool, path: str, attrs: Dict[str, Any]
) -> Dict[str, Any]:
    """Shape raw IPP attributes into the dict the rest of the tool expects."""
    state = attrs.get("printer-state")
    reasons = [r for r in _as_list(attrs.get("printer-state-reasons")) if r != "none"]
    sides = _as_list(attrs.get("sides-supported"))
    scheme = "ipps" if use_tls else "ipp"
    return {
        "host": host,
        "ipp_path": path,
        "ipp_port": port,
        "ipp_tls": use_tls,
        "ipp_uri": f"{scheme}://{host}:{port}{path}",
        "name": attrs.get("printer-name"),
        "make_and_model": attrs.get("printer-make-and-model"),
        "state": IPP_STATES.get(state, "unknown") if isinstance(state, int) else None,
        "state_reasons": reasons,
        "is_accepting_jobs": attrs.get("printer-is-accepting-jobs"),
        "location": attrs.get("printer-location"),
        "info": attrs.get("printer-info"),
        "media_default": attrs.get("media-default"),
        "media_supported": _as_list(attrs.get("media-supported")),
        "media_sources": _as_list(attrs.get("media-source-supported")),
        "media_types": _as_list(attrs.get("media-type-supported")),
        "sides_supported": sides,
        "supports_duplex": any(s.startswith("two-sided") for s in sides),
        "color_modes": _as_list(attrs.get("print-color-mode-supported")),
        "resolutions": _as_list(attrs.get("printer-resolution-supported")),
    }


# ------------------------------------------------------------------- helpers


def arp_table() -> Dict[str, str]:
    """Map IPv4 address -> MAC from the OS neighbour cache."""
    cmd = ["arp", "-a"] if sys.platform == "win32" else ["arp", "-an"]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=15).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug(f"arp lookup failed: {exc}")
        return {}

    table = {}
    pattern = re.compile(
        r"(\d{1,3}(?:\.\d{1,3}){3}).*?((?:[0-9a-fA-F]{2}[-:]){5}[0-9a-fA-F]{2})"
    )
    for line in out.splitlines():
        match = pattern.search(line)
        if match:
            table[match.group(1)] = match.group(2).replace("-", ":").upper()
    return table


def vendor_hint(mac: Optional[str]) -> Optional[str]:
    if not mac:
        return None
    return OUI_HINTS.get(mac.replace(":", "").upper()[:6])


def primary_ipv4() -> Optional[str]:
    """Local address of the interface used for outbound traffic."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))  # no packet is actually sent
            return sock.getsockname()[0]
    except OSError:
        return None


def default_subnet() -> Optional[str]:
    """The local /24 as a prefix string, e.g. '192.168.1'."""
    ip = primary_ipv4()
    return ip.rsplit(".", 1)[0] if ip else None


def host_of(text: Optional[str]) -> Optional[str]:
    """Pull an IPv4 address out of a port name, URI or location string."""
    if not text:
        return None
    match = re.search(r"\d{1,3}(?:\.\d{1,3}){3}", text)
    return match.group(0) if match else None


# --------------------------------------------------------------- the scan


def inspect_host(host: str, timeout: float = 0.6, deep: bool = True) -> Optional[Dict[str, Any]]:
    """Probe one host and, if it looks like a printer, identify it via IPP."""
    open_ports = probe_ports(host, timeout=timeout)
    if not is_printer_host(open_ports):
        return None

    entry: Dict[str, Any] = {
        "host": host,
        "open_ports": open_port_names(open_ports),
        "protocols": [name for port, name in PRINTER_PORTS.items() if open_ports.get(port)],
        "reachable": True,
    }
    if deep and open_ports.get(631):
        identity = ipp_query(host, timeout=max(timeout * 4, 3.0))
        if identity:
            entry["ipp"] = identity
            entry["model"] = identity.get("make_and_model")
            entry["state"] = identity.get("state")
    return entry


def scan_subnet(
    subnet: Optional[str] = None,
    timeout: float = 0.6,
    workers: int = 64,
    deep: bool = True,
) -> Dict[str, Any]:
    """Sweep a /24 for hosts speaking a printing protocol."""
    subnet = subnet or default_subnet()
    if not subnet:
        return {"subnet": None, "printers": [], "count": 0, "error": "no local IPv4 network found"}

    subnet = subnet.rstrip(".")
    hosts = [f"{subnet}.{n}" for n in range(1, 255)]
    macs = arp_table()

    found: List[Dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for entry in pool.map(lambda h: inspect_host(h, timeout, deep), hosts):
            if entry:
                mac = macs.get(entry["host"])
                entry["mac"] = mac
                entry["vendor_hint"] = vendor_hint(mac)
                found.append(entry)

    found.sort(key=lambda e: tuple(int(p) for p in e["host"].split(".")))
    return {"subnet": f"{subnet}.0/24", "printers": found, "count": len(found)}
