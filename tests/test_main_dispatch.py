"""In-process unit tests for main.py's dispatch helpers.

Unlike test_cli.py (which drives the CLI through subprocess), these tests
import `main` directly to exercise `finish`, `_backend`, `_run_net_command`
and `build_parser` without paying for a new interpreter per case.
"""

import argparse

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
