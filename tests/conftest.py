"""Shared pytest fixtures for the printer-ai-skills CLI test suite.

Most tests here drive `main.py` as a real subprocess (the way a human or an
AI caller would invoke `printer-ai`), so behaviour like argparse errors,
stdout/stderr separation and process exit codes is exercised for real rather
than being reimplemented in Python. `test_main_dispatch.py` is the exception:
it imports `main` in-process to unit-test small helpers directly.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.fixture(scope="session")
def repo_root() -> Path:
    """The repository root (parent of the tests/ directory)."""
    return Path(__file__).resolve().parent.parent


@pytest.fixture
def cli(repo_root):
    """Run `main.py` as a subprocess and return (returncode, stdout, stderr).

    Usage:
        returncode, stdout, stderr = cli("printers", "--json")
    """

    def _run(*args, timeout=120, cwd=None):
        cmd = [sys.executable, str(repo_root / "main.py"), *args]
        proc = subprocess.run(
            cmd,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            cwd=cwd or repo_root,
        )
        return proc.returncode, proc.stdout, proc.stderr

    return _run


def parse_json_stdout(stdout: str):
    """Parse a CLI's stdout as JSON, failing the test with useful context if it isn't."""
    try:
        return json.loads(stdout)
    except json.JSONDecodeError as exc:
        pytest.fail(f"stdout was not valid JSON: {exc}\n--- stdout ---\n{stdout}")


def _windows_backend_available() -> bool:
    if sys.platform != "win32":
        return False
    try:
        import win32print  # noqa: F401
    except ImportError:
        return False
    return True


HAS_WINDOWS_BACKEND = _windows_backend_available()

has_windows_backend = pytest.mark.skipif(
    not HAS_WINDOWS_BACKEND,
    reason="win32print backend is not importable on this machine",
)
