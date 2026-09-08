"""
Windows printer setup with real driver discovery.

Installing a network printer with whatever generic driver happens to be present
"works", but silently costs capability: the Microsoft IPP Class Driver bound to
a raw 9100 port reports no duplex, no paper trays and Letter as the default,
even when the hardware supports all of it.

This module instead asks the device what it is (over IPP), then walks a ladder
of setup strategies from best to worst, and finally *verifies* the installed
queue against the device's own advertised capabilities so a downgrade is
reported rather than hidden.
"""

import ctypes
import json
import os
import re
import subprocess
from typing import Any, Dict, List, Optional, Tuple

from utils.logger import logger
from local_printer import discovery

# In-box printer INF files shipped with Windows, searched for a model match.
INF_DIR = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "inf")
INF_GLOB_PREFIXES = ("prn", "ntprint")

# Drivers that work with any device but negotiate nothing over a raw port.
GENERIC_DRIVERS = {
    "Microsoft IPP Class Driver",
    "Universal Print Class Driver",
    "Microsoft enhanced Point and Print compatibility driver",
    "Microsoft Print To PDF",
}


def is_elevated() -> bool:
    """True if the current process has administrator rights."""
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:  # pragma: no cover - non-Windows or restricted host
        return False


def _ps(script: str, timeout: int = 120) -> Tuple[bool, str, str]:
    """Run a PowerShell snippet, returning (ok, stdout, stderr)."""
    try:
        proc = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy", "Bypass",
                "-Command", script,
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return proc.returncode == 0, proc.stdout.strip(), proc.stderr.strip()
    except (OSError, subprocess.SubprocessError) as exc:
        logger.error(f"PowerShell call failed: {exc}")
        return False, "", str(exc)


def _ps_json(script: str, timeout: int = 120) -> Any:
    """Run a PowerShell snippet that emits JSON and parse the result."""
    ok, out, err = _ps(script, timeout)
    if not ok or not out:
        logger.debug(f"PowerShell JSON call empty (err={err})")
        return []
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        logger.debug(f"PowerShell did not return JSON: {out[:200]}")
        return []
    return data if isinstance(data, list) else [data]


# ------------------------------------------------------- inventory helpers


def list_ports() -> List[Dict[str, Any]]:
    """All printer ports known to the spooler."""
    return _ps_json(
        "Get-PrinterPort | Select-Object Name,Description,PrinterHostAddress,PortNumber "
        "| ConvertTo-Json -Compress"
    )


def list_drivers() -> List[Dict[str, Any]]:
    """All printer drivers installed in the driver store."""
    return _ps_json(
        "Get-PrinterDriver | Select-Object Name,Manufacturer,DriverVersion "
        "| ConvertTo-Json -Compress"
    )


def _model_tokens(model: str) -> List[str]:
    """Significant words of a model name, for fuzzy driver matching."""
    cleaned = re.sub(r"[^A-Za-z0-9]+", " ", model or "")
    skip = {"series", "printer", "class", "driver"}
    return [t.lower() for t in cleaned.split() if t and t.lower() not in skip]


def _score_driver(driver_name: str, model: str) -> int:
    """How well an available driver matches the device model."""
    tokens = _model_tokens(model)
    if not tokens:
        return 0
    name = driver_name.lower()
    if driver_name in GENERIC_DRIVERS:
        return 0
    score = sum(3 for t in tokens if t in name)
    if model.lower() in name or name in model.lower():
        score += 10
    return score


def search_inf_drivers(model: str) -> List[str]:
    """Scan the in-box printer INF files for driver names matching `model`.

    These drivers are shipped with Windows but not installed until requested,
    so a hit here means `Add-PrinterDriver` has a real chance of succeeding.
    """
    tokens = _model_tokens(model)
    if not tokens:
        return []
    strong = [t for t in tokens if len(t) > 2]
    matches: List[str] = []
    try:
        names = [
            f for f in os.listdir(INF_DIR)
            if f.lower().startswith(INF_GLOB_PREFIXES) and f.lower().endswith(".inf")
        ]
    except OSError as exc:
        logger.debug(f"cannot list {INF_DIR}: {exc}")
        return []

    for filename in names:
        path = os.path.join(INF_DIR, filename)
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as handle:
                text = handle.read()
        except OSError:
            continue
        for line in text.splitlines():
            if "=" not in line or line.lstrip().startswith(";"):
                continue
            label = line.split("=", 1)[0].strip().strip('"')
            if not label or len(label) > 120:
                continue
            low = label.lower()
            if all(t in low for t in strong):
                matches.append(label)
    # de-duplicate, keep order
    seen, unique = set(), []
    for m in matches:
        if m not in seen:
            seen.add(m)
            unique.append(m)
    return unique[:20]


def find_driver_candidates(model: str) -> Dict[str, Any]:
    """Rank every driver that could serve `model`, installed or in-box."""
    installed = list_drivers()
    scored = [
        {"name": d["Name"], "score": _score_driver(d["Name"], model), "installed": True}
        for d in installed
        if isinstance(d, dict) and d.get("Name")
    ]
    vendor_installed = sorted(
        [d for d in scored if d["score"] > 0], key=lambda d: -d["score"]
    )
    inbox = [
        {"name": n, "score": _score_driver(n, model), "installed": False}
        for n in search_inf_drivers(model)
    ]
    inbox = sorted([d for d in inbox if d["score"] > 0], key=lambda d: -d["score"])
    return {
        "model": model,
        "vendor_installed": vendor_installed,
        "vendor_available_inbox": inbox,
        "generic": [d["name"] for d in scored if d["name"] in GENERIC_DRIVERS],
    }


# ---------------------------------------------------------------- mutations


def install_driver(name: str) -> Tuple[bool, str]:
    """Add a driver from the Windows driver store to the spooler."""
    ok, _, err = _ps(f'Add-PrinterDriver -Name "{name}" -ErrorAction Stop')
    return ok, err or ""


def add_port(name: str, host: Optional[str] = None, port_number: int = 9100) -> Tuple[bool, str]:
    """Create a printer port; URL-shaped names use the IPP monitor."""
    existing = {p.get("Name") for p in list_ports() if isinstance(p, dict)}
    if name in existing:
        return True, "already exists"
    if "://" in name:
        script = f'Add-PrinterPort -Name "{name}" -ErrorAction Stop'
    else:
        script = (
            f'Add-PrinterPort -Name "{name}" -PrinterHostAddress "{host}" '
            f"-PortNumber {port_number} -ErrorAction Stop"
        )
    ok, _, err = _ps(script)
    return ok, err or ""


def add_printer(name: str, driver: str, port: str) -> Tuple[bool, str]:
    ok, _, err = _ps(
        f'Add-Printer -Name "{name}" -DriverName "{driver}" -PortName "{port}" -ErrorAction Stop'
    )
    return ok, err or ""


def remove_printer(name: str) -> Tuple[bool, str]:
    ok, _, err = _ps(f'Remove-Printer -Name "{name}" -ErrorAction Stop')
    return ok, err or ""


def set_default_printer(name: str) -> Tuple[bool, str]:
    ok, _, err = _ps(
        '(Get-WmiObject -Class Win32_Printer -Filter "Name=\'' + name.replace("'", "''") +
        '\'").SetDefaultPrinter()'
    )
    return ok, err or ""


# ------------------------------------------------------------- verification


def verify_capabilities(printer_name: str, identity: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Compare the installed queue's capabilities against the device's own.

    `identity` is the IPP answer from the hardware -- the ground truth.  Any
    feature the device advertises but the queue does not expose is a downgrade
    caused by the driver, and is listed in `missing`.
    """
    from local_printer.windows import get_capabilities

    try:
        caps = get_capabilities(printer_name) or {}
    except Exception as exc:  # driver may refuse to answer
        logger.error(f"cannot read capabilities of {printer_name}: {exc}")
        caps = {}

    duplex = caps.get("Duplex", {}) or {}
    papers = caps.get("Papers", {}) or {}
    bins = caps.get("Bins", {}) or {}
    media = caps.get("MediaTypes", {}) or {}

    queue = {
        "duplex": len(duplex) > 1,
        "paper_sizes": len(papers),
        "trays": len(bins),
        "media_types": len([m for m in media if m != "Default"]),
        "color": len(caps.get("Color", {}) or {}) > 1,
    }

    missing: List[str] = []
    if identity:
        if identity.get("supports_duplex") and not queue["duplex"]:
            missing.append("duplex (device supports two-sided printing)")
        device_media = len(identity.get("media_types", []))
        if device_media > queue["media_types"]:
            missing.append(
                f"media types ({queue['media_types']} exposed, {device_media} on device)"
            )
        device_sources = len(identity.get("media_sources", []))
        if device_sources > queue["trays"] and queue["trays"] == 0:
            missing.append("paper trays (none exposed)")

    return {
        "printer": printer_name,
        "queue_capabilities": queue,
        "device_capabilities": {
            "duplex": bool(identity.get("supports_duplex")) if identity else None,
            "media_types": len(identity.get("media_types", [])) if identity else None,
            "media_sizes": len(identity.get("media_supported", [])) if identity else None,
            "sources": identity.get("media_sources") if identity else None,
        },
        "missing": missing,
        "full_featured": not missing,
    }


# ------------------------------------------------------------------- setup


def plan_setup(host: str, name: Optional[str] = None) -> Dict[str, Any]:
    """Work out what would be installed for `host`, changing nothing."""
    identity = discovery.ipp_query(host, timeout=4.0)
    open_ports = discovery.probe_ports(host)
    if not discovery.is_printer_host(open_ports):
        return {
            "host": host,
            "reachable": False,
            "error": "no printing port (9100/631/515) open on this host",
        }

    model = (identity or {}).get("make_and_model") or ""
    candidates = find_driver_candidates(model) if model else {
        "model": "", "vendor_installed": [], "vendor_available_inbox": [], "generic": []
    }

    steps = _strategy_ladder(host, identity, open_ports, candidates)
    return {
        "host": host,
        "reachable": True,
        "open_ports": discovery.open_port_names(open_ports),
        "identity": identity,
        "model": model,
        "suggested_name": name or (model or f"Printer {host}"),
        "driver_candidates": candidates,
        "strategies": steps,
        "elevated": is_elevated(),
        "note": None if is_elevated() else
        "Not running elevated: IPP/WSD ports and driver installation need "
        "administrator rights. Only the raw-9100 fallback is likely to succeed.",
    }


def _strategy_ladder(
    host: str,
    identity: Optional[Dict[str, Any]],
    open_ports: Dict[str, Any],
    candidates: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Setup attempts ordered from full-featured to lowest common denominator."""
    ladder: List[Dict[str, Any]] = []
    ipp_path = (identity or {}).get("ipp_path", "/ipp/print")
    vendor = (candidates.get("vendor_installed") or []) + (
        candidates.get("vendor_available_inbox") or []
    )

    # 1. Vendor driver -- exposes every device feature the manufacturer supports.
    for cand in vendor[:3]:
        ladder.append({
            "rank": len(ladder) + 1,
            "kind": "vendor-driver",
            "driver": cand["name"],
            "needs_driver_install": not cand["installed"],
            "port": f"IP_{host}",
            "port_kind": "raw-9100",
            "full_featured": True,
            "reason": "manufacturer driver exposes the complete feature set",
        })

    # 2. IPP Everywhere -- the class driver negotiates capabilities live.
    if open_ports.get(631):
        for scheme in ("https", "http"):
            ladder.append({
                "rank": len(ladder) + 1,
                "kind": "ipp-everywhere",
                "driver": "Microsoft IPP Class Driver",
                "needs_driver_install": False,
                "port": f"{scheme}://{host}:631{ipp_path}",
                "port_kind": "ipp",
                "full_featured": True,
                "requires_elevation": True,
                "reason": "class driver negotiates duplex/media from the device over IPP",
            })

    # 3. Raw 9100 -- always works, negotiates nothing.
    if open_ports.get(9100):
        ladder.append({
            "rank": len(ladder) + 1,
            "kind": "raw-fallback",
            "driver": "Microsoft IPP Class Driver",
            "needs_driver_install": False,
            "port": f"IP_{host}",
            "port_kind": "raw-9100",
            "full_featured": False,
            "reason": "last resort: prints, but exposes only generic capabilities",
        })
    return ladder


def setup_printer(
    host: str,
    name: Optional[str] = None,
    allow_generic: bool = True,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Install `host` as a printer, preferring full-featured strategies.

    Walks the strategy ladder until one succeeds, then verifies the result
    against the device's advertised capabilities.
    """
    plan = plan_setup(host, name)
    if not plan.get("reachable"):
        return plan
    if dry_run:
        return plan

    identity = plan["identity"]
    printer_name = name or plan["suggested_name"]
    attempts: List[Dict[str, Any]] = []

    for strategy in plan["strategies"]:
        if not allow_generic and not strategy["full_featured"]:
            attempts.append({**strategy, "status": "skipped", "detail": "generic setup not allowed"})
            continue

        record = {**strategy, "status": "failed", "detail": ""}

        if strategy.get("needs_driver_install"):
            ok, err = install_driver(strategy["driver"])
            if not ok:
                record["detail"] = f"driver install failed: {err[:200]}"
                attempts.append(record)
                continue

        port = strategy["port"]
        ok, err = add_port(port, host=host, port_number=9100)
        if not ok:
            record["detail"] = f"port creation failed: {err[:200]}"
            attempts.append(record)
            continue

        ok, err = add_printer(printer_name, strategy["driver"], port)
        if not ok:
            record["detail"] = f"printer creation failed: {err[:200]}"
            attempts.append(record)
            continue

        record["status"] = "installed"
        attempts.append(record)
        verification = verify_capabilities(printer_name, identity)
        return {
            "host": host,
            "printer": printer_name,
            "installed_with": strategy,
            "attempts": attempts,
            "verification": verification,
            "identity": identity,
            "elevated": is_elevated(),
            "warning": None if verification["full_featured"] else
            "Printer installed but with reduced capabilities -- see verification.missing",
        }

    return {
        "host": host,
        "printer": printer_name,
        "installed_with": None,
        "attempts": attempts,
        "identity": identity,
        "elevated": is_elevated(),
        "error": "every setup strategy failed",
        "hint": None if is_elevated() else
        "Run the CLI from an elevated shell -- port and driver installation need admin rights.",
    }
