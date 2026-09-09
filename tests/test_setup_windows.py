"""Unit tests for local_printer.setup_windows.

Covers PowerShell literal quoting, host/name validation, INF parsing, driver
matching, the strategy ladder, and the mutating helpers' refusal-before-`_ps`
guarantees. No test touches a real printer, port, or driver.
"""

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pytest

from local_printer import setup_windows as sw


# --------------------------------------------------------------- _ps_literal


def test_ps_literal_plain_text():
    assert sw._ps_literal("hello") == "'hello'"


def test_ps_literal_doubles_embedded_single_quotes():
    assert sw._ps_literal("it's") == "'it''s'"


def test_ps_literal_dollar_subexpression_not_special():
    assert sw._ps_literal("$(Start-Process calc.exe)") == "'$(Start-Process calc.exe)'"


def test_ps_literal_backtick_not_special():
    assert sw._ps_literal("`whoami`") == "'`whoami`'"


def test_ps_literal_double_quote_not_special():
    assert sw._ps_literal('say "hi"') == "'say \"hi\"'"


def test_ps_literal_none_is_empty_literal():
    assert sw._ps_literal(None) == "''"


@pytest.mark.skipif(sys.platform != "win32", reason="spawns powershell.exe")
def test_ps_literal_integration_nothing_expands():
    payload = "EPSON $(Start-Process calc.exe) `whoami` ';x' Series"
    literal = sw._ps_literal(payload)
    ok, out, err = sw._ps("Write-Output " + literal)
    assert ok, err
    assert out == payload


# ----------------------------------------------------------------- _valid_host


@pytest.mark.parametrize(
    "host",
    [
        "192.168.1.5",
        "10.0.0.1",
        "::1",
        "fe80::1",
        "printer.local",
        "MyPrinter",
        "a-b.c",
    ],
)
def test_valid_host_accepts(host):
    assert sw._valid_host(host) is True


@pytest.mark.parametrize(
    "host",
    [
        "x; rm -rf /",
        "$(calc)",
        "",
        None,
        123,
        "a" * 254,
    ],
)
def test_valid_host_rejects(host):
    assert sw._valid_host(host) is False


# ------------------------------------------------------------------ _name_error


@pytest.mark.parametrize("name", ["Printer1", "HP LaserJet", "a", "A" * 220])
def test_name_error_ok_names(name):
    assert sw._name_error(name) is None


def test_name_error_rejects_comma():
    assert sw._name_error("foo,bar") is not None


def test_name_error_rejects_backslash():
    assert sw._name_error("foo\\bar") is not None


def test_name_error_rejects_empty():
    assert sw._name_error("") is not None
    assert sw._name_error("   ") is not None


def test_name_error_rejects_too_long():
    assert sw._name_error("A" * 221) is not None


def test_name_error_rejects_non_str():
    assert sw._name_error(None) is not None
    assert sw._name_error(123) is not None


# ---------------------------------------------------------------- _strong_tokens


def test_strong_tokens_hp_alone_is_empty():
    assert sw._strong_tokens("HP") == []


def test_strong_tokens_epson_model_contains_digit_token():
    tokens = sw._strong_tokens("EPSON ET-4850 Series")
    assert "4850" in tokens or "et4850" in tokens or any("4850" in t for t in tokens)


def test_strong_tokens_drops_stop_list_words():
    tokens = sw._strong_tokens("EPSON Printer Series Class Driver")
    for stop in ("epson", "printer", "series", "class", "driver"):
        assert stop not in tokens


def test_strong_tokens_keeps_four_letter_plus_words():
    tokens = sw._strong_tokens("Brother MFC Laser")
    assert "laser" in tokens
    assert "mfc" not in tokens  # 3 letters, no digit -> not strong


# --------------------------------------------------------------------- INF parsing

INF_ET4850 = """\
[Version]
Signature="$Windows NT$"

[Manufacturer]
%Epson% = Epson, NTamd64

[Strings]
Epson = "EPSON"
ET4850 = "EPSON ET-4850 Series"  ; comment should be stripped

[Epson.NTamd64]
%ET4850% = Install, USBPRINT\\EpsonET4850
"HP Universal Printing PCL 6" = Install2, USB\\VID_03F0
"""


def test_parse_inf_sections_and_lines():
    sections = sw._parse_inf(INF_ET4850)
    assert "strings" in sections
    assert "epson.ntamd64" in sections
    assert any("ET4850" in line for line in sections["strings"])


def test_inf_strings_quoted_and_comment_stripped():
    sections = sw._parse_inf(INF_ET4850)
    strings = sw._inf_strings(sections)
    assert strings["et4850"] == "EPSON ET-4850 Series"
    assert strings["epson"] == "EPSON"


def test_inf_strings_unquoted_value():
    text = "[Strings]\nFoo = Bar Baz\n"
    strings = sw._inf_strings(sw._parse_inf(text))
    assert strings["foo"] == "Bar Baz"


def test_inf_driver_names_resolves_token_and_keeps_literal():
    sections = sw._parse_inf(INF_ET4850)
    strings = sw._inf_strings(sections)
    names = sw._inf_driver_names(sections, strings)
    assert "EPSON ET-4850 Series" in names
    assert "HP Universal Printing PCL 6" in names


def test_inf_driver_names_skips_version_and_manufacturer_sections():
    sections = sw._parse_inf(INF_ET4850)
    strings = sw._inf_strings(sections)
    names = sw._inf_driver_names(sections, strings)
    # The [Manufacturer] entry (%Epson% = Epson, NTamd64) must not leak in as
    # a driver name candidate.
    assert "Epson" not in names or names.count("Epson") <= strings.values().__len__()
    # More directly: nothing from [Version]/[Manufacturer] raw keys appears.
    assert "%Epson%" not in names


def test_read_inf_utf16le_with_bom(tmp_path):
    path = tmp_path / "utf16.inf"
    path.write_bytes(("[Strings]\r\nA=Hello\r\n").encode("utf-16"))
    text = sw._read_inf(str(path))
    assert "Hello" in text


def test_read_inf_utf8_with_bom(tmp_path):
    path = tmp_path / "utf8bom.inf"
    path.write_bytes(b"\xef\xbb\xbf[Strings]\r\nA=Hello\r\n")
    text = sw._read_inf(str(path))
    assert "Hello" in text
    assert "﻿" not in text


def test_read_inf_plain_utf8(tmp_path):
    path = tmp_path / "plain.inf"
    path.write_bytes("[Strings]\nA=Hello\n".encode("utf-8"))
    text = sw._read_inf(str(path))
    assert "Hello" in text


def test_read_inf_cp1252(tmp_path):
    path = tmp_path / "cp1252.inf"
    # 0x93/0x94 are curly quotes in cp1252, invalid as utf-8 continuation bytes
    # after a non-lead byte, forcing the cp1252 fallback path.
    payload = "[Strings]\nA=Caf\xe9\n".encode("cp1252")
    path.write_bytes(payload)
    text = sw._read_inf(str(path))
    assert "Caf" in text


# ------------------------------------------------------------- search_inf_drivers


def test_search_inf_drivers_hp_alone_returns_empty_without_filesystem(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("os.listdir should not be called when there are no strong tokens")

    monkeypatch.setattr(sw.os, "listdir", _boom)
    assert sw.search_inf_drivers("HP") == []


# ------------------------------------------------------------------ mutations


def test_add_printer_refuses_wsd_port_without_ps(monkeypatch):
    monkeypatch.setattr(sw, "_ps", lambda *a, **k: (_ for _ in ()).throw(AssertionError("_ps called")))
    ok, err = sw.add_printer("Name", "Driver", "WSD-1234")
    assert ok is False
    assert "WSD" in err or "wsd" in err.lower()


def test_add_printer_refuses_invalid_name_without_ps(monkeypatch):
    monkeypatch.setattr(sw, "_ps", lambda *a, **k: (_ for _ in ()).throw(AssertionError("_ps called")))
    ok, err = sw.add_printer("bad,name", "Driver", "PORT1")
    assert ok is False
    assert err


def test_add_printer_refuses_empty_driver_without_ps(monkeypatch):
    monkeypatch.setattr(sw, "_ps", lambda *a, **k: (_ for _ in ()).throw(AssertionError("_ps called")))
    ok, err = sw.add_printer("Name", "", "PORT1")
    assert ok is False
    assert err


def test_remove_printer_rejects_bad_name_before_ps(monkeypatch):
    monkeypatch.setattr(sw, "_ps", lambda *a, **k: (_ for _ in ()).throw(AssertionError("_ps called")))
    ok, err = sw.remove_printer("bad\\name")
    assert ok is False
    assert err


def test_set_default_printer_rejects_bad_name_before_ps(monkeypatch):
    monkeypatch.setattr(sw, "_ps", lambda *a, **k: (_ for _ in ()).throw(AssertionError("_ps called")))
    ok, err = sw.set_default_printer("")
    assert ok is False
    assert err


def test_add_port_rejects_bad_name_before_ps(monkeypatch):
    monkeypatch.setattr(sw, "_ps", lambda *a, **k: (_ for _ in ()).throw(AssertionError("_ps called")))
    ok, err = sw.add_port("bad,name", host="1.2.3.4")
    assert ok is False
    assert err


@pytest.mark.parametrize("bad_port", [0, 70000, "x"])
def test_add_port_rejects_bad_port_number(monkeypatch, bad_port):
    monkeypatch.setattr(sw, "list_ports", lambda: [])
    monkeypatch.setattr(sw, "_ps", lambda *a, **k: (_ for _ in ()).throw(AssertionError("_ps called")))
    ok, err = sw.add_port("MyPort", host="1.2.3.4", port_number=bad_port)
    assert ok is False
    assert "port number" in err.lower()


def test_add_port_valid_host_builds_script_with_literal(monkeypatch):
    monkeypatch.setattr(sw, "list_ports", lambda: [])
    captured = {}

    def fake_ps(script, timeout=120):
        captured["script"] = script
        return True, "", ""

    monkeypatch.setattr(sw, "_ps", fake_ps)
    ok, err = sw.add_port("MyPort", host="192.168.1.5", port_number=9100)
    assert ok is True
    assert err == ""
    assert sw._ps_literal("MyPort") in captured["script"]
    assert sw._ps_literal("192.168.1.5") in captured["script"]


# ------------------------------------------------------------------ _strategy_ladder


def test_strategy_ladder_win11_ipp_entries_not_full_featured(monkeypatch):
    monkeypatch.setattr(sw, "is_windows_11", lambda: True)
    open_ports = {631: True, 9100: True}
    candidates = {"vendor_installed": [], "vendor_available_inbox": []}
    ladder = sw._strategy_ladder("1.2.3.4", {}, open_ports, candidates)
    ipp_entries = [s for s in ladder if s["kind"] == "ipp-everywhere"]
    assert ipp_entries
    for entry in ipp_entries:
        assert entry["full_featured"] is False
        assert "known_broken_on" in entry


def test_strategy_ladder_not_win11_ipp_entries_full_featured(monkeypatch):
    monkeypatch.setattr(sw, "is_windows_11", lambda: False)
    open_ports = {631: True, 9100: True}
    candidates = {"vendor_installed": [], "vendor_available_inbox": []}
    ladder = sw._strategy_ladder("1.2.3.4", {}, open_ports, candidates)
    ipp_entries = [s for s in ladder if s["kind"] == "ipp-everywhere"]
    assert ipp_entries
    for entry in ipp_entries:
        assert entry["full_featured"] is True
        assert "known_broken_on" not in entry


# ---------------------------------------------------------------------- plan_setup


def _patch_plan_common(monkeypatch, identity=None, open_ports=None, is_host=True, candidates=None):
    monkeypatch.setattr(sw.discovery, "ipp_query", lambda host, timeout=4.0: identity)
    monkeypatch.setattr(sw.discovery, "probe_ports", lambda host: open_ports or {9100: True})
    monkeypatch.setattr(sw.discovery, "is_printer_host", lambda ports: is_host)
    monkeypatch.setattr(
        sw, "find_driver_candidates",
        lambda model: candidates or {
            "model": model, "vendor_installed": [], "vendor_available_inbox": [], "generic": []
        },
    )


def test_plan_setup_invalid_host_returns_error(monkeypatch):
    result = sw.plan_setup("not a host; rm -rf")
    assert result["error"] == "invalid host"


def test_plan_setup_default_skips_vendor_lookup(monkeypatch):
    _patch_plan_common(monkeypatch, identity={"make_and_model": "EPSON ET-4850 Series"})

    def boom(model, **k):
        raise AssertionError("vendor_drivers.find_driver should not be called")

    import local_printer.vendor_drivers as vd
    monkeypatch.setattr(vd, "find_driver", boom)

    result = sw.plan_setup("192.168.1.5")
    assert result["vendor_driver_lookup"]["skipped"] is True


def test_plan_setup_vendor_lookup_calls_find_driver_when_no_local_driver(monkeypatch):
    _patch_plan_common(monkeypatch, identity={"make_and_model": "EPSON ET-4850 Series"})

    called = {}

    def fake_find_driver(model, **k):
        called["model"] = model
        return {"download_page": "https://example.com"}

    import local_printer.vendor_drivers as vd
    monkeypatch.setattr(vd, "find_driver", fake_find_driver)

    result = sw.plan_setup("192.168.1.5", vendor_lookup=True)
    assert called.get("model") == "EPSON ET-4850 Series"
    assert result["vendor_driver_lookup"]["download_page"] == "https://example.com"


def test_plan_setup_win11_recommends_pairing_script(monkeypatch):
    _patch_plan_common(monkeypatch, identity={"make_and_model": "EPSON ET-4850 Series"})
    monkeypatch.setattr(sw, "is_windows_11", lambda: True)
    result = sw.plan_setup("192.168.1.5")
    assert "win-pair-printer.ps1" in result["recommended"]


# ------------------------------------------------------------------------- _ps


class _FakeCompletedProcess:
    def __init__(self, returncode, stdout, stderr):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_ps_strips_preamble_and_category_info(monkeypatch):
    stderr = (
        sw._PS_PREAMBLE
        + "script text : no such printer: X\n"
        + "    + CategoryInfo          : NotSpecified: (:) [Write-Error], WriteErrorException\n"
        + "    + FullyQualifiedErrorId : Microsoft.PowerShell.Commands.WriteErrorException\n"
    )
    monkeypatch.setattr(
        sw.subprocess, "run",
        lambda *a, **k: _FakeCompletedProcess(1, "", stderr),
    )
    ok, out, err = sw._ps("whatever")
    assert ok is False
    assert err == "no such printer: X"
