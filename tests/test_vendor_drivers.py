"""Unit tests for local_printer.vendor_drivers: no real network, deterministic."""

import hashlib
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from local_printer import vendor_drivers


# --------------------------------------------------------------- safe_filename


class TestSafeFilename:
    def test_unchanged_normal_name(self):
        assert vendor_drivers.safe_filename("ET4850_STD_WW_38005_W64.exe") == "ET4850_STD_WW_38005_W64.exe"

    def test_path_traversal_stripped(self):
        assert vendor_drivers.safe_filename("../../evil.exe") == "evil.exe"

    def test_special_chars_replaced(self):
        assert vendor_drivers.safe_filename("a b?c.exe") == "a_b_c.exe"

    def test_reserved_device_name_with_extension(self):
        assert vendor_drivers.safe_filename("CON.exe") == "driver_CON.exe"

    def test_reserved_device_name_bare(self):
        assert vendor_drivers.safe_filename("com1") == "driver_com1"

    def test_empty_falls_back(self):
        assert vendor_drivers.safe_filename("") == "driver.bin"

    def test_dots_only_falls_back(self):
        assert vendor_drivers.safe_filename("..") == "driver.bin"

    def test_long_name_capped(self):
        name = "a" * 300 + ".exe"
        result = vendor_drivers.safe_filename(name)
        assert len(result) == 150
        assert result == name[:150]

    def test_backslashes_are_separators(self):
        assert vendor_drivers.safe_filename("folder\\sub\\driver.exe") == "driver.exe"


# --------------------------------------------------------------- _region_from_tag


class TestRegionFromTag:
    def test_underscore_dot_encoding(self):
        assert vendor_drivers._region_from_tag("de_DE.UTF-8") == "DE"

    def test_dash_at_modifier(self):
        assert vendor_drivers._region_from_tag("de-CH@euro") == "CH"

    def test_posix_c_locale(self):
        assert vendor_drivers._region_from_tag("C") is None

    def test_bare_language_without_allow_bare(self):
        assert vendor_drivers._region_from_tag("de") is None

    def test_bare_region_with_allow_bare(self):
        assert vendor_drivers._region_from_tag("DE", allow_bare=True) == "DE"

    def test_en_us(self):
        assert vendor_drivers._region_from_tag("en_US") == "US"


# --------------------------------------------------------------- detect_region


class TestDetectRegion:
    def _clear_env(self, monkeypatch):
        for key in ("PRINTER_AI_REGION", "LC_ALL", "LANG"):
            monkeypatch.delenv(key, raising=False)

    def test_printer_ai_region_bare(self, monkeypatch):
        self._clear_env(monkeypatch)
        monkeypatch.setenv("PRINTER_AI_REGION", "de")
        assert vendor_drivers.detect_region() == "DE"

    def test_lang_only(self, monkeypatch):
        self._clear_env(monkeypatch)
        monkeypatch.setenv("LANG", "fr_FR.UTF-8")
        assert vendor_drivers.detect_region() == "FR"

    def test_locale_getlocale_fallback(self, monkeypatch):
        self._clear_env(monkeypatch)
        monkeypatch.setattr(vendor_drivers.locale, "getlocale", lambda *a, **k: ("de_DE", "UTF-8"))
        assert vendor_drivers.detect_region() == "DE"

    def test_default_when_everything_empty(self, monkeypatch):
        self._clear_env(monkeypatch)
        monkeypatch.setattr(vendor_drivers.locale, "getlocale", lambda *a, **k: (None, None))
        monkeypatch.setattr(vendor_drivers, "_windows_user_locale", lambda: None)
        assert vendor_drivers.detect_region() == "US"


# --------------------------------------------------------------- split_model


class TestSplitModel:
    def test_epson(self):
        result = vendor_drivers.split_model("EPSON ET-4850 Series")
        assert result == {
            "vendor": "Epson",
            "device_id": "ET-4850 Series",
            "model": "EPSON ET-4850 Series",
        }

    def test_hp(self):
        result = vendor_drivers.split_model("HP OfficeJet Pro 7740")
        assert result == {
            "vendor": "HP",
            "device_id": "OfficeJet Pro 7740",
            "model": "HP OfficeJet Pro 7740",
        }

    def test_unknown_vendor(self):
        text = "Generic Widget Model Z1"
        result = vendor_drivers.split_model(text)
        assert result == {"vendor": "", "device_id": text, "model": text}


# --------------------------------------------------------------- epson_page_url


class TestEpsonPageUrl:
    def test_contains_expected_params(self):
        url = vendor_drivers.epson_page_url("ET-4850 Series", "DE", "WIN1164", "de")
        assert url.startswith(vendor_drivers.EPSON_PAGE)
        assert " " not in url  # url-encoded
        parsed = urllib.parse.urlparse(url)
        qs = urllib.parse.parse_qs(parsed.query)
        assert qs["device_id"] == ["ET-4850 Series"]
        assert qs["region"] == ["DE"]
        assert qs["os"] == ["WIN1164"]
        assert qs["language"] == ["de"]


# --------------------------------------------------------------- open_in_browser


class TestOpenInBrowser:
    def test_refuses_non_https(self, monkeypatch):
        import webbrowser

        def boom(url):
            raise AssertionError("webbrowser.open should not be called for a non-https URL")

        monkeypatch.setattr(webbrowser, "open", boom)
        result = vendor_drivers.open_in_browser("http://x")
        assert result["ok"] is False
        assert "https" in result["error"].lower()

    def test_opens_https(self, monkeypatch):
        import webbrowser

        calls = []
        monkeypatch.setattr(webbrowser, "open", lambda url: calls.append(url) or True)
        result = vendor_drivers.open_in_browser("https://example.com")
        assert calls == ["https://example.com"]
        assert result["ok"] is True
        assert result["url"] == "https://example.com"


# --------------------------------------------------------------- download_driver


class _FakeUrlopenResponse:
    def __init__(self, data: bytes, declared_length: int = None):
        self._data = data
        self._pos = 0
        length = declared_length if declared_length is not None else len(data)
        self.headers = {"Content-Length": str(length)}

    def read(self, n):
        chunk = self._data[self._pos : self._pos + n]
        self._pos += len(chunk)
        return chunk

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class TestDownloadDriver:
    def test_refuses_non_https(self, tmp_path):
        result = vendor_drivers.download_driver("http://example.com/driver.exe", str(tmp_path))
        assert result["ok"] is False
        assert "https" in result["error"].lower()

    def test_refuses_when_target_exists(self, tmp_path):
        (tmp_path / "driver.exe").write_bytes(b"already here")
        result = vendor_drivers.download_driver("https://cdn.example.com/driver.exe", str(tmp_path))
        assert result["ok"] is False
        assert "target exists" in result["error"]

    def test_success_path(self, monkeypatch, tmp_path):
        data = b"MZ" + b"\x90" * 100
        expected_sha = hashlib.sha256(data).hexdigest()

        def fake_urlopen(request, timeout=None):
            return _FakeUrlopenResponse(data)

        monkeypatch.setattr(vendor_drivers.urllib.request, "urlopen", fake_urlopen)
        monkeypatch.setattr(
            vendor_drivers, "_authenticode_signature", lambda path: {"status": "Valid", "signer": "CN=Test"}
        )

        url = "https://cdn.example.com/ET4850_STD_WW_38005_W64.exe"
        result = vendor_drivers.download_driver(url, str(tmp_path))

        assert result["ok"] is True
        assert result["filename"] == "ET4850_STD_WW_38005_W64.exe"
        assert result["path"] == os.path.join(os.path.abspath(str(tmp_path)), "ET4850_STD_WW_38005_W64.exe")
        assert result["size_bytes"] == len(data)
        assert result["sha256"] == expected_sha
        assert result["verification"] == vendor_drivers.VERIFICATION_NOTE
        assert result["signature"] == {"status": "Valid", "signer": "CN=Test"}
        assert os.path.exists(result["path"])

    def test_truncated_download_leaves_no_files(self, monkeypatch, tmp_path):
        data = b"MZ" + b"\x90" * 50

        def fake_urlopen(request, timeout=None):
            # Server declared more bytes than it actually sends.
            return _FakeUrlopenResponse(data, declared_length=len(data) + 500)

        monkeypatch.setattr(vendor_drivers.urllib.request, "urlopen", fake_urlopen)

        url = "https://cdn.example.com/driver.exe"
        result = vendor_drivers.download_driver(url, str(tmp_path))

        assert result["ok"] is False
        assert "truncated" in result["error"]
        target = tmp_path / "driver.exe"
        assert not target.exists()
        assert not (tmp_path / "driver.exe.part").exists()

    def test_http_403_reports_blocked(self, monkeypatch, tmp_path):
        url = "https://cdn.example.com/driver.exe"

        def fake_urlopen(request, timeout=None):
            raise urllib.error.HTTPError(url, 403, "Forbidden", {}, None)

        monkeypatch.setattr(vendor_drivers.urllib.request, "urlopen", fake_urlopen)
        result = vendor_drivers.download_driver(url, str(tmp_path))

        assert result["ok"] is False
        assert result["blocked"] is True
        assert result["http_status"] == 403
        assert result["open_in_browser_url"] == url

    def test_exe_without_mz_header_fails(self, monkeypatch, tmp_path):
        data = b"NOT_AN_EXE_HEADER" + b"\x00" * 20

        def fake_urlopen(request, timeout=None):
            return _FakeUrlopenResponse(data)

        monkeypatch.setattr(vendor_drivers.urllib.request, "urlopen", fake_urlopen)
        url = "https://cdn.example.com/driver.exe"
        result = vendor_drivers.download_driver(url, str(tmp_path))

        assert result["ok"] is False
        assert "MZ" in result["error"] or "executable" in result["error"].lower()


# --------------------------------------------------------------- _get_json


class _FakeJsonResponse:
    def __init__(self, payload: bytes):
        self._payload = payload

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class TestGetJson:
    def test_default_uses_honest_ua_no_referer(self, monkeypatch):
        captured = {}

        def fake_urlopen(request, timeout=None):
            captured["request"] = request
            return _FakeJsonResponse(b'{"ok": true}')

        monkeypatch.setattr(vendor_drivers.urllib.request, "urlopen", fake_urlopen)
        result = vendor_drivers._get_json("https://example.com/api")

        assert result == {"ok": True}
        req = captured["request"]
        assert req.get_header("User-agent") == vendor_drivers.HONEST_UA
        assert req.get_header("Referer") is None

    def test_waf_bypass_uses_browser_ua_and_referer(self, monkeypatch):
        captured = {}

        def fake_urlopen(request, timeout=None):
            captured["request"] = request
            return _FakeJsonResponse(b'{"ok": true}')

        monkeypatch.setattr(vendor_drivers.urllib.request, "urlopen", fake_urlopen)
        result = vendor_drivers._get_json("https://example.com/api", waf_bypass=True)

        assert result == {"ok": True}
        req = captured["request"]
        assert req.get_header("User-agent") == vendor_drivers.BROWSER_UA
        assert req.get_header("Referer") == vendor_drivers.EPSON_PAGE
