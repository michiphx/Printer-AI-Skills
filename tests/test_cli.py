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
    assert "convert" in out
    assert "formats" in out
    assert out.isascii()


def test_print_help_documents_the_conversion_flags(cli):
    code, out, err = cli("print", "--help")
    assert code == 0
    assert "--raw" in out
    assert "--keep-pdf" in out


# ==================== convert / formats ====================
#
# Neither command needs a printer backend, so these run everywhere.


def test_formats_lists_converters(cli):
    code, out, err = cli("formats")
    assert code == 0
    assert "passthrough" in out
    assert "office" in out
    assert out.isascii()


def test_formats_json(cli):
    code, out, err = cli("formats", "--json")
    assert code == 0
    result = parse_json_stdout(out)
    assert result["code"] == 200
    converters = result["data"]["converters"]
    assert {"passthrough", "text", "image"} <= {c["converter"] for c in converters}
    passthrough = next(c for c in converters if c["converter"] == "passthrough")
    assert passthrough["available"] is True


def test_convert_text_file_prints_the_pdf_path(cli, tmp_path):
    source = tmp_path / "notes.txt"
    source.write_text("hello from the CLI\n")

    code, out, err = cli("convert", str(source))
    assert code == 0

    pdf_path = out.strip()
    assert pdf_path.endswith(".pdf")
    with open(pdf_path, "rb") as handle:
        assert handle.read(5) == b"%PDF-"


def test_convert_json_shape(cli, tmp_path):
    source = tmp_path / "notes.txt"
    source.write_text("hello\n")

    code, out, err = cli("convert", str(source), "--json")
    assert code == 0
    data = parse_json_stdout(out)["data"]
    assert data["converter"] == "text"
    assert data["source"] == str(source)
    assert isinstance(data["notes"], list)


def test_convert_missing_file_is_404(cli, tmp_path):
    code, out, err = cli("convert", str(tmp_path / "nope.txt"), "--json")
    assert code == 1
    assert parse_json_stdout(out)["code"] == 404


def test_convert_unsupported_file_is_415_with_hint(cli, tmp_path):
    blob = tmp_path / "blob.bin"
    blob.write_bytes(b"\x00\x01\xff\xfe" * 64)

    code, out, err = cli("convert", str(blob))
    assert code == 1
    result = parse_json_stdout(out)
    assert result["code"] == 415
    assert result["data"]["hint"]


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
def test_print_empty_docx_is_422(cli, tmp_path):
    docx = tmp_path / "empty.docx"
    docx.write_bytes(b"")
    code, out, err = cli("print", str(docx))
    assert code == 1
    result = parse_json_stdout(out)
    assert result["code"] == 422


# ---- print failure paths need no printer backend and always answer in JSON


def test_print_missing_file_is_json_404(cli, tmp_path):
    missing = tmp_path / "missing.pdf"
    code, out, err = cli("print", str(missing))
    assert code == 1
    result = parse_json_stdout(out)
    assert result["code"] == 404
    assert result["data"]["file_path"] == str(missing)
    assert "Traceback" not in err


@pytest.mark.parametrize("raw", ["[1]", '"x"', "42"])
def test_print_options_must_be_a_json_object(cli, tmp_path, raw):
    source = tmp_path / "notes.txt"
    source.write_text("hello\n")
    code, out, err = cli("print", str(source), "--options", raw)
    assert code == 1
    result = parse_json_stdout(out)
    assert result["code"] == 400
    assert "JSON object" in result["msg"]
    assert "Traceback" not in err


def test_print_options_invalid_json_is_400(cli, tmp_path):
    source = tmp_path / "notes.txt"
    source.write_text("hello\n")
    code, out, err = cli("print", str(source), "--options", "{oops")
    assert code == 1
    assert parse_json_stdout(out)["code"] == 400


def test_print_empty_file_is_json_422_or_501(cli, tmp_path):
    """An empty file is the file's fault (422); without a backend it is 501.

    Either way the answer is JSON, never a traceback.
    """
    empty = tmp_path / "empty.docx"
    empty.write_bytes(b"")
    code, out, err = cli("print", str(empty))
    assert code == 1
    assert parse_json_stdout(out)["code"] in (422, 501)
    assert "Traceback" not in err


# ---- convert: overwrite protection and native copies


def test_convert_refuses_to_overwrite_without_flag(cli, tmp_path):
    source = tmp_path / "notes.txt"
    source.write_text("hello\n")
    out_dir = tmp_path / "pdfs"
    out_dir.mkdir()
    existing = out_dir / "notes.pdf"
    existing.write_bytes(b"precious")

    code, out, err = cli("convert", str(source), "--out", str(out_dir), "--json")
    assert code == 1
    result = parse_json_stdout(out)
    assert result["code"] == 409
    assert result["data"]["path"] == str(existing)
    assert existing.read_bytes() == b"precious"

    code, out, err = cli("convert", str(source), "--out", str(out_dir), "--overwrite", "--json")
    assert code == 0
    assert parse_json_stdout(out)["data"]["pdf_path"] == str(existing)
    assert existing.read_bytes().startswith(b"%PDF-")


def test_convert_native_pdf_into_directory_creates_a_copy(cli, tmp_path):
    source = tmp_path / "some.pdf"
    source.write_bytes(b"%PDF-1.4\n%native\n")
    out_dir = tmp_path / "somedir"

    code, out, err = cli("convert", str(source), "--out", str(out_dir) + "/", "--json")
    assert code == 0
    data = parse_json_stdout(out)["data"]
    copied = out_dir / "some.pdf"
    assert data["pdf_path"] == str(copied)
    assert copied.read_bytes() == source.read_bytes()
    assert source.exists()


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
