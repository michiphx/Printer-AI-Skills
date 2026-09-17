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
import sys
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

# Windows rejects these in a queue name, and the spooler caps it well below this.
MAX_QUEUE_NAME = 220
_FORBIDDEN_NAME_CHARS = ("\\", ",")

# Windows 11 and later; Add-PrinterPort cannot create an IPP port on these.
WIN11_BUILD = 22000

# Model words too generic to identify a driver on their own.
INF_STOP_TOKENS = {
    "epson", "hp", "canon", "brother",
    "series", "printer", "class", "driver",
}


def is_elevated() -> bool:
    """True if the current process has administrator rights."""
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:  # pragma: no cover - non-Windows or restricted host
        return False


def windows_build() -> int:
    """The running Windows build number, or 0 when it cannot be read."""
    try:
        return int(sys.getwindowsversion().build)
    except Exception:  # pragma: no cover - non-Windows
        return 0


def is_windows_11() -> bool:
    """True on Windows 11 or later, where IPP ports cannot be scripted."""
    return windows_build() >= WIN11_BUILD


# ------------------------------------------------------- PowerShell plumbing


def _ps_literal(value: Any) -> str:
    """Quote `value` as a single-quoted PowerShell string literal.

    Single-quoted PowerShell strings are fully literal: `$`, backticks and
    `$(...)` subexpressions are not expanded, so a device-supplied name cannot
    become code.  The only escape needed is doubling an embedded quote.

    Every value that reaches a PowerShell command line -- printer names, driver
    names, port names, hosts -- must go through here.  Never interpolate raw
    text into a script.
    """
    text = "" if value is None else str(value)
    return "'" + text.replace("'", "''") + "'"


def _name_error(value: Any, kind: str = "printer name") -> Optional[str]:
    """Return why `value` is unusable as a queue/port name, or None if it is."""
    if not isinstance(value, str) or not value.strip():
        return f"invalid {kind}: must be a non-empty string"
    for bad in _FORBIDDEN_NAME_CHARS:
        if bad in value:
            return f"invalid {kind}: must not contain {bad!r} (Windows forbids it)"
    if len(value) > MAX_QUEUE_NAME:
        return f"invalid {kind}: longer than {MAX_QUEUE_NAME} characters"
    return None


# PowerShell writes the OEM code page to a redirected stdout, which decodes as
# mojibake -- or raises -- on any non-English Windows.  Force UTF-8 per call.
_PS_PREAMBLE = "[Console]::OutputEncoding=[Text.Encoding]::UTF8; "


def _ps(script: str, timeout: int = 120) -> Tuple[bool, str, str]:
    """Run a PowerShell snippet, returning (ok, stdout, stderr)."""
    try:
        proc = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy", "Bypass",
                "-Command", _PS_PREAMBLE + script,
            ],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        err = (proc.stderr or "").strip()
        if err.startswith(_PS_PREAMBLE):
            # PowerShell prefixes -Command errors with the whole command text.
            err = err.split(" : ", 1)[-1]
        err = "\n".join(
            line for line in err.splitlines() if not line.lstrip().startswith("+ ")
        ).strip()
        return proc.returncode == 0, (proc.stdout or "").strip(), err
    except (OSError, subprocess.SubprocessError, UnicodeDecodeError, ValueError) as exc:
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


def _strong_tokens(model: str) -> List[str]:
    """Model words distinctive enough to identify a driver.

    A token qualifies if it carries a digit (`et`+`4850`, `mfc`+`l2750dw`) or is
    at least four letters long, and is not a manufacturer or filler word.  Weak
    tokens alone must never drive a match: `all([])` is True, so a model that
    reduces to nothing would otherwise claim every INF label in Windows.
    """
    strong: List[str] = []
    for token in _model_tokens(model):
        if token in INF_STOP_TOKENS:
            continue
        if any(ch.isdigit() for ch in token) or len(token) >= 4:
            strong.append(token)
    return strong


def _read_inf(path: str) -> str:
    """Decode an INF file, whatever encoding Windows shipped it in.

    The in-box printer INFs are UTF-16LE; reading them as UTF-8 yields bytes
    that decode to nothing usable, which silently turned this whole search into
    a no-op.  Detect the BOM instead of assuming.
    """
    with open(path, "rb") as handle:
        raw = handle.read()
    if raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff"):
        return raw.decode("utf-16", errors="ignore")
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw.decode("utf-8-sig", errors="ignore")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        # Older INFs use the ANSI code page.
        return raw.decode("cp1252", errors="ignore")


def _inf_string_value(raw: str) -> str:
    """The value of an INF `[Strings]` entry, without quotes or trailing comment."""
    value = raw.strip()
    if value.startswith('"'):
        end = value.find('"', 1)
        return value[1:end] if end > 0 else value[1:]
    return value.split(";", 1)[0].strip()


_INF_TOKEN_RE = re.compile(r"^%(.+)%$")

# Sections that hold plumbing, not printer models.
_INF_SKIP_SECTIONS = {
    "version", "manufacturer", "sourcedisksnames", "sourcedisksfiles",
    "destinationdirs", "defaultinstall", "previousnames", "controlflags",
    "classinstall32", "signaturecheck",
}


def _parse_inf(text: str) -> Dict[str, List[str]]:
    """Split an INF into `{section name (lower): [entry lines]}`."""
    sections: Dict[str, List[str]] = {}
    current = ""
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(";"):
            continue
        if stripped.startswith("["):
            end = stripped.find("]")
            current = stripped[1:end].strip().lower() if end > 0 else ""
            sections.setdefault(current, [])
            continue
        if current:
            sections[current].append(stripped)
    return sections


def _inf_strings(sections: Dict[str, List[str]]) -> Dict[str, str]:
    """The `[Strings]` token table: `{token (lower): human-readable value}`."""
    table: Dict[str, str] = {}
    for name, lines in sections.items():
        if not name.startswith("strings"):
            continue
        for line in lines:
            if "=" not in line:
                continue
            token, raw = line.split("=", 1)
            value = _inf_string_value(raw)
            if value:
                table[token.strip().lower()] = value
    return table


def _inf_driver_names(sections: Dict[str, List[str]], strings: Dict[str, str]) -> List[str]:
    """Candidate driver names from an INF, best-readable form first.

    Model entries are written `%Token% = InstallSection, HardwareID`, so the key
    names the driver -- but as a token.  Resolving it through `[Strings]` yields
    the human-readable name the spooler actually accepts; a key already written
    as a quoted literal is that name already.
    """
    resolved: List[str] = []
    literal: List[str] = []
    bare: List[str] = []
    for name, lines in sections.items():
        if name.startswith("strings") or name.split(".")[0] in _INF_SKIP_SECTIONS:
            continue
        for line in lines:
            if "=" not in line:
                continue
            key = line.split("=", 1)[0].strip()
            token = _INF_TOKEN_RE.match(key)
            if token:
                value = strings.get(token.group(1).strip().lower())
                if value:
                    resolved.append(value)
            elif key.startswith('"'):
                literal.append(key.strip('"'))
            elif key:
                bare.append(key)
    # Values from [Strings] itself, for INFs that name the driver only there.
    return resolved + literal + list(strings.values()) + bare


def search_inf_drivers(model: str) -> List[str]:
    """Scan the in-box printer INF files for driver names matching `model`.

    These drivers are shipped with Windows but not installed until requested,
    so a hit here means `Add-PrinterDriver` has a real chance of succeeding.

    Model entries are keyed by a `%Token%` that `[Strings]` maps to the
    human-readable driver name, so tokens are resolved to those values before
    matching -- the raw token (`EPSON.ET4850`) is not a name the spooler accepts.
    """
    strong = _strong_tokens(model)
    if not strong:
        # Nothing distinctive enough to match on -- claiming a driver here would
        # be a guess dressed up as a finding.
        return []
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
            text = _read_inf(path)
        except OSError:
            continue
        sections = _parse_inf(text)
        strings = _inf_strings(sections)
        for candidate in _inf_driver_names(sections, strings):
            if not candidate or len(candidate) > 120:
                continue
            low = candidate.lower()
            if all(t in low for t in strong):
                matches.append(candidate)
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
    if not isinstance(name, str) or not name.strip():
        return False, "invalid driver name: must be a non-empty string"
    ok, _, err = _ps(f"Add-PrinterDriver -Name {_ps_literal(name)} -ErrorAction Stop")
    return ok, err or ""


def add_port(name: str, host: Optional[str] = None, port_number: int = 9100) -> Tuple[bool, str]:
    """Create a printer port; URL-shaped names use the IPP monitor."""
    problem = _name_error(name, "port name")
    if problem:
        return False, problem
    existing = {p.get("Name") for p in list_ports() if isinstance(p, dict)}
    if name in existing:
        return True, "already exists"
    if "://" in name:
        script = f"Add-PrinterPort -Name {_ps_literal(name)} -ErrorAction Stop"
    else:
        if not discovery.valid_host(host):
            return False, "invalid host"
        try:
            port_number = int(port_number)
        except (TypeError, ValueError):
            return False, "invalid port number"
        if not 1 <= port_number <= 65535:
            return False, "invalid port number"
        script = (
            f"Add-PrinterPort -Name {_ps_literal(name)} "
            f"-PrinterHostAddress {_ps_literal(host)} "
            f"-PortNumber {port_number} -ErrorAction Stop"
        )
    ok, _, err = _ps(script)
    return ok, err or ""


def add_printer(name: str, driver: str, port: str) -> Tuple[bool, str]:
    """Create a print queue on an existing port."""
    problem = _name_error(name, "printer name")
    if problem:
        return False, problem
    if not isinstance(driver, str) or not driver.strip():
        return False, "invalid driver name: must be a non-empty string"
    problem = _name_error(port, "port name")
    if problem:
        return False, problem
    if port.upper().startswith("WSD-"):
        # A WSD-<guid> port is orphaned the moment its queue is deleted: the
        # port survives, but a new queue placed on it fails every job.
        return False, "refusing to reuse WSD port; pair through Windows Settings instead"
    ok, _, err = _ps(
        f"Add-Printer -Name {_ps_literal(name)} "
        f"-DriverName {_ps_literal(driver)} "
        f"-PortName {_ps_literal(port)} -ErrorAction Stop"
    )
    return ok, err or ""


def remove_printer(name: str) -> Tuple[bool, str]:
    problem = _name_error(name, "printer name")
    if problem:
        return False, problem
    ok, _, err = _ps(f"Remove-Printer -Name {_ps_literal(name)} -ErrorAction Stop")
    return ok, err or ""


def set_default_printer(name: str) -> Tuple[bool, str]:
    problem = _name_error(name, "printer name")
    if problem:
        return False, problem
    # Compared in PowerShell rather than spliced into a WQL filter, so the name
    # is never parsed as query syntax.
    script = (
        f"$n = {_ps_literal(name)}; "
        "$p = @(Get-CimInstance -ClassName Win32_Printer | Where-Object { $_.Name -eq $n }) "
        "| Select-Object -First 1; "
        "if (-not $p) { Write-Error ('no such printer: ' + $n); exit 1 }; "
        "$r = Invoke-CimMethod -InputObject $p -MethodName SetDefaultPrinter; "
        "if ($r.ReturnValue -ne 0) { "
        "Write-Error ('SetDefaultPrinter returned ' + $r.ReturnValue); exit 1 }"
    )
    ok, _, err = _ps(script)
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
        if device_sources > queue["trays"]:
            missing.append(
                f"paper trays ({queue['trays']} exposed, {device_sources} on device)"
            )
        device_color_modes = identity.get("color_modes") or []
        if len(device_color_modes) > 1 and not queue["color"]:
            missing.append("color (device supports color printing)")

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
        # Without the device's own answer there is nothing to compare against:
        # an empty `missing` then means "unknown", not "nothing lost", and
        # claiming the latter would hide a real downgrade.
        "comparable": bool(identity),
        "full_featured": (not missing) if identity else None,
        "note": None if identity else
        "the device did not answer IPP, so the queue's capabilities could not "
        "be compared against it",
    }


# ------------------------------------------------------------------- setup


def _pairing_regex(model: str, host: str) -> str:
    """A short, distinctive -Match value for win-pair-printer.ps1.

    This is advisory text only (never executed by this codebase - it's
    surfaced to the user as a recommended command line), but falling back to
    a bare `host` when nothing better is available must still be
    `re.escape`d: an unescaped dotted IP like 192.168.1.5 has its dots act as
    regex wildcards in the suggested -Match value, which could match
    unintended printer names.
    """
    strong = _strong_tokens(model)
    numeric = [t for t in strong if any(ch.isdigit() for ch in t)]
    token = (numeric or strong or [""])[0]
    return token or model or re.escape(host)


def plan_setup(
    host: str,
    name: Optional[str] = None,
    vendor_lookup: bool = False,
) -> Dict[str, Any]:
    """Work out what would be installed for `host`, changing nothing.

    `vendor_lookup` is opt-in: it sends the model, region and OS of this machine
    to the manufacturer's download portal, which a plan-only run must not do
    behind the user's back.
    """
    if not discovery.valid_host(host):
        return {"host": host, "reachable": False, "error": "invalid host"}

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

    # Nothing local matches the device: the only real driver left is the
    # vendor's.  Looking it up means talking to a third party, so it happens
    # only when the caller asked for it.
    no_local_driver = not candidates["vendor_installed"] and not candidates["vendor_available_inbox"]
    lookup_result: Optional[Dict[str, Any]] = None
    if not vendor_lookup:
        lookup_result = {
            "skipped": True,
            "hint": "re-run with --vendor-lookup to query the manufacturer's download portal",
        }
    elif model and no_local_driver:
        from local_printer import vendor_drivers

        lookup_result = vendor_drivers.find_driver(model)

    result = {
        "host": host,
        "reachable": True,
        "open_ports": discovery.open_port_names(open_ports),
        "identity": identity,
        "model": model,
        "suggested_name": name or (model or f"Printer {host}"),
        "driver_candidates": candidates,
        "strategies": steps,
        "vendor_driver_lookup": lookup_result,
        "elevated": is_elevated(),
        "note": None if is_elevated() else
        "Not running elevated: IPP/WSD ports and driver installation need "
        "administrator rights. Only the raw-9100 fallback is likely to succeed.",
    }
    if is_windows_11():
        # Every scripted strategy left on Win11 is a downgrade; the UI pairing
        # path is the only one that yields a negotiating queue.
        result["recommended"] = (
            f"scripts/win-pair-printer.ps1 -Match {_ps_literal(_pairing_regex(model, host))}"
        )
        result["recommended_reason"] = (
            "pairs through Windows Settings and yields the full-featured "
            "Microsoft IPP Class Driver queue"
        )
    if lookup_result and not lookup_result.get("skipped"):
        result["driver_hint"] = (
            f"No {model} driver is installed or shipped with Windows. "
            "Install the manufacturer driver from vendor_driver_lookup.download_page, "
            "then re-run setup to get the full feature set."
        )
    return result


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
    #    On Windows 11 Add-PrinterPort simply cannot create an IPP port, so the
    #    strategy is listed for transparency but marked as the dead end it is.
    win11 = is_windows_11()
    if open_ports.get(631):
        for scheme in ("https", "http"):
            entry: Dict[str, Any] = {
                "rank": len(ladder) + 1,
                "kind": "ipp-everywhere",
                "driver": "Microsoft IPP Class Driver",
                "needs_driver_install": False,
                "port": f"{scheme}://{host}:631{ipp_path}",
                "port_kind": "ipp",
                "full_featured": not win11,
                "requires_elevation": True,
                "reason": "class driver negotiates duplex/media from the device over IPP",
            }
            if win11:
                entry["known_broken_on"] = (
                    "Windows 11 (Add-PrinterPort cannot create IPP ports)"
                )
            ladder.append(entry)

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
    vendor_lookup: bool = False,
) -> Dict[str, Any]:
    """Install `host` as a printer, preferring full-featured strategies.

    Walks the strategy ladder until one succeeds, then verifies the result
    against the device's advertised capabilities.
    """
    plan = plan_setup(host, name, vendor_lookup=vendor_lookup)
    if not plan.get("reachable"):
        return plan
    if dry_run:
        return plan

    identity = plan["identity"]
    printer_name = name or plan["suggested_name"]
    problem = _name_error(printer_name, "printer name")
    if problem:
        return {
            "host": host,
            "printer": printer_name,
            "installed_with": None,
            "attempts": [],
            "identity": identity,
            "elevated": is_elevated(),
            "error": problem,
            "hint": "pass a usable queue name with --name",
        }
    attempts: List[Dict[str, Any]] = []

    for strategy in plan["strategies"]:
        if not allow_generic and not strategy["full_featured"]:
            attempts.append({**strategy, "status": "skipped", "detail": "generic setup not allowed"})
            continue
        if strategy.get("kind") == "ipp-everywhere" and is_windows_11():
            attempts.append({
                **strategy,
                "status": "skipped",
                "detail": "cannot create IPP ports on Windows 11; "
                          "use scripts/win-pair-printer.ps1",
            })
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
            "warning": None if verification["full_featured"] else (
                "Printer installed, but its capabilities could not be compared "
                "against the device -- see verification.note"
                if verification["full_featured"] is None else
                "Printer installed but with reduced capabilities -- see verification.missing"
            ),
        }

    failure = {
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
    if plan.get("recommended"):
        failure["recommended"] = plan["recommended"]
        failure["recommended_reason"] = plan.get("recommended_reason")
    return failure
