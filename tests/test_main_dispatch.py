"""In-process unit tests for main.py's dispatch helpers.

Unlike test_cli.py (which drives the CLI through subprocess), these tests
import `main` directly to exercise `finish`, `_backend`, `_run_net_command`
and `build_parser` without paying for a new interpreter per case.

The `print` / `convert` / `formats` commands are exercised here too, with the
platform backend replaced by a fake: the point is what main.py does around the
backend (convert first, clean up the temporary PDF, report the right fields),
not whether this machine happens to own a printer.
"""

import argparse
import json
import os

import pytest

import main


class TestFinish:
    def test_success_json_exits_zero_and_prints_json(self, capsys):
        with pytest.raises(SystemExit) as exc_info:
            main.finish({"code": 200}, True)
        assert exc_info.value.code == 0
        out = capsys.readouterr().out
        assert '"code": 200' in out

    def test_failure_text_exits_one_and_writes_stderr(self, capsys):
        with pytest.raises(SystemExit) as exc_info:
            main.finish({"code": 404, "msg": "x"}, False)
        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert captured.err.strip() == "Error 404: x"
        # human mode prints nothing extra to stdout for a failure
        assert captured.out == ""

    def test_failure_json_exits_one_and_prints_full_result(self, capsys):
        with pytest.raises(SystemExit) as exc_info:
            main.finish({"code": 500, "msg": "boom"}, True)
        assert exc_info.value.code == 1
        out = capsys.readouterr().out
        assert '"code": 500' in out
        assert '"boom"' in out

    def test_success_human_mode_prints_nothing(self, capsys):
        with pytest.raises(SystemExit) as exc_info:
            main.finish({"code": 200}, False)
        assert exc_info.value.code == 0
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == ""


class TestBackend:
    def test_unsupported_platform_returns_501(self, monkeypatch):
        monkeypatch.setattr(main, "platform", "sunos")
        monkeypatch.setattr(main, "_BACKEND_CACHE", None)

        backend, error = main._backend()

        assert backend is None
        assert isinstance(error, dict)
        assert error["code"] == 501
        assert "sunos" in error["msg"]

    def test_result_is_cached(self, monkeypatch):
        monkeypatch.setattr(main, "platform", "sunos")
        monkeypatch.setattr(main, "_BACKEND_CACHE", None)

        first = main._backend()
        # Change the platform again; the cached result must still be returned.
        monkeypatch.setattr(main, "platform", "plan9")
        second = main._backend()

        assert first == second


class TestRunNetCommand:
    def test_uses_fallback_when_handler_missing(self, monkeypatch):
        from local_printer import commands_net

        monkeypatch.delattr(commands_net, "cmd_totally_made_up", raising=False)

        calls = []

        def fallback(args):
            calls.append(args)
            return "fallback-result"

        args = argparse.Namespace(json=False)
        result = main._run_net_command("totally_made_up", args, fallback)

        assert result == "fallback-result"
        assert calls == [args]

    def test_forwards_to_handler_when_present(self, monkeypatch, capsys):
        from local_printer import commands_net

        def fake_handler(args):
            return {"code": 200, "msg": "ok", "data": {}}

        monkeypatch.setattr(commands_net, "cmd_fake_thing", fake_handler, raising=False)

        def fallback(args):
            pytest.fail("fallback should not be called when a handler exists")

        args = argparse.Namespace(json=True)
        with pytest.raises(SystemExit) as exc_info:
            main._run_net_command("fake_thing", args, fallback)

        assert exc_info.value.code == 0
        out = capsys.readouterr().out
        assert '"code": 200' in out

    def test_handler_non_dict_result_exits_zero(self, monkeypatch):
        from local_printer import commands_net

        def fake_handler(args):
            # Handlers that already called sys.exit/finish themselves return None.
            return None

        monkeypatch.setattr(commands_net, "cmd_already_exited", fake_handler, raising=False)

        def fallback(args):
            pytest.fail("fallback should not be called when a handler exists")

        args = argparse.Namespace(json=False)
        with pytest.raises(SystemExit) as exc_info:
            main._run_net_command("already_exited", args, fallback)
        assert exc_info.value.code == 0


class TestBuildParser:
    def test_setup_vendor_lookup_and_dry_run(self):
        parser = main.build_parser()
        args = parser.parse_args(["setup", "1.2.3.4", "--vendor-lookup", "--dry-run"])
        assert args.host == "1.2.3.4"
        assert args.vendor_lookup is True
        assert args.dry_run is True
        assert args.no_generic is False
        assert args.json is False

    def test_discover_force(self):
        parser = main.build_parser()
        args = parser.parse_args(["discover", "--force"])
        assert args.force is True
        assert args.subnet is None

    def test_no_command_has_no_func(self):
        parser = main.build_parser()
        args = parser.parse_args([])
        assert args.command is None

    def test_print_defaults(self):
        parser = main.build_parser()
        args = parser.parse_args(["print", "file.pdf"])
        assert args.file_path == "file.pdf"
        assert args.index is None
        assert args.options is None
        assert args.raw is False
        assert args.keep_pdf is False

    def test_print_conversion_flags(self):
        parser = main.build_parser()
        args = parser.parse_args(["print", "a.docx", "--raw", "--keep-pdf"])
        assert args.raw is True
        assert args.keep_pdf is True

    def test_convert_defaults(self):
        parser = main.build_parser()
        args = parser.parse_args(["convert", "a.md"])
        assert args.file_path == "a.md"
        assert args.out is None
        assert args.json is False

    def test_convert_out_and_json(self):
        parser = main.build_parser()
        args = parser.parse_args(["convert", "a.md", "--out", "/tmp/x.pdf", "--json"])
        assert args.out == "/tmp/x.pdf"
        assert args.json is True

    def test_formats_json_flag(self):
        parser = main.build_parser()
        args = parser.parse_args(["formats", "--json"])
        assert args.json is True
        assert args.func is main.cmd_formats


# ==================== print / convert / formats ====================


class FakeBackend:
    """Stands in for the platform backend: records the call, always succeeds."""

    PrintOptions = None

    def __init__(self):
        self.calls = []

    def print_file(self, index=None, file_path="", options=None, raw=False):
        self.calls.append(
            {"index": index, "file_path": file_path, "options": options, "raw": raw}
        )
        # Real backends read the file before returning; prove it is still here.
        assert os.path.isfile(file_path), f"backend got a missing file: {file_path}"
        return {
            "code": 200,
            "msg": "success",
            "data": {
                "job_id": 42,
                "printer_name": "Fake-Printer",
                "file_path": file_path,
            },
        }


@pytest.fixture
def fake_backend(monkeypatch):
    backend = FakeBackend()
    monkeypatch.setattr(main, "_backend", lambda: (backend, None))
    return backend


def run_cli(argv, expect_code=0):
    """Parse argv and run the handler, returning the SystemExit code."""
    args = main.build_parser().parse_args(argv)
    with pytest.raises(SystemExit) as exc_info:
        args.func(args)
    assert exc_info.value.code == expect_code
    return args


class TestCmdPrint:
    def test_text_file_is_converted_before_the_backend_sees_it(
        self, fake_backend, tmp_path, capsys
    ):
        source = tmp_path / "notes.txt"
        source.write_text("hello\n")

        run_cli(["print", str(source)])

        call = fake_backend.calls[0]
        assert call["file_path"].endswith(".pdf")
        assert call["file_path"] != str(source)
        assert call["raw"] is False

        out = capsys.readouterr().out
        assert "Print job submitted" in out
        assert "converted from" in out
        # the temporary PDF is gone once the backend is done with it
        assert not os.path.exists(call["file_path"])

    def test_pdf_is_passed_through_untouched(self, fake_backend, tmp_path):
        source = tmp_path / "doc.pdf"
        source.write_bytes(b"%PDF-1.4\n%stuff\n")

        run_cli(["print", str(source)])

        assert fake_backend.calls[0]["file_path"] == str(source)
        assert source.exists()

    def test_raw_skips_conversion(self, fake_backend, tmp_path, capsys):
        source = tmp_path / "job.prn"
        source.write_bytes(b"\x1b%-12345X@PJL\n")

        run_cli(["print", str(source), "--raw"])

        call = fake_backend.calls[0]
        assert call["file_path"] == str(source)
        assert call["raw"] is True
        assert "raw (sent unconverted)" in capsys.readouterr().out

    def test_raw_sends_even_an_unconvertible_file(self, fake_backend, tmp_path):
        source = tmp_path / "blob.bin"
        source.write_bytes(b"\x00\x01\xff\xfe" * 32)

        run_cli(["print", str(source), "--raw"])

        assert fake_backend.calls[0]["file_path"] == str(source)

    def test_keep_pdf_reports_and_keeps_the_file(self, fake_backend, tmp_path, capsys):
        source = tmp_path / "notes.txt"
        source.write_text("hello\n")

        run_cli(["print", str(source), "--keep-pdf"])

        out = capsys.readouterr().out
        assert "pdf kept at:" in out
        kept = fake_backend.calls[0]["file_path"]
        assert os.path.isfile(kept)

    def test_unconvertible_file_is_415_with_a_hint(self, fake_backend, tmp_path, capsys):
        source = tmp_path / "blob.bin"
        source.write_bytes(b"\x00\x01\xff\xfe" * 32)

        run_cli(["print", str(source)], expect_code=1)

        result = json.loads(capsys.readouterr().out)
        assert result["code"] == 415
        assert result["data"]["hint"]
        assert fake_backend.calls == []

    def test_missing_office_tool_is_415(self, fake_backend, tmp_path, capsys, monkeypatch):
        from local_printer import convert

        monkeypatch.setattr(convert, "find_soffice", lambda: None)
        monkeypatch.setattr(convert, "_msoffice_com_available", lambda: False)

        import zipfile

        source = tmp_path / "report.docx"
        with zipfile.ZipFile(source, "w") as archive:
            archive.writestr("[Content_Types].xml", "<Types/>")

        run_cli(["print", str(source)], expect_code=1)

        result = json.loads(capsys.readouterr().out)
        assert result["code"] == 415
        assert "libreoffice.org" in result["data"]["hint"].lower()

    def test_missing_file_is_404(self, fake_backend, tmp_path, capsys):
        run_cli(["print", str(tmp_path / "nope.txt")], expect_code=1)
        assert "404" in capsys.readouterr().err


class TestCmdConvert:
    def test_writes_next_to_the_source_by_default(self, tmp_path, capsys):
        source = tmp_path / "notes.txt"
        source.write_text("hello\n")

        run_cli(["convert", str(source)])

        printed = capsys.readouterr().out.strip()
        assert printed == str(tmp_path / "notes.pdf")
        assert os.path.isfile(printed)

    def test_json_shape(self, tmp_path, capsys):
        source = tmp_path / "notes.txt"
        source.write_text("hello\n")

        run_cli(["convert", str(source), "--json"])

        result = json.loads(capsys.readouterr().out)
        assert result["code"] == 200
        data = result["data"]
        assert set(("pdf_path", "source", "converter", "notes")) <= set(data)
        assert data["converter"] == "text"
        assert os.path.isfile(data["pdf_path"])

    def test_out_names_a_file(self, tmp_path, capsys):
        source = tmp_path / "notes.txt"
        source.write_text("hello\n")
        target = tmp_path / "out" / "custom.pdf"

        run_cli(["convert", str(source), "--out", str(target), "--json"])

        data = json.loads(capsys.readouterr().out)["data"]
        assert data["pdf_path"] == str(target)
        assert os.path.isfile(target)

    def test_out_names_a_directory(self, tmp_path, capsys):
        source = tmp_path / "notes.txt"
        source.write_text("hello\n")
        target_dir = tmp_path / "pdfs"

        run_cli(["convert", str(source), "--out", str(target_dir), "--json"])

        data = json.loads(capsys.readouterr().out)["data"]
        assert data["pdf_path"] == str(target_dir / "notes.pdf")

    def test_native_pdf_is_copied_not_moved(self, tmp_path, capsys):
        source = tmp_path / "doc.pdf"
        source.write_bytes(b"%PDF-1.4\n")
        target = tmp_path / "copy.pdf"

        run_cli(["convert", str(source), "--out", str(target), "--json"])

        assert source.exists()
        assert target.exists()

    def test_missing_file_is_404(self, tmp_path, capsys):
        run_cli(["convert", str(tmp_path / "nope.txt"), "--json"], expect_code=1)
        assert json.loads(capsys.readouterr().out)["code"] == 404

    def test_unconvertible_is_415(self, tmp_path, capsys):
        source = tmp_path / "blob.bin"
        source.write_bytes(b"\x00\x01\xff\xfe" * 32)
        run_cli(["convert", str(source)], expect_code=1)
        assert json.loads(capsys.readouterr().out)["code"] == 415


class TestCmdFormats:
    def test_json_lists_converters_with_availability(self, capsys):
        run_cli(["formats", "--json"])

        result = json.loads(capsys.readouterr().out)
        assert result["code"] == 200
        converters = result["data"]["converters"]
        names = {entry["converter"] for entry in converters}
        assert {"passthrough", "image", "text", "markdown", "browser", "office"} <= names
        for entry in converters:
            assert isinstance(entry["available"], bool)
            assert entry["extensions"]
            if not entry["available"]:
                assert entry["install_hint"]

    def test_human_output_mentions_every_converter(self, capsys):
        run_cli(["formats"])
        out = capsys.readouterr().out
        assert "passthrough" in out
        assert "office" in out
        assert ".pdf" in out

    def test_install_hint_is_shown_when_a_tool_is_missing(self, capsys, monkeypatch):
        from local_printer import convert

        monkeypatch.setattr(convert, "find_soffice", lambda: None)
        monkeypatch.setattr(convert, "find_browser", lambda: None)
        monkeypatch.setattr(convert, "_msoffice_com_available", lambda: False)

        run_cli(["formats"])

        out = capsys.readouterr().out
        assert "[missing]" in out
        assert "libreoffice.org" in out.lower()
