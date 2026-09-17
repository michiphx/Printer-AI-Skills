"""Unit tests for local_printer.discovery: pure-stdlib, no real network or subprocess calls."""

import sys
import struct
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from local_printer import discovery


# --------------------------------------------------------------- normalise_subnet


class TestNormaliseSubnet:
    @pytest.mark.parametrize(
        "raw, expected",
        [
            ("192.168.1", "192.168.1"),
            ("192.168.1.", "192.168.1"),
            ("192.168.1/24", "192.168.1"),
            ("10.0.0", "10.0.0"),
        ],
    )
    def test_accepts(self, raw, expected):
        assert discovery.normalise_subnet(raw) == expected

    @pytest.mark.parametrize(
        "raw",
        [
            "192.168",
            "192.168.1.5",
            "256.1.1",
            "01.2.3",
            "a.b.c",
            "",
            None,
        ],
    )
    def test_rejects(self, raw):
        with pytest.raises(ValueError):
            discovery.normalise_subnet(raw)


# --------------------------------------------------------------- is_private_prefix


class TestIsPrivatePrefix:
    @pytest.mark.parametrize(
        "prefix",
        ["10.1.2", "172.16.0", "172.31.255", "192.168.0", "192.168.255", "169.254.1"],
    )
    def test_true(self, prefix):
        assert discovery.is_private_prefix(prefix) is True

    @pytest.mark.parametrize("prefix", ["172.15.0", "172.32.0", "8.8.8", "100.64.0"])
    def test_false(self, prefix):
        assert discovery.is_private_prefix(prefix) is False


# --------------------------------------------------------------- _parse_octets


class TestParseOctets:
    def test_valid(self):
        assert discovery._parse_octets("192.168.1") == [192, 168, 1]

    def test_out_of_range(self):
        assert discovery._parse_octets("256.1.1") is None

    def test_leading_zero_rejected(self):
        assert discovery._parse_octets("01.2.3") is None

    def test_non_numeric(self):
        assert discovery._parse_octets("a.b.c") is None

    def test_empty(self):
        assert discovery._parse_octets("") is None

    def test_extra_octet_still_parses(self):
        # length checking is the caller's job, not _parse_octets'
        assert discovery._parse_octets("1.2.3.4") == [1, 2, 3, 4]


# --------------------------------------------------------------- normalise_mac


class TestNormaliseMac:
    def test_single_digit_colon_form(self):
        assert discovery.normalise_mac("0:1e:8f:a:2:3") == "00:1E:8F:0A:02:03"

    def test_dash_form(self):
        assert discovery.normalise_mac("00-1E-8F-0A-02-03") == "00:1E:8F:0A:02:03"


# --------------------------------------------------------------- arp_table

WINDOWS_ARP_OUTPUT = """
Interface: 192.168.1.5 --- 0xb
  Internet Address      Physical Address      Type
  192.168.1.72          00-1e-8f-0a-02-03     dynamic
  192.168.1.254         aa-bb-cc-dd-ee-ff     dynamic
"""

MACOS_ARP_OUTPUT = """? (192.168.1.72) at 0:1e:8f:a:2:3 on en0 ifscope [ethernet]
? (192.168.1.1) at (incomplete) on en0 ifscope [ethernet]
"""


class _FakeCompleted:
    def __init__(self, stdout):
        self.stdout = stdout


class TestArpTable:
    def test_windows_dash_form(self, monkeypatch):
        captured = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            captured["kwargs"] = kwargs
            return _FakeCompleted(WINDOWS_ARP_OUTPUT)

        monkeypatch.setattr(discovery.subprocess, "run", fake_run)
        table = discovery.arp_table()
        assert table == {
            "192.168.1.72": "00:1E:8F:0A:02:03",
            "192.168.1.254": "AA:BB:CC:DD:EE:FF",
        }
        assert captured["kwargs"]["encoding"] == "utf-8"

    def test_macos_single_digit_octets(self, monkeypatch):
        def fake_run(cmd, **kwargs):
            return _FakeCompleted(MACOS_ARP_OUTPUT)

        monkeypatch.setattr(discovery.subprocess, "run", fake_run)
        table = discovery.arp_table()
        assert table == {"192.168.1.72": "00:1E:8F:0A:02:03"}

    def test_oserror_returns_empty(self, monkeypatch):
        def fake_run(cmd, **kwargs):
            raise OSError("arp not found")

        monkeypatch.setattr(discovery.subprocess, "run", fake_run)
        assert discovery.arp_table() == {}


# --------------------------------------------------------------- ipp_printer_uri


class TestIppPrinterUri:
    def test_plain(self):
        assert discovery.ipp_printer_uri("h", "/ipp/print", 631, False) == "ipp://h:631/ipp/print"

    def test_tls(self):
        assert discovery.ipp_printer_uri("h", "/ipp/print", 443, True) == "ipps://h:443/ipp/print"

    def test_no_port(self):
        uri = discovery.ipp_printer_uri("h", "/ipp/print")
        assert uri == "ipp://h/ipp/print"
        assert ":" not in uri.split("//", 1)[1].split("/", 1)[0]


# --------------------------------------------------------------- _ipp_build_request


def _parse_request_attrs(body: bytes):
    assert body[0:2] == b"\x01\x01"  # version 1.1
    (op,) = struct.unpack(">H", body[2:4])
    pos = 8
    assert body[pos] == 0x01  # operation-attributes-tag
    pos += 1
    attrs = []
    while pos < len(body):
        tag = body[pos]
        pos += 1
        if tag == 0x03:
            break
        (name_len,) = struct.unpack(">H", body[pos : pos + 2])
        pos += 2
        name = body[pos : pos + name_len]
        pos += name_len
        (val_len,) = struct.unpack(">H", body[pos : pos + 2])
        pos += 2
        value = body[pos : pos + val_len]
        pos += val_len
        attrs.append((tag, name, value))
    return op, attrs


class TestIppBuildRequest:
    def test_non_tls(self):
        body = discovery._ipp_build_request("192.168.1.72", "/ipp/print", port=631, use_tls=False)
        op, attrs = _parse_request_attrs(body)
        assert op == 0x000B
        assert attrs[0][1] == b"attributes-charset"
        uri_values = [v for (_, n, v) in attrs if n == b"printer-uri"]
        assert len(uri_values) == 1
        assert uri_values[0].decode().startswith("ipp://")

    def test_tls(self):
        body = discovery._ipp_build_request("192.168.1.72", "/ipp/print", port=443, use_tls=True)
        op, attrs = _parse_request_attrs(body)
        assert op == 0x000B
        assert attrs[0][1] == b"attributes-charset"
        uri_values = [v for (_, n, v) in attrs if n == b"printer-uri"]
        assert uri_values[0].decode().startswith("ipps://")


# --------------------------------------------------------------- IPP response parsing


def _ipp_attr(tag: int, name: bytes, value: bytes) -> bytes:
    return struct.pack(">BH", tag, len(name)) + name + struct.pack(">H", len(value)) + value


def _build_ipp_http_response() -> bytes:
    body = struct.pack(">BBHI", 1, 1, 0x0000, 1)  # version 1.1, status success, request-id 1
    body += b"\x01"  # operation-attributes-tag
    body += _ipp_attr(0x47, b"attributes-charset", b"utf-8")
    body += _ipp_attr(0x48, b"attributes-natural-language", b"en")
    body += b"\x02"  # printer-attributes-tag
    body += _ipp_attr(0x42, b"printer-make-and-model", b"EPSON ET-4850 Series")
    body += _ipp_attr(0x23, b"printer-state", struct.pack(">i", 3))
    body += _ipp_attr(0x44, b"media-supported", b"na_letter_8.5x11in")
    body += _ipp_attr(0x44, b"", b"")  # second value, zero-length name, zero-length value
    body += _ipp_attr(0x44, b"sides-supported", b"one-sided")
    body += _ipp_attr(0x44, b"", b"two-sided-long-edge")
    body += b"\x03"  # end-of-attributes-tag
    header = (
        b"HTTP/1.1 200 OK\r\n"
        b"Content-Type: application/ipp\r\n"
        b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n"
    )
    return header + body


class TestIppResponseParsing:
    def test_ipp_query_normalises_identity(self, monkeypatch):
        raw = _build_ipp_http_response()

        def fake_exchange(host, port, path, use_tls, timeout):
            return raw

        monkeypatch.setattr(discovery, "_ipp_exchange", fake_exchange)
        identity = discovery.ipp_query("192.168.1.72")
        assert identity is not None
        assert identity["make_and_model"] == "EPSON ET-4850 Series"
        assert identity["state"] == "idle"
        assert identity["supports_duplex"] is True
        assert len(identity["media_supported"]) == 2

    def test_normalise_identity_directly(self):
        attrs = {
            "printer-make-and-model": "EPSON ET-4850 Series",
            "printer-state": 3,
            "printer-state-reasons": "none",
            "sides-supported": ["one-sided", "two-sided-long-edge"],
            "media-supported": ["na_letter_8.5x11in", "iso_a4_210x297mm"],
        }
        identity = discovery._normalise_identity("192.168.1.72", 631, False, "/ipp/print", attrs)
        assert identity["make_and_model"] == "EPSON ET-4850 Series"
        assert identity["state"] == "idle"
        assert identity["supports_duplex"] is True
        assert len(identity["media_supported"]) == 2
        assert identity["state_reasons"] == []


# --------------------------------------------------------------- _ipp_exchange cap


class _FakeIppSocket:
    def __init__(self):
        self.sent = None

    def settimeout(self, timeout):
        pass

    def sendall(self, data):
        self.sent = data

    def recv(self, n):
        return b"x" * n

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class TestIppExchangeCap:
    def test_response_is_capped(self, monkeypatch):
        fake_sock = _FakeIppSocket()
        monkeypatch.setattr(discovery.socket, "create_connection", lambda addr, timeout=None: fake_sock)
        result = discovery._ipp_exchange("192.168.1.72", 631, "/ipp/print", False, 1.0)
        assert result is not None
        assert len(result) == discovery.MAX_IPP_RESPONSE_BYTES


# --------------------------------------------------------------- host_of


class TestHostOf:
    @pytest.mark.parametrize(
        "text, expected",
        [
            ("IP_192.168.1.72", "192.168.1.72"),
            ("http://192.168.1.72:80/WSD/DEVICE", "192.168.1.72"),
            ("WSD-4b8f6c11-....", None),
            (None, None),
            ("", None),
            # network URIs with a literal host
            ("ipp://192.168.1.72:631/ipp/print", "192.168.1.72"),
            ("socket://10.0.0.5:9100", "10.0.0.5"),
            ("ipp://[fe80::1]:631/ipp/print", "fe80::1"),
            # hostnames are not resolved by host_of
            ("ipp://EPSON7D8B68.local:631/ipp/print", None),
            ("socket://myprinter:9100", None),
            ("dnssd://EPSON%20ET-4850._ipp._tcp.local/?uuid=abc", None),
            # non-network URIs never yield a host
            ("usb://EPSON/ET-4850?serial=X", None),
            ("file:///dev/null", None),
            ("cups-pdf:/", None),
        ],
    )
    def test_host_of(self, text, expected):
        assert discovery.host_of(text) == expected


# ----------------------------------------------------------- resolve_host


def _fake_getaddrinfo(address, family=None):
    def _gai(host, port, *args, **kwargs):
        fam = family if family is not None else discovery.socket.AF_INET
        return [(fam, discovery.socket.SOCK_STREAM, 6, "", (address, 0))]
    return _gai


def _failing_getaddrinfo(host, port, *args, **kwargs):
    raise discovery.socket.gaierror(-2, "Name or service not known")


class TestResolveHost:
    def test_no_host_text_returns_none(self, monkeypatch):
        monkeypatch.setattr(discovery.socket, "getaddrinfo", _failing_getaddrinfo)
        for text in (None, "", "nul:", "file:///dev/null", "cups-pdf:/",
                     "usb://EPSON/ET-4850?serial=X", "WSD-4b8f6c11", "Room 3"):
            assert discovery.resolve_host(text) is None, text

    def test_literal_ipv4_does_not_touch_dns(self, monkeypatch):
        monkeypatch.setattr(discovery.socket, "getaddrinfo", _failing_getaddrinfo)
        result = discovery.resolve_host("ipp://192.168.1.72:631/ipp/print")
        assert result == {"hostname": "192.168.1.72", "address": "192.168.1.72", "reason": None}
        assert discovery.resolve_host("IP_10.0.0.5")["address"] == "10.0.0.5"

    def test_literal_ipv6(self, monkeypatch):
        monkeypatch.setattr(discovery.socket, "getaddrinfo", _failing_getaddrinfo)
        result = discovery.resolve_host("ipp://[fe80::1]:631/ipp/print")
        assert result["address"] == "fe80::1"
        assert result["reason"] is None

    def test_hostname_resolves_via_getaddrinfo(self, monkeypatch):
        seen = []

        def gai(host, port, *args, **kwargs):
            seen.append(host)
            return [
                (discovery.socket.AF_INET6, 1, 6, "", ("fe80::1", 0, 0, 0)),
                (discovery.socket.AF_INET, 1, 6, "", ("192.168.1.72", 0)),
            ]

        monkeypatch.setattr(discovery.socket, "getaddrinfo", gai)
        result = discovery.resolve_host("ipp://EPSON7D8B68.local:631/ipp/print")
        assert seen == ["epson7d8b68.local"]
        # IPv4 answer is preferred even when it is not first
        assert result == {"hostname": "epson7d8b68.local", "address": "192.168.1.72", "reason": None}

    def test_hostname_falls_back_to_first_answer(self, monkeypatch):
        monkeypatch.setattr(
            discovery.socket, "getaddrinfo",
            _fake_getaddrinfo("fe80::2", family=discovery.socket.AF_INET6),
        )
        assert discovery.resolve_host("socket://myprinter:9100")["address"] == "fe80::2"

    def test_hostname_gaierror_is_unresolved(self, monkeypatch):
        monkeypatch.setattr(discovery.socket, "getaddrinfo", _failing_getaddrinfo)
        result = discovery.resolve_host("socket://myprinter:9100")
        assert result["address"] is None
        assert result["hostname"] == "myprinter"
        assert "does not resolve" in result["reason"]

    def test_hostname_oserror_is_unresolved(self, monkeypatch):
        def gai(host, port, *args, **kwargs):
            raise OSError("network down")

        monkeypatch.setattr(discovery.socket, "getaddrinfo", gai)
        assert discovery.resolve_host("ipp://printer.example:631/")["address"] is None

    def test_dnssd_is_unresolved_without_dns_lookup(self, monkeypatch):
        def gai(host, port, *args, **kwargs):
            raise AssertionError("dnssd:// must not hit getaddrinfo")

        monkeypatch.setattr(discovery.socket, "getaddrinfo", gai)
        result = discovery.resolve_host("dnssd://EPSON%20ET-4850._ipp._tcp.local/?uuid=abc")
        assert result["address"] is None
        assert "DNS-SD" in result["reason"]

    def test_hung_resolver_is_unresolved_within_timeout(self, monkeypatch):
        # A .local name that never answers must not stall diagnose for the OS
        # resolver's full retry budget.  The fake lookup blocks on an event so
        # the worker thread is released at the end of the test rather than
        # holding up interpreter shutdown.
        release = threading.Event()

        def hung_gai(host, port, *args, **kwargs):
            if not release.wait(5.0):
                raise discovery.socket.gaierror(-3, "Temporary failure in name resolution")
            return [(discovery.socket.AF_INET, 1, 6, "", ("192.168.1.72", 0))]

        monkeypatch.setattr(discovery.socket, "getaddrinfo", hung_gai)
        try:
            started = time.monotonic()
            result = discovery.resolve_host("ipp://ghost.local:631/ipp/print", timeout=0.2)
            elapsed = time.monotonic() - started
        finally:
            release.set()
        assert elapsed < 2.0, f"resolve_host blocked for {elapsed:.2f}s"
        assert result["address"] is None
        assert result["hostname"] == "ghost.local"
        assert "does not resolve" in result["reason"]

    def test_resolve_timeout_default_is_bounded(self):
        assert 0 < discovery.DEFAULT_RESOLVE_TIMEOUT <= 5.0

    def test_hung_resolver_does_not_hold_up_process_exit(self):
        # R3-03: the old ThreadPoolExecutor version returned on time, but
        # concurrent.futures joins its workers at interpreter exit, so the
        # whole CLI process hung until getaddrinfo actually came back.  Run
        # the resolver in a real subprocess with a getaddrinfo that sleeps
        # well past the timeout and check the process itself exits promptly.
        import subprocess

        repo_root = Path(__file__).resolve().parent.parent
        script = "\n".join([
            "import socket, sys, time",
            "from local_printer import discovery",
            "def slow_gai(*a, **k):",
            "    time.sleep(8.0)",
            "    return [(socket.AF_INET, 1, 6, '', ('192.168.1.72', 0))]",
            "socket.getaddrinfo = slow_gai",
            "t0 = time.monotonic()",
            "result = discovery._resolve_hostname('ghost.local', timeout=0.5)",
            "print(repr(result), round(time.monotonic() - t0, 3))",
        ])
        started = time.monotonic()
        proc = subprocess.run(
            [sys.executable, "-c", script],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=3.0,  # raises TimeoutExpired if exit is blocked by the worker
        )
        wall = time.monotonic() - started
        assert proc.returncode == 0, proc.stderr
        value, elapsed = proc.stdout.split()
        assert value == "None"
        assert float(elapsed) < 2.0, f"_resolve_hostname blocked for {elapsed}s"
        assert wall < 3.0, f"subprocess took {wall:.2f}s to exit"


# --------------------------------------------------------------- valid_host


class TestValidHost:
    @pytest.mark.parametrize("host", [
        "192.168.1.72",
        "10.0.0.5",
        "fe80::1",
        "fe80::1%eth0",
        "2001:db8::42",
        "printer.local",
        "EPSON-ET-4850",
        "my-printer.example.com",
        "a",
        "office_printer.local",
        "printer_local",
    ])
    def test_accepts_ip_literals_and_hostnames(self, host):
        assert discovery.valid_host(host) is True

    @pytest.mark.parametrize("host", [
        "",
        None,
        42,
        "x; rm",
        "x rm",
        "host&&id",
        "$(id)",
        "`id`",
        "host|cat",
        "ipp://printer.local",
        "printer.local/ipp/print",
        "[fe80::1]",
        "a" * 254,
        "999.999.999.999;x",
    ])
    def test_rejects_malformed_input(self, host):
        assert discovery.valid_host(host) is False

    def test_percent_encoded_ipv6_zone_is_literal(self, monkeypatch):
        # RFC 6874 writes a zone index as %25 inside the URI; the decoded form
        # is a valid IPv6 literal and must not go anywhere near the resolver.
        monkeypatch.setattr(discovery.socket, "getaddrinfo", _failing_getaddrinfo)
        encoded = discovery.resolve_host("ipp://[fe80::1%25eth0]:631/ipp/print")
        plain = discovery.resolve_host("ipp://[fe80::1%eth0]:631/ipp/print")
        assert encoded == plain
        assert encoded["address"] == "fe80::1%eth0"
        assert encoded["reason"] is None
        assert discovery.host_of("ipp://[fe80::1%25eth0]:631/") == "fe80::1%eth0"

    def test_percent_encoded_hostname_is_decoded_before_lookup(self, monkeypatch):
        seen = []

        def gai(host, port, *args, **kwargs):
            seen.append(host)
            return [(discovery.socket.AF_INET, 1, 6, "", ("192.168.1.72", 0))]

        monkeypatch.setattr(discovery.socket, "getaddrinfo", gai)
        encoded = discovery.resolve_host("ipp://my%2Dprinter.local:631/ipp/print")
        plain = discovery.resolve_host("ipp://my-printer.local:631/ipp/print")
        assert seen == ["my-printer.local", "my-printer.local"]
        assert encoded == plain
        assert encoded["hostname"] == "my-printer.local"


# --------------------------------------------------------------- scan_subnet


class _InspectSpy:
    def __init__(self, result=None):
        self.calls = []
        self.result = result

    def __call__(self, host, timeout=0.6, deep=True):
        self.calls.append(host)
        return self.result


class _SpyExecutor:
    instances = []

    def __init__(self, max_workers=None):
        self.max_workers = max_workers
        _SpyExecutor.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def map(self, fn, iterable):
        return [fn(x) for x in iterable]


class TestScanSubnet:
    def _patch_common(self, monkeypatch, spy):
        monkeypatch.setattr(discovery, "local_ipv4_networks", lambda: ["192.168.1"])
        monkeypatch.setattr(discovery, "default_subnet", lambda: "192.168.1")
        monkeypatch.setattr(discovery, "arp_table", lambda: {})
        monkeypatch.setattr(discovery, "inspect_host", spy)

    def test_refuses_non_private_without_force(self, monkeypatch):
        spy = _InspectSpy()
        self._patch_common(monkeypatch, spy)
        result = discovery.scan_subnet(subnet="8.8.8")
        assert result["code"] == 400
        assert "refusing" in result["msg"]
        assert spy.calls == []

    def test_force_scans_254_hosts(self, monkeypatch):
        spy = _InspectSpy()
        self._patch_common(monkeypatch, spy)
        result = discovery.scan_subnet(subnet="8.8.8", force=True)
        assert len(spy.calls) == 254
        assert result["subnet"] == "8.8.8.0/24"

    def test_own_network_scans_without_force(self, monkeypatch):
        spy = _InspectSpy()
        self._patch_common(monkeypatch, spy)
        result = discovery.scan_subnet(subnet="192.168.1")
        assert len(spy.calls) == 254
        assert result["subnet"] == "192.168.1.0/24"
        assert "code" not in result

    def test_private_subnet_scans_without_force(self, monkeypatch):
        spy = _InspectSpy()
        self._patch_common(monkeypatch, spy)
        result = discovery.scan_subnet(subnet="10.5.5")
        assert len(spy.calls) == 254
        assert result["subnet"] == "10.5.5.0/24"

    def test_invalid_prefix_returns_400(self, monkeypatch):
        spy = _InspectSpy()
        self._patch_common(monkeypatch, spy)
        result = discovery.scan_subnet(subnet="256.1.1")
        assert result["code"] == 400
        assert spy.calls == []

    def test_workers_clamped_to_max(self, monkeypatch):
        spy = _InspectSpy()
        self._patch_common(monkeypatch, spy)
        _SpyExecutor.instances = []
        monkeypatch.setattr(discovery, "ThreadPoolExecutor", _SpyExecutor)
        discovery.scan_subnet(subnet="192.168.1", workers=500, force=True)
        assert _SpyExecutor.instances[-1].max_workers == discovery.MAX_SCAN_WORKERS


# --------------------------------------- undecodable attribute values


def _ipp_attr_bytes(tag, name, value):
    return struct.pack(">BH", tag, len(name)) + name + struct.pack(">H", len(value)) + value


def _ipp_body(*attrs):
    body = struct.pack(">HHI", 0x0200, 0x0000, 1) + b"\x01"
    for chunk in attrs:
        body += chunk
    return body + b"\x03"


def test_undecodable_attribute_values_do_not_join_the_previous_attribute():
    # tag 0x32 is `resolution`, which this parser does not decode. Its extra
    # values arrive with a zero-length name; appending them to the text
    # attribute before it would corrupt the model string.
    body = _ipp_body(
        _ipp_attr_bytes(0x42, b"printer-make-and-model", b"EPSON ET-4850 Series"),
        _ipp_attr_bytes(0x32, b"printer-resolution-supported", b"\x00\x00\x01,\x00\x00\x01,\x03"),
        _ipp_attr_bytes(0x32, b"", b"\x00\x00\x02X\x00\x00\x02X\x03"),
        _ipp_attr_bytes(0x23, b"printer-state", struct.pack(">i", 3)),
    )
    parsed = discovery._ipp_parse_response(body)
    assert parsed["printer-make-and-model"] == "EPSON ET-4850 Series"
    assert parsed["printer-state"] == 3
    assert "printer-resolution-supported" not in parsed


def test_multi_value_attributes_still_collect():
    body = _ipp_body(
        _ipp_attr_bytes(0x44, b"media-supported", b"iso_a4_210x297mm"),
        _ipp_attr_bytes(0x44, b"", b"na_letter_8.5x11in"),
        _ipp_attr_bytes(0x44, b"", b"iso_a5_148x210mm"),
    )
    parsed = discovery._ipp_parse_response(body)
    assert parsed["media-supported"] == [
        "iso_a4_210x297mm",
        "na_letter_8.5x11in",
        "iso_a5_148x210mm",
    ]


def test_orphan_additional_value_is_dropped_not_crashed():
    # A continuation value with no named attribute before it (malformed device).
    body = _ipp_body(_ipp_attr_bytes(0x44, b"", b"orphan"))
    assert discovery._ipp_parse_response(body) == {}
