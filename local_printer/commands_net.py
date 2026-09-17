"""
Network-aware CLI commands: discovery, diagnosis and printer setup.

The commands here exist because the spooler's own view of a printer is a cache.
`diagnose` and `discover` go to the wire instead, and `setup` installs a device
with the best driver available rather than the first one that happens to fit.
"""

import sys
from typing import Any, Dict, List, Optional

from models.model import APIResponse
from local_printer import discovery

IS_WINDOWS = sys.platform == "win32"

if IS_WINDOWS:
    from local_printer import setup_windows as _setup
else:
    _setup = None


# ------------------------------------------------------------------ discover


def discover(
    subnet: Optional[str] = None,
    timeout: float = 0.6,
    deep: bool = True,
    force: bool = False,
) -> Dict[str, Any]:
    """Sweep the local network for devices speaking a printing protocol.

    `force` allows a subnet that is neither one of this machine's own /24s nor
    a private range; without it such a scan is refused with code 400.
    """
    result = discovery.scan_subnet(subnet=subnet, timeout=timeout, deep=deep, force=force)
    if result.get("error"):
        code = result.get("code") if isinstance(result.get("code"), int) else 500
        return APIResponse(code=code, msg=result["error"], data=result).to_dict()
    return APIResponse.success(result).to_dict()


def probe(host: str, timeout: float = 1.0) -> Dict[str, Any]:
    """Check one host: which printing ports answer, and what the device says."""
    if not discovery.valid_host(host):
        return APIResponse.error(400, "invalid host").to_dict()
    open_ports = discovery.probe_ports(host, timeout=timeout)
    identity = discovery.ipp_query(host, timeout=max(timeout * 3, 3.0))
    data = {
        "host": host,
        "open_ports": discovery.open_port_names(open_ports),
        "is_printer": discovery.is_printer_host(open_ports),
        "reachable": any(open_ports.values()),
        "identity": identity,
    }
    return APIResponse.success(data).to_dict()


# ------------------------------------------------------------------ diagnose


def diagnose(deep: bool = True, timeout: float = 1.0) -> Dict[str, Any]:
    """Cross-check every installed printer against reality.

    The spooler reports a queue as `idle` whether or not the hardware behind it
    still exists.  For each network queue this resolves the host and probes it,
    so a printer that is off, or stranded on an old subnet, is named as such.
    """
    if IS_WINDOWS:
        from local_printer.windows import get_printer_list
    else:
        from local_printer.cups import get_printer_list

    listing = get_printer_list()
    if listing.get("code") != 200:
        return listing

    results: List[Dict[str, Any]] = []
    for printer in listing["data"]["printers"]:
        entry: Dict[str, Any] = {
            "index": printer["index"],
            "name": printer["name"],
            "spooler_status": printer["status"],
            "port": printer.get("port", ""),
            "is_default": printer.get("is_default", False),
        }
        port_name = str(printer.get("port") or "")
        # First candidate that yields an address wins; if none does but one of
        # them named a host we could not resolve, remember that so the queue is
        # reported as unresolved rather than silently treated as local.
        host: Optional[str] = None
        unresolved: Optional[Dict[str, Any]] = None
        for candidate in (port_name, printer.get("uri"), printer.get("location")):
            resolution = discovery.resolve_host(candidate)
            if resolution is None:
                continue
            if resolution.get("address"):
                host = resolution["address"]
                if resolution.get("hostname") and resolution["hostname"] != host:
                    entry["hostname"] = resolution["hostname"]
                break
            unresolved = unresolved or resolution
        if not host and unresolved:
            entry["kind"] = "network-unresolved"
            entry["hostname"] = unresolved.get("hostname")
            entry["really_online"] = None
            entry["verdict"] = f"UNRESOLVED - {unresolved.get('reason')}"
            entry["hint"] = (
                "check that the name is still valid on this network (mDNS/.local "
                "names need the printer to be powered on and Bonjour/Avahi to be "
                "running), or re-add the queue using the printer's IP address"
            )
            results.append(entry)
            continue
        if not host and port_name.upper().startswith("WSD-"):
            # Windows WSD/IPP pairing ports resolve the device by UUID at print
            # time; the port itself stores no address we could probe.
            entry["kind"] = "unknown"
            entry["really_online"] = None
            entry["verdict"] = (
                "WSD port - address not stored in the port; print a test page to check"
            )
            results.append(entry)
            continue
        if not host:
            entry["kind"] = "virtual-or-local"
            entry["really_online"] = True
            entry["verdict"] = "local/virtual queue - nothing to reach over the network"
            results.append(entry)
            continue

        entry["kind"] = "network"
        entry["host"] = host
        open_ports = discovery.probe_ports(host, timeout=timeout)
        entry["open_ports"] = discovery.open_port_names(open_ports)
        online = discovery.is_printer_host(open_ports)
        entry["really_online"] = online

        if online and deep:
            identity = discovery.ipp_query(host, timeout=max(timeout * 3, 3.0))
            if identity:
                entry["device_state"] = identity.get("state")
                entry["model"] = identity.get("make_and_model")
                entry["state_reasons"] = identity.get("state_reasons")

        if online:
            entry["verdict"] = "reachable"
        else:
            entry["verdict"] = f"UNREACHABLE - nothing answers on {host}"
        results.append(entry)

    online_count = sum(1 for r in results if r["really_online"] is True)
    unknown_count = sum(1 for r in results if r["really_online"] is None)
    return APIResponse.success({
        "printers": results,
        "count": len(results),
        "online": online_count,
        "unknown": unknown_count,
        "offline": len(results) - online_count - unknown_count,
    }).to_dict()


# --------------------------------------------------------------- inventory


def ports() -> Dict[str, Any]:
    if not IS_WINDOWS:
        return APIResponse.error(501, "ports is only implemented on Windows").to_dict()
    entries = _setup.list_ports()
    return APIResponse.success({"ports": entries, "count": len(entries)}).to_dict()


def drivers(model: Optional[str] = None) -> Dict[str, Any]:
    if not IS_WINDOWS:
        return APIResponse.error(501, "drivers is only implemented on Windows").to_dict()
    entries = _setup.list_drivers()
    data: Dict[str, Any] = {"drivers": entries, "count": len(entries)}
    if model:
        data["candidates_for_model"] = _setup.find_driver_candidates(model)
    return APIResponse.success(data).to_dict()


# ------------------------------------------------------------------- setup


def setup(
    host: str,
    name: Optional[str] = None,
    dry_run: bool = False,
    allow_generic: bool = True,
    vendor_lookup: bool = False,
) -> Dict[str, Any]:
    """Install a network printer, preferring a full-featured driver.

    `vendor_lookup` is off by default: querying the manufacturer's portal sends
    the model and this machine's OS/region to a third party, which must be the
    user's decision rather than a side effect of planning a setup.
    """
    if IS_WINDOWS:
        result = _setup.setup_printer(
            host, name=name, allow_generic=allow_generic, dry_run=dry_run,
            vendor_lookup=vendor_lookup,
        )
        error = result.get("error")
        code = _setup_error_code(error) if error else 200
        return APIResponse(code=code, msg=error or "success", data=result).to_dict()
    return _setup_cups(host, name=name, dry_run=dry_run)


# Error strings from setup_windows.setup_printer / plan_setup that describe the
# caller's input or the device rather than a failure on this machine.
_SETUP_BAD_INPUT_MARKERS = ("invalid ",)
_SETUP_NOT_FOUND_MARKERS = (
    "no printing port",
    "not reachable",
    "unreachable",
    "not found",
    "does not answer",
)


def _setup_error_code(error: Any) -> int:
    """Map a setup error message to an HTTP-style code.

    Bad input (malformed host, unusable queue name) is 400; a host with no
    printing port / that cannot be reached is 404; anything else - a strategy,
    driver or port installation failing - is a genuine 500.
    """
    text = str(error or "").strip().lower()
    if not text:
        return 500
    if any(text.startswith(marker) for marker in _SETUP_BAD_INPUT_MARKERS):
        return 400
    if any(marker in text for marker in _SETUP_NOT_FOUND_MARKERS):
        return 404
    return 500


def _setup_cups(host: str, name: Optional[str] = None, dry_run: bool = False) -> Dict[str, Any]:
    """CUPS equivalent: driverless IPP Everywhere via lpadmin."""
    import shlex
    import subprocess

    # Same check the Windows path applies: a malformed host must fail fast
    # rather than spending tens of seconds in socket timeouts before a 404.
    if not discovery.valid_host(host):
        return APIResponse.error(400, "invalid host").to_dict()

    identity = discovery.ipp_query(host, timeout=4.0)
    if identity:
        queue = name or (identity.get("make_and_model") or f"printer-{host}")
        queue = "".join(c if c.isalnum() or c in "-_" else "_" for c in queue)
        uri = identity["ipp_uri"]
        cmd = ["lpadmin", "-p", queue, "-E", "-v", uri, "-m", "everywhere"]

        if dry_run:
            return APIResponse.success({
                "host": host, "identity": identity, "would_run": shlex.join(cmd)
            }).to_dict()

        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        except (OSError, subprocess.SubprocessError) as exc:
            return APIResponse.server_error(f"lpadmin failed: {exc}").to_dict()

        if proc.returncode != 0:
            return APIResponse.server_error(
                f"lpadmin failed: {proc.stderr.strip()}", {"command": shlex.join(cmd)}
            ).to_dict()
        return APIResponse.success({
            "printer": queue, "host": host, "uri": uri, "identity": identity,
            "installed_with": {"kind": "ipp-everywhere", "driver": "everywhere"},
        }).to_dict()

    # No IPP answer at all: mirror the Windows ladder's last resort (see
    # setup_windows.plan_setup's "raw-fallback" rung) instead of giving up
    # immediately - a device with IPP disabled/unsupported but a plain
    # JetDirect/AppSocket port open can still be printed to, just without
    # driverless capability negotiation.
    open_ports = discovery.probe_ports(host, ports=[9100], timeout=4.0)
    if not open_ports.get(9100):
        return APIResponse.error(
            404, f"{host} does not answer IPP - cannot set up driverless printing"
        ).to_dict()

    queue = name or f"printer-{host}"
    queue = "".join(c if c.isalnum() or c in "-_" else "_" for c in queue)
    uri = f"socket://{host}:9100"
    cmd = ["lpadmin", "-p", queue, "-E", "-v", uri, "-m", "raw"]

    if dry_run:
        return APIResponse.success({
            "host": host, "identity": None, "would_run": shlex.join(cmd),
            "raw_fallback": True,
        }).to_dict()

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError) as exc:
        return APIResponse.server_error(f"lpadmin failed: {exc}").to_dict()

    if proc.returncode != 0:
        return APIResponse.server_error(
            f"lpadmin failed: {proc.stderr.strip()}", {"command": shlex.join(cmd)}
        ).to_dict()
    return APIResponse.success({
        "printer": queue, "host": host, "uri": uri, "identity": None,
        "raw_fallback": True,
        "installed_with": {"kind": "raw-fallback", "driver": "raw"},
        "note": (
            f"{host} did not answer IPP; installed a generic raw/9100 queue "
            "instead - no driverless capability negotiation (duplex, media "
            "size, colour, ...), just a plain print stream"
        ),
    }).to_dict()


def driver_search(
    model: Optional[str] = None,
    host: Optional[str] = None,
    region: Optional[str] = None,
    os_code: Optional[str] = None,
    download_dir: Optional[str] = None,
    open_browser: bool = False,
) -> Dict[str, Any]:
    """Find a manufacturer driver online for a model, or for the device at `host`."""
    from local_printer import vendor_drivers

    if not model and not host:
        return APIResponse.error(400, "give either a model or --host").to_dict()

    identity = None
    if not model:
        identity = discovery.ipp_query(host, timeout=4.0)
        if not identity or not identity.get("make_and_model"):
            return APIResponse.error(
                404, f"could not read a model from {host} over IPP - pass the model explicitly"
            ).to_dict()
        model = identity["make_and_model"]

    result = vendor_drivers.find_driver(model, region=region, os_code=os_code)
    if identity:
        result["identity"] = identity

    downloads = result.get("downloads") or []
    best = downloads[0] if downloads else None

    if download_dir:
        if not best:
            # No direct URL: the portal itself is the only way through.
            result["download"] = {
                "ok": False,
                "blocked": True,
                "error": "no direct download URL available (vendor API unreachable)",
                "open_in_browser_url": result.get("download_page"),
            }
        else:
            result["download"] = vendor_drivers.download_driver(
                best["url"], download_dir, expected_size=best.get("size_bytes")
            )

    if open_browser:
        # Prefer the exact installer, fall back to the filtered download page.
        target = (
            (result.get("download") or {}).get("open_in_browser_url")
            or (best or {}).get("url")
            or result.get("download_page")
            or result.get("support_site")
        )
        if target:
            result["opened"] = vendor_drivers.open_in_browser(target)
        else:
            result["opened"] = {"ok": False, "error": "no URL to open for this vendor"}

    return APIResponse.success(result).to_dict()


def remove(name: str) -> Dict[str, Any]:
    if not IS_WINDOWS:
        return _remove_cups(name)
    ok, err = _setup.remove_printer(name)
    if not ok:
        return APIResponse.server_error(f"could not remove {name}: {err}").to_dict()
    return APIResponse.success({"removed": name}).to_dict()


def set_default(name: str) -> Dict[str, Any]:
    if not IS_WINDOWS:
        return _set_default_cups(name)
    ok, err = _setup.set_default_printer(name)
    if not ok:
        return APIResponse.server_error(f"could not set default: {err}").to_dict()
    return APIResponse.success({"default": name}).to_dict()


# Mirrors setup_windows._name_error: reject an unusable queue name before it
# ever reaches a subprocess argument list. CUPS forbids "/" and "#" in a queue
# name and treats a leading space specially, so those join the same forbidden
# set that already protects the Windows path against control characters.
_CUPS_MAX_QUEUE_NAME = 220
_CUPS_FORBIDDEN_NAME_CHARS = ("/", "#", " ", "\\", ",")


def _cups_name_error(value: Any) -> Optional[str]:
    """Return why `value` is unusable as a CUPS queue name, or None if it is."""
    if not isinstance(value, str) or not value.strip():
        return "invalid printer name: must be a non-empty string"
    for bad in _CUPS_FORBIDDEN_NAME_CHARS:
        if bad in value:
            return f"invalid printer name: must not contain {bad!r} (CUPS forbids it)"
    if len(value) > _CUPS_MAX_QUEUE_NAME:
        return f"invalid printer name: longer than {_CUPS_MAX_QUEUE_NAME} characters"
    return None


def _run_lpadmin(args: List[str]) -> Dict[str, Any]:
    """Run `lpadmin <args>`, returning an APIResponse-shaped dict on failure.

    Mirrors the subprocess handling in `_setup_cups`: an argument list (never
    `shell=True`), a missing binary and a non-zero exit both surfaced as clear
    errors, and a permission-denied exit hinting at the privilege it needs.
    """
    import subprocess

    cmd = ["lpadmin"] + args
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except FileNotFoundError:
        return APIResponse.server_error(
            "lpadmin not found - is CUPS installed on this machine?"
        ).to_dict()
    except (OSError, subprocess.SubprocessError) as exc:
        return APIResponse.server_error(f"lpadmin failed: {exc}").to_dict()

    if proc.returncode != 0:
        stderr = proc.stderr.strip()
        msg = f"lpadmin failed: {stderr}" if stderr else "lpadmin failed"
        if "not authorized" in stderr.lower() or "permission" in stderr.lower():
            msg += " (this usually needs to run as root or a member of the lpadmin group)"
        return APIResponse.server_error(msg).to_dict()
    return {}


def _remove_cups(name: str) -> Dict[str, Any]:
    """CUPS equivalent of the Windows remove: `lpadmin -x <name>`."""
    problem = _cups_name_error(name)
    if problem:
        return APIResponse.error(400, problem).to_dict()

    failure = _run_lpadmin(["-x", name])
    if failure:
        return failure
    return APIResponse.success({"removed": name}).to_dict()


def _set_default_cups(name: str) -> Dict[str, Any]:
    """CUPS equivalent of the Windows set-default: `lpadmin -d <name>`."""
    problem = _cups_name_error(name)
    if problem:
        return APIResponse.error(400, problem).to_dict()

    failure = _run_lpadmin(["-d", name])
    if failure:
        return failure
    return APIResponse.success({"default": name}).to_dict()
