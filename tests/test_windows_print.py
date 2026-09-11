"""Unit tests for the Windows GDI print path.

Windows is not available here, so every win32 module (plus PIL.ImageWin and
pypdfium2) is replaced by a recording fake in `sys.modules` before
`local_printer.windows` is (re)imported. The fakes record the exact GDI call
sequence, which is what these tests assert on. No real printer is touched.
"""

import importlib
import os
import sys
import types

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pytest

from local_printer import win_render


# --------------------------------------------------------------------- fakes


class _Const:
    """Stand-in for a win32 constants module.

    Known constants keep their real values; anything else gets a stable,
    distinct bit so `Fields |= DM_X` stays meaningful.
    """

    _KNOWN = {
        "DM_ORIENTATION": 0x00000001,
        "DM_PAPERSIZE": 0x00000002,
        "DM_PAPERLENGTH": 0x00000004,
        "DM_PAPERWIDTH": 0x00000008,
        "DM_COPIES": 0x00000100,
        "DM_DEFAULTSOURCE": 0x00000200,
        "DM_PRINTQUALITY": 0x00000400,
        "DM_COLOR": 0x00000800,
        "DM_DUPLEX": 0x00001000,
        "DM_COLLATE": 0x00008000,
        "DM_MEDIATYPE": 0x00800000,
        "DM_IN_BUFFER": 8,
        "DM_OUT_BUFFER": 2,
        "DMORIENT_PORTRAIT": 1,
        "DMORIENT_LANDSCAPE": 2,
        "HORZRES": 8,
        "VERTRES": 10,
        "LOGPIXELSX": 88,
        "LOGPIXELSY": 90,
        "PHYSICALWIDTH": 110,
        "PHYSICALHEIGHT": 111,
        "PHYSICALOFFSETX": 112,
        "PHYSICALOFFSETY": 113,
        "DC_COPIES": 18,
        "DC_COLORDEVICE": 32,
    }

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        value = self._KNOWN.get(name, 1 << (len(name) % 24))
        object.__setattr__(self, name, value)
        return value


class FakeDevMode:
    """Minimal DEVMODE: plain attributes plus a Fields bitmask."""

    def __init__(self):
        self.Fields = 0
        self.Orientation = 1
        self.Copies = 1
        self.Color = 1
        self.PaperSize = 9
        self.Duplex = 1
        self.Collate = 1
        self.DefaultSource = 7
        self.MediaType = 1
        self.PrintQuality = -4
        self.PaperLength = 0
        self.PaperWidth = 0


class FakeWin32Print:
    """Recording stand-in for win32print."""

    PRINTER_ENUM_LOCAL = 2
    PRINTER_ENUM_CONNECTIONS = 4
    PRINTER_STATUS_BUSY = 0x200
    PRINTER_STATUS_ERROR = 0x2
    PRINTER_STATUS_OFFLINE = 0x80
    PRINTER_STATUS_PAUSED = 0x1
    PRINTER_STATUS_PAPER_OUT = 0x10
    PRINTER_STATUS_PAPER_JAM = 0x8
    PRINTER_STATUS_DOOR_OPEN = 0x400000
    PRINTER_STATUS_TONER_LOW = 0x20000
    PRINTER_STATUS_NO_TONER = 0x40000

    def __init__(self, calls, devmode, dc_copies=1):
        self.calls = calls
        self.devmode = devmode
        self.dc_copies = dc_copies
        self.written = []

    # --- queue discovery
    def EnumPrinters(self, flags):
        return [(0, "", "Fake Printer", "")]

    def GetDefaultPrinter(self):
        return "Fake Printer"

    def OpenPrinter(self, name):
        self.calls.append(("OpenPrinter", name))
        return "HPRINTER"

    def GetPrinter(self, handle, level):
        return {
            "Status": 0,
            "pDriverName": "Fake Driver",
            "pPortName": "FAKE:",
            "pLocation": "",
            "cJobs": 0,
            "pDevMode": self.devmode,
        }

    def ClosePrinter(self, handle):
        self.calls.append(("ClosePrinter",))

    def DeviceCapabilities(self, name, port, cap):
        if cap == _WIN32CON.DC_COPIES:
            return self.dc_copies
        return 0

    def DocumentProperties(self, hwnd, handle, name, out_dm, in_dm, mode):
        self.calls.append(("DocumentProperties", name, mode))
        return 1

    # --- raw spooling
    def StartDocPrinter(self, handle, level, doc_info):
        self.calls.append(("StartDocPrinter", doc_info))
        return 4242

    def StartPagePrinter(self, handle):
        self.calls.append(("StartPagePrinter",))

    def WritePrinter(self, handle, data):
        self.written.append(data)
        self.calls.append(("WritePrinter", len(data)))

    def EndPagePrinter(self, handle):
        self.calls.append(("EndPagePrinter",))

    def EndDocPrinter(self, handle):
        self.calls.append(("EndDocPrinter",))

    def EnumJobs(self, handle, first, count, level):
        return []

    def SetJob(self, *a):
        return None


class FakeDC:
    """Recording stand-in for a win32ui CDC on a printer."""

    CAPS = {
        88: 600,   # LOGPIXELSX
        90: 600,   # LOGPIXELSY
        8: 4800,   # HORZRES  (printable)
        10: 6600,  # VERTRES
        110: 5100,  # PHYSICALWIDTH
        111: 6900,  # PHYSICALHEIGHT
        112: 150,   # PHYSICALOFFSETX
        113: 150,   # PHYSICALOFFSETY
    }

    def __init__(self, calls):
        self.calls = calls

    def GetDeviceCaps(self, index):
        return self.CAPS.get(index, 0)

    def StartDoc(self, title):
        self.calls.append(("StartDoc", title))
        return 77

    def StartPage(self):
        self.calls.append(("StartPage",))

    def EndPage(self):
        self.calls.append(("EndPage",))

    def EndDoc(self):
        self.calls.append(("EndDoc",))

    def AbortDoc(self):
        self.calls.append(("AbortDoc",))

    def DeleteDC(self):
        self.calls.append(("DeleteDC",))

    def GetHandleOutput(self):
        return "HDC-OUT"


class FakeImage:
    def __init__(self, width, height, page_index):
        self.width = width
        self.height = height
        self.page_index = page_index


class FakeBitmap:
    def __init__(self, image):
        self._image = image

    def to_pil(self):
        return self._image


class FakePage:
    def __init__(self, index, size, calls, render_error=None):
        self.index = index
        self._size = size
        self.calls = calls
        self.render_error = render_error

    def get_size(self):
        return self._size

    def render(self, scale=1.0):
        self.calls.append(("render", self.index, round(scale, 4)))
        if self.render_error:
            raise self.render_error
        width = max(1, int(self._size[0] * scale))
        height = max(1, int(self._size[1] * scale))
        return FakeBitmap(FakeImage(width, height, self.index))


class FakePdfDocument:
    #: set per test before the module builds one
    sizes = [(612, 792), (612, 792)]
    calls = None
    render_error = None
    open_error = None

    def __init__(self, path):
        if type(self).open_error:
            raise type(self).open_error
        self.path = path
        self.pages = [
            FakePage(i, size, type(self).calls, type(self).render_error)
            for i, size in enumerate(type(self).sizes)
        ]

    def __len__(self):
        return len(self.pages)

    def __getitem__(self, index):
        return self.pages[index]

    def close(self):
        pass


class FakeDib:
    def __init__(self, image):
        self.image = image

    def draw(self, handle, rect):
        FakeDib.calls.append(("draw", self.image.page_index, handle, rect))


_WIN32CON = _Const()


@pytest.fixture
def win(monkeypatch, tmp_path):
    """Install the fakes, reload local_printer.windows, hand back a harness."""

    calls = []
    devmode = FakeDevMode()

    class Harness:
        pass

    harness = Harness()
    harness.calls = calls
    harness.devmode = devmode
    harness.win32print = FakeWin32Print(calls, devmode)

    dc = FakeDC(calls)
    harness.dc = dc

    win32ui = types.SimpleNamespace(CreateDCFromHandle=lambda hdc: dc)
    win32gui = types.SimpleNamespace(
        CreateDC=lambda driver, name, dm: calls.append(("CreateDC", driver, name, dm))
        or "HDC"
    )

    FakePdfDocument.sizes = [(612, 792), (612, 792)]
    FakePdfDocument.calls = calls
    FakePdfDocument.render_error = None
    FakePdfDocument.open_error = None
    FakeDib.calls = calls
    harness.pdf_document = FakePdfDocument

    pypdfium2 = types.SimpleNamespace(PdfDocument=FakePdfDocument)
    pil = types.SimpleNamespace(ImageWin=types.SimpleNamespace(Dib=FakeDib))
    pywintypes = types.SimpleNamespace(error=type("error", (Exception,), {}))

    monkeypatch.setitem(sys.modules, "win32con", _WIN32CON)
    monkeypatch.setitem(sys.modules, "win32print", harness.win32print)
    monkeypatch.setitem(sys.modules, "win32ui", win32ui)
    monkeypatch.setitem(sys.modules, "win32gui", win32gui)
    monkeypatch.setitem(sys.modules, "pywintypes", pywintypes)
    monkeypatch.setitem(sys.modules, "pypdfium2", pypdfium2)
    monkeypatch.setitem(sys.modules, "PIL", pil)

    import local_printer.windows as windows

    harness.module = importlib.reload(windows)
    harness.tmp_path = tmp_path
    yield harness

    # Leave no fake-backed module behind for the rest of the session
    sys.modules.pop("local_printer.windows", None)


def _pdf(tmp_path, name="doc.pdf"):
    path = tmp_path / name
    path.write_bytes(b"%PDF-1.7\n% fake\n")
    return str(path)


def _names(calls, wanted):
    return [c[0] for c in calls if c[0] in wanted]


# ------------------------------------------------------------------ fit_rect


def test_fit_rect_portrait_is_height_bound_and_centered():
    # 100x200 source into a 200x200 area -> height fills, width centered
    assert win_render.fit_rect(100, 200, 200, 200) == (50, 0, 150, 200)


def test_fit_rect_landscape_is_width_bound_and_centered():
    assert win_render.fit_rect(200, 100, 200, 200) == (0, 50, 200, 150)


def test_fit_rect_exact_fit_uses_whole_area():
    assert win_render.fit_rect(400, 300, 400, 300) == (0, 0, 400, 300)


def test_fit_rect_scales_up_to_fill_area():
    assert win_render.fit_rect(100, 100, 500, 400) == (50, 0, 450, 400)


def test_fit_rect_applies_offsets():
    x0, y0, x1, y1 = win_render.fit_rect(100, 200, 200, 200, off_x=150, off_y=75)
    assert (x0, y0, x1, y1) == (200, 75, 300, 275)


def test_fit_rect_preserves_aspect_ratio():
    x0, y0, x1, y1 = win_render.fit_rect(1700, 2200, 4800, 6600)
    src_ratio = 1700 / 2200
    dst_ratio = (x1 - x0) / (y1 - y0)
    assert abs(src_ratio - dst_ratio) < 0.01
    assert x1 - x0 <= 4800 and y1 - y0 <= 6600


@pytest.mark.parametrize(
    "args",
    [(0, 10, 100, 100), (10, 0, 100, 100), (10, 10, 0, 100), (10, 10, 100, -1)],
)
def test_fit_rect_rejects_non_positive_dimensions(args):
    with pytest.raises(ValueError):
        win_render.fit_rect(*args)


# ------------------------------------------------------------------ dpi / order


def test_resolve_render_dpi_caps_at_300():
    assert win_render.resolve_render_dpi(1200) == 300


def test_resolve_render_dpi_keeps_low_device_dpi():
    assert win_render.resolve_render_dpi(203) == 203


def test_resolve_render_dpi_env_override(monkeypatch):
    monkeypatch.setenv(win_render.RENDER_DPI_ENV, "600")
    assert win_render.resolve_render_dpi(600) == 600


def test_resolve_render_dpi_ignores_garbage_env(monkeypatch):
    monkeypatch.setenv(win_render.RENDER_DPI_ENV, "not-a-number")
    assert win_render.resolve_render_dpi(600) == 300


def test_page_order_collated_repeats_document():
    assert win_render.page_order(3, 2, collate=True) == [0, 1, 2, 0, 1, 2]


def test_page_order_uncollated_repeats_each_page():
    assert win_render.page_order(3, 2, collate=False) == [0, 0, 1, 1, 2, 2]


# -------------------------------------------------------------- the GDI path


def test_gdi_call_sequence_for_two_page_pdf(win):
    result = win.module.print_file(None, _pdf(win.tmp_path), None)

    assert result["code"] == 200, result
    data = result["data"]
    assert data["method"] == "gdi"
    assert data["pages"] == 2
    assert data["status"] == "submitted"
    assert data["job_id"] == 77
    assert data["dpi"] == 300  # LOGPIXELSX 600, capped by the memory guard

    sequence = _names(
        win.calls, {"StartDoc", "StartPage", "draw", "EndPage", "EndDoc", "AbortDoc"}
    )
    assert sequence == [
        "StartDoc",
        "StartPage",
        "draw",
        "EndPage",
        "StartPage",
        "draw",
        "EndPage",
        "EndDoc",
    ]


def test_gdi_doc_title_is_basename_and_dc_is_released(win):
    win.module.print_file(None, _pdf(win.tmp_path, "report.pdf"), None)
    assert ("StartDoc", "report.pdf") in win.calls
    assert ("DeleteDC",) in win.calls
    assert ("ClosePrinter",) in win.calls


def test_gdi_draws_into_the_printable_area(win):
    win.module.print_file(None, _pdf(win.tmp_path), None)
    draws = [c for c in win.calls if c[0] == "draw"]
    assert draws
    for _name, _page, handle, rect in draws:
        assert handle == "HDC-OUT"
        x0, y0, x1, y1 = rect
        assert 0 <= x0 < x1 <= FakeDC.CAPS[8]
        assert 0 <= y0 < y1 <= FakeDC.CAPS[10]


def test_gdi_renders_at_dpi_over_72_scale(win):
    win.module.print_file(None, _pdf(win.tmp_path), None)
    renders = [c for c in win.calls if c[0] == "render"]
    assert renders
    # the fake records the scale rounded to 4 decimals
    assert all(abs(c[2] - 300 / 72.0) < 1e-3 for c in renders)


def test_render_dpi_env_override_reaches_the_rasteriser(win, monkeypatch):
    monkeypatch.setenv(win_render.RENDER_DPI_ENV, "150")
    result = win.module.print_file(None, _pdf(win.tmp_path), None)
    assert result["data"]["dpi"] == 150


# ------------------------------------------------------------------- DEVMODE


def test_devmode_options_are_applied_and_validated(win):
    from models.model import WindowsPrintOptions

    options = WindowsPrintOptions(dmColor=2, dmDuplex=2, dmPaperSize=9, dmPrintQuality=-4)
    result = win.module.print_file(None, _pdf(win.tmp_path), options)

    assert result["code"] == 200, result
    assert win.devmode.Color == 2
    assert win.devmode.Duplex == 2
    assert win.devmode.PaperSize == 9
    assert win.devmode.PrintQuality == -4
    assert win.devmode.Fields & _WIN32CON.DM_COLOR
    assert win.devmode.Fields & _WIN32CON.DM_DUPLEX

    doc_props = [c for c in win.calls if c[0] == "DocumentProperties"]
    assert doc_props
    assert doc_props[0][2] == _WIN32CON.DM_IN_BUFFER | _WIN32CON.DM_OUT_BUFFER

    # The DC must be created with the validated DEVMODE
    create = [c for c in win.calls if c[0] == "CreateDC"]
    assert create and create[0][1] == "WINSPOOL"
    assert create[0][3] is win.devmode


def test_auto_landscape_when_first_page_is_wide(win):
    win.pdf_document.sizes = [(792, 612)]
    result = win.module.print_file(None, _pdf(win.tmp_path), None)

    assert result["code"] == 200, result
    assert result["data"]["auto_landscape"] is True
    assert win.devmode.Orientation == _WIN32CON.DMORIENT_LANDSCAPE
    assert win.devmode.Fields & _WIN32CON.DM_ORIENTATION


def test_no_auto_landscape_for_portrait_pages(win):
    result = win.module.print_file(None, _pdf(win.tmp_path), None)
    assert result["data"]["auto_landscape"] is False
    assert win.devmode.Orientation == 1


def test_explicit_orientation_wins_over_auto_landscape(win):
    from models.model import WindowsPrintOptions

    win.pdf_document.sizes = [(792, 612)]
    result = win.module.print_file(
        None, _pdf(win.tmp_path), WindowsPrintOptions(dmOrientation=1)
    )
    assert result["data"]["auto_landscape"] is False
    assert win.devmode.Orientation == 1


# -------------------------------------------------------------------- copies


def test_client_side_collated_copies_when_driver_cannot(win):
    from models.model import WindowsPrintOptions

    win.win32print.dc_copies = 1
    win.devmode.Collate = 1
    result = win.module.print_file(
        None, _pdf(win.tmp_path), WindowsPrintOptions(dmCopies=2)
    )

    assert result["code"] == 200, result
    assert result["data"]["copies"] == 2
    assert result["data"]["copies_handled_by"] == "client"
    assert result["data"]["sheets_drawn"] == 4
    assert [c[1] for c in win.calls if c[0] == "draw"] == [0, 1, 0, 1]
    assert win.devmode.Copies == 1


def test_client_side_uncollated_copies_repeat_each_page(win):
    from models.model import WindowsPrintOptions

    win.win32print.dc_copies = 1
    win.devmode.Collate = 0
    result = win.module.print_file(
        None, _pdf(win.tmp_path), WindowsPrintOptions(dmCopies=2, dmCollate=0)
    )

    assert result["code"] == 200, result
    assert result["data"]["collate"] is False
    assert [c[1] for c in win.calls if c[0] == "draw"] == [0, 0, 1, 1]


def test_driver_handles_copies_when_supported(win):
    from models.model import WindowsPrintOptions

    win.win32print.dc_copies = 99
    result = win.module.print_file(
        None, _pdf(win.tmp_path), WindowsPrintOptions(dmCopies=3)
    )

    assert result["data"]["copies_handled_by"] == "driver"
    assert result["data"]["sheets_drawn"] == 2
    assert win.devmode.Copies == 3


# ----------------------------------------------------------------- raw paths


def test_raw_true_spools_bytes_verbatim(win):
    path = win.tmp_path / "native.pdf"
    path.write_bytes(b"%PDF-1.7 raw bytes")
    result = win.module.print_file(None, str(path), None, raw=True)

    assert result["code"] == 200, result
    assert result["data"]["method"] == "raw"
    assert result["data"]["job_id"] == 4242
    assert "raw data" in result["data"]["note"]
    assert win.win32print.written == [b"%PDF-1.7 raw bytes"]
    doc_info = [c for c in win.calls if c[0] == "StartDocPrinter"][0][1]
    assert doc_info[2] == "RAW"
    assert not [c for c in win.calls if c[0] == "StartDoc"]


def test_raw_true_works_for_any_extension(win):
    path = win.tmp_path / "anything.xyz"
    path.write_bytes(b"\x01\x02\x03")
    result = win.module.print_file(None, str(path), None, raw=True)
    assert result["code"] == 200
    assert result["data"]["method"] == "raw"


def test_prn_file_falls_back_to_raw(win):
    path = win.tmp_path / "job.prn"
    path.write_bytes(b"\x1b%-12345X@PJL\n")
    result = win.module.print_file(None, str(path), None)

    assert result["code"] == 200, result
    assert result["data"]["method"] == "raw"
    assert ".prn" in result["data"]["note"]


def test_postscript_file_falls_back_to_raw(win):
    path = win.tmp_path / "job.ps"
    path.write_bytes(b"%!PS-Adobe-3.0\n")
    result = win.module.print_file(None, str(path), None)
    assert result["data"]["method"] == "raw"


def test_non_pdf_without_raw_is_415(win):
    path = win.tmp_path / "letter.docx"
    path.write_bytes(b"PK\x03\x04not a pdf")
    result = win.module.print_file(None, str(path), None)

    assert result["code"] == 415
    assert "PDF" in result["msg"]
    assert result["data"]["extension"] == ".docx"


def test_pdf_extension_with_non_pdf_content_is_415(win):
    # Extension lies, header does not: sniffing wins.
    path = win.tmp_path / "fake.pdf"
    path.write_bytes(b"PK\x03\x04still not a pdf")
    result = win.module.print_file(None, str(path), None)
    assert result["code"] == 415


def test_raw_safe_extensions_still_exported(win):
    assert ".pdf" in win.module.RAW_SAFE_EXTENSIONS
    assert ".ps" in win.module.RAW_SAFE_EXTENSIONS


# -------------------------------------------------------------------- errors


def test_missing_pypdfium2_is_501(win, monkeypatch):
    monkeypatch.setitem(sys.modules, "pypdfium2", None)
    result = win.module.print_file(None, _pdf(win.tmp_path), None)

    assert result["code"] == 501
    assert "pypdfium2" in result["msg"]
    assert "uv tool install --reinstall printer-ai-skills" in result["msg"]


def test_password_protected_pdf_is_4xx(win):
    win.pdf_document.open_error = RuntimeError(
        "Failed to load document (PDFium: Incorrect password error)"
    )
    result = win.module.print_file(None, _pdf(win.tmp_path), None)

    assert 400 <= result["code"] < 500
    assert "password" in result["msg"].lower()
    assert not [c for c in win.calls if c[0] == "StartDoc"]


def test_invalid_pdf_is_4xx(win):
    win.pdf_document.open_error = RuntimeError("Failed to load document (PDFium: File access error)")
    result = win.module.print_file(None, _pdf(win.tmp_path), None)
    assert result["code"] == 400
    assert "cannot read PDF" in result["msg"]


def test_render_failure_aborts_the_document_and_cleans_up(win):
    win.pdf_document.render_error = RuntimeError("out of memory")
    result = win.module.print_file(None, _pdf(win.tmp_path), None)

    assert result["code"] == 500
    assert "out of memory" in result["msg"]
    sequence = _names(win.calls, {"StartDoc", "EndDoc", "AbortDoc", "DeleteDC"})
    assert sequence == ["StartDoc", "AbortDoc", "DeleteDC"]
    assert ("ClosePrinter",) in win.calls


def test_unreadable_file_is_404(win):
    result = win.module.print_file(None, str(win.tmp_path / "gone.pdf"), None)
    assert result["code"] == 404


def test_unknown_printer_index_is_404(win):
    result = win.module.print_file(9999, _pdf(win.tmp_path), None)
    assert result["code"] == 404
