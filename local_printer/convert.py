"""Universal "convert anything to PDF" layer.

Every platform backend in this project can print a PDF and little else: CUPS
filters a handful of formats, and the Windows backend hands bytes to a printer
driver. Rather than teaching each backend about Word documents and PNGs, this
module normalises *any* common file to a PDF **before** the backend is called,
so a backend only ever sees a PDF (or a format a print system takes verbatim).

Public API
----------
``to_pdf(path, out_dir=None) -> ConvertResult``
    The one entry point. Raises :class:`ConversionError` (which carries a
    ``hint`` naming the missing tool and how to install it) when the file
    cannot be turned into a PDF.
``format_catalog() -> list[dict]``
    What the CLI's ``formats`` command reports: every converter, the
    extensions it claims, whether it is usable right now, and an install hint.
``cleanup(result)``
    Remove the temporary directory ``to_pdf`` created (see "Ownership").

Ownership of the output file
----------------------------
When ``out_dir`` is given the PDF is written there, ``ConvertResult.temp`` is
False and the file belongs to the caller forever. When ``out_dir`` is omitted
the PDF lands in a fresh ``tempfile.mkdtemp()`` directory, ``temp`` is True and
**the caller must delete it** by calling :func:`cleanup` once the file has been
read. ``main.py`` does this right after the backend returns (both backends read
the file synchronously), unless ``--keep-pdf`` was passed. Converters clean up
their own intermediate scratch files; only the final directory is the caller's.

Rules this module follows
-------------------------
* Nothing is ever written to stdout - stdout carries the CLI's JSON payload.
  Diagnostics go to ``utils.logger``.
* No ``shell=True``, ever. External tools are invoked with argument lists;
  file names are data, not shell text.
"""

from __future__ import annotations

import locale
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import zipfile
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from utils.logger import logger

# ==================== result / error types ====================


@dataclass
class ConvertResult:
    """The outcome of a successful conversion."""

    pdf_path: str
    source_path: str
    converter: str
    converted: bool
    native: bool = False
    temp: bool = False
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "pdf_path": self.pdf_path,
            "source_path": self.source_path,
            "converter": self.converter,
            "converted": self.converted,
            "native": self.native,
            "temp": self.temp,
            "notes": list(self.notes),
        }


class ConversionError(Exception):
    """Raised when a file cannot be turned into a PDF.

    ``hint`` always names the tool that is missing and how to install it, so
    the CLI can pass something actionable back to a human or an AI caller.

    ``code`` is the HTTP-style status main.py reports for the failure, so a
    caller can tell *whose* fault it is without parsing the message:

    * 415 - the format is unsupported here, or the tool that would convert it
      is not installed (``available()`` said no). Installing something fixes it.
    * 422 - the input file itself is broken: empty, corrupt, or password
      protected. No install will help; the file has to change.
    * 504 - an external converter (LibreOffice, browser, Office COM) timed out.
    * 500 - an external converter crashed or exited non-zero unexpectedly.
    * 404 - the file does not exist (main.py normally catches this earlier).

    415 is the default when nothing more specific is known.
    """

    def __init__(self, msg: str, hint: str = "", source_path: str = "",
                 detected: str = "", converter: str = "", code: int = 415):
        super().__init__(msg)
        self.msg = msg
        self.hint = hint
        self.source_path = source_path
        self.detected = detected
        self.converter = converter
        self.code = code

    def to_data(self) -> Dict[str, Any]:
        return {
            "file_path": self.source_path,
            "detected": self.detected,
            "converter": self.converter,
            "hint": self.hint,
        }


# ==================== install hints ====================

LIBREOFFICE_HINT = (
    "LibreOffice is required for this format. Install it from "
    "https://www.libreoffice.org/download/ (Linux: apt install libreoffice / "
    "dnf install libreoffice; macOS: brew install --cask libreoffice)"
)
BROWSER_HINT = (
    "A Chromium-family browser is required to render HTML/SVG. On Windows "
    "Microsoft Edge is preinstalled; otherwise install Google Chrome "
    "(https://www.google.com/chrome/) or Chromium, or point "
    "PRINTER_AI_BROWSER at the executable"
)
PILLOW_HINT = "Pillow is required for images: pip install Pillow"
REPORTLAB_HINT = "reportlab is required for text/image layout: pip install reportlab"
MARKDOWN_HINT = "the markdown package is required: pip install markdown"


# ==================== format detection ====================

NATIVE_EXTENSIONS = {".pdf", ".ps", ".prn"}

IMAGE_EXTENSIONS = {
    ".jpg", ".jpeg", ".jpe", ".png", ".gif", ".bmp", ".dib", ".tif", ".tiff",
    ".webp", ".ico", ".ppm", ".pgm", ".pbm", ".tga", ".jfif",
}

MARKDOWN_EXTENSIONS = {".md", ".markdown", ".mdown", ".mkd", ".mdtext"}

HTML_EXTENSIONS = {".html", ".htm", ".xhtml"}

SVG_EXTENSIONS = {".svg"}

CSV_EXTENSIONS = {".csv", ".tsv"}

OFFICE_EXTENSIONS = {
    # word processing
    ".doc", ".docx", ".docm", ".dot", ".dotx", ".odt", ".ott", ".fodt", ".rtf",
    ".wps", ".pages",
    # spreadsheets
    ".xls", ".xlsx", ".xlsm", ".xlt", ".xltx", ".ods", ".ots", ".fods",
    # presentations
    ".ppt", ".pptx", ".pptm", ".pps", ".ppsx", ".pot", ".potx", ".odp", ".otp",
    ".fodp",
    # drawings
    ".odg", ".otg", ".vsd", ".vsdx",
}

TEXT_EXTENSIONS = {
    ".txt", ".text", ".log", ".json", ".jsonl", ".ndjson", ".yaml", ".yml",
    ".toml", ".ini", ".cfg", ".conf", ".properties", ".env", ".xml", ".rst",
    ".tex", ".diff", ".patch", ".sql",  # .csv/.tsv are routed to the csv converter
    ".py", ".pyi", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".sh", ".bash",
    ".zsh", ".fish", ".ps1", ".psm1", ".bat", ".cmd", ".c", ".h", ".cc", ".cpp",
    ".hpp", ".cs", ".java", ".kt", ".kts", ".go", ".rs", ".rb", ".php", ".pl",
    ".pm", ".lua", ".r", ".swift", ".m", ".mm", ".scala", ".clj", ".ex", ".exs",
    ".erl", ".hs", ".dart", ".vue", ".svelte", ".css", ".scss", ".less",
    ".gradle", ".cmake", ".mk", ".makefile", ".dockerfile", ".gitignore",
}

# Format kinds used internally
KIND_NATIVE = "native"
KIND_IMAGE = "image"
KIND_TEXT = "text"
KIND_MARKDOWN = "markdown"
KIND_HTML = "html"
KIND_SVG = "svg"
KIND_OFFICE = "office"
KIND_CSV = "csv"
KIND_UNSUPPORTED = "unsupported"

_EXTENSION_KINDS: List[Tuple[set, str]] = [
    (NATIVE_EXTENSIONS, KIND_NATIVE),
    (IMAGE_EXTENSIONS, KIND_IMAGE),
    (MARKDOWN_EXTENSIONS, KIND_MARKDOWN),
    (HTML_EXTENSIONS, KIND_HTML),
    (SVG_EXTENSIONS, KIND_SVG),
    (CSV_EXTENSIONS, KIND_CSV),
    (OFFICE_EXTENSIONS, KIND_OFFICE),
    (TEXT_EXTENSIONS, KIND_TEXT),
]


def kind_for_extension(ext: str) -> Optional[str]:
    """Map a lowercase extension (with dot) to a format kind, or None."""
    ext = (ext or "").lower()
    if not ext:
        return None
    for extensions, kind in _EXTENSION_KINDS:
        if ext in extensions:
            return kind
    return None


def _sniff_zip(path: str) -> Optional[str]:
    """Look inside a ZIP container: OOXML and ODF are both ZIPs."""
    try:
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
    except Exception as exc:  # not a readable zip after all
        logger.debug("zip sniff failed for %s: %s", path, exc)
        return None
    if "[Content_Types].xml" in names:
        return KIND_OFFICE
    if "mimetype" in names:
        return KIND_OFFICE
    return None


def sniff_format(path: str) -> str:
    """Identify a file by its bytes alone. Returns a KIND_* constant."""
    try:
        with open(path, "rb") as handle:
            head = handle.read(4096)
    except OSError as exc:
        logger.debug("could not read %s for sniffing: %s", path, exc)
        return KIND_UNSUPPORTED

    if not head:
        return KIND_UNSUPPORTED

    if head.startswith(b"%PDF"):
        return KIND_NATIVE
    if head.startswith(b"%!PS") or head.startswith(b"\x04%!PS"):
        return KIND_NATIVE
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return KIND_IMAGE
    if head.startswith(b"\xff\xd8\xff"):  # JPEG
        return KIND_IMAGE
    if head.startswith(b"GIF87a") or head.startswith(b"GIF89a"):
        return KIND_IMAGE
    if head.startswith(b"II*\x00") or head.startswith(b"MM\x00*"):  # TIFF
        return KIND_IMAGE
    if head.startswith(b"BM"):  # BMP
        return KIND_IMAGE
    if head.startswith(b"RIFF") and head[8:12] == b"WEBP":
        return KIND_IMAGE
    if head.startswith(b"PK\x03\x04"):
        zip_kind = _sniff_zip(path)
        if zip_kind:
            return zip_kind
        return KIND_UNSUPPORTED
    if head.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        # OLE2 compound file: legacy .doc/.xls/.ppt
        return KIND_OFFICE
    if head.lstrip()[:5].lower() == b"{\\rtf"[:5]:
        return KIND_OFFICE

    # Anything that decodes as UTF-8 is treated as text. HTML and SVG get their
    # own kinds so they render properly rather than printing as source code.
    try:
        text = head.decode("utf-8")
    except UnicodeDecodeError:
        return KIND_UNSUPPORTED

    lowered = text.lstrip().lower()
    if lowered.startswith("<svg") or ("<svg" in lowered and "xmlns" in lowered):
        return KIND_SVG
    if lowered.startswith("<!doctype html") or lowered.startswith("<html"):
        return KIND_HTML
    return KIND_TEXT


def detect_format(path: str) -> Tuple[str, str]:
    """Determine the format of ``path``.

    Extension first (it carries the user's intent), magic bytes when the
    extension is missing, unknown, or clearly misleading - a ``.txt`` holding a
    PDF is printed as a PDF, not as a page of binary garbage.

    Returns:
        (kind, how) where ``how`` is "extension", "magic" or "magic-override".
    """
    ext = os.path.splitext(path)[1].lower()
    ext_kind = kind_for_extension(ext)

    if ext_kind is None:
        return sniff_format(path), "magic"

    # A generic text-ish extension must not hide a binary document.
    if ext_kind in (KIND_TEXT, KIND_CSV):
        sniffed = sniff_format(path)
        if sniffed in (KIND_NATIVE, KIND_IMAGE, KIND_OFFICE):
            return sniffed, "magic-override"

    # A "native" extension goes to the printer untouched, so it must really
    # be a PDF/PostScript: an HTML error page saved as .pdf, or plain text
    # saved as .ps, would otherwise print as garbage (or not at all).
    if ext_kind == KIND_NATIVE and not _has_native_magic(path, ext):
        sniffed = sniff_format(path)
        if sniffed == KIND_NATIVE:  # pragma: no cover - defensive
            return sniffed, "extension"
        return sniffed, "magic-override"

    return ext_kind, "extension"


def _looks_like_pdf(path: str) -> bool:
    try:
        with open(path, "rb") as handle:
            return handle.read(4) == b"%PDF"
    except OSError:
        return False


def _has_native_magic(path: str, ext: str) -> bool:
    """True when a .pdf/.ps/.prn file starts with the bytes its name promises."""
    try:
        with open(path, "rb") as handle:
            head = handle.read(16)
    except OSError:
        return False
    if ext == ".pdf":
        return head.startswith(b"%PDF")
    if head.startswith(b"%!") or head.startswith(b"\x04%!"):
        return True  # PostScript, possibly with a leading ^D
    if ext == ".prn":
        # Captured printer streams: PJL universal exit language or a PCL reset.
        return head.startswith(b"\x1b%-12345X") or head.startswith(b"\x1bE")
    return False


#: How much of a PDF's tail to scan for the trailer's /Encrypt entry.
_PDF_TRAILER_WINDOW = 2048


def pdf_is_encrypted(path: str) -> bool:
    """Heuristic: does this PDF's trailer reference an /Encrypt dictionary?

    Not a PDF parser - it looks for ``/Encrypt`` in the last couple of
    kilobytes, which is where the trailer (or the cross-reference stream
    dictionary) of an encrypted file lives.
    """
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as handle:
            handle.seek(max(0, size - _PDF_TRAILER_WINDOW))
            tail = handle.read(_PDF_TRAILER_WINDOW)
    except OSError:
        return False
    return b"/Encrypt" in tail


# ==================== page geometry ====================

# PostScript points (1/72 inch)
PAGE_SIZES = {
    "A4": (595.276, 841.890),
    "LETTER": (612.0, 792.0),
    "LEGAL": (612.0, 1008.0),
    "A3": (841.890, 1190.551),
    "A5": (419.528, 595.276),
}

_LETTER_LOCALES = ("en_us", "en_ca", "en_ph", "es_mx")


def _system_locale() -> str:
    """Best-effort locale name, lowercase, without an encoding suffix."""
    for var in ("LC_ALL", "LC_CTYPE", "LANG", "LANGUAGE"):
        value = os.environ.get(var)
        if value:
            return value.split(".")[0].split(":")[0].strip().lower()
    try:
        name = locale.getlocale()[0]
    except (ValueError, TypeError):  # pragma: no cover - platform dependent
        name = None
    if not name:
        try:
            name = locale.getdefaultlocale()[0]  # noqa: DEP001 - fallback only
        except Exception:  # pragma: no cover
            name = None
    return (name or "").split(".")[0].strip().lower()


def page_size() -> Tuple[float, float]:
    """The page the layout converters target.

    A4 everywhere, Letter in the regions that actually use it, and
    ``PRINTER_AI_PAGE_SIZE=A4|Letter`` beats both.
    """
    override = os.environ.get("PRINTER_AI_PAGE_SIZE", "").strip().upper()
    if override in PAGE_SIZES:
        return PAGE_SIZES[override]
    if override:
        logger.warning("ignoring unknown PRINTER_AI_PAGE_SIZE=%r", override)

    name = _system_locale().replace("-", "_")
    if name.startswith(_LETTER_LOCALES):
        return PAGE_SIZES["LETTER"]
    return PAGE_SIZES["A4"]


def page_size_name() -> str:
    size = page_size()
    for name, value in PAGE_SIZES.items():
        if value == size:
            return name.title() if name != "A4" else "A4"
    return "custom"  # pragma: no cover


MARGIN = 36.0  # 0.5 inch


# ==================== external tool discovery ====================

def _is_windows() -> bool:
    """Platform test as a function so tests can monkeypatch ``sys.platform``."""
    return sys.platform == "win32"


def _is_macos() -> bool:
    return sys.platform == "darwin"


SOFFICE_NAMES = ("soffice", "libreoffice")

SOFFICE_WINDOWS_PATHS = (
    r"C:\Program Files\LibreOffice\program\soffice.exe",
    r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
)

SOFFICE_MAC_PATHS = ("/Applications/LibreOffice.app/Contents/MacOS/soffice",)

SOFFICE_UNIX_PATHS = (
    "/usr/bin/soffice",
    "/usr/bin/libreoffice",
    "/usr/local/bin/soffice",
    "/snap/bin/libreoffice",
    "/snap/bin/soffice",
    "/opt/libreoffice/program/soffice",
)


def find_soffice() -> Optional[str]:
    """Locate the LibreOffice launcher, or None."""
    override = os.environ.get("PRINTER_AI_SOFFICE", "").strip()
    if override and os.path.isfile(override):
        return override

    for name in SOFFICE_NAMES:
        found = shutil.which(name)
        if found:
            return found

    if _is_windows():
        candidates = SOFFICE_WINDOWS_PATHS
    elif _is_macos():
        candidates = SOFFICE_MAC_PATHS + SOFFICE_UNIX_PATHS
    else:
        candidates = SOFFICE_UNIX_PATHS
    for candidate in candidates:
        if os.path.isfile(candidate):
            return candidate
    return None


BROWSER_LINUX_NAMES = (
    "chromium", "chromium-browser", "google-chrome", "google-chrome-stable",
    "microsoft-edge", "microsoft-edge-stable", "brave-browser",
)

BROWSER_MAC_PATHS = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
)

BROWSER_WINDOWS_RELATIVE = (
    r"Microsoft\Edge\Application\msedge.exe",
    r"Google\Chrome\Application\chrome.exe",
    r"Chromium\Application\chrome.exe",
    r"BraveSoftware\Brave-Browser\Application\brave.exe",
)


def _windows_browser_roots() -> List[str]:
    roots = []
    for var in ("ProgramFiles", "ProgramFiles(x86)", "LocalAppData"):
        value = os.environ.get(var)
        if value:
            roots.append(value)
    # Sensible defaults when the environment does not carry them.
    roots.extend([r"C:\Program Files", r"C:\Program Files (x86)"])
    return roots


def find_browser() -> Optional[str]:
    """Locate a Chromium-family browser able to print a page to PDF."""
    override = os.environ.get("PRINTER_AI_BROWSER", "").strip()
    if override:
        for candidate in override.split(os.pathsep):
            candidate = candidate.strip()
            if not candidate:
                continue
            if os.path.isfile(candidate):
                return candidate
            found = shutil.which(candidate)
            if found:
                return found

    if _is_windows():
        for root in _windows_browser_roots():
            for relative in BROWSER_WINDOWS_RELATIVE:
                candidate = os.path.join(root, relative)
                if os.path.isfile(candidate):
                    return candidate
        for name in ("msedge", "chrome"):
            found = shutil.which(name)
            if found:
                return found
        return None

    if _is_macos():
        for candidate in BROWSER_MAC_PATHS:
            if os.path.isfile(candidate):
                return candidate

    for name in BROWSER_LINUX_NAMES:
        found = shutil.which(name)
        if found:
            return found
    return None


def _msoffice_com_available() -> bool:
    """True when Microsoft Office can be driven through COM on this machine."""
    if not _is_windows():
        return False
    try:
        import win32com.client  # noqa: F401
        import pythoncom  # noqa: F401
    except Exception as exc:
        logger.debug("pywin32 COM unavailable: %s", exc)
        return False
    return True


COM_TIMEOUT = 60.0


def _com_timeout() -> float:
    """Seconds an Office COM conversion may take: PRINTER_AI_COM_TIMEOUT or 60."""
    raw = os.environ.get("PRINTER_AI_COM_TIMEOUT", "").strip()
    if raw:
        try:
            value = float(raw)
            if value > 0:
                return value
        except ValueError:
            pass
        logger.warning("ignoring invalid PRINTER_AI_COM_TIMEOUT=%r", raw)
    return COM_TIMEOUT


def _run_tool(cmd: List[str], timeout: float, what: str) -> None:
    """Run an external converter, raising ConversionError on any failure.

    Never uses a shell: the file name is data, not a command line.
    """
    logger.info("%s: running %s", what, cmd)
    try:
        proc = subprocess.run(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise ConversionError(
            f"{what} timed out after {timeout:.0f}s",
            hint=f"The document may be too large or {what} is waiting for input.",
            code=504,
        )
    except FileNotFoundError as exc:
        # The executable vanished between discovery and use: a missing tool.
        raise ConversionError(f"{what} could not be started: {exc}", code=415)
    except OSError as exc:
        raise ConversionError(f"{what} could not be started: {exc}", code=500)
    output = (proc.stdout or b"").decode("utf-8", "replace").strip()
    if output:
        logger.info("%s output: %s", what, output[:2000])
    if proc.returncode != 0:
        raise ConversionError(
            f"{what} failed with exit code {proc.returncode}: {output[:500]}",
            code=500,
        )


# ==================== converters ====================


class BaseConverter:
    """A converter turns one family of formats into a PDF.

    ``available()`` answers "can this machine run me right now?" and returns
    ``(bool, detail)`` where detail is human text - the tool and its path when
    available, the reason when not.
    """

    name = "base"
    kinds: Tuple[str, ...] = ()
    extensions: Tuple[str, ...] = ()
    install_hint = ""

    def available(self) -> Tuple[bool, str]:  # pragma: no cover - abstract
        raise NotImplementedError

    def convert(self, source: str, out_dir: str, notes: List[str]) -> str:
        """Write a PDF into ``out_dir`` and return its path."""
        raise NotImplementedError  # pragma: no cover - abstract

    # -- helpers -------------------------------------------------------
    @staticmethod
    def _target(source: str, out_dir: str) -> str:
        stem = os.path.splitext(os.path.basename(source))[0] or "document"
        return os.path.join(out_dir, stem + ".pdf")


class PassthroughConverter(BaseConverter):
    """Formats a print system accepts as-is: PDF, PostScript, printer-ready."""

    name = "passthrough"
    kinds = (KIND_NATIVE,)
    extensions = tuple(sorted(NATIVE_EXTENSIONS))

    def available(self) -> Tuple[bool, str]:
        return True, "built-in"

    def convert(self, source: str, out_dir: str, notes: List[str]) -> str:
        # Nothing to do: the file is already in a form a printer understands.
        return source


class ImageConverter(BaseConverter):
    """Images to PDF: decoded by Pillow, laid out one page per frame.

    Every frame is placed scaled-to-fit and centred on a real page with a
    margin, which is what makes an arbitrary printer produce a sane print
    instead of a tiled or clipped mess.
    """

    name = "image"
    kinds = (KIND_IMAGE,)
    extensions = tuple(sorted(IMAGE_EXTENSIONS))
    install_hint = PILLOW_HINT

    def available(self) -> Tuple[bool, str]:
        try:
            import PIL
        except ImportError:
            return False, "Pillow is not installed"
        try:
            import reportlab
        except ImportError:
            return False, "reportlab is not installed"
        return True, (
            f"Pillow {getattr(PIL, '__version__', '?')} + "
            f"reportlab {getattr(reportlab, 'Version', '?')}"
        )

    @staticmethod
    def _to_rgb(frame):
        """Normalise a frame to something a PDF can carry.

        RGBA/LA/P images are flattened onto white (a PDF page has no alpha),
        CMYK and 16-bit greyscale are converted so every consumer agrees on
        the colours.
        """
        from PIL import Image

        mode = frame.mode
        if mode in ("RGBA", "LA") or (mode == "P" and "transparency" in frame.info):
            rgba = frame.convert("RGBA")
            background = Image.new("RGB", rgba.size, (255, 255, 255))
            background.paste(rgba, mask=rgba.split()[-1])
            return background
        if mode == "I;16" or mode.startswith("I;16") or mode in ("I", "F"):
            return frame.convert("L").convert("RGB")
        if mode in ("RGB", "L"):
            return frame.convert("RGB") if mode != "RGB" else frame
        # P without transparency, CMYK, YCbCr, 1-bit, ...
        return frame.convert("RGB")

    def convert(self, source: str, out_dir: str, notes: List[str]) -> str:
        ok, detail = self.available()
        if not ok:
            raise ConversionError(f"cannot convert images: {detail}",
                                  hint=self.install_hint)
        from PIL import Image, ImageOps, ImageSequence
        from reportlab.lib.utils import ImageReader
        from reportlab.pdfgen import canvas as rl_canvas

        target = self._target(source, out_dir)
        width, height = page_size()
        max_w = width - 2 * MARGIN
        max_h = height - 2 * MARGIN

        try:
            image = Image.open(source)
        except Exception as exc:
            # Pillow raises UnidentifiedImageError (or a plain OSError for a
            # truncated header): either way the bytes are not a readable image.
            raise ConversionError(
                f"the image could not be read: {exc}",
                hint="The file may be corrupt or in a format Pillow cannot decode.",
                code=422,
            )

        pdf = rl_canvas.Canvas(target, pagesize=(width, height))
        frames = 0
        with image:
            for frame in ImageSequence.Iterator(image):
                try:
                    prepared = ImageOps.exif_transpose(frame) or frame
                except Exception:  # pragma: no cover - broken EXIF
                    prepared = frame
                prepared = self._to_rgb(prepared)
                iw, ih = prepared.size
                if iw <= 0 or ih <= 0:  # pragma: no cover - defensive
                    continue
                scale = min(max_w / iw, max_h / ih)
                draw_w, draw_h = iw * scale, ih * scale
                pdf.drawImage(
                    ImageReader(prepared),
                    (width - draw_w) / 2.0,
                    (height - draw_h) / 2.0,
                    width=draw_w,
                    height=draw_h,
                )
                pdf.showPage()
                frames += 1
        if frames == 0:  # pragma: no cover - defensive
            raise ConversionError("the image contained no frames", code=422)
        pdf.save()
        notes.append(
            f"{frames} image frame(s) placed on {page_size_name()} pages"
            if frames > 1
            else f"image scaled to fit a {page_size_name()} page"
        )
        return target


class TextConverter(BaseConverter):
    """Plain text, code, logs, config and data files to PDF via reportlab.

    Monospaced so code and log columns line up, wrapped so nothing runs off
    the paper, with the file name in the header and a page number in the
    footer - the things that make a printout usable.
    """

    name = "text"
    kinds = (KIND_TEXT,)
    extensions = tuple(sorted(TEXT_EXTENSIONS))
    install_hint = REPORTLAB_HINT

    FONT = "Courier"
    FONT_SIZE = 10.0
    LEADING = 12.0
    TAB_WIDTH = 4

    def available(self) -> Tuple[bool, str]:
        try:
            import reportlab
        except ImportError:
            return False, "reportlab is not installed"
        return True, f"reportlab {getattr(reportlab, 'Version', '?')}"

    @staticmethod
    def read_text(source: str) -> str:
        """Read a file as text, never failing on a stray byte."""
        with open(source, "rb") as handle:
            raw = handle.read()
        # UTF-16 is only tried on an explicit BOM: without one it happily
        # "decodes" ordinary 8-bit text into mojibake.
        if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
            try:
                return raw.decode("utf-16")
            except UnicodeError:
                pass
        for encoding in ("utf-8", "cp1252"):
            try:
                return raw.decode(encoding)
            except (UnicodeDecodeError, UnicodeError):
                continue
        return raw.decode("utf-8", "replace")

    def _wrap(self, text: str, columns: int) -> List[str]:
        """Expand tabs and hard-wrap to ``columns`` characters."""
        import textwrap

        lines: List[str] = []
        for raw_line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
            line = raw_line.expandtabs(self.TAB_WIDTH)
            if not line:
                lines.append("")
                continue
            wrapped = textwrap.wrap(
                line,
                width=columns,
                drop_whitespace=False,
                replace_whitespace=False,
                break_long_words=True,
                break_on_hyphens=False,
            )
            lines.extend(wrapped or [""])
        return lines

    def render(self, text: str, target: str, title: str, notes: List[str]) -> str:
        from reportlab.pdfgen import canvas as rl_canvas

        width, height = page_size()
        usable_w = width - 2 * MARGIN
        # Courier is exactly 0.6 em wide.
        char_width = self.FONT_SIZE * 0.6
        columns = max(20, int(usable_w // char_width))

        header_gap = 20.0
        top = height - MARGIN - header_gap
        bottom = MARGIN + 18.0
        rows = max(1, int((top - bottom) // self.LEADING))

        lines = self._wrap(text, columns)
        if not lines:
            lines = [""]

        pdf = rl_canvas.Canvas(target, pagesize=(width, height))
        pdf.setTitle(title)
        page = 0
        for start in range(0, len(lines), rows):
            page += 1
            chunk = lines[start:start + rows]
            pdf.setFont("Helvetica", 8)
            pdf.drawString(MARGIN, height - MARGIN, title[:120])
            pdf.setLineWidth(0.4)
            pdf.line(MARGIN, height - MARGIN - 4, width - MARGIN, height - MARGIN - 4)
            pdf.setFont(self.FONT, self.FONT_SIZE)
            y = top
            for line in chunk:
                pdf.drawString(MARGIN, y, line)
                y -= self.LEADING
            pdf.setFont("Helvetica", 8)
            pdf.drawCentredString(width / 2.0, MARGIN - 4, f"Page {page}")
            pdf.showPage()
        pdf.save()
        notes.append(f"{len(lines)} line(s) on {page} page(s), wrapped at {columns} columns")
        return target

    def convert(self, source: str, out_dir: str, notes: List[str]) -> str:
        ok, detail = self.available()
        if not ok:
            raise ConversionError(f"cannot render text: {detail}", hint=self.install_hint)
        text = self.read_text(source)
        return self.render(text, self._target(source, out_dir),
                           os.path.basename(source), notes)


class BrowserConverter(BaseConverter):
    """HTML and SVG to PDF through a headless Chromium-family browser.

    A browser is the only thing that renders real HTML/CSS/SVG faithfully;
    LibreOffice is kept as a fallback because it is far more likely to be
    installed on a Linux box than Chrome is.
    """

    name = "browser"
    kinds = (KIND_HTML, KIND_SVG)
    extensions = tuple(sorted(HTML_EXTENSIONS | SVG_EXTENSIONS))
    install_hint = BROWSER_HINT
    TIMEOUT = 60.0

    def available(self) -> Tuple[bool, str]:
        browser = find_browser()
        if browser:
            return True, f"browser at {browser}"
        if find_soffice():
            return True, f"LibreOffice at {find_soffice()} (fallback, basic HTML only)"
        return False, "no Chromium-family browser and no LibreOffice found"

    def convert(self, source: str, out_dir: str, notes: List[str]) -> str:
        browser = find_browser()
        if not browser:
            if find_soffice():
                notes.append(
                    "no browser found - rendered with LibreOffice, which supports "
                    "only basic HTML/CSS"
                )
                return OfficeConverter().convert(source, out_dir, notes)
            raise ConversionError(
                "no Chromium-family browser is available to render HTML/SVG",
                hint=BROWSER_HINT,
            )

        target = self._target(source, out_dir)
        # Chrome refuses to start against a profile another instance holds, so
        # every run gets a throwaway profile of its own.
        profile = tempfile.mkdtemp(prefix="printer-ai-browser-")
        try:
            cmd = [
                browser,
                "--headless=new",
                "--disable-gpu",
                "--no-pdf-header-footer",
                f"--user-data-dir={profile}",
                f"--print-to-pdf={target}",
                _file_url(source),
            ]
            _run_tool(cmd, self.TIMEOUT, f"browser ({os.path.basename(browser)})")
            if not os.path.isfile(target):
                raise ConversionError(
                    "the browser produced no PDF", hint=BROWSER_HINT, code=500
                )
        except ConversionError as exc:
            # A browser that is installed but cannot run headless here (a
            # sandbox, a locked profile, a kiosk policy) must not make the
            # document unprintable when LibreOffice can still render it.
            if not find_soffice():
                raise
            logger.warning("browser rendering failed (%s); falling back to LibreOffice", exc)
            notes.append(
                f"{os.path.basename(browser)} could not render the page ({exc}); "
                "used LibreOffice instead, which supports only basic HTML/CSS"
            )
            return OfficeConverter().convert_with_libreoffice(source, out_dir, notes)
        finally:
            shutil.rmtree(profile, ignore_errors=True)

        notes.append(f"rendered by {os.path.basename(browser)} (headless)")
        return target


class MarkdownConverter(BaseConverter):
    """Markdown to PDF: rendered to HTML, then handed to the HTML converter."""

    name = "markdown"
    kinds = (KIND_MARKDOWN,)
    extensions = tuple(sorted(MARKDOWN_EXTENSIONS))
    install_hint = MARKDOWN_HINT

    CSS = (
        "body{font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;"
        "font-size:11pt;line-height:1.5;margin:2em;color:#111}"
        "h1,h2,h3,h4{line-height:1.25;margin:1.2em 0 .5em}"
        "h1{font-size:1.9em;border-bottom:1px solid #ddd;padding-bottom:.2em}"
        "h2{font-size:1.45em;border-bottom:1px solid #eee;padding-bottom:.15em}"
        "code,pre{font-family:Menlo,Consolas,monospace;font-size:.9em}"
        "pre{background:#f6f6f6;padding:.8em;border-radius:4px;"
        "white-space:pre-wrap;word-wrap:break-word}"
        "code{background:#f2f2f2;padding:.1em .3em;border-radius:3px}"
        "pre code{background:none;padding:0}"
        "table{border-collapse:collapse;margin:1em 0}"
        "th,td{border:1px solid #bbb;padding:.4em .6em;text-align:left}"
        "th{background:#f2f2f2}"
        "blockquote{margin:1em 0;padding:.2em 1em;border-left:4px solid #ddd;color:#555}"
        "img{max-width:100%}"
    )

    def available(self) -> Tuple[bool, str]:
        try:
            import markdown
        except ImportError:
            return False, "the markdown package is not installed"
        html_ok, html_detail = BrowserConverter().available()
        if html_ok:
            return True, f"markdown {getattr(markdown, '__version__', '?')} -> {html_detail}"
        text_ok, _ = TextConverter().available()
        if text_ok:
            return True, (
                f"markdown {getattr(markdown, '__version__', '?')} "
                "(no HTML renderer - falls back to plain text)"
            )
        return False, html_detail

    def to_html(self, source: str) -> str:
        """Render the Markdown file to a standalone HTML document."""
        import markdown as markdown_lib

        text = TextConverter.read_text(source)
        body = markdown_lib.markdown(
            text, extensions=["fenced_code", "tables"]
        )
        title = _html_escape(os.path.basename(source))
        return (
            "<!DOCTYPE html><html><head><meta charset=\"utf-8\">"
            f"<title>{title}</title><style>{self.CSS}</style></head>"
            f"<body>{body}</body></html>"
        )

    def convert(self, source: str, out_dir: str, notes: List[str]) -> str:
        try:
            import markdown  # noqa: F401
        except ImportError:
            raise ConversionError(
                "the markdown package is not installed", hint=MARKDOWN_HINT
            )

        html_converter = BrowserConverter()
        if not find_browser() and not find_soffice():
            # No HTML renderer at all: print the Markdown source itself rather
            # than failing - the content is still readable that way.
            notes.append(
                "no HTML renderer available - printed the Markdown source as plain text"
            )
            return TextConverter().convert(source, out_dir, notes)

        scratch = tempfile.mkdtemp(prefix="printer-ai-md-")
        try:
            stem = os.path.splitext(os.path.basename(source))[0] or "document"
            html_path = os.path.join(scratch, stem + ".html")
            with open(html_path, "w", encoding="utf-8") as handle:
                handle.write(self.to_html(source))
            produced = html_converter.convert(html_path, out_dir, notes)
            notes.append("Markdown rendered through HTML")
            return produced
        finally:
            shutil.rmtree(scratch, ignore_errors=True)


class OfficeConverter(BaseConverter):
    """Office documents to PDF: Microsoft Office via COM, else LibreOffice.

    On Windows, real Office produces the layout the author saw, so it is tried
    first when pywin32 is present. Everywhere else (and as the Windows
    fallback) LibreOffice does the job headlessly.
    """

    name = "office"
    kinds = (KIND_OFFICE, KIND_CSV)
    extensions = tuple(sorted(OFFICE_EXTENSIONS | CSV_EXTENSIONS))
    install_hint = LIBREOFFICE_HINT
    TIMEOUT = 120.0

    WORD_EXTENSIONS = {".doc", ".docx", ".docm", ".dot", ".dotx", ".odt", ".ott",
                       ".rtf", ".txt", ".wps"}
    EXCEL_EXTENSIONS = {".xls", ".xlsx", ".xlsm", ".xlt", ".xltx", ".ods",
                        ".csv", ".tsv"}
    POWERPOINT_EXTENSIONS = {".ppt", ".pptx", ".pptm", ".pps", ".ppsx", ".pot",
                             ".potx", ".odp", ".otp"}

    def available(self) -> Tuple[bool, str]:
        if _msoffice_com_available():
            return True, "Microsoft Office via COM (LibreOffice as fallback)"
        soffice = find_soffice()
        if soffice:
            return True, f"LibreOffice at {soffice}"
        return False, "neither Microsoft Office (COM) nor LibreOffice found"

    # -- LibreOffice ---------------------------------------------------
    def convert_with_libreoffice(self, source: str, out_dir: str,
                                 notes: List[str]) -> str:
        soffice = find_soffice()
        if not soffice:
            raise ConversionError(
                "LibreOffice is not installed, so this document cannot be converted",
                hint=LIBREOFFICE_HINT,
            )

        # A private user profile is not optional: with the shared default
        # profile, a conversion started while a LibreOffice window is open
        # silently produces nothing at all.
        profile = tempfile.mkdtemp(prefix="printer-ai-lo-")
        scratch = tempfile.mkdtemp(prefix="printer-ai-lo-out-")
        try:
            cmd = [
                soffice,
                f"-env:UserInstallation={_file_url(profile)}",
                "--headless",
                "--norestore",
                "--nologo",
                "--convert-to", "pdf",
                "--outdir", scratch,
                os.path.abspath(source),
            ]
            _run_tool(cmd, self.TIMEOUT, "LibreOffice")

            produced = [
                os.path.join(scratch, name)
                for name in sorted(os.listdir(scratch))
                if name.lower().endswith(".pdf")
            ]
            if not produced:
                # LibreOffice exits 0 but writes nothing for a document it
                # cannot open: corrupt, or password protected (it never
                # prompts in headless mode). That is a property of the file.
                raise ConversionError(
                    "LibreOffice produced no PDF for this document",
                    hint=(
                        "The file may be corrupt or password protected. "
                        + LIBREOFFICE_HINT
                    ),
                    code=422,
                )
            target = self._target(source, out_dir)
            shutil.move(produced[0], target)
            notes.append(f"converted by LibreOffice ({os.path.basename(soffice)})")
            return target
        finally:
            shutil.rmtree(profile, ignore_errors=True)
            shutil.rmtree(scratch, ignore_errors=True)

    # -- Microsoft Office (COM) ---------------------------------------
    #: msoAutomationSecurityForceDisable - macros in the opened document
    #: never run, whatever the user's Trust Center says.
    AUTOMATION_SECURITY_FORCE_DISABLE = 3

    #: Top-level window class of each Office application, used to find the
    #: process behind a COM server so a hung one can be killed by PID.
    OFFICE_WINDOW_CLASSES = {
        "Word.Application": "OpusApp",
        "Excel.Application": "XLMAIN",
        "PowerPoint.Application": "PPTFrameClass",
    }

    @classmethod
    def _office_window_pids(cls, prog_id: str) -> set:
        """PIDs of every process owning a top-level window of ``prog_id``'s class.

        Best effort: returns an empty set when the win32 modules are missing
        or enumeration fails.
        """
        wanted = cls.OFFICE_WINDOW_CLASSES.get(prog_id)
        if not wanted:
            return set()
        try:
            import win32gui
            import win32process
        except Exception as exc:  # pragma: no cover - only off Windows
            logger.debug("win32gui/win32process unavailable: %s", exc)
            return set()

        pids: set = set()

        def visit(hwnd, _extra):
            try:
                if win32gui.GetClassName(hwnd) == wanted:
                    pids.add(win32process.GetWindowThreadProcessId(hwnd)[1])
            except Exception:  # a window may vanish mid-enumeration
                pass
            return True

        try:
            win32gui.EnumWindows(visit, None)
        except Exception as exc:
            logger.debug("EnumWindows failed: %s", exc)
        return pids

    @classmethod
    def _office_pid(cls, app: Any, prog_id: str, pids_before: set) -> Optional[int]:
        """Work out the process id behind a freshly started Office COM server.

        Excel and PowerPoint expose ``Hwnd``; Word does not, so the fallback
        is to find a top-level window of the application's class owned by a
        process that did not exist before ``DispatchEx``.
        """
        hwnd = None
        try:
            hwnd = getattr(app, "Hwnd", None)
        except Exception:  # COM property may throw on a wedged server
            hwnd = None
        if hwnd:
            try:
                import win32process

                return int(win32process.GetWindowThreadProcessId(hwnd)[1])
            except Exception as exc:
                logger.debug("GetWindowThreadProcessId(%r) failed: %s", hwnd, exc)

        fresh = cls._office_window_pids(prog_id) - set(pids_before)
        if fresh:
            return int(sorted(fresh)[0])
        return None

    @staticmethod
    def _terminate_pid(pid: int) -> bool:
        """Kill ``pid`` outright via the Win32 API; True when it was signalled.

        Used only for an Office instance that has stopped responding to COM.
        ``Quit()`` cannot be used for that: the object lives in the worker
        thread's apartment and calling it from another thread either fails
        with RPC_E_WRONG_THREAD or blocks on the hung message pump.
        """
        try:
            import win32api
            import win32process
        except Exception as exc:  # pragma: no cover - only off Windows
            logger.warning("cannot terminate pid %s: %s", pid, exc)
            return False
        PROCESS_TERMINATE = 0x0001
        try:
            handle = win32api.OpenProcess(PROCESS_TERMINATE, False, pid)
        except Exception as exc:
            logger.warning("OpenProcess(%s) failed: %s", pid, exc)
            return False
        try:
            win32process.TerminateProcess(handle, 1)
            return True
        except Exception as exc:
            logger.warning("TerminateProcess(%s) failed: %s", pid, exc)
            return False
        finally:
            try:
                win32api.CloseHandle(handle)
            except Exception:  # pragma: no cover - best effort
                pass

    def _dispatch(self, prog_id: str, state: Dict[str, Any]) -> Any:
        """``DispatchEx`` plus bookkeeping so a hung server can be killed later."""
        import win32com.client

        pids_before = self._office_window_pids(prog_id)
        app = win32com.client.DispatchEx(prog_id)
        state["app"] = app
        state["prog_id"] = prog_id
        try:
            state["pid"] = self._office_pid(app, prog_id, pids_before)
        except Exception as exc:  # never let bookkeeping break the conversion
            logger.debug("could not determine the %s pid: %s", prog_id, exc)
            state["pid"] = None
        return app

    def _com_export(self, ext: str, source: str, target: str,
                    state: Dict[str, Any]) -> None:
        """The COM conversion proper; runs inside the worker thread.

        ``state["pid"]`` is set as soon as the application exists so the
        caller can terminate it if this thread never returns. Macros are
        disabled before any document is opened, and every open uses flags
        that never prompt, repair, convert or touch the recent-files list.
        """
        if ext in self.WORD_EXTENSIONS:
            app = self._dispatch("Word.Application", state)
            try:
                app.AutomationSecurity = self.AUTOMATION_SECURITY_FORCE_DISABLE
                app.Visible = False
                app.DisplayAlerts = False
                document = app.Documents.Open(
                    source,
                    ReadOnly=True,
                    AddToRecentFiles=False,
                    ConfirmConversions=False,
                    OpenAndRepair=False,
                )
                try:
                    # 17 == wdExportFormatPDF
                    document.ExportAsFixedFormat(target, 17)
                finally:
                    document.Close(False)
            finally:
                app.Quit()
        elif ext in self.EXCEL_EXTENSIONS:
            app = self._dispatch("Excel.Application", state)
            try:
                app.AutomationSecurity = self.AUTOMATION_SECURITY_FORCE_DISABLE
                app.Visible = False
                app.DisplayAlerts = False
                # UpdateLinks=0: never follow external links in the workbook.
                workbook = app.Workbooks.Open(source, ReadOnly=True, UpdateLinks=0)
                try:
                    # 0 == xlTypePDF
                    workbook.ExportAsFixedFormat(0, target)
                finally:
                    workbook.Close(False)
            finally:
                app.Quit()
        elif ext in self.POWERPOINT_EXTENSIONS:
            app = self._dispatch("PowerPoint.Application", state)
            try:
                app.AutomationSecurity = self.AUTOMATION_SECURITY_FORCE_DISABLE
                presentation = app.Presentations.Open(
                    source, ReadOnly=True, WithWindow=False
                )
                try:
                    # 32 == ppSaveAsPDF
                    presentation.SaveAs(target, 32)
                finally:
                    presentation.Close()
            finally:
                app.Quit()

    def convert_with_com(self, source: str, target: str,
                         timeout: Optional[float] = None) -> None:
        """Drive Microsoft Office through COM to export a PDF.

        Windows only, and only when pywin32 is installed. Every application is
        quit in a ``finally`` block: an orphaned invisible WINWORD.EXE would
        block the next conversion forever.

        The conversion runs in a worker thread with its own COM apartment and
        is given ``timeout`` seconds (default :func:`_com_timeout`, i.e.
        ``PRINTER_AI_COM_TIMEOUT`` or 60). Office can hang on a modal dialog
        that ``DisplayAlerts = False`` does not cover; without a timeout the
        CLI would hang with it.
        """
        import pythoncom  # noqa: F401 - fail early, like the worker would
        import win32com.client  # noqa: F401

        source = os.path.abspath(source)
        target = os.path.abspath(target)
        ext = os.path.splitext(source)[1].lower()
        if timeout is None:
            timeout = _com_timeout()

        if not (
            ext in self.WORD_EXTENSIONS
            or ext in self.EXCEL_EXTENSIONS
            or ext in self.POWERPOINT_EXTENSIONS
        ):
            raise ConversionError(
                f"no Microsoft Office application handles '{ext}'", code=415
            )

        state: Dict[str, Any] = {"app": None, "error": None, "pid": None,
                                 "prog_id": None}

        def worker() -> None:
            import pythoncom as _pythoncom

            # COM apartments are per thread, so initialise here, not outside.
            _pythoncom.CoInitialize()
            try:
                self._com_export(ext, source, target, state)
            except BaseException as exc:  # re-raised in the calling thread
                state["error"] = exc
            finally:
                try:
                    _pythoncom.CoUninitialize()
                except Exception:  # pragma: no cover - best effort
                    pass

        thread = threading.Thread(
            target=worker, name="printer-ai-office-com", daemon=True
        )
        thread.start()
        thread.join(timeout)
        if thread.is_alive():
            # The worker is stuck inside Office. The COM object belongs to
            # the worker's apartment, so Quit() from here would fail or hang;
            # kill the process by PID instead so it does not linger as an
            # invisible WINWORD/EXCEL/POWERPNT. The daemon thread is
            # abandoned (it dies with the interpreter).
            pid = state.get("pid")
            killed = False
            if pid:
                killed = self._terminate_pid(pid)
            if not killed:
                logger.warning(
                    "hung %s (pid %s) could not be terminated; it may linger",
                    state.get("prog_id") or "Office application", pid,
                )
            raise ConversionError(
                f"Office COM conversion timed out after {timeout:.0f}s",
                hint=(
                    "Microsoft Office did not finish exporting the document. It "
                    "may be waiting on a dialog; set PRINTER_AI_COM_TIMEOUT to "
                    "allow more time, or install LibreOffice as a fallback."
                ),
                code=504,
            )
        if state["error"] is not None:
            raise state["error"]

    def convert(self, source: str, out_dir: str, notes: List[str]) -> str:
        target = self._target(source, out_dir)
        ext = os.path.splitext(source)[1].lower()
        known_to_office = (
            ext in self.WORD_EXTENSIONS
            or ext in self.EXCEL_EXTENSIONS
            or ext in self.POWERPOINT_EXTENSIONS
        )
        if _msoffice_com_available() and known_to_office:
            try:
                self.convert_with_com(source, target)
                if os.path.isfile(target):
                    notes.append("converted by Microsoft Office (COM)")
                    return target
                logger.warning("Office COM reported success but wrote no file")
            except ConversionError as exc:
                # A hung (504) or crashed (500) Office is a problem with the
                # tool, not the document: give LibreOffice a turn when it is
                # installed. Document problems (415/422) are final.
                if exc.code in (504, 500) and find_soffice():
                    logger.warning(
                        "Office COM failed (%s), trying LibreOffice", exc
                    )
                    notes.append(
                        "Office COM failed/timed out; converted via "
                        f"LibreOffice instead ({exc})"
                    )
                else:
                    raise
            except Exception as exc:
                logger.warning("Office COM conversion failed (%s), trying LibreOffice", exc)
                notes.append(f"Microsoft Office was unusable ({exc}); used LibreOffice")
        return self.convert_with_libreoffice(source, out_dir, notes)


class CsvConverter(OfficeConverter):
    """CSV/TSV: a spreadsheet when one is available, plain text otherwise.

    A table rendered by LibreOffice is far easier to read on paper than raw
    comma-separated lines, but a missing LibreOffice must not stop a CSV from
    printing at all.
    """

    name = "csv"
    kinds = (KIND_CSV,)
    extensions = tuple(sorted(CSV_EXTENSIONS))

    def available(self) -> Tuple[bool, str]:
        ok, detail = super().available()
        if ok:
            return True, detail + " (table layout)"
        text_ok, text_detail = TextConverter().available()
        if text_ok:
            return True, f"{text_detail} (plain text - install LibreOffice for a table)"
        return False, detail

    def convert(self, source: str, out_dir: str, notes: List[str]) -> str:
        if find_soffice() or _msoffice_com_available():
            return super().convert(source, out_dir, notes)
        notes.append("no spreadsheet application found - printed as plain text")
        return TextConverter().convert(source, out_dir, notes)


# ==================== registry ====================

PASSTHROUGH = PassthroughConverter()
IMAGE = ImageConverter()
TEXT = TextConverter()
MARKDOWN = MarkdownConverter()
BROWSER = BrowserConverter()
OFFICE = OfficeConverter()
CSV = CsvConverter()

REGISTRY: Tuple[BaseConverter, ...] = (
    PASSTHROUGH, IMAGE, TEXT, MARKDOWN, BROWSER, OFFICE, CSV,
)

_KIND_CONVERTERS: Dict[str, BaseConverter] = {
    KIND_NATIVE: PASSTHROUGH,
    KIND_IMAGE: IMAGE,
    KIND_TEXT: TEXT,
    KIND_MARKDOWN: MARKDOWN,
    KIND_HTML: BROWSER,
    KIND_SVG: BROWSER,
    KIND_OFFICE: OFFICE,
    KIND_CSV: CSV,
}


def converter_for_kind(kind: str) -> Optional[BaseConverter]:
    return _KIND_CONVERTERS.get(kind)


def format_catalog() -> List[Dict[str, Any]]:
    """Describe every converter for the CLI's ``formats`` command."""
    catalog = []
    for converter in REGISTRY:
        ok, detail = converter.available()
        entry: Dict[str, Any] = {
            "converter": converter.name,
            "extensions": list(converter.extensions),
            "available": ok,
            "via": detail if ok else None,
            "detail": detail,
        }
        if not ok:
            entry["install_hint"] = converter.install_hint or detail
        catalog.append(entry)
    return catalog


# ==================== helpers ====================


def _file_url(path: str) -> str:
    """An absolute file:// URL that Chrome and LibreOffice both accept."""
    from urllib.request import pathname2url

    return "file:///" + pathname2url(os.path.abspath(path)).lstrip("/")


def _html_escape(text: str) -> str:
    import html

    return html.escape(text, quote=True)


# ==================== entry point ====================


def to_pdf(path: str, out_dir: Optional[str] = None) -> ConvertResult:
    """Turn any supported file into a PDF a printer backend can accept.

    Args:
        path: the file to convert.
        out_dir: where to put the PDF. When omitted a temporary directory is
            created and ``ConvertResult.temp`` is True - the caller must then
            call :func:`cleanup` once the PDF has been read.

    Returns:
        ConvertResult. ``converted`` is False for files that were already
        printable (``native`` True), True for everything else.

    Raises:
        ConversionError: with a ``hint`` naming the missing tool.
    """
    if not os.path.isfile(path):
        raise ConversionError(
            f"file not found: {path}",
            hint="Check the path; the file must exist and be readable.",
            source_path=path,
            code=404,
        )
    if os.path.getsize(path) == 0:
        raise ConversionError(
            "the file is empty, there is nothing to print",
            hint="Check that the file was written completely.",
            source_path=path,
            detected="empty",
            code=422,
        )

    kind, how = detect_format(path)
    logger.info("detected %s as %s (by %s)", path, kind, how)

    if kind == KIND_UNSUPPORTED:
        raise ConversionError(
            f"unsupported file format: {os.path.basename(path)}",
            hint=(
                "This file is neither a PDF, an image, an Office document, nor "
                "readable text. Convert it to PDF yourself, or pass --raw to send "
                "the bytes to the printer unchanged."
            ),
            source_path=path,
            detected=kind,
        )

    converter = converter_for_kind(kind)
    if converter is None:  # pragma: no cover - registry is exhaustive
        raise ConversionError(
            f"no converter is registered for '{kind}'",
            source_path=path,
            detected=kind,
        )

    notes: List[str] = []
    if how == "magic-override":
        notes.append(
            f"the extension did not match the content; handled as {kind} "
            "based on the file's bytes"
        )

    if isinstance(converter, PassthroughConverter):
        if _looks_like_pdf(path) and pdf_is_encrypted(path):
            raise ConversionError(
                "password-protected or encrypted PDF",
                hint=(
                    "The printer cannot open an encrypted PDF. Remove the "
                    "password (e.g. print/export it to a new PDF from a viewer "
                    "after entering the password) and print the result."
                ),
                source_path=path,
                detected=kind,
                converter=converter.name,
                code=422,
            )
        return ConvertResult(
            pdf_path=os.path.abspath(path),
            source_path=os.path.abspath(path),
            converter=converter.name,
            converted=False,
            native=True,
            temp=False,
            notes=notes + ["already in a printable format"],
        )

    ok, detail = converter.available()
    if not ok:
        raise ConversionError(
            f"cannot convert {os.path.basename(path)}: {detail}",
            hint=converter.install_hint or detail,
            source_path=path,
            detected=kind,
            converter=converter.name,
        )

    temp = out_dir is None
    target_dir = out_dir or tempfile.mkdtemp(prefix="printer-ai-pdf-")
    os.makedirs(target_dir, exist_ok=True)

    try:
        pdf_path = converter.convert(path, target_dir, notes)
    except ConversionError as exc:
        if temp:
            shutil.rmtree(target_dir, ignore_errors=True)
        exc.source_path = exc.source_path or path
        exc.detected = exc.detected or kind
        exc.converter = exc.converter or converter.name
        raise
    except Exception as exc:
        if temp:
            shutil.rmtree(target_dir, ignore_errors=True)
        # An unexpected exception inside a converter: a crash, not a format
        # or install problem.
        raise ConversionError(
            f"conversion failed: {exc}",
            hint=converter.install_hint,
            source_path=path,
            detected=kind,
            converter=converter.name,
            code=500,
        )

    if not os.path.isfile(pdf_path):  # pragma: no cover - defensive
        if temp:
            shutil.rmtree(target_dir, ignore_errors=True)
        raise ConversionError(
            "the converter reported success but produced no PDF",
            hint=converter.install_hint,
            source_path=path,
            detected=kind,
            converter=converter.name,
            code=500,
        )

    return ConvertResult(
        pdf_path=os.path.abspath(pdf_path),
        source_path=os.path.abspath(path),
        converter=converter.name,
        converted=True,
        native=False,
        temp=temp,
        notes=notes,
    )


def cleanup(result: Optional[ConvertResult]) -> None:
    """Delete the temporary directory ``to_pdf`` created, if it made one.

    Safe to call with anything: a permanent result, None, or twice.
    """
    if result is None or not result.temp:
        return
    directory = os.path.dirname(result.pdf_path)
    if not directory:
        return
    if os.path.basename(directory).startswith("printer-ai-"):
        shutil.rmtree(directory, ignore_errors=True)
    else:  # pragma: no cover - defensive: never delete a stranger's directory
        logger.warning("refusing to remove unexpected temp directory %s", directory)


__all__ = [
    "ConvertResult",
    "ConversionError",
    "to_pdf",
    "cleanup",
    "detect_format",
    "sniff_format",
    "format_catalog",
    "page_size",
    "page_size_name",
    "find_soffice",
    "find_browser",
    "REGISTRY",
]
