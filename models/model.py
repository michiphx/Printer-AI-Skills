"""
Data models for printer operations
"""

from dataclasses import dataclass, asdict, fields
from typing import List, Optional, Dict, Any
from enum import Enum


class PrinterStatus(Enum):
    """Printer status enumeration"""

    IDLE = "idle"
    PROCESSING = "processing"
    STOPPED = "stopped"
    UNKNOWN = "unknown"

    @classmethod
    def from_cups_state(cls, state: int) -> "PrinterStatus":
        """Convert CUPS printer state to PrinterStatus enum"""
        state_map = {3: cls.IDLE, 4: cls.PROCESSING, 5: cls.STOPPED}
        return state_map.get(state, cls.UNKNOWN)

    @classmethod
    def from_string(cls, status_str: str) -> "PrinterStatus":
        """Convert string status to PrinterStatus enum"""
        status_map = {
            "idle": cls.IDLE,
            "processing": cls.PROCESSING,
            "stopped": cls.STOPPED,
            "unknown": cls.UNKNOWN,
        }
        return status_map.get(status_str.lower(), cls.UNKNOWN)


@dataclass
class Printer:
    """Printer information model"""

    index: int
    name: str
    status: PrinterStatus
    status_reasons: List[str]
    is_accepting: bool
    type: str
    is_default: bool
    location: str = ""
    model: str = ""
    uri: str = ""
    driver: str = ""
    port: str = ""
    job_count: int = 0

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary"""
        data = asdict(self)
        # Convert PrinterStatus enum to string value
        if "status" in data and isinstance(data["status"], PrinterStatus):
            data["status"] = data["status"].value
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Printer":
        """Create from dictionary.

        The input is copied and unknown keys are dropped, so a caller's dict is
        never mutated and a backend that grows an extra field cannot break this.
        """
        known = {f.name for f in fields(cls)}
        values = {k: v for k, v in data.items() if k in known}
        # Convert string status to PrinterStatus enum
        if isinstance(values.get("status"), str):
            values["status"] = PrinterStatus.from_string(values["status"])
        return cls(**values)


@dataclass
class APIResponse:
    """Unified API response model"""

    code: int
    msg: str
    data: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary"""
        return asdict(self)

    @classmethod
    def success(cls, data: Optional[Dict[str, Any]] = None) -> "APIResponse":
        """Create success response"""
        return cls(code=200, msg="success", data=data or {})

    @classmethod
    def error(cls, code: int, msg: str, data: Optional[Dict[str, Any]] = None) -> "APIResponse":
        """Create error response"""
        return cls(code=code, msg=msg, data=data or {})

    @classmethod
    def not_found(
        cls, msg: str = "Resource not found", data: Optional[Dict[str, Any]] = None
    ) -> "APIResponse":
        """Create not found response"""
        return cls(code=404, msg=msg, data=data or {})

    @classmethod
    def server_error(cls, msg: str, data: Optional[Dict[str, Any]] = None) -> "APIResponse":
        """Create server error response"""
        return cls(code=500, msg=msg, data=data or {})

    @classmethod
    def unsupported_media_type(
        cls, msg: str, data: Optional[Dict[str, Any]] = None
    ) -> "APIResponse":
        """Create a 415 response: the file format cannot be handled"""
        return cls(code=415, msg=msg, data=data or {})

    @classmethod
    def not_implemented(cls, msg: str, data: Optional[Dict[str, Any]] = None) -> "APIResponse":
        """Create a 501 response: this platform/backend cannot do it"""
        return cls(code=501, msg=msg, data=data or {})


@dataclass
class PrintJob:
    """Print job information model"""

    job_id: int
    printer_name: str
    job_name: str
    status: str
    priority: int = 0
    size: int = 0
    pages: int = 0
    user: str = ""
    submitted_time: int = 0
    total_pages: int = 0
    pages_printed: int = 0

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary"""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PrintJob":
        """Create from dictionary, dropping unknown keys"""
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})




@dataclass
class WindowsPrintOptions:
    """Windows-specific print options using dmXXX format"""

    # Device Mode parameters for Windows API
    dmOrientation: Optional[int] = None  # 1=Portrait, 2=Landscape
    dmCopies: Optional[int] = None  # Number of copies
    dmColor: Optional[int] = None  # 1=Monochrome, 2=Color
    dmPaperSize: Optional[int] = None  # Paper size constant
    dmDuplex: Optional[int] = None  # 1=Simplex, 2=Vertical, 3=Horizontal
    dmDefaultSource: Optional[int] = None  # Paper source/bin
    dmMediaType: Optional[int] = None  # Media type
    dmPaperLength: Optional[int] = None  # Custom paper length (0.1mm units)
    dmPaperWidth: Optional[int] = None  # Custom paper width (0.1mm units)
    dmPrintQuality: Optional[int] = None  # Print quality
    dmCollate: Optional[int] = None  # 1=Collate, 0=No collate

    # Extra options not defined above
    extra_options: Optional[Dict[str, Any]] = None

    # CUPS/IPP keys with an obvious DEVMODE equivalent. An agent that learned
    # the Linux dialect ({"copies": 2, "sides": "two-sided-long-edge"}) gets
    # the same result on Windows instead of a silent no-op. Value maps are
    # keyed by the IPP value; a value with no entry is left in extra_options
    # (and reported as ignored) rather than guessed.
    #: Upper bound for a translated ``copies`` value. A caller asking for more
    #: than this almost certainly made a mistake (e.g. a fat-fingered JSON
    #: value); rather than clamp it silently and surprise them with a
    #: plausible-looking copy count, the value is left untranslated so it
    #: shows up in ``extra_options``/``ignored_options`` instead. This also
    #: protects local_printer.win_render.page_order from being asked to
    #: materialise an unbounded page-index list.
    MAX_COPIES = 999

    _CUPS_COPIES_KEYS = ("copies",)
    _CUPS_VALUE_MAPS = {
        # ipp key -> (dm field, {ipp value: dm value})
        "print-color-mode": ("dmColor", {"monochrome": 1, "color": 2}),
        "sides": (
            "dmDuplex",
            {"one-sided": 1, "two-sided-long-edge": 2, "two-sided-short-edge": 3},
        ),
        "orientation-requested": ("dmOrientation", {"3": 1, "4": 2}),
    }

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary, excluding None values and merging extra_options"""
        result = {k: v for k, v in asdict(self).items() if v is not None and k != "extra_options"}
        if self.extra_options:
            result.update(self.extra_options)
        return result

    @classmethod
    def _translate_cups_key(cls, key: str, value: Any) -> Optional[tuple]:
        """Map one CUPS-style option to ``(dm_field, dm_value)``.

        Accepts the hyphenated IPP spelling and the snake_case one the Linux
        model also takes. Returns None when the key or the value has no clean
        DEVMODE equivalent.
        """
        ipp_key = key.replace("_", "-")
        if ipp_key in cls._CUPS_COPIES_KEYS:
            if isinstance(value, bool):
                return None
            try:
                copies = int(value)
            except (TypeError, ValueError):
                return None
            if copies < 1 or copies > cls.MAX_COPIES:
                return None
            return ("dmCopies", copies)
        mapping = cls._CUPS_VALUE_MAPS.get(ipp_key)
        if mapping is None or isinstance(value, bool):
            return None
        dm_field, values = mapping
        dm_value = values.get(str(value).strip().lower())
        return (dm_field, dm_value) if dm_value is not None else None

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "WindowsPrintOptions":
        """Create from dictionary, unknown keys are stored in extra_options.

        CUPS-style keys with a direct DEVMODE equivalent (``copies``,
        ``print-color-mode``, ``sides``, ``orientation-requested``) are
        translated to the matching dmXXX field. An explicit dmXXX key always
        wins over a translated one. Anything else lands in extra_options.
        """
        known_fields = {f.name for f in fields(cls)} - {"extra_options"}
        known = {k: v for k, v in data.items() if k in known_fields}
        extra: Dict[str, Any] = {}
        for k, v in data.items():
            if k in known_fields:
                continue
            translated = cls._translate_cups_key(k, v)
            if translated is not None and translated[0] not in known:
                known[translated[0]] = translated[1]
            else:
                # No clean equivalent, or the caller also set the dmXXX field
                # (dm wins): keep it visible so the backend can report it.
                extra[k] = v
        return cls(**known, extra_options=extra if extra else None)




@dataclass
class LinuxPrintOptions:
    """Linux/CUPS-specific print options using IPP format"""

    # Standard CUPS/IPP options
    copies: Optional[str] = None  # Number of copies as string
    media: Optional[str] = None  # Paper size (e.g., "A4", "Letter")
    orientation_requested: Optional[str] = None  # "3"=Portrait, "4"=Landscape
    print_color_mode: Optional[str] = None  # "monochrome", "color"
    print_quality: Optional[str] = None  # "3"=Draft, "4"=Normal, "5"=High
    sides: Optional[str] = (
        None  # "one-sided", "two-sided-long-edge", "two-sided-short-edge"
    )
    page_ranges: Optional[str] = None  # "1-5,10-15"
    number_up: Optional[str] = None  # Pages per sheet
    fit_to_page: Optional[str] = None  # "true", "false"
    scaling: Optional[str] = None  # Scaling percentage
    media_source: Optional[str] = None  # Paper source/tray
    media_type: Optional[str] = None  # Media type
    resolution: Optional[str] = None  # Print resolution

    # Extra options not defined above
    extra_options: Optional[Dict[str, Any]] = None

    # Field name -> IPP attribute name. CUPS/IPP option names are hyphenated;
    # the dataclass fields use underscores because Python identifiers must.
    # Fields without an underscore (copies, media, sides, scaling, resolution)
    # are the same in both dialects and need no entry.
    IPP_NAMES = {
        "orientation_requested": "orientation-requested",
        "print_color_mode": "print-color-mode",
        "print_quality": "print-quality",
        "page_ranges": "page-ranges",
        "number_up": "number-up",
        "fit_to_page": "fit-to-page",
        "media_source": "media-source",
        "media_type": "media-type",
    }

    @staticmethod
    def _to_ipp_value(value: Any) -> str:
        """CUPS (pycups) accepts option values as strings only."""
        if isinstance(value, bool):
            return "true" if value else "false"
        return str(value)

    def to_dict(self) -> Dict[str, str]:
        """Convert to the dict pycups' printFile() expects.

        Keys are the hyphenated IPP attribute names (``print-color-mode``,
        not ``print_color_mode``), None values are dropped, every remaining
        value is coerced to ``str`` (pycups raises TypeError on anything
        else, e.g. an int ``copies``) and extra_options are merged in.
        """
        result: Dict[str, str] = {}
        for k, v in asdict(self).items():
            if v is None or k == "extra_options":
                continue
            result[self.IPP_NAMES.get(k, k)] = self._to_ipp_value(v)
        if self.extra_options:
            for k, v in self.extra_options.items():
                if v is not None:
                    result[k] = self._to_ipp_value(v)
        return result

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "LinuxPrintOptions":
        """Create from dictionary, unknown keys are stored in extra_options.

        Both the snake_case field names (``print_color_mode``) and the
        hyphenated IPP names (``print-color-mode``) are accepted.
        """
        known_fields = {f.name for f in fields(cls)} - {"extra_options"}
        field_names = {v: k for k, v in cls.IPP_NAMES.items()}  # ipp -> field
        known: Dict[str, Any] = {}
        extra: Dict[str, Any] = {}
        for k, v in data.items():
            field_name = field_names.get(k, k)
            if field_name in known_fields:
                known[field_name] = v
            else:
                extra[k] = v
        return cls(**known, extra_options=extra if extra else None)


