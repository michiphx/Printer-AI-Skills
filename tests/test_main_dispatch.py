"""In-process unit tests for main.py's dispatch helpers.

Unlike test_cli.py (which drives the CLI through subprocess), these tests
import `main` directly to exercise `finish`, `_backend` and `build_parser`
without paying for a new interpreter per case.

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


class TestNetCommandsBindDirectly:
    """The network subcommands call main's own cmd_* functions, no indirection."""

    @pytest.mark.parametrize(
        "argv, func",
        [
            (["discover"], main.cmd_discover),
            (["probe", "1.2.3.4"], main.cmd_probe),
            (["diagnose"], main.cmd_diagnose),
            (["ports"], main.cmd_ports),
            (["drivers"], main.cmd_drivers),
            (["setup", "1.2.3.4"], main.cmd_setup),
            (["driver-search", "x"], main.cmd_driver_search),
            (["remove", "x"], main.cmd_remove),
            (["set-default", "x"], main.cmd_set_default),
        ],
    )
    def test_func_is_the_local_handler(self, argv, func):
        args = main.build_parser().parse_args(argv)
        assert args.func is func

    def test_no_dead_dispatch_helpers(self):
        assert not hasattr(main, "_run_net_command")
        assert not hasattr(main, "_net")


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
        assert args.overwrite is False

    def test_convert_overwrite_flag(self):
        args = main.build_parser().parse_args(["convert", "a.md", "--overwrite"])
        assert args.overwrite is True

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


class TestPruneKeptPdfs:
    """Unit tests for main._prune_kept_pdfs (R5-05), independent of the full
    print path - it just needs a directory of files with controllable mtimes."""

    def test_old_files_removed_recent_files_kept(self, tmp_path):
        old = tmp_path / "old.pdf"
        recent = tmp_path / "recent.pdf"
        old.write_text("old")
        recent.write_text("recent")

        now = 1_000_000.0
        eight_days_ago = now - 8 * 86400
        one_day_ago = now - 86400
        os.utime(old, (eight_days_ago, eight_days_ago))
        os.utime(recent, (one_day_ago, one_day_ago))

        main._prune_kept_pdfs(str(tmp_path), max_age_days=7, now=now)

        assert not old.exists()
        assert recent.exists()

    def test_missing_directory_does_not_raise(self, tmp_path):
        main._prune_kept_pdfs(str(tmp_path / "does-not-exist"))

    def test_removal_error_does_not_propagate(self, tmp_path, monkeypatch):
        stale = tmp_path / "stale.pdf"
        stale.write_text("stale")
        old_time = 0.0
        os.utime(stale, (old_time, old_time))

        def boom(path):
            raise PermissionError("nope")

        monkeypatch.setattr(os, "remove", boom)

        # Must not raise even though the removal itself fails.
        main._prune_kept_pdfs(str(tmp_path), max_age_days=7, now=1_000_000.0)
        assert stale.exists()


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

    def test_json_flag_prints_the_full_result_on_success(
        self, fake_backend, tmp_path, capsys
    ):
        source = tmp_path / "doc.pdf"
        source.write_bytes(b"%PDF-1.4\n%stuff\n")

        run_cli(["print", str(source), "--json"])

        out = capsys.readouterr().out
        assert "Print job submitted" not in out
        result = json.loads(out)
        assert result["code"] == 200
        data = result["data"]
        assert data["job_id"] == 42
        assert data["printer_name"] == "Fake-Printer"
        assert data["converter"] == "passthrough"
        assert data["converted"] is False
        assert data["conversion_notes"]

    def test_json_flag_carries_conversion_details(self, fake_backend, tmp_path, capsys):
        pytest.importorskip("reportlab")
        source = tmp_path / "notes.txt"
        source.write_text("hello\n")

        run_cli(["print", str(source), "--json"])

        result = json.loads(capsys.readouterr().out)
        data = result["data"]
        assert data["converted"] is True
        assert data["converted_from"] == str(source)
        assert data["converter"] == "text"

    def test_default_output_stays_human_readable(self, fake_backend, tmp_path, capsys):
        source = tmp_path / "doc.pdf"
        source.write_bytes(b"%PDF-1.4\n%stuff\n")
        run_cli(["print", str(source)])
        out = capsys.readouterr().out
        assert "Print job submitted" in out
        with pytest.raises(ValueError):
            json.loads(out)

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

    def test_keep_pdf_reports_and_keeps_the_file(
        self, fake_backend, tmp_path, capsys, monkeypatch
    ):
        # Isolate ~/.cache/printer-ai/kept under tmp_path for this test.
        monkeypatch.setenv("HOME", str(tmp_path))
        source = tmp_path / "notes.txt"
        source.write_text("hello\n")

        run_cli(["print", str(source), "--keep-pdf"])

        out = capsys.readouterr().out
        assert "pdf kept at:" in out

        # The backend was handed the file while it still lived in the
        # ephemeral temp directory to_pdf() created...
        ephemeral_path = fake_backend.calls[0]["file_path"]
        ephemeral_dir = os.path.dirname(ephemeral_path)
        assert os.path.basename(ephemeral_dir).startswith("printer-ai-")

        # ...but by the time cmd_print is done, the PDF has been relocated
        # to the stable, documented ~/.cache/printer-ai/kept location...
        kept_dir = os.path.expanduser("~/.cache/printer-ai/kept")
        assert os.path.isdir(kept_dir)
        kept_files = os.listdir(kept_dir)
        assert len(kept_files) == 1
        kept_path = os.path.join(kept_dir, kept_files[0])
        assert os.path.isfile(kept_path)
        assert kept_files[0].startswith("notes-")

        # ...and the ephemeral temp directory to_pdf() made no longer exists,
        # so it can never leak on disk.
        assert not os.path.exists(ephemeral_dir)

    def test_keep_pdf_reports_the_stable_path_in_result_data(
        self, fake_backend, tmp_path, capsys, monkeypatch
    ):
        monkeypatch.setenv("HOME", str(tmp_path))
        source = tmp_path / "notes.txt"
        source.write_text("hello\n")

        run_cli(["print", str(source), "--keep-pdf", "--json"])

        result = json.loads(capsys.readouterr().out)
        pdf_path = result["data"]["pdf_path"]
        kept_dir = os.path.expanduser("~/.cache/printer-ai/kept")
        assert os.path.dirname(pdf_path) == kept_dir
        assert os.path.isfile(pdf_path)

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

    def test_missing_file_is_404_as_json(self, fake_backend, tmp_path, capsys):
        run_cli(["print", str(tmp_path / "nope.txt")], expect_code=1)
        result = json.loads(capsys.readouterr().out)
        assert result["code"] == 404
        assert result["data"]["file_path"].endswith("nope.txt")

    def test_missing_file_is_404_even_without_a_backend(self, tmp_path, capsys, monkeypatch):
        monkeypatch.setattr(main, "_backend", lambda: (None, {"code": 501, "msg": "no"}))
        run_cli(["print", str(tmp_path / "nope.txt")], expect_code=1)
        assert json.loads(capsys.readouterr().out)["code"] == 404

    def test_empty_file_is_422(self, fake_backend, tmp_path, capsys):
        source = tmp_path / "empty.docx"
        source.write_bytes(b"")
        run_cli(["print", str(source)], expect_code=1)
        result = json.loads(capsys.readouterr().out)
        assert result["code"] == 422
        assert fake_backend.calls == []

    def test_conversion_failure_uses_the_exception_code(
        self, fake_backend, tmp_path, capsys, monkeypatch
    ):
        from local_printer import convert

        source = tmp_path / "notes.txt"
        source.write_text("hello\n")

        def slow(path, out_dir=None):
            raise convert.ConversionError("tool timed out", code=504)

        monkeypatch.setattr(convert, "to_pdf", slow)
        run_cli(["print", str(source)], expect_code=1)
        assert json.loads(capsys.readouterr().out)["code"] == 504

    def test_conversion_error_without_code_falls_back_to_415(self):
        class Legacy(Exception):
            def to_data(self):
                return {}

        assert main._conversion_failure(Legacy("old"), "f")["code"] == 415

    @pytest.mark.parametrize("raw", ["[1]", '"x"', "3", "null", "true"])
    def test_options_must_be_a_json_object(self, fake_backend, tmp_path, capsys, raw):
        fake_backend.PrintOptions = object  # from_dict would blow up if reached
        source = tmp_path / "notes.txt"
        source.write_text("hello\n")

        run_cli(["print", str(source), "--options", raw], expect_code=1)

        result = json.loads(capsys.readouterr().out)
        assert result["code"] == 400
        assert "JSON object" in result["msg"]
        assert fake_backend.calls == []

    def test_options_invalid_json_is_400(self, fake_backend, tmp_path, capsys):
        source = tmp_path / "notes.txt"
        source.write_text("hello\n")
        run_cli(["print", str(source), "--options", "{not json"], expect_code=1)
        result = json.loads(capsys.readouterr().out)
        assert result["code"] == 400
        assert "JSON" in result["msg"]

    def test_backend_failure_is_json(self, fake_backend, tmp_path, capsys, monkeypatch):
        source = tmp_path / "doc.pdf"
        source.write_bytes(b"%PDF-1.4\n")
        monkeypatch.setattr(
            fake_backend, "print_file",
            lambda *a, **k: {"code": 503, "msg": "stopped", "data": {}},
        )
        run_cli(["print", str(source)], expect_code=1)
        assert json.loads(capsys.readouterr().out)["code"] == 503

    def test_no_backend_is_501_json(self, tmp_path, capsys, monkeypatch):
        source = tmp_path / "doc.pdf"
        source.write_bytes(b"%PDF-1.4\n")
        monkeypatch.setattr(main, "_backend", lambda: (None, {"code": 501, "msg": "no"}))
        run_cli(["print", str(source)], expect_code=1)
        assert json.loads(capsys.readouterr().out)["code"] == 501


class TestMainCatchAll:
    def test_unexpected_exception_emits_json_and_stderr(self, capsys, monkeypatch):
        def boom(args):
            raise RuntimeError("wires crossed")

        # build_parser() binds the module-level cmd_formats at call time.
        monkeypatch.setattr(main, "cmd_formats", boom)
        monkeypatch.setattr("sys.argv", ["printer-ai", "formats"])

        with pytest.raises(SystemExit) as exc_info:
            main.main()

        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "Error: wires crossed" in captured.err
        result = json.loads(captured.out)
        assert result == {"code": 500, "msg": "wires crossed", "data": {}}


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

    def test_native_pdf_into_a_directory_is_actually_copied(self, tmp_path, capsys):
        source = tmp_path / "doc.pdf"
        source.write_bytes(b"%PDF-1.4\n")
        target_dir = tmp_path / "pdfs"

        run_cli(["convert", str(source), "--out", str(target_dir), "--json"])

        data = json.loads(capsys.readouterr().out)["data"]
        assert data["pdf_path"] == str(target_dir / "doc.pdf")
        assert (target_dir / "doc.pdf").read_bytes() == b"%PDF-1.4\n"
        assert source.exists()
        assert any("copied" in n for n in data["notes"])

    def test_native_postscript_keeps_its_own_name_in_a_directory(self, tmp_path, capsys):
        source = tmp_path / "job.ps"
        source.write_bytes(b"%!PS-Adobe-3.0\n")
        target_dir = tmp_path / "out"

        run_cli(["convert", str(source), "--out", str(target_dir), "--json"])

        data = json.loads(capsys.readouterr().out)["data"]
        assert data["pdf_path"] == str(target_dir / "job.ps")
        assert (target_dir / "job.ps").exists()
        assert not (target_dir / "job.pdf").exists()

    def test_native_pdf_without_out_is_left_alone(self, tmp_path, capsys):
        """No --out and a native source: nothing to write, so nothing to refuse."""
        source = tmp_path / "doc.pdf"
        source.write_bytes(b"%PDF-1.4\n")

        run_cli(["convert", str(source), "--json"])

        data = json.loads(capsys.readouterr().out)["data"]
        assert data["pdf_path"] == str(source)
        assert source.read_bytes() == b"%PDF-1.4\n"

    def test_refuses_to_overwrite_an_existing_pdf(self, tmp_path, capsys):
        source = tmp_path / "notes.txt"
        source.write_text("hello\n")
        existing = tmp_path / "notes.pdf"
        existing.write_bytes(b"precious")

        run_cli(["convert", str(source), "--json"], expect_code=1)

        result = json.loads(capsys.readouterr().out)
        assert result["code"] == 409
        assert result["data"]["path"] == str(existing)
        assert "refusing to overwrite" in result["msg"]
        assert existing.read_bytes() == b"precious"

    def test_overwrite_flag_replaces_the_existing_pdf(self, tmp_path, capsys):
        source = tmp_path / "notes.txt"
        source.write_text("hello\n")
        existing = tmp_path / "notes.pdf"
        existing.write_bytes(b"precious")

        run_cli(["convert", str(source), "--overwrite", "--json"])

        data = json.loads(capsys.readouterr().out)["data"]
        assert data["pdf_path"] == str(existing)
        assert existing.read_bytes().startswith(b"%PDF-")

    def test_refuses_to_overwrite_a_named_target(self, tmp_path, capsys):
        source = tmp_path / "notes.txt"
        source.write_text("hello\n")
        target = tmp_path / "custom.pdf"
        target.write_bytes(b"precious")

        run_cli(["convert", str(source), "--out", str(target), "--json"], expect_code=1)

        assert json.loads(capsys.readouterr().out)["code"] == 409
        assert target.read_bytes() == b"precious"

    def test_named_target_does_not_clobber_a_sibling_pdf(self, tmp_path, capsys):
        """Converting to custom.pdf must not touch an unrelated notes.pdf next to it."""
        source = tmp_path / "notes.txt"
        source.write_text("hello\n")
        sibling = tmp_path / "notes.pdf"
        sibling.write_bytes(b"precious")
        target = tmp_path / "custom.pdf"

        run_cli(["convert", str(source), "--out", str(target), "--json"])

        assert target.read_bytes().startswith(b"%PDF-")
        assert sibling.read_bytes() == b"precious"

    def test_native_copy_into_directory_refuses_overwrite(self, tmp_path, capsys):
        source = tmp_path / "doc.pdf"
        source.write_bytes(b"%PDF-1.4\n")
        target_dir = tmp_path / "pdfs"
        target_dir.mkdir()
        (target_dir / "doc.pdf").write_bytes(b"precious")

        run_cli(["convert", str(source), "--out", str(target_dir), "--json"], expect_code=1)
        assert json.loads(capsys.readouterr().out)["code"] == 409

        run_cli(["convert", str(source), "--out", str(target_dir), "--overwrite", "--json"])
        assert (target_dir / "doc.pdf").read_bytes() == b"%PDF-1.4\n"

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
