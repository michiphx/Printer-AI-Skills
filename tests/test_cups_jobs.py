"""Unit tests for the CUPS job-listing path (`local_printer.cups`).

Real pycups may or may not be installed, and either way we do not want these
tests talking to a live CUPS daemon, so a recording fake `cups` module is
injected into `sys.modules` and `local_printer.cups` is reloaded against it
(the same pattern `test_windows_print.py` uses for the win32 modules).
No real printer or job is touched.

The regression under test: `getJobs()` returns only a tiny default attribute
set (in practice just `job-uri`), so the job list came back with empty names,
`status: "unknown"` and zero counters. It also has no `job-printer-name`
attribute at all - the queue name lives in `job-printer-uri`.
"""

import importlib
import os
import sys
import types

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pytest


# --------------------------------------------------------------------- fakes


class FakeIPPError(Exception):
    pass


class FakeConnection:
    """Recording stand-in for cups.Connection().

    `jobs` is the mapping getJobs() should return; `job_attributes` backs
    getJobAttributes(). Every getJobs() call is recorded in `calls`.
    """

    # Set by the fixture before local_printer.cups is reloaded
    jobs = {}
    job_attributes = {}
    calls = []
    supports_requested_attributes = True

    def __init__(self, *args, **kwargs):
        pass

    def getJobs(self, which_jobs=None, my_jobs=None, requested_attributes=None):
        if requested_attributes is not None and not type(
            self
        ).supports_requested_attributes:
            # Mimic an old pycups that has no such keyword
            raise TypeError(
                "getJobs() got an unexpected keyword argument 'requested_attributes'"
            )
        type(self).calls.append(
            {
                "which_jobs": which_jobs,
                "my_jobs": my_jobs,
                "requested_attributes": requested_attributes,
            }
        )
        return type(self).jobs

    def getJobAttributes(self, job_id):
        try:
            return type(self).job_attributes[job_id]
        except KeyError:
            raise FakeIPPError(f"no such job {job_id}")


def _job(printer="ET-4850", **overrides):
    """A realistic completed-job record as CUPS returns it."""
    record = {
        "job-printer-uri": f"ipp://localhost/printers/{printer}",
        "job-state": 9,
        "job-state-reasons": "job-completed-successfully",
        "job-priority": 50,
        "job-k-octets": 17,
        "job-impressions": 0,
        "job-impressions-completed": 1,
        "document-name-supplied": "test.pdf",
        "time-at-creation": 1789115954,
        "time-at-processing": 1789115954,
        "time-at-completed": 1789115969,
    }
    record.update(overrides)
    return record


@pytest.fixture
def cups_backend(monkeypatch):
    """Install the fake `cups` module, reload local_printer.cups, hand it back."""

    FakeConnection.jobs = {}
    FakeConnection.job_attributes = {}
    FakeConnection.calls = []
    FakeConnection.supports_requested_attributes = True

    fake_cups = types.SimpleNamespace(
        Connection=FakeConnection,
        IPPError=FakeIPPError,
    )
    monkeypatch.setitem(sys.modules, "cups", fake_cups)

    import local_printer.cups as cups_module

    module = importlib.reload(cups_module)
    module._fake = FakeConnection  # convenience handle for the tests
    yield module

    # Leave no fake-backed module behind for the rest of the session
    sys.modules.pop("local_printer.cups", None)


def _jobs_ok(result):
    assert result["code"] == 200, result
    return result["data"]["jobs"]


# ------------------------------------------------------- requested attributes


def test_get_jobs_requests_the_attributes_it_reads(cups_backend):
    """The regression: without requested_attributes CUPS returns almost nothing."""
    cups_backend._fake.jobs = {22: _job()}

    cups_backend.get_print_jobs()

    assert len(cups_backend._fake.calls) == 1
    call = cups_backend._fake.calls[0]
    requested = call["requested_attributes"]
    assert requested is not None, "getJobs() must ask for attributes explicitly"

    # Everything the PrintJob fields are built from has to be in the list
    for attr in (
        "job-id",
        "job-name",
        "job-printer-uri",
        "job-state",
        "job-priority",
        "job-k-octets",
        "job-impressions",
        "job-impressions-completed",
        "job-originating-user-name",
        "time-at-creation",
        "document-name-supplied",
    ):
        assert attr in requested, f"{attr} missing from requested_attributes"

    assert call["which_jobs"] == "all"
    assert call["my_jobs"] is False


def test_requested_attributes_list_is_not_shared_mutable_state(cups_backend):
    """A caller mutating the passed list must not corrupt the module constant."""
    cups_backend._fake.jobs = {1: _job()}
    cups_backend.get_print_jobs()
    cups_backend._fake.calls[0]["requested_attributes"].clear()

    cups_backend.get_print_jobs()
    assert "job-state" in cups_backend._fake.calls[1]["requested_attributes"]


def test_falls_back_when_pycups_has_no_requested_attributes(cups_backend):
    """Old pycups (< 1.9.72) raises TypeError - we retry bare instead of failing."""
    cups_backend._fake.supports_requested_attributes = False
    cups_backend._fake.jobs = {7: {"job-uri": "ipp://localhost/jobs/7"}}

    jobs = _jobs_ok(cups_backend.get_print_jobs())

    assert len(cups_backend._fake.calls) == 1  # the TypeError call is not recorded
    assert cups_backend._fake.calls[0]["requested_attributes"] is None
    assert jobs[0]["job_id"] == 7
    assert jobs[0]["status"] == "unknown"


# ------------------------------------------------------------ name derivation


def test_printer_name_is_derived_from_job_printer_uri(cups_backend):
    cups_backend._fake.jobs = {22: _job(printer="ET-4850")}

    job = _jobs_ok(cups_backend.get_print_jobs())[0]

    assert job["printer_name"] == "ET-4850"
    assert job["job_name"] == "test.pdf"
    assert job["job_id"] == 22


@pytest.mark.parametrize(
    "record, expected",
    [
        ({"job-printer-uri": "ipp://localhost/printers/ET-4850"}, "ET-4850"),
        ({"job-printer-uri": "ipp://localhost/printers/ET-4850/"}, "ET-4850"),
        ({"printer-uri": "ipp://localhost/printers/Canon_G5080_series_2"},
         "Canon_G5080_series_2"),
        ({"job-printer-uri": ["ipp://localhost/printers/ET-4850"]}, "ET-4850"),
        ({"job-printer-uri": "ipp://localhost/classes/Group"}, ""),
        ({}, ""),
    ],
)
def test_job_printer_name_helper(cups_backend, record, expected):
    assert cups_backend.job_printer_name(record) == expected


def test_job_name_prefers_document_name_then_job_name(cups_backend):
    assert cups_backend.job_display_name(
        {"document-name-supplied": "a.pdf", "job-name": "b.pdf"}
    ) == "a.pdf"
    assert cups_backend.job_display_name({"job-name": "b.pdf"}) == "b.pdf"
    assert cups_backend.job_display_name({}) == ""


def test_job_status_shares_the_same_derivation(cups_backend):
    """get_print_job_status and get_print_jobs must agree on the same job."""
    cups_backend._fake.jobs = {22: _job()}
    cups_backend._fake.job_attributes = {22: _job()}

    listed = _jobs_ok(cups_backend.get_print_jobs())[0]
    single = cups_backend.get_print_job_status(22)

    assert single["code"] == 200
    assert single["data"]["printer_name"] == listed["printer_name"] == "ET-4850"
    assert single["data"]["job_name"] == listed["job_name"] == "test.pdf"
    assert single["data"]["job_state"] == listed["status"] == "completed"


def test_job_status_missing_job_is_404(cups_backend):
    cups_backend._fake.job_attributes = {}
    result = cups_backend.get_print_job_status(999)
    assert result["code"] == 404


# --------------------------------------------------------------- state mapping


@pytest.mark.parametrize(
    "state, expected",
    [
        (3, "pending"),
        (4, "pending-held"),
        (5, "processing"),
        (6, "processing-stopped"),
        (7, "canceled"),
        (8, "aborted"),
        (9, "completed"),
        (0, "unknown"),
        (99, "unknown"),
    ],
)
def test_state_mapping(cups_backend, state, expected):
    cups_backend._fake.jobs = {1: _job(**{"job-state": state})}
    assert _jobs_ok(cups_backend.get_print_jobs())[0]["status"] == expected


def test_state_mapping_when_state_is_absent(cups_backend):
    cups_backend._fake.jobs = {1: {"job-printer-uri": "ipp://localhost/printers/P"}}
    assert _jobs_ok(cups_backend.get_print_jobs())[0]["status"] == "unknown"


# ------------------------------------------------------------------- counters


def test_counters_are_populated(cups_backend):
    cups_backend._fake.jobs = {
        21: _job(
            **{
                "job-k-octets": 137,
                "job-impressions": 2,
                "job-impressions-completed": 2,
                "job-priority": 50,
                "job-originating-user-name": "michael",
                "time-at-creation": 1788880801,
            }
        )
    }

    job = _jobs_ok(cups_backend.get_print_jobs())[0]

    assert job["size"] == 137 * 1024
    assert job["total_pages"] == 2
    assert job["pages_printed"] == 2
    assert job["pages"] == 2
    assert job["priority"] == 50
    assert job["user"] == "michael"
    assert job["submitted_time"] == 1788880801


# --------------------------------------------------------------------- filter


def test_printer_filter_matches_derived_name(cups_backend):
    cups_backend._fake.jobs = {
        1: _job(printer="ET-4850"),
        2: _job(printer="Other_Printer"),
        3: _job(printer="ET-4850"),
    }

    filtered = _jobs_ok(cups_backend.get_print_jobs("ET-4850"))
    assert [j["job_id"] for j in filtered] == [1, 3]
    assert {j["printer_name"] for j in filtered} == {"ET-4850"}

    assert cups_backend.get_print_jobs("ET-4850")["data"]["count"] == 2
    assert cups_backend.get_print_jobs("Nope")["data"]["count"] == 0
    assert cups_backend.get_print_jobs()["data"]["count"] == 3


def test_printer_filter_is_exact_not_prefix(cups_backend):
    cups_backend._fake.jobs = {1: _job(printer="ET-4850_2")}
    assert cups_backend.get_print_jobs("ET-4850")["data"]["count"] == 0


# ---------------------------------------------------------------- robustness


def test_missing_attributes_do_not_crash(cups_backend):
    """CUPS omits attributes it has no value for - every field must degrade."""
    cups_backend._fake.jobs = {5: {}}

    job = _jobs_ok(cups_backend.get_print_jobs())[0]

    assert job == {
        "job_id": 5,
        "printer_name": "",
        "job_name": "",
        "status": "unknown",
        "priority": 0,
        "size": 0,
        "pages": 0,
        "user": "",
        "submitted_time": 0,
        "total_pages": 0,
        "pages_printed": 0,
    }


def test_none_valued_attributes_do_not_crash(cups_backend):
    cups_backend._fake.jobs = {
        6: _job(
            **{
                "job-k-octets": None,
                "job-impressions": None,
                "job-impressions-completed": None,
                "job-priority": None,
                "job-originating-user-name": None,
                "time-at-creation": None,
            }
        )
    }

    job = _jobs_ok(cups_backend.get_print_jobs())[0]
    assert job["size"] == 0
    assert job["pages_printed"] == 0
    assert job["priority"] == 0
    assert job["user"] == ""
    assert job["submitted_time"] == 0


def test_response_shape_is_preserved(cups_backend):
    """main.py's cmd_jobs reads data.jobs / data.count and the PrintJob fields."""
    cups_backend._fake.jobs = {1: _job(), 2: _job()}

    result = cups_backend.get_print_jobs()

    assert set(result) >= {"code", "msg", "data"}
    assert set(result["data"]) == {"jobs", "count"}
    assert result["data"]["count"] == len(result["data"]["jobs"]) == 2
    assert set(result["data"]["jobs"][0]) == {
        "job_id",
        "printer_name",
        "job_name",
        "status",
        "priority",
        "size",
        "pages",
        "user",
        "submitted_time",
        "total_pages",
        "pages_printed",
    }


def test_cups_error_becomes_a_500(cups_backend):
    def boom(*args, **kwargs):
        raise RuntimeError("cups down")

    cups_backend._fake.getJobs = boom
    result = cups_backend.get_print_jobs()
    assert result["code"] == 500
    assert "cups down" in result["msg"]
