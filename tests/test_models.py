"""Unit tests for the dataclasses in models/model.py."""

from models.model import APIResponse, Printer, PrinterStatus, PrintJob


class TestPrinterFromDict:
    def test_drops_unknown_keys(self):
        data = {
            "index": 1,
            "name": "EPSON ET-4850 (Netzwerk)",
            "status": "idle",
            "status_reasons": [],
            "is_accepting": True,
            "type": "EPSON Universal",
            "is_default": True,
            "some_future_field": "should be dropped",
            "another_unknown": 42,
        }
        printer = Printer.from_dict(data)
        assert printer.name == "EPSON ET-4850 (Netzwerk)"
        assert not hasattr(printer, "some_future_field")
        assert not hasattr(printer, "another_unknown")

    def test_does_not_mutate_input(self):
        data = {
            "index": 1,
            "name": "P",
            "status": "idle",
            "status_reasons": [],
            "is_accepting": True,
            "type": "T",
            "is_default": False,
            "extra": "x",
        }
        original = dict(data)
        Printer.from_dict(data)
        assert data == original

    def test_converts_status_string_to_enum(self):
        data = {
            "index": 1,
            "name": "P",
            "status": "processing",
            "status_reasons": [],
            "is_accepting": True,
            "type": "T",
            "is_default": False,
        }
        printer = Printer.from_dict(data)
        assert printer.status is PrinterStatus.PROCESSING
        assert isinstance(printer.status, PrinterStatus)

    def test_unknown_status_string_maps_to_unknown(self):
        data = {
            "index": 1,
            "name": "P",
            "status": "some-weird-state",
            "status_reasons": [],
            "is_accepting": True,
            "type": "T",
            "is_default": False,
        }
        printer = Printer.from_dict(data)
        assert printer.status is PrinterStatus.UNKNOWN

    def test_round_trip_to_dict(self):
        data = {
            "index": 2,
            "name": "P",
            "status": "stopped",
            "status_reasons": ["offline"],
            "is_accepting": False,
            "type": "T",
            "is_default": False,
        }
        printer = Printer.from_dict(data)
        as_dict = printer.to_dict()
        assert as_dict["status"] == "stopped"
        assert isinstance(as_dict["status"], str)


class TestPrintJobFromDict:
    def test_drops_unknown_keys(self):
        data = {
            "job_id": 123,
            "printer_name": "EPSON ET-4850 (Netzwerk)",
            "job_name": "test.pdf",
            "status": "queued",
            "unexpected_field": "drop me",
        }
        job = PrintJob.from_dict(data)
        assert job.job_id == 123
        assert job.printer_name == "EPSON ET-4850 (Netzwerk)"
        assert not hasattr(job, "unexpected_field")

    def test_defaults_for_missing_optional_fields(self):
        data = {
            "job_id": 1,
            "printer_name": "P",
            "job_name": "doc",
            "status": "printing",
        }
        job = PrintJob.from_dict(data)
        assert job.priority == 0
        assert job.size == 0
        assert job.pages == 0
        assert job.user == ""

    def test_to_dict_shape(self):
        job = PrintJob.from_dict(
            {"job_id": 5, "printer_name": "P", "job_name": "doc", "status": "completed"}
        )
        as_dict = job.to_dict()
        assert as_dict["job_id"] == 5
        assert as_dict["status"] == "completed"


class TestAPIResponse:
    def test_unsupported_media_type_is_415(self):
        resp = APIResponse.unsupported_media_type("bad file type")
        assert resp.code == 415
        assert resp.msg == "bad file type"
        assert resp.data == {}

    def test_not_implemented_is_501(self):
        resp = APIResponse.not_implemented("no backend on this platform")
        assert resp.code == 501
        assert resp.msg == "no backend on this platform"

    def test_success_is_200(self):
        resp = APIResponse.success({"a": 1})
        assert resp.code == 200
        assert resp.data == {"a": 1}

    def test_to_dict_shape(self):
        resp = APIResponse.error(400, "bad request", {"detail": "x"})
        as_dict = resp.to_dict()
        assert set(as_dict.keys()) == {"code", "msg", "data"}
        assert as_dict["code"] == 400
        assert as_dict["msg"] == "bad request"
        assert as_dict["data"] == {"detail": "x"}

    def test_unsupported_media_type_default_data(self):
        resp = APIResponse.unsupported_media_type("msg", None)
        assert resp.data == {}
