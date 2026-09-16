"""Unit tests for local_printer.commands_net: discover, diagnose, setup.

All network I/O is monkeypatched out at the `discovery` module boundary, and
`local_printer.windows.get_printer_list` / `local_printer.setup_windows.setup_printer`
are stubbed so nothing here touches a real socket, spooler, or printer.
"""

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pytest

from local_printer import commands_net as cn
from local_printer import discovery
from local_printer import setup_windows as sw


@pytest.fixture(autouse=True)
def _no_real_io(monkeypatch):
    """Fail fast if a test reaches the wire or the spooler without stubbing it.

    `cn.setup` on non-Windows hosts goes through `_setup_cups`, which calls
    `discovery.ipp_query` (multi-second socket timeouts) and then `lpadmin`.
    Tests that want these seams patch them explicitly on top of this fixture;
    anything that forgets gets an AssertionError instead of a 48s DNS wait or
    a real queue being installed.
    """
    import subprocess

    def _unpatched(name):
        def _raise(*args, **kwargs):
            raise AssertionError(f"{name} reached real I/O; patch it in the test")
        return _raise

    monkeypatch.setattr(discovery, "ipp_query", _unpatched("discovery.ipp_query"))
    monkeypatch.setattr(discovery, "probe_ports", _unpatched("discovery.probe_ports"))
    monkeypatch.setattr(discovery, "scan_subnet", _unpatched("discovery.scan_subnet"))

    real_run = subprocess.run

    def guarded_run(cmd, *args, **kwargs):
        head = str(cmd[0] if isinstance(cmd, (list, tuple)) and cmd else cmd)
        if os.path.basename(head) in {"lpadmin", "lp", "lpr", "lpstat", "cancel"}:
            raise AssertionError(f"test tried to run real {head!r}; patch subprocess.run")
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", guarded_run)


# ------------------------------------------------------------------- discover


def test_discover_refusal_with_code_passes_through(monkeypatch):
    monkeypatch.setattr(discovery, "scan_subnet", lambda **k: {"code": 400, "error": "refused"})
    result = cn.discover()
    assert result["code"] == 400
    assert result["msg"] == "refused"


def test_discover_error_without_code_maps_to_500(monkeypatch):
    monkeypatch.setattr(
        discovery, "scan_subnet",
        lambda **k: {"error": "no local IPv4 network found"},
    )
    result = cn.discover()
    assert result["code"] == 500
    assert result["msg"] == "no local IPv4 network found"


def test_discover_success_returns_200_with_data_passthrough(monkeypatch):
    payload = {"printers": [{"host": "10.0.0.5"}], "count": 1}
    monkeypatch.setattr(discovery, "scan_subnet", lambda **k: payload)
    result = cn.discover()
    assert result["code"] == 200
    assert result["data"] == payload


def test_discover_forwards_force_kwarg(monkeypatch):
    captured = {}

    def fake_scan_subnet(**kwargs):
        captured.update(kwargs)
        return {"printers": [], "count": 0}

    monkeypatch.setattr(discovery, "scan_subnet", fake_scan_subnet)
    cn.discover(subnet="10.0.0", timeout=1.5, deep=False, force=True)
    assert captured["force"] is True
    assert captured["subnet"] == "10.0.0"
    assert captured["timeout"] == 1.5
    assert captured["deep"] is False


# ------------------------------------------------------------------- diagnose


def _listing(printers):
    return {"code": 200, "msg": "success", "data": {"printers": printers}}


def _patch_no_network_io(monkeypatch, open_ports_by_host):
    def fake_probe_ports(host, timeout=1.0):
        return open_ports_by_host.get(host, {})

    def fake_ipp_query(host, timeout=3.0):
        return None

    monkeypatch.setattr(discovery, "probe_ports", fake_probe_ports)
    monkeypatch.setattr(discovery, "ipp_query", fake_ipp_query)
    # is_printer_host is pure (no I/O); keep the real implementation but patch
    # it through the same seam the instructions call out, to guarantee no
    # accidental network path is exercised even if the module is changed later.
    real_is_printer_host = discovery.is_printer_host
    monkeypatch.setattr(discovery, "is_printer_host", lambda ports: real_is_printer_host(ports))


@pytest.mark.skipif(sys.platform != "win32", reason="diagnose imports local_printer.windows on win32")
def test_diagnose_classifies_queues_and_counts(monkeypatch):
    printers = [
        {  # WSD port, no address anywhere -> unknown
            "index": 1, "name": "WSDOnly", "status": "idle",
            "port": "WSD-aaaa1111", "is_default": False,
            "uri": None, "location": None,
        },
        {  # WSD port but location holds an IP -> network
            "index": 2, "name": "WSDWithIP", "status": "idle",
            "port": "WSD-bbbb2222", "is_default": False,
            "uri": None, "location": "10.0.0.9",
        },
        {  # nul: port -> virtual-or-local
            "index": 3, "name": "LocalPDF", "status": "idle",
            "port": "nul:", "is_default": True,
            "uri": None, "location": "",
        },
        {  # IP-named port, nothing open -> unreachable
            "index": 4, "name": "DeadPrinter", "status": "idle",
            "port": "IP_10.0.0.5", "is_default": False,
            "uri": None, "location": "",
        },
    ]

    import local_printer.windows as winmod
    monkeypatch.setattr(winmod, "get_printer_list", lambda: _listing(printers))

    _patch_no_network_io(monkeypatch, {
        "10.0.0.9": {9100: True, 631: False, 515: False},
        "10.0.0.5": {9100: False, 631: False, 515: False},
    })

    result = cn.diagnose()
    assert result["code"] == 200
    entries = {e["name"]: e for e in result["data"]["printers"]}

    assert entries["WSDOnly"]["kind"] == "unknown"
    assert entries["WSDOnly"]["really_online"] is None

    assert entries["WSDWithIP"]["kind"] == "network"
    assert entries["WSDWithIP"]["really_online"] is True

    assert entries["LocalPDF"]["kind"] == "virtual-or-local"
    assert entries["LocalPDF"]["really_online"] is True

    assert entries["DeadPrinter"]["kind"] == "network"
    assert entries["DeadPrinter"]["really_online"] is False
    assert entries["DeadPrinter"]["verdict"].startswith("UNREACHABLE")

    assert result["data"]["online"] == 2
    assert result["data"]["offline"] == 1
    assert result["data"]["unknown"] == 1
    assert result["data"]["count"] == 4


def _patch_listing(monkeypatch, printers):
    """Stub the platform's own get_printer_list, so these run on any OS."""
    if sys.platform == "win32":
        import local_printer.windows as platmod
    else:
        import local_printer.cups as platmod
    monkeypatch.setattr(platmod, "get_printer_list", lambda: _listing(printers))


def _queue(index, name, uri, port="", location=""):
    return {
        "index": index, "name": name, "status": "idle", "is_default": False,
        "port": port, "uri": uri, "location": location,
    }


def _gai_failing(host, port, *args, **kwargs):
    raise discovery.socket.gaierror(-2, "Name or service not known")


def test_diagnose_hostname_uri_resolves_and_is_probed(monkeypatch):
    seen = []

    def gai(host, port, *args, **kwargs):
        seen.append(host)
        return [(discovery.socket.AF_INET, 1, 6, "", ("192.168.1.72", 0))]

    monkeypatch.setattr(discovery.socket, "getaddrinfo", gai)
    _patch_listing(monkeypatch, [
        _queue(1, "EpsonByName", "ipp://EPSON7D8B68.local:631/ipp/print"),
        _queue(2, "RawByName", "socket://myprinter:9100"),
    ])
    _patch_no_network_io(monkeypatch, {
        "192.168.1.72": {9100: False, 631: True, 515: False},
    })

    result = cn.diagnose(deep=False)
    assert result["code"] == 200
    assert seen == ["epson7d8b68.local", "myprinter"]
    entries = {e["name"]: e for e in result["data"]["printers"]}

    # both resolved to the same fake address, which "answers" on 631
    for name in ("EpsonByName", "RawByName"):
        assert entries[name]["kind"] == "network"
        assert entries[name]["host"] == "192.168.1.72"
        assert entries[name]["really_online"] is True
        assert entries[name]["verdict"] == "reachable"
    assert entries["EpsonByName"]["hostname"] == "epson7d8b68.local"
    assert result["data"]["online"] == 2
    assert result["data"]["unknown"] == 0


def test_diagnose_hostname_uri_that_does_not_resolve(monkeypatch):
    probed = []

    def fake_probe_ports(host, timeout=1.0):
        probed.append(host)
        return {}

    monkeypatch.setattr(discovery.socket, "getaddrinfo", _gai_failing)
    monkeypatch.setattr(discovery, "probe_ports", fake_probe_ports)
    monkeypatch.setattr(discovery, "ipp_query", lambda host, timeout=3.0: None)
    _patch_listing(monkeypatch, [
        _queue(1, "Ghost", "ipp://EPSON7D8B68.local:631/ipp/print"),
    ])

    result = cn.diagnose()
    assert result["code"] == 200
    entry = result["data"]["printers"][0]
    assert entry["kind"] == "network-unresolved"
    assert entry["really_online"] is None
    assert entry["hostname"] == "epson7d8b68.local"
    assert "does not resolve" in entry["verdict"]
    assert "epson7d8b68.local" in entry["verdict"]
    assert entry["hint"]
    assert probed == []
    assert result["data"]["unknown"] == 1
    assert result["data"]["online"] == 0
    assert result["data"]["offline"] == 0


def test_diagnose_dnssd_uri_is_unresolved(monkeypatch):
    def gai(host, port, *args, **kwargs):
        raise AssertionError("dnssd:// must not be passed to getaddrinfo")

    monkeypatch.setattr(discovery.socket, "getaddrinfo", gai)
    _patch_no_network_io(monkeypatch, {})
    _patch_listing(monkeypatch, [
        _queue(1, "Bonjour", "dnssd://EPSON%20ET-4850._ipp._tcp.local/?uuid=abc"),
    ])

    result = cn.diagnose()
    entry = result["data"]["printers"][0]
    assert entry["kind"] == "network-unresolved"
    assert entry["really_online"] is None
    assert "DNS-SD" in entry["verdict"]
    assert result["data"]["unknown"] == 1


def test_diagnose_dnssd_uri_with_ip_in_location_is_probed(monkeypatch):
    monkeypatch.setattr(discovery.socket, "getaddrinfo", _gai_failing)
    _patch_no_network_io(monkeypatch, {"10.0.0.9": {9100: True, 631: False, 515: False}})
    _patch_listing(monkeypatch, [
        _queue(1, "Bonjour", "dnssd://EPSON._ipp._tcp.local/", location="10.0.0.9"),
    ])

    entry = cn.diagnose(deep=False)["data"]["printers"][0]
    assert entry["kind"] == "network"
    assert entry["host"] == "10.0.0.9"
    assert entry["really_online"] is True


def test_diagnose_dotted_quad_and_local_paths_unchanged(monkeypatch):
    # getaddrinfo must never be consulted for literal IPs or local queues
    monkeypatch.setattr(discovery.socket, "getaddrinfo", _gai_failing)
    _patch_listing(monkeypatch, [
        _queue(1, "LiveIP", "ipp://10.0.0.9:631/ipp/print"),
        _queue(2, "DeadIP", "socket://10.0.0.5:9100"),
        _queue(3, "PortName", None, port="IP_10.0.0.9"),
        _queue(4, "PDF", "cups-pdf:/"),
        _queue(5, "USB", "usb://EPSON/ET-4850?serial=X"),
        _queue(6, "File", "file:///dev/null"),
        _queue(7, "WSDOnly", None, port="WSD-aaaa1111"),
    ])
    _patch_no_network_io(monkeypatch, {
        "10.0.0.9": {9100: True, 631: False, 515: False},
        "10.0.0.5": {9100: False, 631: False, 515: False},
    })

    result = cn.diagnose(deep=False)
    entries = {e["name"]: e for e in result["data"]["printers"]}

    assert entries["LiveIP"]["kind"] == "network"
    assert entries["LiveIP"]["host"] == "10.0.0.9"
    assert entries["LiveIP"]["really_online"] is True
    assert "hostname" not in entries["LiveIP"]

    assert entries["DeadIP"]["kind"] == "network"
    assert entries["DeadIP"]["really_online"] is False
    assert entries["DeadIP"]["verdict"].startswith("UNREACHABLE")

    assert entries["PortName"]["kind"] == "network"
    assert entries["PortName"]["really_online"] is True

    for name in ("PDF", "USB", "File"):
        assert entries[name]["kind"] == "virtual-or-local", name
        assert entries[name]["really_online"] is True, name

    assert entries["WSDOnly"]["kind"] == "unknown"
    assert entries["WSDOnly"]["really_online"] is None

    assert result["data"]["online"] == 5
    assert result["data"]["offline"] == 1
    assert result["data"]["unknown"] == 1
    assert result["data"]["count"] == 7


# ----------------------------------------------------------------------- setup


def test_setup_forwards_vendor_lookup_and_maps_error_to_500(monkeypatch):
    captured = {}

    def fake_setup_printer(host, name=None, allow_generic=True, dry_run=False, vendor_lookup=False):
        captured["host"] = host
        captured["vendor_lookup"] = vendor_lookup
        captured["allow_generic"] = allow_generic
        captured["dry_run"] = dry_run
        return {"error": "every setup strategy failed"}

    # This exercises the Windows branch of cn.setup; on Linux/macOS the call
    # would otherwise fall through to _setup_cups and hit the network.
    _patch_windows_setup(monkeypatch, fake_setup_printer)

    result = cn.setup("10.0.0.5", vendor_lookup=True)

    assert captured["host"] == "10.0.0.5"
    assert captured["vendor_lookup"] is True
    assert result["code"] == 500
    assert result["msg"] == "every setup strategy failed"


def test_setup_success_maps_to_200(monkeypatch):
    def fake_setup_printer(host, name=None, allow_generic=True, dry_run=False, vendor_lookup=False):
        return {"printer": "Foo", "installed_with": {"kind": "raw-fallback"}}

    _patch_windows_setup(monkeypatch, fake_setup_printer)

    result = cn.setup("10.0.0.5", vendor_lookup=False)
    assert result["code"] == 200


# ------------------------------------------------------- setup: CUPS branch


_IDENTITY = {
    "host": "10.0.0.5", "ipp_path": "/ipp/print", "ipp_port": 631, "ipp_tls": False,
    "ipp_uri": "ipp://10.0.0.5:631/ipp/print", "make_and_model": "EPSON ET-4850",
}


def _patch_cups_setup(monkeypatch, identity, run_result=None, calls=None):
    """Route cn.setup through _setup_cups with IPP and lpadmin stubbed out."""
    import subprocess

    monkeypatch.setattr(cn, "IS_WINDOWS", False)
    monkeypatch.setattr(discovery, "ipp_query", lambda host, timeout=3.0: identity)

    def fake_run(cmd, *args, **kwargs):
        if calls is not None:
            calls.append(list(cmd))
        return run_result or subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)


def test_setup_cups_host_without_ipp_is_404(monkeypatch):
    calls = []
    _patch_cups_setup(monkeypatch, identity=None, calls=calls)

    result = cn.setup("10.0.0.5")

    assert result["code"] == 404
    assert "does not answer IPP" in result["msg"]
    assert calls == []


def test_setup_cups_dry_run_reports_command_and_runs_nothing(monkeypatch):
    calls = []
    _patch_cups_setup(monkeypatch, identity=_IDENTITY, calls=calls)

    result = cn.setup("10.0.0.5", dry_run=True)

    assert result["code"] == 200
    assert result["data"]["identity"] == _IDENTITY
    assert result["data"]["would_run"].startswith("lpadmin -p EPSON_ET-4850 -E -v ipp://10.0.0.5:631/ipp/print")
    assert calls == []


def test_setup_cups_runs_lpadmin_with_sanitised_queue_name(monkeypatch):
    calls = []
    _patch_cups_setup(monkeypatch, identity=_IDENTITY, calls=calls)

    result = cn.setup("10.0.0.5", name="Office Printer #2")

    assert result["code"] == 200
    assert result["data"]["printer"] == "Office_Printer__2"
    assert result["data"]["installed_with"] == {"kind": "ipp-everywhere", "driver": "everywhere"}
    assert calls == [[
        "lpadmin", "-p", "Office_Printer__2", "-E", "-v", "ipp://10.0.0.5:631/ipp/print",
        "-m", "everywhere",
    ]]


def test_setup_cups_lpadmin_failure_is_500_with_stderr(monkeypatch):
    import subprocess

    failed = subprocess.CompletedProcess([], 1, stdout="", stderr="lpadmin: Bad device-uri\n")
    _patch_cups_setup(monkeypatch, identity=_IDENTITY, run_result=failed)

    result = cn.setup("10.0.0.5")

    assert result["code"] == 500
    assert "Bad device-uri" in result["msg"]
    assert result["data"]["command"].startswith("lpadmin ")


# ------------------------------------------------- setup error -> HTTP code (R2-19)


def _patch_windows_setup(monkeypatch, fake_setup_printer):
    """Route cn.setup through the Windows branch with setup_printer stubbed out.

    On non-Windows hosts cn.IS_WINDOWS is False and cn._setup is None, so
    without this the call would fall into _setup_cups and hit the network.
    """
    monkeypatch.setattr(cn, "IS_WINDOWS", True)
    monkeypatch.setattr(cn, "_setup", sw, raising=False)
    monkeypatch.setattr(sw, "setup_printer", fake_setup_printer)


@pytest.mark.parametrize("error, expected", [
    ("invalid host", 400),
    ("invalid printer name: must be a non-empty string", 400),
    ("invalid printer name: longer than 220 characters", 400),
    ("no printing port (9100/631/515) open on this host", 404),
    ("host unreachable", 404),
    ("10.0.0.5 does not answer IPP", 404),
    ("printer not found", 404),
    ("every setup strategy failed", 500),
    ("driver install failed: access denied", 500),
    ("something nobody anticipated", 500),
    ("", 500),
    (None, 500),
])
def test_setup_error_code_helper(error, expected):
    assert cn._setup_error_code(error) == expected


def test_setup_error_code_is_case_insensitive():
    assert cn._setup_error_code("Invalid Host") == 400
    assert cn._setup_error_code("No Printing Port open") == 404


@pytest.mark.parametrize("payload, expected_code", [
    ({"host": "not a host", "reachable": False, "error": "invalid host"}, 400),
    (
        {"host": "10.0.0.5", "reachable": False,
         "error": "no printing port (9100/631/515) open on this host"},
        404,
    ),
    (
        {"host": "10.0.0.5", "printer": "bad|name", "installed_with": None,
         "attempts": [], "error": "invalid printer name: must not contain '|' (Windows forbids it)",
         "hint": "pass a usable queue name with --name"},
        400,
    ),
    (
        {"host": "10.0.0.5", "printer": "Foo", "installed_with": None,
         "attempts": [{"status": "failed", "detail": "driver install failed: x"}],
         "error": "every setup strategy failed"},
        500,
    ),
])
def test_setup_maps_error_shapes_to_codes(monkeypatch, payload, expected_code):
    def fake_setup_printer(host, name=None, allow_generic=True, dry_run=False, vendor_lookup=False):
        return payload

    _patch_windows_setup(monkeypatch, fake_setup_printer)

    result = cn.setup(payload["host"])

    assert result["code"] == expected_code
    assert result["msg"] == payload["error"]
    assert result["data"] == payload


def test_setup_without_error_still_returns_200(monkeypatch):
    payload = {"host": "10.0.0.5", "printer": "Foo", "installed_with": {"kind": "raw-fallback"}}

    def fake_setup_printer(host, name=None, allow_generic=True, dry_run=False, vendor_lookup=False):
        return payload

    _patch_windows_setup(monkeypatch, fake_setup_printer)

    result = cn.setup("10.0.0.5")

    assert result["code"] == 200
    assert result["msg"] == "success"
    assert result["data"] == payload
