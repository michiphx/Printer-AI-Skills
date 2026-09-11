"""Tests for the universal "convert anything to PDF" layer.

Three groups live here:

* **Live conversions** - image->PDF, text->PDF and (guarded by a skipif)
  LibreOffice->PDF really run and the produced file is checked for a `%PDF`
  header. These are the ones that prove the layer works end to end.
* **Detection** - extension routing and magic-byte sniffing, including files
  whose extension lies about their content.
* **Tool discovery** - browser probing, Windows paths and Microsoft Office COM
  are exercised with fakes and a monkeypatched `shutil.which`, because none of
  them can run on this machine.
"""

import os
import shutil
import sys
import types

import pytest

from local_printer import convert


# ==================== helpers ====================


def write(path, text, encoding="utf-8"):
    path.write_text(text, encoding=encoding)
    return str(path)


def is_pdf(path):
    with open(path, "rb") as handle:
        return handle.read(5) == b"%PDF-"


HAS_SOFFICE = convert.find_soffice() is not None
needs_soffice = pytest.mark.skipif(
    not HAS_SOFFICE, reason="LibreOffice (soffice) is not installed on this machine"
)

try:
    import PIL  # noqa: F401

    HAS_PILLOW = True
except ImportError:  # pragma: no cover
    HAS_PILLOW = False

try:
    import reportlab  # noqa: F401

    HAS_REPORTLAB = True
except ImportError:  # pragma: no cover
    HAS_REPORTLAB = False

needs_pillow = pytest.mark.skipif(not HAS_PILLOW, reason="Pillow is not installed")
needs_reportlab = pytest.mark.skipif(
    not HAS_REPORTLAB, reason="reportlab is not installed"
)


@pytest.fixture
def no_external_tools(monkeypatch):
    """Pretend no browser and no LibreOffice exist."""
    monkeypatch.setattr(convert, "find_browser", lambda: None)
    monkeypatch.setattr(convert, "find_soffice", lambda: None)
    monkeypatch.setattr(convert, "_msoffice_com_available", lambda: False)


@pytest.fixture
def no_browser(monkeypatch):
    """Pretend no browser exists, but keep whatever LibreOffice is installed."""
    monkeypatch.setattr(convert, "find_browser", lambda: None)


# ==================== format detection ====================


class TestDetectByExtension:
    @pytest.mark.parametrize(
        "name, expected",
        [
            ("a.pdf", convert.KIND_NATIVE),
            ("a.ps", convert.KIND_NATIVE),
            ("a.prn", convert.KIND_NATIVE),
            ("a.png", convert.KIND_IMAGE),
            ("a.JPEG", convert.KIND_IMAGE),
            ("a.md", convert.KIND_MARKDOWN),
            ("a.html", convert.KIND_HTML),
            ("a.svg", convert.KIND_SVG),
            ("a.csv", convert.KIND_CSV),
            ("a.docx", convert.KIND_OFFICE),
            ("a.xlsx", convert.KIND_OFFICE),
            ("a.odp", convert.KIND_OFFICE),
            ("a.rtf", convert.KIND_OFFICE),
            ("a.py", convert.KIND_TEXT),
            ("a.log", convert.KIND_TEXT),
            ("a.yaml", convert.KIND_TEXT),
        ],
    )
    def test_extension_kinds(self, name, expected):
        assert convert.kind_for_extension(os.path.splitext(name)[1]) == expected

    def test_unknown_extension_is_none(self):
        assert convert.kind_for_extension(".zzz") is None
        assert convert.kind_for_extension("") is None

    def test_known_extension_wins(self, tmp_path):
        path = write(tmp_path / "notes.py", "print('hi')\n")
        assert convert.detect_format(path) == (convert.KIND_TEXT, "extension")


class TestSniffing:
    def test_pdf_magic_without_extension(self, tmp_path):
        path = tmp_path / "mystery"
        path.write_bytes(b"%PDF-1.7\n%stuff\n")
        assert convert.sniff_format(str(path)) == convert.KIND_NATIVE
        assert convert.detect_format(str(path)) == (convert.KIND_NATIVE, "magic")

    def test_postscript_magic(self, tmp_path):
        path = tmp_path / "mystery"
        path.write_bytes(b"%!PS-Adobe-3.0\n")
        assert convert.sniff_format(str(path)) == convert.KIND_NATIVE

    @pytest.mark.parametrize(
        "head",
        [
            b"\x89PNG\r\n\x1a\n" + b"\x00" * 16,
            b"\xff\xd8\xff\xe0" + b"\x00" * 16,
            b"GIF89a" + b"\x00" * 16,
            b"II*\x00" + b"\x00" * 16,
            b"MM\x00*" + b"\x00" * 16,
            b"BM" + b"\x00" * 16,
            b"RIFF\x00\x00\x00\x00WEBPVP8 ",
        ],
    )
    def test_image_magics(self, tmp_path, head):
        path = tmp_path / "mystery"
        path.write_bytes(head)
        assert convert.sniff_format(str(path)) == convert.KIND_IMAGE

    def test_ooxml_zip_is_office(self, tmp_path):
        import zipfile

        path = tmp_path / "mystery"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("[Content_Types].xml", "<Types/>")
            archive.writestr("word/document.xml", "<w/>")
        assert convert.sniff_format(str(path)) == convert.KIND_OFFICE

    def test_odf_zip_is_office(self, tmp_path):
        import zipfile

        path = tmp_path / "mystery"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("mimetype", "application/vnd.oasis.opendocument.text")
        assert convert.sniff_format(str(path)) == convert.KIND_OFFICE

    def test_plain_zip_is_unsupported(self, tmp_path):
        import zipfile

        path = tmp_path / "mystery"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("hello.txt", "hi")
        assert convert.sniff_format(str(path)) == convert.KIND_UNSUPPORTED

    def test_ole2_is_office(self, tmp_path):
        path = tmp_path / "mystery"
        path.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 32)
        assert convert.sniff_format(str(path)) == convert.KIND_OFFICE

    def test_utf8_is_text(self, tmp_path):
        path = tmp_path / "mystery"
        path.write_bytes("héllo wörld\n".encode("utf-8"))
        assert convert.sniff_format(str(path)) == convert.KIND_TEXT

    def test_binary_is_unsupported(self, tmp_path):
        path = tmp_path / "mystery"
        path.write_bytes(b"\x00\x01\x02\xff\xfe\xfd" * 8)
        assert convert.sniff_format(str(path)) == convert.KIND_UNSUPPORTED

    def test_html_and_svg_sniffed(self, tmp_path):
        html = tmp_path / "a"
        html.write_bytes(b"<!DOCTYPE html><html><body>hi</body></html>")
        assert convert.sniff_format(str(html)) == convert.KIND_HTML

        svg = tmp_path / "b"
        svg.write_bytes(b'<svg xmlns="http://www.w3.org/2000/svg"></svg>')
        assert convert.sniff_format(str(svg)) == convert.KIND_SVG

    def test_misleading_txt_extension_is_overridden(self, tmp_path):
        path = tmp_path / "report.txt"
        path.write_bytes(b"%PDF-1.4\n%stuff\n")
        kind, how = convert.detect_format(str(path))
        assert kind == convert.KIND_NATIVE
        assert how == "magic-override"


# ==================== page size ====================


class TestPageSize:
    def test_env_override_wins(self, monkeypatch):
        monkeypatch.setenv("PRINTER_AI_PAGE_SIZE", "Letter")
        assert convert.page_size() == convert.PAGE_SIZES["LETTER"]
        monkeypatch.setenv("PRINTER_AI_PAGE_SIZE", "a4")
        assert convert.page_size() == convert.PAGE_SIZES["A4"]

    def test_us_locale_gets_letter(self, monkeypatch):
        monkeypatch.delenv("PRINTER_AI_PAGE_SIZE", raising=False)
        monkeypatch.setenv("LC_ALL", "en_US.UTF-8")
        assert convert.page_size() == convert.PAGE_SIZES["LETTER"]
        assert convert.page_size_name() == "Letter"

    def test_canadian_locale_gets_letter(self, monkeypatch):
        monkeypatch.delenv("PRINTER_AI_PAGE_SIZE", raising=False)
        monkeypatch.setenv("LC_ALL", "en_CA.UTF-8")
        assert convert.page_size() == convert.PAGE_SIZES["LETTER"]

    def test_other_locale_gets_a4(self, monkeypatch):
        monkeypatch.delenv("PRINTER_AI_PAGE_SIZE", raising=False)
        monkeypatch.setenv("LC_ALL", "de_DE.UTF-8")
        assert convert.page_size() == convert.PAGE_SIZES["A4"]
        assert convert.page_size_name() == "A4"

    def test_unknown_override_falls_back_to_locale(self, monkeypatch):
        monkeypatch.setenv("PRINTER_AI_PAGE_SIZE", "Foolscap")
        monkeypatch.setenv("LC_ALL", "de_DE.UTF-8")
        assert convert.page_size() == convert.PAGE_SIZES["A4"]


# ==================== live: text -> PDF ====================


@needs_reportlab
class TestTextConversion:
    def test_text_file_becomes_pdf(self, tmp_path):
        source = write(tmp_path / "notes.txt", "hello world\nsecond line\n")
        result = convert.to_pdf(source, out_dir=str(tmp_path / "out"))

        assert result.converter == "text"
        assert result.converted is True
        assert result.native is False
        assert result.temp is False
        assert result.pdf_path.endswith("notes.pdf")
        assert is_pdf(result.pdf_path)

    def test_long_lines_are_wrapped_over_pages(self, tmp_path):
        body = "\n".join("x" * 400 for _ in range(400))
        source = write(tmp_path / "big.log", body)
        result = convert.to_pdf(source, out_dir=str(tmp_path / "out"))
        assert is_pdf(result.pdf_path)
        # 400 very long lines cannot fit on a single page
        assert "page(s)" in " ".join(result.notes)

    def test_tabs_are_expanded(self):
        lines = convert.TextConverter()._wrap("a\tb", columns=80)
        assert lines == ["a   b"]

    def test_unknown_extension_utf8_is_text(self, tmp_path):
        source = write(tmp_path / "weird.zzz", "just some text\n")
        result = convert.to_pdf(source, out_dir=str(tmp_path / "out"))
        assert result.converter == "text"
        assert is_pdf(result.pdf_path)

    def test_read_text_survives_bad_bytes(self, tmp_path):
        path = tmp_path / "latin.txt"
        path.write_bytes(b"caf\xe9 time\n")
        text = convert.TextConverter.read_text(str(path))
        assert "caf" in text

    def test_source_without_extension(self, tmp_path):
        source = tmp_path / "README"
        source.write_text("plain content\n")
        result = convert.to_pdf(str(source), out_dir=str(tmp_path / "out"))
        assert result.pdf_path.endswith("README.pdf")


# ==================== live: image -> PDF ====================


@needs_pillow
@needs_reportlab
class TestImageConversion:
    def _png(self, tmp_path, mode="RGB", size=(120, 80), name="pic.png"):
        from PIL import Image

        path = tmp_path / name
        Image.new(mode, size, 255 if mode in ("L", "1") else None).save(path)
        return str(path)

    def test_rgb_png(self, tmp_path):
        from PIL import Image

        path = tmp_path / "pic.png"
        Image.new("RGB", (200, 100), (10, 120, 200)).save(path)
        result = convert.to_pdf(str(path), out_dir=str(tmp_path / "out"))

        assert result.converter == "image"
        assert result.converted is True
        assert is_pdf(result.pdf_path)
        assert "scaled to fit" in " ".join(result.notes)

    @pytest.mark.parametrize("mode", ["RGBA", "P", "LA", "CMYK", "I;16", "L", "1"])
    def test_awkward_modes_are_flattened(self, tmp_path, mode):
        from PIL import Image

        path = tmp_path / f"pic_{mode.replace(';', '_')}.png"
        image = Image.new("RGBA", (40, 30), (255, 0, 0, 128)).convert(
            mode if mode != "I;16" else "I;16"
        )
        # PNG cannot hold CMYK; use TIFF for the modes PNG refuses.
        if mode in ("CMYK", "I;16"):
            path = path.with_suffix(".tiff")
        image.save(path)

        result = convert.to_pdf(str(path), out_dir=str(tmp_path / "out"))
        assert is_pdf(result.pdf_path)

    def test_multi_frame_gif_becomes_multi_page(self, tmp_path):
        from PIL import Image

        path = tmp_path / "anim.gif"
        frames = [
            Image.new("RGB", (60, 40), (0, 0, 0)).convert("P"),
            Image.new("RGB", (60, 40), (255, 0, 0)).convert("P"),
            Image.new("RGB", (60, 40), (0, 255, 0)).convert("P"),
        ]
        frames[0].save(path, save_all=True, append_images=frames[1:])

        result = convert.to_pdf(str(path), out_dir=str(tmp_path / "out"))
        assert is_pdf(result.pdf_path)
        assert "3 image frame(s)" in " ".join(result.notes)

    def test_multi_page_tiff(self, tmp_path):
        from PIL import Image

        path = tmp_path / "scan.tiff"
        frames = [Image.new("RGB", (50, 70), (i * 40, 0, 0)) for i in range(2)]
        frames[0].save(path, save_all=True, append_images=frames[1:])

        result = convert.to_pdf(str(path), out_dir=str(tmp_path / "out"))
        assert "2 image frame(s)" in " ".join(result.notes)

    def test_broken_image_raises_with_hint(self, tmp_path):
        path = tmp_path / "broken.png"
        path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"garbage" * 4)
        with pytest.raises(convert.ConversionError) as exc_info:
            convert.to_pdf(str(path), out_dir=str(tmp_path / "out"))
        assert exc_info.value.hint

    def test_letter_page_size_is_honoured(self, tmp_path, monkeypatch):
        from PIL import Image

        monkeypatch.setenv("PRINTER_AI_PAGE_SIZE", "Letter")
        path = tmp_path / "pic.png"
        Image.new("RGB", (100, 100), (0, 0, 0)).save(path)
        result = convert.to_pdf(str(path), out_dir=str(tmp_path / "out"))
        assert "Letter" in " ".join(result.notes)


# ==================== live: markdown ====================


class TestMarkdown:
    def test_to_html_renders_fenced_code_and_tables(self, tmp_path):
        source = write(
            tmp_path / "doc.md",
            "# Title\n\ntext\n\n```python\nx = 1\n```\n\n| a | b |\n|---|---|\n| 1 | 2 |\n",
        )
        html = convert.MarkdownConverter().to_html(source)

        assert "<h1>Title</h1>" in html
        assert "<table>" in html
        assert "<code" in html
        assert "<!DOCTYPE html>" in html
        assert "doc.md" in html  # the <title>
        assert "font-family" in html  # embedded CSS

    def test_falls_back_to_text_without_any_html_renderer(
        self, tmp_path, no_external_tools
    ):
        source = write(tmp_path / "doc.md", "# Title\n\nbody\n")
        result = convert.to_pdf(source, out_dir=str(tmp_path / "out"))

        assert result.converter == "markdown"
        assert is_pdf(result.pdf_path)
        assert any("plain text" in note for note in result.notes)

    @needs_soffice
    def test_markdown_without_browser_uses_libreoffice(self, tmp_path, no_browser):
        source = write(tmp_path / "doc.md", "# Title\n\nbody text\n")
        result = convert.to_pdf(source, out_dir=str(tmp_path / "out"))
        assert result.converter == "markdown"
        assert is_pdf(result.pdf_path)

    def test_missing_markdown_package_raises(self, tmp_path, monkeypatch):
        source = write(tmp_path / "doc.md", "# Title\n")
        real_import = __import__

        def fake_import(name, *args, **kwargs):
            if name == "markdown":
                raise ImportError("no markdown")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr("builtins.__import__", fake_import)
        ok, detail = convert.MarkdownConverter().available()
        assert ok is False
        assert "markdown" in detail


# ==================== live: LibreOffice ====================


@needs_soffice
class TestLibreOffice:
    def test_rtf_becomes_pdf(self, tmp_path):
        source = tmp_path / "hello.rtf"
        source.write_text(r"{\rtf1\ansi Hello}", encoding="ascii")

        result = convert.to_pdf(str(source), out_dir=str(tmp_path / "out"))

        assert result.converter == "office"
        assert result.converted is True
        assert result.pdf_path.endswith("hello.pdf")
        assert is_pdf(result.pdf_path)

    def test_csv_uses_the_spreadsheet_path(self, tmp_path):
        source = write(tmp_path / "data.csv", "a,b,c\n1,2,3\n4,5,6\n")
        result = convert.to_pdf(source, out_dir=str(tmp_path / "out"))

        assert result.converter == "csv"
        assert is_pdf(result.pdf_path)
        assert any("LibreOffice" in note for note in result.notes)

    def test_private_profile_is_passed(self, tmp_path, monkeypatch):
        """The -env:UserInstallation profile is not optional - without it a
        conversion started while a LibreOffice window is open does nothing."""
        captured = {}

        def fake_run(cmd, timeout, what):
            captured["cmd"] = cmd
            # Pretend LibreOffice wrote its PDF into --outdir
            outdir = cmd[cmd.index("--outdir") + 1]
            with open(os.path.join(outdir, "x.pdf"), "wb") as handle:
                handle.write(b"%PDF-1.4\n")

        monkeypatch.setattr(convert, "_run_tool", fake_run)
        source = write(tmp_path / "doc.odt", "x")
        convert.OfficeConverter().convert_with_libreoffice(
            source, str(tmp_path), []
        )

        cmd = captured["cmd"]
        assert any(part.startswith("-env:UserInstallation=file:///") for part in cmd)
        assert "--headless" in cmd
        assert "--norestore" in cmd
        assert "--nologo" in cmd
        assert cmd[cmd.index("--convert-to") + 1] == "pdf"


class TestLibreOfficeWithoutIt:
    def test_office_document_without_libreoffice_is_415_with_hint(
        self, tmp_path, no_external_tools
    ):
        import zipfile

        path = tmp_path / "report.docx"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("[Content_Types].xml", "<Types/>")

        with pytest.raises(convert.ConversionError) as exc_info:
            convert.to_pdf(str(path), out_dir=str(tmp_path / "out"))

        assert "libreoffice.org" in exc_info.value.hint.lower()

    def test_csv_without_libreoffice_falls_back_to_text(
        self, tmp_path, no_external_tools
    ):
        source = write(tmp_path / "data.csv", "a,b\n1,2\n")
        result = convert.to_pdf(source, out_dir=str(tmp_path / "out"))
        assert is_pdf(result.pdf_path)
        assert any("plain text" in note for note in result.notes)


# ==================== browser probing ====================


class TestBrowserDiscovery:
    def test_env_override_by_absolute_path(self, tmp_path, monkeypatch):
        fake = tmp_path / "my-browser"
        fake.write_text("#!/bin/sh\n")
        monkeypatch.setenv("PRINTER_AI_BROWSER", str(fake))
        assert convert.find_browser() == str(fake)

    def test_env_override_by_name_on_path(self, monkeypatch):
        monkeypatch.setenv("PRINTER_AI_BROWSER", "weirdbrowser")
        monkeypatch.setattr(
            shutil, "which", lambda name: "/opt/wb" if name == "weirdbrowser" else None
        )
        assert convert.find_browser() == "/opt/wb"

    def test_linux_candidates_in_order(self, monkeypatch):
        monkeypatch.delenv("PRINTER_AI_BROWSER", raising=False)
        monkeypatch.setattr(convert, "_is_windows", lambda: False)
        monkeypatch.setattr(convert, "_is_macos", lambda: False)
        found = {"chromium": None, "google-chrome": "/usr/bin/google-chrome"}
        monkeypatch.setattr(shutil, "which", lambda name: found.get(name))
        assert convert.find_browser() == "/usr/bin/google-chrome"

    def test_nothing_found(self, monkeypatch):
        monkeypatch.delenv("PRINTER_AI_BROWSER", raising=False)
        monkeypatch.setattr(convert, "_is_windows", lambda: False)
        monkeypatch.setattr(convert, "_is_macos", lambda: False)
        monkeypatch.setattr(shutil, "which", lambda name: None)
        assert convert.find_browser() is None

    def test_macos_app_bundle(self, monkeypatch, tmp_path):
        monkeypatch.delenv("PRINTER_AI_BROWSER", raising=False)
        monkeypatch.setattr(convert, "_is_windows", lambda: False)
        monkeypatch.setattr(convert, "_is_macos", lambda: True)
        monkeypatch.setattr(shutil, "which", lambda name: None)

        edge = "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"
        monkeypatch.setattr(os.path, "isfile", lambda p: p == edge)
        assert convert.find_browser() == edge

    def test_windows_prefers_edge_under_program_files(self, monkeypatch):
        monkeypatch.delenv("PRINTER_AI_BROWSER", raising=False)
        monkeypatch.setattr(convert, "_is_windows", lambda: True)
        monkeypatch.setenv("ProgramFiles", r"C:\Program Files")
        monkeypatch.setenv("LocalAppData", r"C:\Users\me\AppData\Local")

        # os.path.join uses this host's separator, so build the expectation the
        # same way find_browser does rather than hard-coding backslashes.
        edge = os.path.join(
            r"C:\Program Files", r"Microsoft\Edge\Application\msedge.exe"
        )
        chrome = os.path.join(
            r"C:\Program Files", r"Google\Chrome\Application\chrome.exe"
        )
        monkeypatch.setattr(os.path, "isfile", lambda p: p in (edge, chrome))
        assert convert.find_browser() == edge

    def test_windows_falls_back_to_chrome_in_localappdata(self, monkeypatch):
        monkeypatch.delenv("PRINTER_AI_BROWSER", raising=False)
        monkeypatch.setattr(convert, "_is_windows", lambda: True)
        monkeypatch.setenv("ProgramFiles", r"C:\Program Files")
        monkeypatch.setenv("LocalAppData", r"C:\Users\me\AppData\Local")

        chrome = os.path.join(
            r"C:\Users\me\AppData\Local", r"Google\Chrome\Application\chrome.exe"
        )
        monkeypatch.setattr(os.path, "isfile", lambda p: p == chrome)
        assert convert.find_browser() == chrome

    def test_browser_command_line(self, tmp_path, monkeypatch):
        captured = {}

        def fake_run(cmd, timeout, what):
            captured["cmd"] = cmd
            target = [c for c in cmd if c.startswith("--print-to-pdf=")][0].split("=", 1)[1]
            with open(target, "wb") as handle:
                handle.write(b"%PDF-1.4\n")

        monkeypatch.setattr(convert, "find_browser", lambda: "/usr/bin/chromium")
        monkeypatch.setattr(convert, "_run_tool", fake_run)

        source = write(tmp_path / "page.html", "<html><body>hi</body></html>")
        result = convert.to_pdf(source, out_dir=str(tmp_path / "out"))

        cmd = captured["cmd"]
        assert cmd[0] == "/usr/bin/chromium"
        assert "--headless=new" in cmd
        assert "--disable-gpu" in cmd
        assert "--no-pdf-header-footer" in cmd
        assert any(c.startswith("--user-data-dir=") for c in cmd)
        assert cmd[-1].startswith("file:///")
        assert result.converter == "browser"
        assert is_pdf(result.pdf_path)

    def test_svg_goes_through_the_browser(self, tmp_path, monkeypatch):
        def fake_run(cmd, timeout, what):
            target = [c for c in cmd if c.startswith("--print-to-pdf=")][0].split("=", 1)[1]
            with open(target, "wb") as handle:
                handle.write(b"%PDF-1.4\n")

        monkeypatch.setattr(convert, "find_browser", lambda: "/usr/bin/chromium")
        monkeypatch.setattr(convert, "_run_tool", fake_run)

        source = write(tmp_path / "logo.svg", '<svg xmlns="http://www.w3.org/2000/svg"/>')
        result = convert.to_pdf(source, out_dir=str(tmp_path / "out"))
        assert result.converter == "browser"

    def test_html_without_browser_or_libreoffice_is_415(self, tmp_path, no_external_tools):
        source = write(tmp_path / "page.html", "<html></html>")
        with pytest.raises(convert.ConversionError) as exc_info:
            convert.to_pdf(source, out_dir=str(tmp_path / "out"))
        assert "chrome" in exc_info.value.hint.lower()

    def test_failing_browser_falls_back_to_libreoffice(self, tmp_path, monkeypatch):
        monkeypatch.setattr(convert, "find_browser", lambda: "/usr/bin/chromium")
        monkeypatch.setattr(convert, "find_soffice", lambda: "/usr/bin/soffice")

        def boom(cmd, timeout, what):
            raise convert.ConversionError("browser exploded")

        monkeypatch.setattr(convert, "_run_tool", boom)

        calls = []

        def fake_lo(self, source, out_dir, notes):
            calls.append(source)
            target = os.path.join(out_dir, "page.pdf")
            with open(target, "wb") as handle:
                handle.write(b"%PDF-1.4\n")
            return target

        monkeypatch.setattr(
            convert.OfficeConverter, "convert_with_libreoffice", fake_lo
        )

        source = write(tmp_path / "page.html", "<html></html>")
        out_dir = tmp_path / "out"
        out_dir.mkdir()
        notes = []
        produced = convert.BrowserConverter().convert(source, str(out_dir), notes)

        assert calls == [source]
        assert is_pdf(produced)
        assert any("LibreOffice" in note for note in notes)


# ==================== Microsoft Office via COM ====================


class FakeCOMDocument:
    def __init__(self, log, kind):
        self.log = log
        self.kind = kind

    def ExportAsFixedFormat(self, *args):
        self.log.append(("export", self.kind, args))
        # Word: (path, 17); Excel: (0, path)
        path = args[0] if isinstance(args[0], str) else args[1]
        with open(path, "wb") as handle:
            handle.write(b"%PDF-1.5\n")

    def SaveAs(self, path, fmt):
        self.log.append(("saveas", self.kind, (path, fmt)))
        with open(path, "wb") as handle:
            handle.write(b"%PDF-1.5\n")

    def Close(self, *args):
        self.log.append(("close", self.kind, args))


class FakeCOMCollection:
    def __init__(self, log, kind):
        self.log = log
        self.kind = kind

    def Open(self, path, **kwargs):
        self.log.append(("open", self.kind, path, kwargs))
        return FakeCOMDocument(self.log, self.kind)


class FakeCOMApp:
    def __init__(self, log, kind):
        self.log = log
        self.kind = kind
        self.Visible = True
        self.DisplayAlerts = True

    @property
    def Documents(self):
        return FakeCOMCollection(self.log, self.kind)

    @property
    def Workbooks(self):
        return FakeCOMCollection(self.log, self.kind)

    @property
    def Presentations(self):
        return FakeCOMCollection(self.log, self.kind)

    def Quit(self):
        self.log.append(("quit", self.kind, None))


@pytest.fixture
def fake_com(monkeypatch):
    """Install fake pywin32 COM modules so the Windows path can be tested here."""
    log = []

    pythoncom = types.ModuleType("pythoncom")
    pythoncom.CoInitialize = lambda: log.append(("coinit", None, None))
    pythoncom.CoUninitialize = lambda: log.append(("councinit", None, None))

    client = types.ModuleType("win32com.client")

    def dispatch(prog_id):
        log.append(("dispatch", prog_id, None))
        return FakeCOMApp(log, prog_id)

    client.DispatchEx = dispatch
    win32com = types.ModuleType("win32com")
    win32com.client = client

    monkeypatch.setitem(sys.modules, "pythoncom", pythoncom)
    monkeypatch.setitem(sys.modules, "win32com", win32com)
    monkeypatch.setitem(sys.modules, "win32com.client", client)
    return log


class TestOfficeCOM:
    def test_com_unavailable_off_windows(self, monkeypatch):
        monkeypatch.setattr(convert, "_is_windows", lambda: False)
        assert convert._msoffice_com_available() is False

    def test_com_available_on_windows_with_pywin32(self, monkeypatch, fake_com):
        monkeypatch.setattr(convert, "_is_windows", lambda: True)
        assert convert._msoffice_com_available() is True

    def test_word_export(self, tmp_path, fake_com):
        source = write(tmp_path / "letter.docx", "x")
        target = str(tmp_path / "letter.pdf")
        convert.OfficeConverter().convert_with_com(source, target)

        assert ("dispatch", "Word.Application", None) in fake_com
        export = [e for e in fake_com if e[0] == "export"][0]
        assert export[2][1] == 17  # wdExportFormatPDF
        assert ("quit", "Word.Application", None) in fake_com
        assert is_pdf(target)

    def test_excel_export(self, tmp_path, fake_com):
        source = write(tmp_path / "sheet.xlsx", "x")
        target = str(tmp_path / "sheet.pdf")
        convert.OfficeConverter().convert_with_com(source, target)

        assert ("dispatch", "Excel.Application", None) in fake_com
        export = [e for e in fake_com if e[0] == "export"][0]
        assert export[2][0] == 0  # xlTypePDF
        assert ("quit", "Excel.Application", None) in fake_com
        assert is_pdf(target)

    def test_powerpoint_export_without_window(self, tmp_path, fake_com):
        source = write(tmp_path / "deck.pptx", "x")
        target = str(tmp_path / "deck.pdf")
        convert.OfficeConverter().convert_with_com(source, target)

        opened = [e for e in fake_com if e[0] == "open"][0]
        assert opened[3]["WithWindow"] is False
        saved = [e for e in fake_com if e[0] == "saveas"][0]
        assert saved[2][1] == 32  # ppSaveAsPDF
        assert ("quit", "PowerPoint.Application", None) in fake_com

    def test_app_is_quit_even_when_export_fails(self, tmp_path, fake_com, monkeypatch):
        def explode(self, *args):
            raise RuntimeError("Office is sulking")

        monkeypatch.setattr(FakeCOMDocument, "ExportAsFixedFormat", explode)
        source = write(tmp_path / "letter.docx", "x")
        with pytest.raises(RuntimeError):
            convert.OfficeConverter().convert_with_com(source, str(tmp_path / "o.pdf"))
        assert ("quit", "Word.Application", None) in fake_com

    def test_unknown_extension_for_com(self, tmp_path, fake_com):
        source = write(tmp_path / "thing.odg", "x")
        with pytest.raises(convert.ConversionError):
            convert.OfficeConverter().convert_with_com(source, str(tmp_path / "o.pdf"))

    def test_com_is_preferred_then_falls_back(self, tmp_path, monkeypatch, fake_com):
        """When COM blows up, LibreOffice still gets its turn."""
        monkeypatch.setattr(convert, "_msoffice_com_available", lambda: True)

        def explode(self, *args):
            raise RuntimeError("no Office installed")

        monkeypatch.setattr(FakeCOMDocument, "ExportAsFixedFormat", explode)

        used = []

        def fake_lo(self, source, out_dir, notes):
            used.append(source)
            target = os.path.join(out_dir, "letter.pdf")
            with open(target, "wb") as handle:
                handle.write(b"%PDF-1.4\n")
            notes.append("converted by LibreOffice (fake)")
            return target

        monkeypatch.setattr(convert.OfficeConverter, "convert_with_libreoffice", fake_lo)

        source = write(tmp_path / "letter.docx", "x")
        out_dir = tmp_path / "out"
        out_dir.mkdir()
        notes = []
        produced = convert.OfficeConverter().convert(source, str(out_dir), notes)

        assert used == [source]
        assert is_pdf(produced)
        assert any("unusable" in note for note in notes)


# ==================== soffice discovery ====================


class TestSofficeDiscovery:
    def test_path_lookup_first(self, monkeypatch):
        monkeypatch.delenv("PRINTER_AI_SOFFICE", raising=False)
        monkeypatch.setattr(
            shutil, "which", lambda name: "/usr/bin/soffice" if name == "soffice" else None
        )
        assert convert.find_soffice() == "/usr/bin/soffice"

    def test_windows_program_files(self, monkeypatch):
        monkeypatch.delenv("PRINTER_AI_SOFFICE", raising=False)
        monkeypatch.setattr(shutil, "which", lambda name: None)
        monkeypatch.setattr(convert, "_is_windows", lambda: True)
        monkeypatch.setattr(convert, "_is_macos", lambda: False)
        expected = r"C:\Program Files\LibreOffice\program\soffice.exe"
        monkeypatch.setattr(os.path, "isfile", lambda p: p == expected)
        assert convert.find_soffice() == expected

    def test_macos_app_bundle(self, monkeypatch):
        monkeypatch.delenv("PRINTER_AI_SOFFICE", raising=False)
        monkeypatch.setattr(shutil, "which", lambda name: None)
        monkeypatch.setattr(convert, "_is_windows", lambda: False)
        monkeypatch.setattr(convert, "_is_macos", lambda: True)
        expected = "/Applications/LibreOffice.app/Contents/MacOS/soffice"
        monkeypatch.setattr(os.path, "isfile", lambda p: p == expected)
        assert convert.find_soffice() == expected

    def test_snap_path(self, monkeypatch):
        monkeypatch.delenv("PRINTER_AI_SOFFICE", raising=False)
        monkeypatch.setattr(shutil, "which", lambda name: None)
        monkeypatch.setattr(convert, "_is_windows", lambda: False)
        monkeypatch.setattr(convert, "_is_macos", lambda: False)
        monkeypatch.setattr(os.path, "isfile", lambda p: p == "/snap/bin/libreoffice")
        assert convert.find_soffice() == "/snap/bin/libreoffice"

    def test_nothing_found(self, monkeypatch):
        monkeypatch.delenv("PRINTER_AI_SOFFICE", raising=False)
        monkeypatch.setattr(shutil, "which", lambda name: None)
        monkeypatch.setattr(convert, "_is_windows", lambda: False)
        monkeypatch.setattr(convert, "_is_macos", lambda: False)
        monkeypatch.setattr(os.path, "isfile", lambda p: False)
        assert convert.find_soffice() is None


# ==================== passthrough, errors, temp handling ====================


class TestPassthrough:
    def test_pdf_is_not_touched(self, tmp_path):
        path = tmp_path / "already.pdf"
        path.write_bytes(b"%PDF-1.4\n%stuff\n")

        result = convert.to_pdf(str(path))

        assert result.converted is False
        assert result.native is True
        assert result.temp is False
        assert result.pdf_path == str(path)

    def test_postscript_is_native(self, tmp_path):
        path = tmp_path / "job.ps"
        path.write_bytes(b"%!PS-Adobe-3.0\n")
        result = convert.to_pdf(str(path))
        assert result.native is True

    def test_cleanup_is_a_no_op_for_native(self, tmp_path):
        path = tmp_path / "already.pdf"
        path.write_bytes(b"%PDF-1.4\n")
        result = convert.to_pdf(str(path))
        convert.cleanup(result)
        assert path.exists()


class TestErrors:
    def test_missing_file(self, tmp_path):
        with pytest.raises(convert.ConversionError) as exc_info:
            convert.to_pdf(str(tmp_path / "nope.txt"))
        assert "not found" in str(exc_info.value)
        assert exc_info.value.hint

    def test_empty_file(self, tmp_path):
        path = tmp_path / "empty.docx"
        path.write_bytes(b"")
        with pytest.raises(convert.ConversionError) as exc_info:
            convert.to_pdf(str(path))
        assert "empty" in str(exc_info.value)

    def test_unsupported_binary(self, tmp_path):
        path = tmp_path / "thing.bin"
        path.write_bytes(b"\x00\x01\xff\xfe" * 32)
        with pytest.raises(convert.ConversionError) as exc_info:
            convert.to_pdf(str(path))
        assert exc_info.value.detected == convert.KIND_UNSUPPORTED
        assert "--raw" in exc_info.value.hint

    def test_error_data_shape(self, tmp_path):
        with pytest.raises(convert.ConversionError) as exc_info:
            convert.to_pdf(str(tmp_path / "nope.txt"))
        data = exc_info.value.to_data()
        assert set(data) == {"file_path", "detected", "converter", "hint"}

    def test_timeout_becomes_a_conversion_error(self, monkeypatch):
        import subprocess

        def timeout(*args, **kwargs):
            raise subprocess.TimeoutExpired(cmd="x", timeout=1)

        monkeypatch.setattr(subprocess, "run", timeout)
        with pytest.raises(convert.ConversionError) as exc_info:
            convert._run_tool(["x"], 1.0, "thing")
        assert "timed out" in str(exc_info.value)

    def test_nonzero_exit_becomes_a_conversion_error(self, monkeypatch):
        import subprocess

        class Proc:
            returncode = 3
            stdout = b"it broke"

        monkeypatch.setattr(subprocess, "run", lambda *a, **k: Proc())
        with pytest.raises(convert.ConversionError) as exc_info:
            convert._run_tool(["x"], 1.0, "thing")
        assert "exit code 3" in str(exc_info.value)

    def test_run_tool_never_uses_a_shell(self, monkeypatch):
        import subprocess

        captured = {}

        class Proc:
            returncode = 0
            stdout = b""

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            captured["kwargs"] = kwargs
            return Proc()

        monkeypatch.setattr(subprocess, "run", fake_run)
        convert._run_tool(["a", "b c"], 1.0, "thing")
        assert captured["cmd"] == ["a", "b c"]
        assert captured["kwargs"].get("shell") in (None, False)


@needs_reportlab
class TestTempHandling:
    def test_temp_dir_is_used_and_cleaned(self, tmp_path):
        source = write(tmp_path / "notes.txt", "hello\n")
        result = convert.to_pdf(source)

        assert result.temp is True
        assert os.path.isfile(result.pdf_path)
        directory = os.path.dirname(result.pdf_path)

        convert.cleanup(result)
        assert not os.path.exists(directory)
        # the source survives
        assert os.path.isfile(source)

    def test_cleanup_is_idempotent(self, tmp_path):
        source = write(tmp_path / "notes.txt", "hello\n")
        result = convert.to_pdf(source)
        convert.cleanup(result)
        convert.cleanup(result)
        convert.cleanup(None)

    def test_cleanup_refuses_foreign_directories(self, tmp_path):
        keep = tmp_path / "precious"
        keep.mkdir()
        pdf = keep / "x.pdf"
        pdf.write_bytes(b"%PDF-1.4\n")
        result = convert.ConvertResult(
            pdf_path=str(pdf), source_path=str(pdf), converter="text",
            converted=True, temp=True,
        )
        convert.cleanup(result)
        assert keep.exists()

    def test_out_dir_is_created(self, tmp_path):
        source = write(tmp_path / "notes.txt", "hello\n")
        out = tmp_path / "deep" / "nested"
        result = convert.to_pdf(source, out_dir=str(out))
        assert result.temp is False
        assert result.pdf_path == str(out / "notes.pdf")


# ==================== catalog ====================


class TestFormatCatalog:
    def test_every_converter_is_listed(self):
        catalog = convert.format_catalog()
        names = [entry["converter"] for entry in catalog]
        assert names == [c.name for c in convert.REGISTRY]

    def test_passthrough_is_always_available(self):
        entry = next(
            e for e in convert.format_catalog() if e["converter"] == "passthrough"
        )
        assert entry["available"] is True
        assert entry["via"] == "built-in"
        assert ".pdf" in entry["extensions"]

    def test_unavailable_entries_carry_an_install_hint(self, no_external_tools):
        catalog = convert.format_catalog()
        for entry in catalog:
            if not entry["available"]:
                assert entry["install_hint"]
        office = next(e for e in catalog if e["converter"] == "office")
        assert office["available"] is False
        assert "libreoffice.org" in office["install_hint"].lower()

    def test_result_serialises(self, tmp_path):
        path = tmp_path / "a.pdf"
        path.write_bytes(b"%PDF-1.4\n")
        data = convert.to_pdf(str(path)).to_dict()
        assert data["native"] is True
        assert data["converter"] == "passthrough"
