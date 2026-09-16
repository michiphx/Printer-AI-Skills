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

    monkeypatch.setattr(sw, "setup_printer", fake_setup_printer)

    result = cn.setup("10.0.0.5", vendor_lookup=True)

    assert captured["host"] == "10.0.0.5"
    assert captured["vendor_lookup"] is True
    assert result["code"] == 500
    assert result["msg"] == "every setup strategy failed"


def test_setup_success_maps_to_200(monkeypatch):
    def fake_setup_printer(host, name=None, allow_generic=True, dry_run=False, vendor_lookup=False):
        return {"printer": "Foo", "installed_with": {"kind": "raw-fallback"}}

    monkeypatch.setattr(sw, "setup_printer", fake_setup_printer)

    result = cn.setup("10.0.0.5", vendor_lookup=False)
    assert result["code"] == 200
