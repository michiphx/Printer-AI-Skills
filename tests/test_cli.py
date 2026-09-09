"""CLI-level (subprocess/black-box) tests for `printer-ai` / main.py.

These drive `main.py` exactly the way a human or an AI caller would: as a
subprocess, asserting on exit codes and on stdout/stderr text. Tests that need
a real printer backend are skipped on machines without one (see
`has_windows_backend` in conftest.py); everything else (argument validation,
network-scan guardrails, help text) runs everywhere.
"""

import sys

import pytest

from tests.conftest import parse_json_stdout, has_windows_backend


def test_help_exits_zero_and_lists_commands(cli):
    code, out, err = cli("--help")
    assert code == 0
    assert "printers" in out
    assert "setup" in out
    assert "driver-search" in out
    assert out.isascii()


@has_windows_backend
def test_printers_json_shape(cli):
    code, out, err = cli("printers", "--json")
    assert code == 0
    result = parse_json_stdout(out)
    assert result["code"] == 200
    printers = result["data"]["printers"]
    for p in printers:
        assert "index" in p
        assert "name" in p
        assert "status" in p
        assert "is_default" in p


@has_windows_backend
def test_status_default_mentions_default_printer_name(cli):
    code, out, err = cli("printers", "--json")
    printers = parse_json_stdout(out)["data"]["printers"]
    if not printers:
        pytest.skip("no printers installed on this machine")

    code, out, err = cli("status")
    assert code == 0
    default_printer = next((p for p in printers if p.get("is_default")), printers[0])
    assert default_printer["name"] in out


@has_windows_backend
def test_status_unknown_index_is_404(cli):
    code, out, err = cli("status", "9999")
    assert code == 1
    combined = out + err
    assert "404" in combined
    assert "not found" in combined.lower()


@has_windows_backend
def test_print_empty_docx_is_415(cli, tmp_path):
    docx = tmp_path / "empty.docx"
    docx.write_bytes(b"")
    code, out, err = cli("print", str(docx))
    assert code == 1
    result = parse_json_stdout(out)
    assert result["code"] == 415


@has_windows_backend
def test_print_missing_pdf_is_404(cli, tmp_path):
    missing = tmp_path / "missing.pdf"
    code, out, err = cli("print", str(missing))
    assert code == 1
    combined = out + err
    assert "404" in combined


def test_discover_refuses_non_local_non_private_subnet(cli):
    code, out, err = cli("discover", "--subnet", "8.8.8")
    assert code == 1
    assert "refusing" in (out + err).lower()


def test_discover_rejects_invalid_subnet(cli):
    code, out, err = cli("discover", "--subnet", "999.1")
    assert code == 1
    assert "invalid subnet" in (out + err).lower()


def test_remove_without_yes_is_refused(cli):
    code, out, err = cli("remove", "Nope")
    assert code == 1
    assert "without --yes" in (out + err)


def test_remove_comma_in_name_is_rejected(cli):
    code, out, err = cli("remove", "a,b", "--yes")
    assert code == 1
    assert "','" in (out + err)


def test_set_default_unknown_printer_fails(cli):
    code, out, err = cli("set-default", "Definitely-Not-A-Printer-xyz")
    assert code == 1


def test_setup_invalid_host_dry_run(cli):
    code, out, err = cli("setup", "x; rm", "--dry-run")
    assert code == 1
    assert "invalid host" in (out + err).lower()


def test_probe_unreachable_host(cli):
    code, out, err = cli("probe", "192.0.2.1", "--timeout", "0.3")
    assert code == 0
    result = parse_json_stdout(out)
    assert result["data"]["reachable"] is False


@pytest.mark.parametrize(
    "args",
    [
        ("discover", "--subnet", "8.8.8", "--json"),
        ("discover", "--subnet", "999.1", "--json"),
        ("remove", "Nope"),  # always JSON, --yes missing
        ("remove", "a,b", "--yes"),  # always JSON, invalid name
        ("set-default", "Definitely-Not-A-Printer-xyz"),  # always JSON
        ("setup", "x; rm", "--dry-run"),  # always JSON
    ],
)
def test_json_failures_are_valid_json_with_non_200_code(cli, args):
    code, out, err = cli(*args)
    assert code == 1
    result = parse_json_stdout(out)
    assert isinstance(result.get("code"), int)
    assert result["code"] != 200
