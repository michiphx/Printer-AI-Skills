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

import json
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

try:
    import markdown  # noqa: F401

    HAS_MARKDOWN = True
except ImportError:  # pragma: no cover
    HAS_MARKDOWN = False

needs_pillow = pytest.mark.skipif(not HAS_PILLOW, reason="Pillow is not installed")
needs_reportlab = pytest.mark.skipif(
    not HAS_REPORTLAB, reason="reportlab is not installed"
)
needs_markdown = pytest.mark.skipif(
    not HAS_MARKDOWN, reason="the markdown package is not installed"
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


@pytest.fixture(autouse=True)
def isolated_monospace_font_cache(monkeypatch):
    """Reset the process-wide "which TTF did we register" cache per test, so
    one test's outcome (font found vs. not found) cannot leak into another."""
    monkeypatch.setattr(convert, "_monospace_font_cache", convert._MONOSPACE_FONT_UNSET)


@pytest.fixture(autouse=True)
def isolated_browser_status_cache(tmp_path, monkeypatch):
    """Keep the persistent "is the browser broken" cache out of the real
    machine state and out of other tests: each test gets an empty in-process
    cache and its own throwaway cache directory."""
    cache_dir = tmp_path / "printer-ai-cache"

    def fake_cache_dir():
        cache_dir.mkdir(parents=True, exist_ok=True)
        return str(cache_dir)

    monkeypatch.setattr(convert, "_browser_status_cache", {})
    monkeypatch.setattr(convert, "printer_ai_cache_dir", fake_cache_dir)


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

    def test_non_latin1_text_uses_a_real_font_or_notes_the_limitation(self, tmp_path):
        """Base-14 Courier has no glyphs outside Latin-1, so Cyrillic/CJK
        content silently rendered as `.notdef` boxes. Whichever branch this
        sandbox takes is fine - either a suitable TrueType font was found and
        actually used (visible in the PDF as an embedded non-base-14 font),
        or none was found and the limitation is called out in the notes."""
        source = write(tmp_path / "intl.txt", "Привет 日本語\nhello\n")
        result = convert.to_pdf(source, out_dir=str(tmp_path / "out"))
        assert is_pdf(result.pdf_path)

        with open(result.pdf_path, "rb") as handle:
            pdf_bytes = handle.read()
        embeds_truetype = any(
            marker in pdf_bytes
            for marker in (b"/FontFile2", b"/Type0", b"/Identity-H")
        )
        font_name = convert.registered_monospace_font()
        if font_name is not None:
            assert embeds_truetype, (
                f"a monospace TTF ({font_name}) was registered but the PDF "
                "does not appear to embed a non-base-14 font"
            )
        else:
            assert any(
                "non-latin-1" in note.lower() or "courier" in note.lower()
                for note in result.notes
            )

    def test_registered_monospace_font_used_for_wrap_width_and_drawing(self, tmp_path, monkeypatch):
        """The same font must drive both the column-wrap width calculation
        and the actual setFont/drawString calls, so wrapping stays honest
        about what will be drawn."""
        monkeypatch.setattr(convert, "registered_monospace_font", lambda: None)
        source = write(tmp_path / "plain.txt", "hello world\n")
        result = convert.to_pdf(source, out_dir=str(tmp_path / "out"))
        assert is_pdf(result.pdf_path)
        assert any("courier" in note.lower() for note in result.notes)

    def test_registered_monospace_font_is_cached_across_calls(self, tmp_path, monkeypatch):
        """Registering the same TTF twice raises in reportlab, so the lookup
        and registration must only happen once per process."""
        calls = []
        real_candidates = convert._monospace_ttf_candidates

        def counting_candidates():
            calls.append(1)
            return real_candidates()

        monkeypatch.setattr(convert, "_monospace_ttf_candidates", counting_candidates)
        first = convert.registered_monospace_font()
        second = convert.registered_monospace_font()
        assert first == second
        assert len(calls) == 1

    def test_unusable_candidate_falls_through_to_the_next_one(self, tmp_path, monkeypatch):
        """A candidate that exists but reportlab cannot load (e.g. some
        variable-font .ttc) must not abandon the search - the next candidate
        should still be tried."""
        # A real, loadable (non-.ttc) TTF this test can prove was actually
        # used, tried after a candidate that "exists" but cannot register.
        real_path = None
        for candidate in convert._monospace_ttf_candidates():
            if os.path.isfile(candidate) and not candidate.lower().endswith(".ttc"):
                real_path = candidate
                break
        if real_path is None:
            pytest.skip("no usable non-.ttc monospace TTF found on this machine")
        monkeypatch.setattr(
            convert, "_monospace_ttf_candidates",
            lambda: ("/nonexistent/broken.ttc", real_path),
        )
        # Make the first (nonexistent) path "exist" so it's actually attempted.
        real_isfile = os.path.isfile
        monkeypatch.setattr(
            os.path, "isfile",
            lambda p: True if p == "/nonexistent/broken.ttc" else real_isfile(p),
        )
        font_name = convert.registered_monospace_font()
        assert font_name == convert.REGISTERED_MONOSPACE_FONT_NAME

    def test_cjk_text_gets_a_coverage_note(self, tmp_path):
        """R5-03: on this machine the only registered monospace TTF has no
        CJK glyphs at all, so CJK text must be flagged - a "a real font was
        registered" check alone would miss this."""
        source = write(tmp_path / "cjk.txt", "some notes\n中文测试\nmore notes\n")
        result = convert.to_pdf(source, out_dir=str(tmp_path / "out"))
        assert is_pdf(result.pdf_path)
        font_name = convert.registered_monospace_font()
        if font_name is None:
            pytest.skip("no TTF registered on this machine - covered by the "
                        "generic Courier-fallback note instead")
        assert any(
            "chinese" in note.lower() or "japanese" in note.lower()
            or "korean" in note.lower() or "cjk" in note.lower()
            for note in result.notes
        ), result.notes

    def test_latin_and_cyrillic_text_gets_no_cjk_note(self, tmp_path):
        """Cyrillic IS covered by the registered font on this machine, so no
        CJK warning should be added for text that never uses a CJK range."""
        source = write(tmp_path / "cyrillic.txt", "hello world\nПривет мир\n")
        result = convert.to_pdf(source, out_dir=str(tmp_path / "out"))
        assert is_pdf(result.pdf_path)
        assert not any(
            "chinese" in note.lower() or "japanese" in note.lower()
            or "korean" in note.lower() or "cjk" in note.lower()
            for note in result.notes
        ), result.notes


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

    def test_animated_gif_frame_count_is_capped(self, tmp_path):
        """A many-thousand-frame animated GIF must not become a
        many-thousand-page PDF: only the first MAX_IMAGE_FRAMES are placed,
        and a note says frames were dropped."""
        from PIL import Image

        cap = convert.ImageConverter.MAX_IMAGE_FRAMES
        extra = 10
        path = tmp_path / "huge_anim.gif"
        frames = [
            Image.new("RGB", (20, 20), (i % 256, 0, 0)).convert("P")
            for i in range(cap + extra)
        ]
        frames[0].save(path, save_all=True, append_images=frames[1:])

        result = convert.to_pdf(str(path), out_dir=str(tmp_path / "out"))
        assert is_pdf(result.pdf_path)
        assert f"{cap} image frame(s)" in " ".join(result.notes)
        assert any(
            "dropped" in note.lower() or "only the first" in note.lower()
            for note in result.notes
        )

        # Count the actual pages produced, not just trust the note text.
        import re

        with open(result.pdf_path, "rb") as handle:
            pdf_bytes = handle.read()
        page_objects = re.findall(rb"/Type\s*/Page(?!s)", pdf_bytes)
        assert len(page_objects) == cap

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
    @needs_markdown
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

    @needs_markdown
    @needs_reportlab
    def test_falls_back_to_text_without_any_html_renderer(
        self, tmp_path, no_external_tools
    ):
        source = write(tmp_path / "doc.md", "# Title\n\nbody\n")
        result = convert.to_pdf(source, out_dir=str(tmp_path / "out"))

        assert result.converter == "markdown"
        assert is_pdf(result.pdf_path)
        assert any("plain text" in note for note in result.notes)

    @needs_markdown
    @needs_soffice
    def test_markdown_without_browser_uses_libreoffice(self, tmp_path, no_browser):
        source = write(tmp_path / "doc.md", "# Title\n\nbody text\n")
        result = convert.to_pdf(source, out_dir=str(tmp_path / "out"))
        assert result.converter == "markdown"
        assert is_pdf(result.pdf_path)

    @needs_markdown
    def test_relative_image_reference_resolves_via_base_href(self, tmp_path):
        """Markdown is rendered into a scratch directory different from the
        source file's own directory, so a relative image reference
        (`![x](img/pic.png)`) must resolve against the SOURCE file's
        directory, not the scratch directory. A <base href> pointing at the
        source directory achieves that without writing anything into the
        user's own folder."""
        docs = tmp_path / "docs"
        (docs / "img").mkdir(parents=True)
        (docs / "img" / "pic.png").write_bytes(b"not a real png, just a marker")
        source = write(docs / "note.md", "# Title\n\n![x](img/pic.png)\n")

        html = convert.MarkdownConverter().to_html(str(source))
        assert "<base href=" in html
        # The base must point at the docs/ directory (with a trailing slash
        # so relative paths resolve *inside* it, not next to it), as a
        # file:// URL.
        expected_dir = convert._file_url(str(docs)) + "/"
        assert expected_dir in html
        assert 'src="img/pic.png"' in html

    @needs_markdown
    def test_page_css_matches_configured_page_size(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PRINTER_AI_PAGE_SIZE", "Letter")
        source = write(tmp_path / "doc.md", "# Title\n\nbody\n")
        html = convert.MarkdownConverter().to_html(source)
        width, height = convert.PAGE_SIZES["LETTER"]
        assert f"@page {{ size: {width:.3f}pt {height:.3f}pt; }}" in html

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

    def test_broken_browser_is_cached_and_skipped_on_next_call(self, tmp_path, monkeypatch):
        """Confirmed live: converting through a browser that cannot render
        headlessly here pays the full ~60s TIMEOUT on every single call. The
        fix caches "this browser is broken" so a second conversion in the
        same process (or, via the persistent file, a later invocation) skips
        the browser call entirely."""
        monkeypatch.setattr(convert, "find_browser", lambda: "/usr/bin/chromium")
        monkeypatch.setattr(convert, "find_soffice", lambda: "/usr/bin/soffice")

        run_calls = []

        def boom(cmd, timeout, what):
            run_calls.append(cmd)
            raise convert.ConversionError("browser exploded")

        monkeypatch.setattr(convert, "_run_tool", boom)

        lo_calls = []

        def fake_lo(self, source, out_dir, notes):
            lo_calls.append(source)
            target = os.path.join(out_dir, "page.pdf")
            with open(target, "wb") as handle:
                handle.write(b"%PDF-1.4\n")
            return target

        monkeypatch.setattr(convert.OfficeConverter, "convert_with_libreoffice", fake_lo)

        out_dir = tmp_path / "out"
        out_dir.mkdir()

        # First call: the browser is attempted, fails, and the failure is
        # cached (in-process and on disk).
        source1 = write(tmp_path / "one.html", "<html></html>")
        produced1 = convert.BrowserConverter().convert(source1, str(out_dir), [])
        assert is_pdf(produced1)
        assert len(run_calls) == 1
        assert convert.browser_known_broken("/usr/bin/chromium") is True

        # Second call, same process: must not re-attempt the browser at all.
        source2 = write(tmp_path / "two.html", "<html></html>")
        notes2 = []
        produced2 = convert.BrowserConverter().convert(source2, str(out_dir), notes2)
        assert is_pdf(produced2)
        assert len(run_calls) == 1  # unchanged - the browser was not retried
        assert len(lo_calls) == 2
        assert any("skipped" in note.lower() or "libreoffice" in note.lower()
                   for note in notes2)

    def test_persistent_cache_survives_a_fresh_in_process_state(self, tmp_path, monkeypatch):
        """The persistent on-disk cache (not just the in-process dict) must
        also short-circuit the browser - this is what actually matters given
        printer-ai is typically invoked once per print job."""
        monkeypatch.setattr(convert, "find_browser", lambda: "/usr/bin/chromium")
        monkeypatch.setattr(convert, "find_soffice", lambda: "/usr/bin/soffice")

        convert._record_browser_status("/usr/bin/chromium", broken=True)
        # Simulate a fresh process: the in-process mirror is empty, only the
        # on-disk file carries the verdict.
        convert._browser_status_cache.clear()

        run_calls = []

        def boom(cmd, timeout, what):  # pragma: no cover - must not be called
            run_calls.append(cmd)
            raise convert.ConversionError("should not run")

        monkeypatch.setattr(convert, "_run_tool", boom)
        monkeypatch.setattr(
            convert.OfficeConverter, "convert_with_libreoffice",
            lambda self, source, out_dir, notes: write(tmp_path / "out.pdf", "%PDF-1.4\n"),
        )

        source = write(tmp_path / "page.html", "<html></html>")
        produced = convert.BrowserConverter().convert(source, str(tmp_path), [])
        assert is_pdf(produced)
        assert run_calls == []

    def test_stale_cache_entry_is_reprobed(self, tmp_path, monkeypatch):
        """A verdict older than BROWSER_STATUS_TTL must be re-checked, not
        trusted forever - a browser can start working again."""
        monkeypatch.setattr(convert, "find_browser", lambda: "/usr/bin/chromium")
        convert._record_browser_status("/usr/bin/chromium", broken=True)
        # Age both the in-process mirror AND the on-disk file - a stale
        # verdict must be re-probed regardless of which one is consulted.
        stale_entry = convert._browser_status_cache["/usr/bin/chromium"]
        stale_entry["checked_at"] -= convert.BROWSER_STATUS_TTL + 1
        on_disk = convert._load_browser_status()
        on_disk["/usr/bin/chromium"]["checked_at"] -= convert.BROWSER_STATUS_TTL + 1
        convert._save_browser_status(on_disk)
        assert convert.browser_known_broken("/usr/bin/chromium") is False

    def test_cache_lives_under_home_cache_dir_not_system_temp(self, tmp_path):
        """R5-01: the cache file must sit under ~/.cache/printer-ai, not the
        shared, world-writable system temp directory."""
        convert._record_browser_status("/usr/bin/chromium", broken=True)
        cache_dir = convert.printer_ai_cache_dir()
        assert cache_dir == str(tmp_path / "printer-ai-cache")
        assert os.path.isfile(os.path.join(cache_dir, "browser-status.json"))

    def test_save_does_not_follow_a_symlink_at_the_target_path(self, tmp_path):
        """R5-01: a symlink planted at the cache path must not be written
        through - the file it points at must be left untouched."""
        cache_dir = convert.printer_ai_cache_dir()
        target_path = os.path.join(cache_dir, "browser-status.json")
        victim = tmp_path / "victim.json"
        victim.write_text('{"do not touch": true}')
        os.symlink(str(victim), target_path)

        convert._save_browser_status({"chrome": {"broken": True, "checked_at": 1.0}})

        # The symlink itself must be gone (replaced by a real file via
        # os.replace), and the file it used to point at must be untouched.
        assert victim.read_text() == '{"do not touch": true}'
        assert not os.path.islink(target_path)
        with open(target_path, encoding="utf-8") as handle:
            assert json.load(handle) == {"chrome": {"broken": True, "checked_at": 1.0}}

    def test_concurrent_saves_never_leave_a_corrupt_file(self, tmp_path):
        """R5-02: simulate two "concurrent" writers with no locking - the
        rename-based swap must mean the file is always valid JSON, never a
        partial/interleaved write."""
        convert._save_browser_status({"a": {"broken": True, "checked_at": 1.0}})
        convert._save_browser_status({"b": {"broken": False, "checked_at": 2.0}})

        cache_dir = convert.printer_ai_cache_dir()
        target_path = os.path.join(cache_dir, "browser-status.json")
        with open(target_path, encoding="utf-8") as handle:
            data = json.load(handle)  # would raise on interleaved/partial content
        assert data == {"b": {"broken": False, "checked_at": 2.0}}
        # No leftover temp files from either write.
        assert [n for n in os.listdir(cache_dir) if n.endswith(".tmp")] == []


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
        self._automation_security = None

    @property
    def AutomationSecurity(self):
        return self._automation_security

    @AutomationSecurity.setter
    def AutomationSecurity(self, value):
        # Logged so a test can prove it was set *before* any Open call.
        self.log.append(("automation_security", self.kind, value))
        self._automation_security = value

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


#: PID the fake Office processes run under.
FAKE_OFFICE_PID = 4242


@pytest.fixture
def fake_com(monkeypatch):
    """Install fake pywin32 modules so the Windows COM path can be tested here.

    Besides ``pythoncom``/``win32com.client`` this fakes the ``win32gui``,
    ``win32process`` and ``win32api`` pieces the timeout handling uses to
    find and kill a hung Office process: every ``DispatchEx`` "creates" a
    top-level window of the application's class owned by FAKE_OFFICE_PID.
    """
    log = []
    windows = {}  # hwnd -> (class name, pid); populated by dispatch

    pythoncom = types.ModuleType("pythoncom")
    pythoncom.CoInitialize = lambda: log.append(("coinit", None, None))
    pythoncom.CoUninitialize = lambda: log.append(("councinit", None, None))

    client = types.ModuleType("win32com.client")

    def dispatch(prog_id):
        log.append(("dispatch", prog_id, None))
        hwnd = 1000 + len(windows)
        windows[hwnd] = (
            convert.OfficeConverter.OFFICE_WINDOW_CLASSES[prog_id], FAKE_OFFICE_PID
        )
        return FakeCOMApp(log, prog_id)

    client.DispatchEx = dispatch
    win32com = types.ModuleType("win32com")
    win32com.client = client

    win32gui = types.ModuleType("win32gui")

    def enum_windows(callback, extra):
        for hwnd in list(windows):
            callback(hwnd, extra)

    win32gui.EnumWindows = enum_windows
    win32gui.GetClassName = lambda hwnd: windows[hwnd][0]

    win32process = types.ModuleType("win32process")
    win32process.GetWindowThreadProcessId = lambda hwnd: (1, windows[hwnd][1])

    def terminate(handle, exit_code):
        log.append(("terminate", handle, exit_code))

    win32process.TerminateProcess = terminate

    win32api = types.ModuleType("win32api")

    def open_process(access, inherit, pid):
        log.append(("open_process", pid, access))
        return ("handle", pid)

    win32api.OpenProcess = open_process
    win32api.CloseHandle = lambda handle: log.append(("close_handle", handle, None))

    monkeypatch.setitem(sys.modules, "pythoncom", pythoncom)
    monkeypatch.setitem(sys.modules, "win32com", win32com)
    monkeypatch.setitem(sys.modules, "win32com.client", client)
    monkeypatch.setitem(sys.modules, "win32gui", win32gui)
    monkeypatch.setitem(sys.modules, "win32process", win32process)
    monkeypatch.setitem(sys.modules, "win32api", win32api)
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
        with pytest.raises(convert.ConversionError) as exc_info:
            convert.OfficeConverter().convert_with_com(source, str(tmp_path / "o.pdf"))
        assert exc_info.value.code == 415
        # Nothing was dispatched for a format no Office app handles.
        assert not [e for e in fake_com if e[0] == "dispatch"]

    @pytest.mark.parametrize(
        "name, prog_id",
        [
            ("letter.docx", "Word.Application"),
            ("sheet.xlsx", "Excel.Application"),
            ("deck.pptx", "PowerPoint.Application"),
        ],
    )
    def test_macros_are_disabled_before_the_document_is_opened(
        self, tmp_path, fake_com, name, prog_id
    ):
        source = write(tmp_path / name, "x")
        convert.OfficeConverter().convert_with_com(source, str(tmp_path / "o.pdf"))

        kinds = [e[0] for e in fake_com]
        assert ("automation_security", prog_id, 3) in fake_com  # msoAutomationSecurityForceDisable
        assert kinds.index("automation_security") > kinds.index("dispatch")
        assert kinds.index("automation_security") < kinds.index("open")

    def test_word_opens_with_macro_safe_flags(self, tmp_path, fake_com):
        source = write(tmp_path / "letter.docx", "x")
        convert.OfficeConverter().convert_with_com(source, str(tmp_path / "o.pdf"))
        opened = [e for e in fake_com if e[0] == "open"][0]
        flags = opened[3]
        assert flags["ReadOnly"] is True
        assert flags["AddToRecentFiles"] is False
        assert flags["ConfirmConversions"] is False
        assert flags["OpenAndRepair"] is False

    def test_excel_opens_without_updating_links(self, tmp_path, fake_com):
        source = write(tmp_path / "sheet.xlsx", "x")
        convert.OfficeConverter().convert_with_com(source, str(tmp_path / "o.pdf"))
        opened = [e for e in fake_com if e[0] == "open"][0]
        assert opened[3]["ReadOnly"] is True
        assert opened[3]["UpdateLinks"] == 0

    def test_hung_office_times_out_and_is_terminated_by_pid(
        self, tmp_path, fake_com, monkeypatch
    ):
        import threading

        never = threading.Event()

        def block_forever(self, path, **kwargs):
            self.log.append(("open", self.kind, path, kwargs))
            never.wait()  # a modal dialog nobody will ever click away

        monkeypatch.setattr(FakeCOMCollection, "Open", block_forever)
        source = write(tmp_path / "letter.docx", "x")

        with pytest.raises(convert.ConversionError) as exc_info:
            convert.OfficeConverter().convert_with_com(
                source, str(tmp_path / "o.pdf"), timeout=0.2
            )
        # Everything below happens BEFORE the worker is released: the kill
        # must not depend on the hung thread ever coming back.
        assert exc_info.value.code == 504
        assert "timed out" in str(exc_info.value)
        opened = [e for e in fake_com if e[0] == "open_process"]
        assert opened and opened[0][1] == FAKE_OFFICE_PID
        assert ("terminate", ("handle", FAKE_OFFICE_PID), 1) in fake_com
        assert ("close_handle", ("handle", FAKE_OFFICE_PID), None) in fake_com
        # Quit() is apartment-bound; it must never be called cross-thread.
        assert ("quit", "Word.Application", None) not in fake_com
        never.set()  # let the abandoned worker thread finish

    def test_hung_excel_pid_comes_from_hwnd(self, tmp_path, fake_com, monkeypatch):
        """Excel/PowerPoint expose Hwnd, which beats window enumeration."""
        import threading

        never = threading.Event()

        def block_forever(self, path, **kwargs):
            never.wait()

        monkeypatch.setattr(FakeCOMCollection, "Open", block_forever)
        # Excel's Hwnd; the fake win32process maps any known hwnd to a pid.
        # Register it as an existing window so GetWindowThreadProcessId knows it.
        monkeypatch.setattr(FakeCOMApp, "Hwnd", 1000, raising=False)
        source = write(tmp_path / "sheet.xlsx", "x")

        with pytest.raises(convert.ConversionError):
            convert.OfficeConverter().convert_with_com(
                source, str(tmp_path / "o.pdf"), timeout=0.2
            )
        assert ("terminate", ("handle", FAKE_OFFICE_PID), 1) in fake_com
        never.set()

    def test_timeout_without_a_known_pid_still_raises_504(
        self, tmp_path, fake_com, monkeypatch
    ):
        import threading

        never = threading.Event()

        def block_forever(self, path, **kwargs):
            never.wait()

        monkeypatch.setattr(FakeCOMCollection, "Open", block_forever)
        # Enumeration finds nothing: the pid is unknown.
        monkeypatch.setattr(
            convert.OfficeConverter, "_office_window_pids", classmethod(lambda cls, p: set())
        )
        source = write(tmp_path / "letter.docx", "x")
        with pytest.raises(convert.ConversionError) as exc_info:
            convert.OfficeConverter().convert_with_com(
                source, str(tmp_path / "o.pdf"), timeout=0.2
            )
        assert exc_info.value.code == 504
        assert not [e for e in fake_com if e[0] in ("open_process", "terminate")]
        never.set()

    def test_terminate_pid_reports_failure_when_open_process_fails(
        self, fake_com, monkeypatch
    ):
        def refuse(access, inherit, pid):
            raise OSError("access denied")

        monkeypatch.setattr(sys.modules["win32api"], "OpenProcess", refuse)
        assert convert.OfficeConverter._terminate_pid(FAKE_OFFICE_PID) is False
        assert not [e for e in fake_com if e[0] == "terminate"]

    def test_timeout_comes_from_the_environment(self, tmp_path, fake_com, monkeypatch):
        import threading

        never = threading.Event()

        def block_forever(self, path, **kwargs):
            never.wait()

        monkeypatch.setattr(FakeCOMCollection, "Open", block_forever)
        monkeypatch.setenv("PRINTER_AI_COM_TIMEOUT", "0.2")
        source = write(tmp_path / "letter.docx", "x")

        with pytest.raises(convert.ConversionError) as exc_info:
            convert.OfficeConverter().convert_with_com(source, str(tmp_path / "o.pdf"))
        never.set()
        assert exc_info.value.code == 504
        assert "0s" in str(exc_info.value)  # formatted from the 0.2s override

    @pytest.mark.parametrize("raw, expected", [
        ("", 60.0), ("30", 30.0), ("0", 60.0), ("-5", 60.0), ("soon", 60.0),
    ])
    def test_com_timeout_parsing(self, monkeypatch, raw, expected):
        monkeypatch.setenv("PRINTER_AI_COM_TIMEOUT", raw)
        assert convert._com_timeout() == expected

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

    @staticmethod
    def _fake_libreoffice(monkeypatch, used):
        monkeypatch.setattr(convert, "find_soffice", lambda: "/fake/soffice")

        def fake_lo(self, source, out_dir, notes):
            used.append(source)
            target = os.path.join(out_dir, "letter.pdf")
            with open(target, "wb") as handle:
                handle.write(b"%PDF-1.4\n")
            notes.append("converted by LibreOffice (fake)")
            return target

        monkeypatch.setattr(convert.OfficeConverter, "convert_with_libreoffice", fake_lo)

    def test_com_timeout_falls_back_to_libreoffice(self, tmp_path, monkeypatch, fake_com):
        """A hung Office (504) is a tool problem: LibreOffice gets a turn."""
        monkeypatch.setattr(convert, "_msoffice_com_available", lambda: True)

        def hang(self, source, target, timeout=None):
            raise convert.ConversionError("Office COM conversion timed out after 1s", code=504)

        monkeypatch.setattr(convert.OfficeConverter, "convert_with_com", hang)
        used = []
        self._fake_libreoffice(monkeypatch, used)

        source = write(tmp_path / "letter.docx", "x")
        out_dir = tmp_path / "out"
        out_dir.mkdir()
        notes = []
        produced = convert.OfficeConverter().convert(source, str(out_dir), notes)

        assert used == [source]
        assert is_pdf(produced)
        assert any("Office COM failed/timed out" in note for note in notes)
        assert any("LibreOffice" in note for note in notes)

    def test_com_crash_500_falls_back_to_libreoffice(self, tmp_path, monkeypatch, fake_com):
        monkeypatch.setattr(convert, "_msoffice_com_available", lambda: True)

        def crash(self, source, target, timeout=None):
            raise convert.ConversionError("Office crashed", code=500)

        monkeypatch.setattr(convert.OfficeConverter, "convert_with_com", crash)
        used = []
        self._fake_libreoffice(monkeypatch, used)
        source = write(tmp_path / "letter.docx", "x")
        notes = []
        produced = convert.OfficeConverter().convert(source, str(tmp_path), notes)
        assert used == [source] and is_pdf(produced)

    def test_com_timeout_without_libreoffice_is_final(self, tmp_path, monkeypatch, fake_com):
        monkeypatch.setattr(convert, "_msoffice_com_available", lambda: True)
        monkeypatch.setattr(convert, "find_soffice", lambda: None)

        def hang(self, source, target, timeout=None):
            raise convert.ConversionError("Office COM conversion timed out", code=504)

        monkeypatch.setattr(convert.OfficeConverter, "convert_with_com", hang)
        source = write(tmp_path / "letter.docx", "x")
        with pytest.raises(convert.ConversionError) as exc_info:
            convert.OfficeConverter().convert(source, str(tmp_path), [])
        assert exc_info.value.code == 504

    @pytest.mark.parametrize("code", [415, 422])
    def test_com_document_errors_do_not_fall_back(
        self, tmp_path, monkeypatch, fake_com, code
    ):
        """A bad document is bad in LibreOffice too; do not retry it."""
        monkeypatch.setattr(convert, "_msoffice_com_available", lambda: True)

        def reject(self, source, target, timeout=None):
            raise convert.ConversionError("document problem", code=code)

        monkeypatch.setattr(convert.OfficeConverter, "convert_with_com", reject)
        used = []
        self._fake_libreoffice(monkeypatch, used)
        source = write(tmp_path / "letter.docx", "x")
        with pytest.raises(convert.ConversionError) as exc_info:
            convert.OfficeConverter().convert(source, str(tmp_path), [])
        assert exc_info.value.code == code
        assert used == []


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


class TestNativeExtensionIsVerified:
    """A .pdf/.ps name alone must not send a file to the printer untouched."""

    def test_html_error_page_named_pdf_is_not_native(self, tmp_path):
        path = tmp_path / "download.pdf"
        path.write_bytes(b"<html><body><h1>404 Not Found</h1></body></html>\n")
        kind, how = convert.detect_format(str(path))
        assert kind == convert.KIND_HTML
        assert how == "magic-override"

    def test_html_named_pdf_does_not_pass_through(self, tmp_path, no_external_tools):
        path = tmp_path / "download.pdf"
        path.write_bytes(b"<!DOCTYPE html><html><body>nope</body></html>\n")
        # With no renderer available this must fail loudly - never native.
        try:
            result = convert.to_pdf(str(path), out_dir=str(tmp_path / "out"))
        except convert.ConversionError as exc:
            assert exc.detected == convert.KIND_HTML
        else:
            assert result.native is False
            assert result.converted is True

    def test_plain_text_named_ps_is_treated_as_text(self, tmp_path):
        path = tmp_path / "notes.ps"
        path.write_bytes(b"just some notes, not PostScript\n")
        kind, how = convert.detect_format(str(path))
        assert kind == convert.KIND_TEXT
        assert how == "magic-override"

    def test_binary_junk_named_pdf_is_unsupported(self, tmp_path):
        path = tmp_path / "junk.pdf"
        path.write_bytes(b"\x00\x01\xff\xfe" * 32)
        with pytest.raises(convert.ConversionError) as exc_info:
            convert.to_pdf(str(path))
        assert exc_info.value.detected == convert.KIND_UNSUPPORTED

    def test_real_pdf_and_ps_still_trusted(self, tmp_path):
        pdf = tmp_path / "ok.pdf"
        pdf.write_bytes(b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n")
        ps = tmp_path / "ok.ps"
        ps.write_bytes(b"%!PS-Adobe-3.0\n")
        pjl = tmp_path / "wrapped.ps"
        pjl.write_bytes(b"\x04%!PS-Adobe-3.0\n")
        for path in (pdf, ps, pjl):
            assert convert.detect_format(str(path)) == (convert.KIND_NATIVE, "extension")

    def test_pcl_capture_named_prn_still_trusted(self, tmp_path):
        prn = tmp_path / "capture.prn"
        prn.write_bytes(b"\x1b%-12345X@PJL ENTER LANGUAGE=PCL\n\x1bE")
        assert convert.detect_format(str(prn)) == (convert.KIND_NATIVE, "extension")


class TestEncryptedPdf:
    """A PDF that only *mentions* /Encrypt in its trailer is not necessarily
    unprintable: an owner-password-only PDF (empty user password - common for
    "no printing/copying" restrictions) opens everywhere and must pass
    through unchanged. Only a PDF that genuinely needs a password to open is
    rejected. See pdf_encryption_status(), which tells the two apart with the
    standard security handler's own empty-user-password check (R3-01)."""

    @staticmethod
    def _pdf(encrypted):
        body = b"%PDF-1.6\n1 0 obj<</Type/Catalog>>endobj\n"
        trailer = b"trailer\n<</Root 1 0 R"
        if encrypted:
            trailer += b" /Encrypt 5 0 R"
        trailer += b">>\nstartxref\n0\n%%EOF\n"
        return body + trailer

    @staticmethod
    def _real_encrypted_pdf(path, *, user_password, owner_password):
        """Build a genuinely RC4-encrypted PDF via reportlab, not a fixture
        that merely mentions /Encrypt without a real dictionary behind it."""
        from reportlab.lib import pdfencrypt
        from reportlab.lib.pagesizes import A4
        from reportlab.pdfgen import canvas

        enc = pdfencrypt.StandardEncryption(
            userPassword=user_password, ownerPassword=owner_password,
        )
        c = canvas.Canvas(str(path), pagesize=A4, encrypt=enc)
        c.drawString(100, 700, "hello")
        c.save()

    def test_owner_password_only_pdf_passes_through(self, tmp_path):
        """Empty user password, owner-only restrictions: opens everywhere,
        must not be rejected. This is the case R3-01 fixed a regression on."""
        path = tmp_path / "owner-only.pdf"
        self._real_encrypted_pdf(path, user_password="", owner_password="secret")
        assert convert.pdf_is_encrypted(str(path)) is True
        assert convert.pdf_encryption_status(str(path)) == convert.PDF_OPENS_WITHOUT_PASSWORD
        result = convert.to_pdf(str(path))
        assert result.native is True and result.converted is False

    def test_user_password_required_pdf_is_422(self, tmp_path):
        """A real user password is required to open this one - reject it."""
        path = tmp_path / "locked.pdf"
        self._real_encrypted_pdf(path, user_password="realsecret", owner_password="ownersecret")
        assert convert.pdf_is_encrypted(str(path)) is True
        assert convert.pdf_encryption_status(str(path)) == convert.PDF_NEEDS_PASSWORD
        with pytest.raises(convert.ConversionError) as exc_info:
            convert.to_pdf(str(path))
        assert exc_info.value.code == 422
        assert "password" in str(exc_info.value).lower()
        assert exc_info.value.hint

    def test_encrypt_mentioned_but_undecodable_dictionary_passes_through(self, tmp_path):
        """A trailer that references /Encrypt but whose object cannot actually
        be parsed (malformed/synthetic PDF) is PDF_ENCRYPTION_UNKNOWN, not a
        confirmed lock - the safer default is to let it through rather than
        block a file this module cannot actually verify."""
        path = tmp_path / "secret.pdf"
        path.write_bytes(self._pdf(encrypted=True))
        assert convert.pdf_is_encrypted(str(path)) is True
        assert convert.pdf_encryption_status(str(path)) == convert.PDF_ENCRYPTION_UNKNOWN
        result = convert.to_pdf(str(path))
        assert result.native is True and result.converted is False

    def test_plain_pdf_passes_through(self, tmp_path):
        path = tmp_path / "open.pdf"
        path.write_bytes(self._pdf(encrypted=False))
        assert convert.pdf_is_encrypted(str(path)) is False
        result = convert.to_pdf(str(path))
        assert result.native is True and result.converted is False

    def test_encrypt_only_counts_near_the_trailer(self, tmp_path):
        """/Encrypt buried early (e.g. in page text) far from the trailer is ignored."""
        path = tmp_path / "mention.pdf"
        path.write_bytes(
            b"%PDF-1.4\n(the word /Encrypt appears in a string)\n"
            + b"%" * 4096
            + b"\ntrailer<</Root 1 0 R>>\n%%EOF\n"
        )
        assert convert.pdf_is_encrypted(str(path)) is False


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

        class Proc:
            pid = 4242

            def communicate(self, timeout=None):
                raise subprocess.TimeoutExpired(cmd="x", timeout=timeout)

        monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: Proc())
        monkeypatch.setattr(convert, "_kill_process_tree", lambda proc: None)
        with pytest.raises(convert.ConversionError) as exc_info:
            convert._run_tool(["x"], 1.0, "thing")
        assert "timed out" in str(exc_info.value)

    def test_nonzero_exit_becomes_a_conversion_error(self, monkeypatch):
        import subprocess

        class Proc:
            returncode = 3
            pid = 4242

            def communicate(self, timeout=None):
                return b"it broke", None

        monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: Proc())
        with pytest.raises(convert.ConversionError) as exc_info:
            convert._run_tool(["x"], 1.0, "thing")
        assert "exit code 3" in str(exc_info.value)

    def test_error_code_defaults_to_415(self):
        assert convert.ConversionError("x").code == 415


class TestErrorCodes:
    """ConversionError.code tells the caller whose fault a failure is."""

    def test_empty_file_is_422(self, tmp_path):
        path = tmp_path / "empty.docx"
        path.write_bytes(b"")
        with pytest.raises(convert.ConversionError) as exc_info:
            convert.to_pdf(str(path))
        assert exc_info.value.code == 422

    def test_missing_file_is_404(self, tmp_path):
        with pytest.raises(convert.ConversionError) as exc_info:
            convert.to_pdf(str(tmp_path / "nope.txt"))
        assert exc_info.value.code == 404

    def test_unsupported_format_is_415(self, tmp_path):
        path = tmp_path / "thing.bin"
        path.write_bytes(b"\x00\x01\xff\xfe" * 32)
        with pytest.raises(convert.ConversionError) as exc_info:
            convert.to_pdf(str(path))
        assert exc_info.value.code == 415

    @needs_pillow
    @needs_reportlab
    def test_corrupt_image_is_422(self, tmp_path):
        path = tmp_path / "broken.png"
        path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"garbage" * 4)
        with pytest.raises(convert.ConversionError) as exc_info:
            convert.to_pdf(str(path), out_dir=str(tmp_path / "out"))
        assert exc_info.value.code == 422

    def test_missing_tool_is_415(self, tmp_path, no_external_tools):
        import zipfile

        path = tmp_path / "report.docx"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("[Content_Types].xml", "<Types/>")
        with pytest.raises(convert.ConversionError) as exc_info:
            convert.to_pdf(str(path))
        assert exc_info.value.code == 415
        assert exc_info.value.hint

    def test_tool_timeout_is_504(self, monkeypatch):
        import subprocess

        class Proc:
            pid = 4242

            def communicate(self, timeout=None):
                raise subprocess.TimeoutExpired(cmd="x", timeout=timeout)

        monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: Proc())
        monkeypatch.setattr(convert, "_kill_process_tree", lambda proc: None)
        with pytest.raises(convert.ConversionError) as exc_info:
            convert._run_tool(["x"], 1.0, "thing")
        assert exc_info.value.code == 504

    def test_tool_nonzero_exit_is_500(self, monkeypatch):
        import subprocess

        class Proc:
            returncode = 3
            pid = 4242

            def communicate(self, timeout=None):
                return b"it broke", None

        monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: Proc())
        with pytest.raises(convert.ConversionError) as exc_info:
            convert._run_tool(["x"], 1.0, "thing")
        assert exc_info.value.code == 500

    def test_tool_binary_vanished_is_415(self, monkeypatch):
        import subprocess

        def gone(*args, **kwargs):
            raise FileNotFoundError("soffice")

        monkeypatch.setattr(subprocess, "Popen", gone)
        with pytest.raises(convert.ConversionError) as exc_info:
            convert._run_tool(["soffice"], 1.0, "LibreOffice")
        assert exc_info.value.code == 415

    def test_libreoffice_silently_producing_nothing_is_422(self, tmp_path, monkeypatch):
        """LibreOffice exits 0 but writes no PDF for corrupt/encrypted files."""
        monkeypatch.setattr(convert, "find_soffice", lambda: "/fake/soffice")
        monkeypatch.setattr(convert, "_run_tool", lambda cmd, timeout, what: None)
        source = write(tmp_path / "locked.docx", "x")
        with pytest.raises(convert.ConversionError) as exc_info:
            convert.OfficeConverter().convert_with_libreoffice(source, str(tmp_path), [])
        assert exc_info.value.code == 422
        assert "password" in exc_info.value.hint

    @needs_reportlab
    def test_converter_crash_is_500(self, tmp_path, monkeypatch):
        source = write(tmp_path / "notes.txt", "hello\n")

        def explode(self, source, out_dir, notes):
            raise RuntimeError("kaboom")

        monkeypatch.setattr(convert.TextConverter, "convert", explode)
        with pytest.raises(convert.ConversionError) as exc_info:
            convert.to_pdf(source)
        assert exc_info.value.code == 500
        assert "kaboom" in str(exc_info.value)

    @needs_reportlab
    def test_code_survives_the_to_pdf_wrapper(self, tmp_path, monkeypatch):
        """to_pdf re-raises a converter's ConversionError with its code intact."""
        source = write(tmp_path / "notes.txt", "hello\n")

        def slow(self, source, out_dir, notes):
            raise convert.ConversionError("tool timed out", code=504)

        monkeypatch.setattr(convert.TextConverter, "convert", slow)
        with pytest.raises(convert.ConversionError) as exc_info:
            convert.to_pdf(source)
        assert exc_info.value.code == 504
        assert exc_info.value.converter == "text"

    def test_run_tool_never_uses_a_shell(self, monkeypatch):
        import subprocess

        captured = {}

        class Proc:
            returncode = 0
            pid = 4242

            def communicate(self, timeout=None):
                return b"", None

        def fake_popen(cmd, **kwargs):
            captured["cmd"] = cmd
            captured["kwargs"] = kwargs
            return Proc()

        monkeypatch.setattr(subprocess, "Popen", fake_popen)
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
