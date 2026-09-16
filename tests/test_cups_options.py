"""Unit tests for the option dict `local_printer.cups.print_file` hands to pycups.

Same recording-fake pattern as `test_cups_jobs.py`: a fake `cups` module is
injected into `sys.modules` and `local_printer.cups` is reloaded against it.
No real CUPS daemon or printer is touched - `printFile` only records its
arguments.

The regression under test: `LinuxPrintOptions` used to hand pycups its
snake_case field names (`print_color_mode`) and whatever value type the caller
passed. CUPS/IPP wants the hyphenated attribute names (`print-color-mode`) and
pycups raises TypeError on any non-string option value (an int `copies`, say).
"""

import importlib
import os
import sys
import types

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pytest

from models.model import LinuxPrintOptions


class FakeIPPError(Exception):
    pass


class FakeConnection:
    """Recording stand-in for cups.Connection() with one idle queue."""

    print_calls = []

    def __init__(self, *args, **kwargs):
        pass

    def getPrinters(self):
        return {
            "ET-4850": {
                "printer-state": 3,
                "printer-state-reasons": ["none"],
                "printer-is-accepting-jobs": True,
                "printer-make-and-model": "EPSON ET-4850 Series",
                "device-uri": "ipp://192.0.2.10/ipp/print",
            }
        }

    def getDefault(self):
        return "ET-4850"

    def getPrinterAttributes(self, name):
        return {"printer-state": 3, "printer-is-accepting-jobs": True}

    def printFile(self, printer, filename, title, options):
        # Mimic pycups' own argument check
        for key, value in options.items():
            if not isinstance(key, str) or not isinstance(value, str):
                raise TypeError(
                    f"option {key!r}: pycups requires string keys and values"
                )
        type(self).print_calls.append(
            {"printer": printer, "filename": filename, "title": title,
             "options": options}
        )
        return 42


@pytest.fixture
def cups_backend(monkeypatch):
    """Install the fake `cups` module, reload local_printer.cups, hand it back."""
    FakeConnection.print_calls = []
    fake_cups = types.SimpleNamespace(
        Connection=FakeConnection,
        IPPError=FakeIPPError,
    )
    monkeypatch.setitem(sys.modules, "cups", fake_cups)

    import local_printer.cups as cups_module

    module = importlib.reload(cups_module)
    module._fake = FakeConnection
    yield module

    sys.modules.pop("local_printer.cups", None)


@pytest.fixture
def pdf_path(tmp_path):
    path = tmp_path / "doc.pdf"
    path.write_bytes(b"%PDF-1.4\n%%EOF\n")
    return str(path)


def test_print_file_hands_pycups_hyphenated_string_options(cups_backend, pdf_path):
    options = LinuxPrintOptions.from_dict(
        {"print_color_mode": "monochrome", "copies": 2, "media": "A4",
         "number-up": 2, "fit_to_page": True}
    )

    result = cups_backend.print_file(None, pdf_path, options)

    assert result["code"] == 200, result
    assert result["data"]["job_id"] == 42
    assert len(cups_backend._fake.print_calls) == 1
    call = cups_backend._fake.print_calls[0]
    assert call["printer"] == "ET-4850"
    assert call["filename"] == pdf_path
    assert call["options"] == {
        "print-color-mode": "monochrome",
        "copies": "2",
        "media": "A4",
        "number-up": "2",
        "fit-to-page": "true",
    }
    for key, value in call["options"].items():
        assert "_" not in key, f"{key!r} is not an IPP attribute name"
        assert isinstance(value, str), f"{key}={value!r} is not a str"


def test_print_file_without_options_sends_empty_dict(cups_backend, pdf_path):
    result = cups_backend.print_file(None, pdf_path)
    assert result["code"] == 200, result
    assert cups_backend._fake.print_calls[0]["options"] == {}


def test_print_file_raw_flag_is_a_string_option_too(cups_backend, pdf_path):
    options = LinuxPrintOptions.from_dict({"copies": 1})
    result = cups_backend.print_file(None, pdf_path, options, raw=True)
    assert result["code"] == 200, result
    assert cups_backend._fake.print_calls[0]["options"] == {
        "copies": "1",
        "raw": "true",
    }
