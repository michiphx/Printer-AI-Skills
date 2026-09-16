"""Unit tests for the dataclasses in models/model.py."""

from models.model import (
    APIResponse,
    LinuxPrintOptions,
    Printer,
    PrinterStatus,
    PrintJob,
)


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


class TestLinuxPrintOptions:
    """to_dict() must speak CUPS: hyphenated IPP names, string values only."""

    def test_to_dict_emits_ipp_names_and_string_values(self):
        opts = LinuxPrintOptions.from_dict(
            {"print_color_mode": "monochrome", "copies": 2}
        )
        as_dict = opts.to_dict()
        assert as_dict == {"print-color-mode": "monochrome", "copies": "2"}
        for key, value in as_dict.items():
            assert "_" not in key, f"{key!r} is not an IPP attribute name"
            assert isinstance(value, str), f"{key}={value!r} is not a str"

    def test_every_underscored_field_is_translated(self):
        opts = LinuxPrintOptions(
            copies="2",
            media="A4",
            sides="two-sided-long-edge",
            orientation_requested="4",
            print_color_mode="color",
            print_quality="4",
            page_ranges="1-5",
            number_up="2",
            fit_to_page="true",
            media_source="tray-1",
            media_type="photographic",
            scaling="100",
            resolution="600dpi",
        )
        assert set(opts.to_dict()) == {
            "copies",
            "media",
            "sides",
            "orientation-requested",
            "print-color-mode",
            "print-quality",
            "page-ranges",
            "number-up",
            "fit-to-page",
            "media-source",
            "media-type",
            "scaling",
            "resolution",
        }

    def test_bools_and_ints_are_coerced_to_cups_strings(self):
        opts = LinuxPrintOptions.from_dict(
            {"fit_to_page": True, "number_up": 4, "copies": 3, "custom-flag": False}
        )
        as_dict = opts.to_dict()
        assert as_dict["fit-to-page"] == "true"
        assert as_dict["custom-flag"] == "false"
        assert as_dict["number-up"] == "4"
        assert as_dict["copies"] == "3"
        assert all(isinstance(v, str) for v in as_dict.values())

    def test_from_dict_accepts_hyphenated_ipp_keys(self):
        opts = LinuxPrintOptions.from_dict(
            {"print-color-mode": "color", "orientation-requested": "4", "copies": "1"}
        )
        assert opts.print_color_mode == "color"
        assert opts.orientation_requested == "4"
        assert opts.copies == "1"
        assert opts.extra_options is None
        assert opts.to_dict() == {
            "print-color-mode": "color",
            "orientation-requested": "4",
            "copies": "1",
        }

    def test_snake_and_hyphen_input_produce_the_same_dict(self):
        snake = LinuxPrintOptions.from_dict({"print_color_mode": "color", "number_up": 2})
        ipp = LinuxPrintOptions.from_dict({"print-color-mode": "color", "number-up": "2"})
        assert snake.to_dict() == ipp.to_dict()

    def test_unknown_keys_survive_untouched_in_extra_options(self):
        opts = LinuxPrintOptions.from_dict({"copies": "1", "ColorModel": "RGB"})
        assert opts.extra_options == {"ColorModel": "RGB"}
        assert opts.to_dict() == {"copies": "1", "ColorModel": "RGB"}

    def test_none_values_are_dropped(self):
        opts = LinuxPrintOptions.from_dict({"copies": None, "media": "A4"})
        assert opts.to_dict() == {"media": "A4"}

    def test_does_not_mutate_input(self):
        data = {"print-color-mode": "color", "copies": 2}
        original = dict(data)
        LinuxPrintOptions.from_dict(data)
        assert data == original
