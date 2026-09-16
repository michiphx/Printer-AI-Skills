"""
Windows Printer Operations Module
"""

import os
from typing import Dict, Any, List, Optional, Tuple

import win32con
import win32print
import pywintypes
from utils.logger import logger
from local_printer import win_render
from models.model import (
    APIResponse,
    PrinterStatus,
    Printer,
    PrintJob,
    WindowsPrintOptions,
)
from typing import Optional


def print_file_prompt():
    return """
    Windows Printer Operations Module
    
    This module provides comprehensive printer management capabilities for Windows systems using the Windows Print API.
    
    Workflow:
    1. Get printer list - Retrieve all available printers with their status and capabilities
    2. Get printer status - Check if a specific printer is ready and accepting jobs
    3. Get printer attributes - Obtain detailed printer capabilities and current settings
    4. Print file - Submit a print job with custom options
    
    Key Features:
    - Support for both local and network printers
    - Real-time printer status monitoring
    - Flexible print options (copies, orientation, color, paper size, duplex, etc.)
    - Print job management (status tracking, cancellation)
    - Automatic format conversion between generic and Windows-specific options
    
    Print Options Format:
    Windows uses Device Mode (dmXXX) parameters:
    - dmCopies: Number of copies (integer)
    - dmOrientation: 1=Portrait, 2=Landscape
    - dmColor: 1=Monochrome, 2=Color
    - dmPaperSize: Paper size constant (9=A4, 1=Letter, etc.)
    - dmDuplex: 1=Simplex, 2=Vertical, 3=Horizontal
    - dmDefaultSource: Paper source/bin
    - dmPrintQuality: Print quality (-4=Default, positive values=DPI)
    - dmCollate: 1=Collate, 0=No collate
    
    Usage Tips:
    - Always check printer status before printing
    - Use printer attributes to determine available options
    - Handle errors gracefully (printer offline, paper out, etc.)
    - Monitor print jobs for completion status
    
    """


def get_print_options_format():
    """Get Windows-specific print options format

    Returns:
        dict: Windows print options format and examples
    """
    response = {
        "platform": "Windows",
        "format": "dmXXX (Device Mode parameters)",
        "description": "Windows uses Device Mode (DEVMODE) parameters with dm prefix for print options",
        "documentation": "https://learn.microsoft.com/en-us/windows/win32/api/wingdi/ns-wingdi-devmodew",
        "options": {
            "dmCopies": {
                "type": "int",
                "description": "Number of copies to print",
            },
            "dmOrientation": {
                "type": "int",
                "description": "Paper orientation",
                "values": {"1": "Portrait (DMORIENT_PORTRAIT)", "2": "Landscape (DMORIENT_LANDSCAPE)"},
            },
            "dmColor": {
                "type": "int",
                "description": "Color mode",
                "values": {"1": "Monochrome (DMCOLOR_MONOCHROME)", "2": "Color (DMCOLOR_COLOR)"},
            },
            "dmPaperSize": {
                "type": "int",
                "description": "Paper size constant, see: https://learn.microsoft.com/en-us/windows/win32/intl/paper-sizes",
                "common_values": {
                    "1": "Letter (DMPAPER_LETTER)",
                    "5": "Legal (DMPAPER_LEGAL)",
                    "8": "A3 (DMPAPER_A3)",
                    "9": "A4 (DMPAPER_A4)",
                    "11": "A5 (DMPAPER_A5)",
                    "12": "B4 (DMPAPER_B4)",
                    "13": "B5 (DMPAPER_B5)",
                    "7": "Executive (DMPAPER_EXECUTIVE)",
                    "14": "Folio (DMPAPER_FOLIO)",
                },
            },
            "dmDuplex": {
                "type": "int",
                "description": "Duplex (double-sided) printing mode",
                "values": {
                    "1": "Simplex (DMDUP_SIMPLEX)",
                    "2": "Long edge (DMDUP_VERTICAL)",
                    "3": "Short edge (DMDUP_HORIZONTAL)",
                },
            },
            "dmDefaultSource": {
                "type": "int",
                "description": "Paper source/bin, device-specific values",
            },
            "dmMediaType": {
                "type": "int",
                "description": "Media type, device-specific values",
            },
            "dmPrintQuality": {
                "type": "int",
                "description": "Print quality. Negative values are predefined, positive values are DPI",
                "predefined_values": {
                    "-1": "Draft (DMRES_DRAFT)",
                    "-2": "Low (DMRES_LOW)",
                    "-3": "Medium (DMRES_MEDIUM)",
                    "-4": "High (DMRES_HIGH)",
                },
            },
            "dmCollate": {
                "type": "int",
                "description": "Collation mode",
                "values": {"0": "No collate", "1": "Collate (DMCOLLATE_TRUE)"},
            },
            "dmPaperLength": {
                "type": "int",
                "description": "Custom paper length in tenths of a millimeter, overrides dmPaperSize",
            },
            "dmPaperWidth": {
                "type": "int",
                "description": "Custom paper width in tenths of a millimeter, overrides dmPaperSize",
            },
        },
        "examples": {
            "basic_print": {
                "dmCopies": 1,
                "dmOrientation": 1,
                "dmColor": 1,
                "dmPaperSize": 9,
            },
            "advanced_print": {
                "dmCopies": 2,
                "dmOrientation": 2,
                "dmColor": 2,
                "dmPaperSize": 9,
                "dmDuplex": 2,
                "dmDefaultSource": 7,
                "dmPrintQuality": -4,
                "dmCollate": 1,
            },
        },
    }
    return response





def _status_bit(name: str, default: int) -> int:
    """PRINTER_STATUS_* from win32print, or the WINSPOOL.H value if pywin32
    does not export that constant on this build."""
    return getattr(win32print, name, default)


def classify_printer_status(status_bits: int):
    """Map a PRINTER_INFO_2.Status bitmask to (status, reasons, is_accepting).

    Three buckets, in priority order:

    * **stopped, not accepting** - the queue would only swallow the job:
      paused, error, offline, paper out, paper jam, door open, no toner,
      user intervention, not available, server unknown, power save (the
      spooler reports the device unreachable), page punt, out of memory.
      ``toner-low`` is merely reported as a reason: the printer still prints.
    * **processing** - busy, printing, I/O active, warming up, initialising,
      processing, waiting.
    * **idle** - nothing set.

    Anything else that is set but not understood is left as ``unknown``.
    """
    bits = int(status_bits or 0)
    if bits == 0:
        return PrinterStatus.IDLE, [], True

    stop_reasons = [
        ("PRINTER_STATUS_PAUSED", 0x00000001, "paused"),
        ("PRINTER_STATUS_ERROR", 0x00000002, "error"),
        ("PRINTER_STATUS_OFFLINE", 0x00000080, "offline"),
        ("PRINTER_STATUS_PAPER_JAM", 0x00000008, "paper-jam"),
        ("PRINTER_STATUS_PAPER_OUT", 0x00000010, "out-of-paper"),
        ("PRINTER_STATUS_PAPER_PROBLEM", 0x00000040, "paper-problem"),
        ("PRINTER_STATUS_MANUAL_FEED", 0x00000020, "manual-feed"),
        ("PRINTER_STATUS_DOOR_OPEN", 0x00400000, "door-open"),
        ("PRINTER_STATUS_NO_TONER", 0x00040000, "no-toner"),
        ("PRINTER_STATUS_OUTPUT_BIN_FULL", 0x00000800, "output-bin-full"),
        ("PRINTER_STATUS_NOT_AVAILABLE", 0x00001000, "not-available"),
        ("PRINTER_STATUS_USER_INTERVENTION", 0x00100000, "user-intervention"),
        ("PRINTER_STATUS_OUT_OF_MEMORY", 0x00200000, "out-of-memory"),
        ("PRINTER_STATUS_SERVER_UNKNOWN", 0x00800000, "server-unknown"),
        ("PRINTER_STATUS_PAGE_PUNT", 0x00080000, "page-punt"),
    ]
    busy_reasons = [
        ("PRINTER_STATUS_BUSY", 0x00000200, "busy"),
        ("PRINTER_STATUS_PRINTING", 0x00000400, "printing"),
        ("PRINTER_STATUS_IO_ACTIVE", 0x00000100, "io-active"),
        ("PRINTER_STATUS_WARMING_UP", 0x00010000, "warming-up"),
        ("PRINTER_STATUS_INITIALIZING", 0x00008000, "initializing"),
        ("PRINTER_STATUS_PROCESSING", 0x00004000, "processing"),
        ("PRINTER_STATUS_WAITING", 0x00002000, "waiting"),
        ("PRINTER_STATUS_PENDING_DELETION", 0x00000004, "pending-deletion"),
    ]
    advisory_reasons = [
        ("PRINTER_STATUS_TONER_LOW", 0x00020000, "toner-low"),
        ("PRINTER_STATUS_POWER_SAVE", 0x01000000, "power-save"),
    ]

    reasons: List[str] = []
    stopped = False
    busy = False
    for name, default, reason in stop_reasons:
        if bits & _status_bit(name, default):
            reasons.append(reason)
            stopped = True
    for name, default, reason in busy_reasons:
        if bits & _status_bit(name, default):
            reasons.append(reason)
            busy = True
    for name, default, reason in advisory_reasons:
        if bits & _status_bit(name, default):
            reasons.append(reason)

    if stopped:
        return PrinterStatus.STOPPED, reasons, False
    if busy:
        return PrinterStatus.PROCESSING, reasons, True
    if reasons:
        # Only advisory bits (toner low, power save): still prints.
        return PrinterStatus.IDLE, reasons, True
    return PrinterStatus.UNKNOWN, reasons, True


def get_printer_list() -> Dict[str, Any]:
    """Get the list of printers on Windows"""
    try:
        # Local queues plus \\server\share connections mapped into this profile
        printers = win32print.EnumPrinters(
            win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS
        )
        printer_list: List[Printer] = []

        # Get default printer name
        default_printer = None
        try:
            default_printer = win32print.GetDefaultPrinter()
        except Exception:
            pass

        index = 0
        seen = set()
        for printer in printers:
            printer_name = printer[2]
            if printer_name in seen:
                continue
            seen.add(printer_name)
            index += 1

            # Get printer detailed information
            try:
                # Open printer handle
                printer_handle = win32print.OpenPrinter(printer_name)
                printer_info = win32print.GetPrinter(printer_handle, 2)

                # Get printer status
                status = PrinterStatus.UNKNOWN
                status_reasons = []
                is_accepting = True

                # Set status based on printer state
                status, status_reasons, is_accepting = classify_printer_status(
                    printer_info["Status"]
                )

                # "Use Printer Offline" (the queue-level toggle in the Windows
                # printer menu) is NOT reflected in Status: the spooler records
                # it in Attributes as PRINTER_ATTRIBUTE_WORK_OFFLINE, which is
                # 0x00000400 in WINSPOOL.H. pywin32 does not always export the
                # constant, so fall back to the header value.
                work_offline = getattr(
                    win32print, "PRINTER_ATTRIBUTE_WORK_OFFLINE", 0x00000400
                )
                if printer_info.get("Attributes", 0) & work_offline:
                    status = PrinterStatus.STOPPED
                    is_accepting = False
                    if "offline" not in status_reasons:
                        status_reasons.append("offline")

                printer_obj = Printer(
                    index=index,
                    name=printer_name,
                    status=status,
                    status_reasons=status_reasons,
                    is_accepting=is_accepting,
                    type=printer_info.get("pDriverName", "Unknown"),
                    is_default=(printer_name == default_printer),
                    location=printer_info.get("pLocation", ""),
                    model=printer_info.get("pDriverName", ""),
                    uri=printer_info.get("pPortName", ""),
                    driver=printer_info.get("pDriverName", ""),
                    port=printer_info.get("pPortName", ""),
                    job_count=printer_info.get("cJobs", 0),
                )

                win32print.ClosePrinter(printer_handle)
                printer_list.append(printer_obj)
            except Exception as e:
                logger.error(f"Error getting details for printer {printer_name}: {e}")

        printer_dicts = [printer.to_dict() for printer in printer_list]

        response = APIResponse.success(
            {
                "printers": printer_dicts,
                "count": len(printer_list),
            }
        )
        return response.to_dict()
    except Exception as e:
        logger.error(f"Error getting printer list: {e}")
        response = APIResponse.server_error(
            f"Error getting printer list: {str(e)}", {"printers": [], "count": 0}
        )
        return response.to_dict()


def resolve_printer(index: Optional[int] = None) -> Tuple[Optional[Printer], Optional[Dict[str, Any]]]:
    """Resolve a printer index to a queue.

    Args:
        index: Printer index (1-based). None means "the default printer".

    Returns:
        (Printer, None) on success, (None, error_response_dict) otherwise.
    """
    printer_result = get_printer_list()
    if printer_result.get("code") != 200:
        return None, printer_result

    printer_list = printer_result.get("data", {}).get("printers", [])
    if not printer_list:
        return None, APIResponse.not_found("no printers installed").to_dict()

    if index is None:
        for printer_data in printer_list:
            if printer_data.get("is_default"):
                return Printer.from_dict(dict(printer_data)), None
        # Nothing is flagged as default - fall back to the first queue
        return Printer.from_dict(dict(printer_list[0])), None

    for printer_data in printer_list:
        if printer_data.get("index") == index:
            return Printer.from_dict(dict(printer_data)), None

    return None, APIResponse.not_found(f"Printer not found: index {index}").to_dict()


def get_index_printer_from_list(index: Optional[int] = None) -> Optional[Printer]:
    """Get printer by index from printer list

    Args:
        index: Printer index (1-based), or None for the default printer

    Returns:
        Printer object if found, None otherwise
    """
    printer, _error = resolve_printer(index)
    return printer


def get_printer_status(index: Optional[int] = None) -> Dict[str, Any]:
    """Get printer status by index

    Args:
        index: Printer index (1-based), or None for the default printer

    Returns:
        dict: Printer status information following CUPS format
    """
    printer, error = resolve_printer(index)
    if printer is None:
        return error

    try:
        # Directly use printer data from the list
        response = APIResponse.success(
            {
                "index": printer.index,
                "name": printer.name,
                "is_accepting_jobs": printer.is_accepting,
                "status_reasons": printer.status_reasons,
                "status": printer.status.value,
            }
        )
        return response.to_dict()

    except Exception as e:
        response = APIResponse.server_error(f"Error getting printer status: {str(e)}")
        return response.to_dict()


def get_dev_mode(devmode, printer_name):
    dev_mode = {}
    for attr in devmode.__dir__():
        if attr.startswith("_"):
            continue
        elif isinstance(getattr(devmode, attr), int):
            dev_mode[attr] = getattr(devmode, attr)

    has_color = False
    try:
        if (
            win32print.DeviceCapabilities(
                printer_name, "FILE:", win32con.DC_COLORDEVICE
            )
            == 1
        ):
            has_color = True
    except Exception as e:
        logger.error(f"[get_dev_mode] DC_COLORDEVICE query failed: {e}")

    if not has_color and dev_mode.get("Color", 1) != 1:
        dev_mode["Color"] = 1

    return dev_mode


def get_capabilities_dict(printer_name, port, dc_names, dc_values):
    data = {}
    try:
        names = win32print.DeviceCapabilities(printer_name, port, dc_names)
        values = win32print.DeviceCapabilities(printer_name, port, dc_values)
    except Exception as e:
        # Never print here: stdout carries the CLI's JSON payload
        logger.error(
            f"[get_capabilities_dict] {printer_name} ({dc_names}/{dc_values}) failed: {e}"
        )
        return {}
    for index, name in enumerate(names or []):
        if not name:
            continue
        try:
            data[name] = values[index]
        except (IndexError, TypeError):
            continue
    return data


def get_capabilities(printer_name):
    """Collect the printer's capabilities.

    Every DeviceCapabilities call is guarded on its own: a driver that fails one
    query must not wipe out the capabilities that were read successfully.
    """
    port = "FILE:"
    capabilities = {
        "Bins": get_capabilities_dict(
            printer_name, port, win32con.DC_BINNAMES, win32con.DC_BINS
        )
    }

    color = {"Black": 1}
    try:
        if (
            win32print.DeviceCapabilities(printer_name, port, win32con.DC_COLORDEVICE)
            == 1
        ):
            color["Color"] = 2
    except Exception as e:
        # No color option available
        logger.error(f"[get_capabilities] {printer_name} DC_COLORDEVICE failed: {e}")
    capabilities["Color"] = color

    media_types = get_capabilities_dict(
        printer_name, port, win32con.DC_MEDIATYPENAMES, win32con.DC_MEDIATYPES
    )
    capabilities["MediaTypes"] = media_types or {"Default": 0}
    capabilities["Papers"] = get_capabilities_dict(
        printer_name, port, win32con.DC_PAPERNAMES, win32con.DC_PAPERS
    )

    try:
        max_copies = win32print.DeviceCapabilities(
            printer_name, port, win32con.DC_COPIES
        )
    except Exception as e:
        logger.error(f"[get_capabilities] {printer_name} DC_COPIES failed: {e}")
        max_copies = 99
    if not isinstance(max_copies, int) or max_copies <= 1:
        max_copies = 99
    capabilities["Copies"] = max_copies

    capabilities["Orientation"] = {"Portrait": 1, "Landscape": 2}

    duplex = {"Off": 1}
    try:
        if win32print.DeviceCapabilities(printer_name, port, win32con.DC_DUPLEX) == 1:
            duplex["Long Edge"] = 2
            duplex["Short Edge"] = 3
    except Exception as e:
        logger.error(f"[get_capabilities] {printer_name} DC_DUPLEX failed: {e}")
    capabilities["Duplex"] = duplex

    return capabilities


def get_printer_attrs(index: Optional[int] = None):
    printer, error = resolve_printer(index)
    if printer is None:
        return error
    printer_name = printer.name
    try:
        p = win32print.OpenPrinter(printer_name)
    except Exception as e:
        logger.error(f"[get_printer_attrs] open printer {printer_name} failed: {e}")
        return APIResponse.error(500, f"get printer params error, err: {e}").to_dict()

    try:
        printer_info = win32print.GetPrinter(p, 2)
        devmode = printer_info["pDevMode"]
        dev_mode = get_dev_mode(devmode, printer_name)
        capabilities = get_capabilities(printer_name)
    except Exception as e:
        logger.error(f"[get_printer_attrs] {printer_name} error: {e}")
        return APIResponse.error(500, f"get printer params error, err: {e}").to_dict()
    finally:
        win32print.ClosePrinter(p)

    data = {
        "Capabilities": capabilities,
        "DevMode": dev_mode,
        "Name": printer_name,
    }

    result = APIResponse.success(data)
    return result.to_dict()


def get_print_jobs(printer_name: str = None) -> Dict[str, Any]:
    """Get print jobs for a specific printer or all printers

    Args:
        printer_name: Printer name. If None, get jobs from all printers

    Returns:
        dict: Print jobs information
    """
    try:
        all_jobs = []

        if printer_name:
            # Get jobs for specific printer
            printer_names = [printer_name]
        else:
            # Get jobs for all printers
            printer_result = get_printer_list()
            if printer_result["code"] != 200:
                return printer_result
            printer_names = [p["name"] for p in printer_result["data"]["printers"]]

        for pname in printer_names:
            try:
                # Open printer handle
                printer_handle = win32print.OpenPrinter(pname)

                # Enumerate print jobs
                jobs = win32print.EnumJobs(printer_handle, 0, -1, 1)

                for job in jobs:
                    # Map job status to string
                    status_map = {
                        0: "queued",
                        win32print.JOB_STATUS_PAUSED: "paused",
                        win32print.JOB_STATUS_ERROR: "error",
                        win32print.JOB_STATUS_DELETING: "canceling",
                        win32print.JOB_STATUS_SPOOLING: "spooling",
                        win32print.JOB_STATUS_PRINTING: "printing",
                        win32print.JOB_STATUS_OFFLINE: "offline",
                        win32print.JOB_STATUS_PAPEROUT: "out-of-paper",
                        win32print.JOB_STATUS_PRINTED: "completed",
                        win32print.JOB_STATUS_DELETED: "canceled",
                        win32print.JOB_STATUS_BLOCKED_DEVQ: "blocked",
                        win32print.JOB_STATUS_USER_INTERVENTION: "user-intervention",
                        win32print.JOB_STATUS_RESTART: "restart",
                    }

                    job_status = "unknown"
                    for status_flag, status_name in status_map.items():
                        if job.get("Status", 0) & status_flag:
                            job_status = status_name
                            break
                    if job.get("Status", 0) == 0:
                        job_status = "queued"

                    print_job = PrintJob(
                        job_id=job.get("JobId", 0),
                        printer_name=pname,
                        job_name=job.get("pDocument", "Unknown"),
                        status=job_status,
                        priority=job.get("Priority", 0),
                        size=job.get("Size", 0),
                        pages=job.get("PagesPrinted", 0),
                        user=job.get("pUserName", ""),
                        submitted_time=job.get("Submitted", 0),
                        total_pages=job.get("TotalPages", 0),
                        pages_printed=job.get("PagesPrinted", 0),
                    )
                    all_jobs.append(print_job.to_dict())

                win32print.ClosePrinter(printer_handle)

            except Exception as e:
                logger.error(f"Error getting jobs for printer {pname}: {e}")
                continue

        response = APIResponse.success({"jobs": all_jobs, "count": len(all_jobs)})
        return response.to_dict()

    except Exception as e:
        logger.error(f"Error getting print jobs: {e}")
        response = APIResponse.server_error(f"Error getting print jobs: {str(e)}")
        return response.to_dict()


def get_print_job_status(job_id: int) -> Dict[str, Any]:
    """Get print job status by job ID

    Args:
        job_id: Print job ID

    Returns:
        dict: Job status information
    """
    try:
        # Get all jobs and find the one with matching job_id
        jobs_result = get_print_jobs()
        if jobs_result["code"] != 200:
            return jobs_result

        jobs = jobs_result["data"]["jobs"]
        for job in jobs:
            if job["job_id"] == job_id:
                response = APIResponse.success(job)
                return response.to_dict()

        # Job not found
        response = APIResponse.not_found(f"Print job {job_id} not found")
        return response.to_dict()

    except Exception as e:
        logger.error(f"Error getting job status for job {job_id}: {e}")
        response = APIResponse.server_error(f"Error getting job status: {str(e)}")
        return response.to_dict()


def cancel_print_job(job_id: int) -> Dict[str, Any]:
    """Cancel a print job by job ID

    Args:
        job_id: Print job ID to cancel

    Returns:
        dict: Response indicating success or failure
    """
    try:
        # First, find the job to get the printer name
        job_status_result = get_print_job_status(job_id)
        if job_status_result["code"] != 200:
            return job_status_result

        job_info = job_status_result["data"]
        printer_name = job_info["printer_name"]

        # Open printer handle
        printer_handle = win32print.OpenPrinter(printer_name)

        try:
            # Cancel the specific job
            win32print.SetJob(
                printer_handle, job_id, 0, None, win32print.JOB_CONTROL_CANCEL
            )

            response = APIResponse.success(
                {"job_id": job_id, "printer_name": printer_name, "status": "canceled"}
            )
            return response.to_dict()

        finally:
            win32print.ClosePrinter(printer_handle)

    except pywintypes.error as e:
        logger.error(f"Windows API error canceling job {job_id}: {e}")
        response = APIResponse.server_error(f"Windows API error: {str(e)}")
        return response.to_dict()
    except Exception as e:
        logger.error(f"Error canceling job {job_id}: {e}")
        response = APIResponse.server_error(f"Error canceling job: {str(e)}")
        return response.to_dict()


def set_dev_mode(devmode, options: WindowsPrintOptions):
    """Set device mode parameters from WindowsPrintOptions

    Args:
        devmode: Windows device mode object
        options: WindowsPrintOptions instance
    """
    if not options:
        return

    # Set device mode fields
    fields_to_set = 0

    if options.dmOrientation is not None:
        fields_to_set |= win32con.DM_ORIENTATION
        devmode.Orientation = int(options.dmOrientation)

    if options.dmCopies is not None:
        fields_to_set |= win32con.DM_COPIES
        devmode.Copies = int(options.dmCopies)

    if options.dmColor is not None:
        fields_to_set |= win32con.DM_COLOR
        devmode.Color = int(options.dmColor)

    if options.dmPaperSize is not None:
        fields_to_set |= win32con.DM_PAPERSIZE
        devmode.PaperSize = int(options.dmPaperSize)

    if options.dmDuplex is not None:
        fields_to_set |= win32con.DM_DUPLEX
        devmode.Duplex = int(options.dmDuplex)

    if options.dmDefaultSource is not None:
        fields_to_set |= win32con.DM_DEFAULTSOURCE
        devmode.DefaultSource = int(options.dmDefaultSource)

    if options.dmMediaType is not None:
        fields_to_set |= win32con.DM_MEDIATYPE
        devmode.MediaType = int(options.dmMediaType)

    if options.dmPrintQuality is not None:
        fields_to_set |= win32con.DM_PRINTQUALITY
        devmode.PrintQuality = int(options.dmPrintQuality)

    if options.dmCollate is not None:
        fields_to_set |= win32con.DM_COLLATE
        devmode.Collate = int(options.dmCollate)

    # Handle custom paper size
    if options.dmPaperSize == 0 or options.dmPaperSize is None:
        if options.dmPaperLength is not None:
            fields_to_set |= win32con.DM_PAPERLENGTH
            devmode.PaperLength = int(options.dmPaperLength)

        if options.dmPaperWidth is not None:
            fields_to_set |= win32con.DM_PAPERWIDTH
            devmode.PaperWidth = int(options.dmPaperWidth)

    # Apply all fields at once
    devmode.Fields = devmode.Fields | fields_to_set


# Formats a printer can be expected to interpret when handed the bytes verbatim.
# Nothing is blocked on this any more: it is the list of extensions that may
# fall back to the raw spooler path when the file is not a PDF.
RAW_SAFE_EXTENSIONS = (".pdf", ".ps", ".prn", ".txt")

# Extensions that still make sense to push through the spooler untouched once
# we know the file is not a PDF (PostScript, driver-ready spool files, text).
RAW_FALLBACK_EXTENSIONS = (".ps", ".prn", ".txt")

RAW_NOTE = "sent as raw data; the printer must understand this format natively"

PYPDFIUM2_HINT = "pypdfium2 missing: uv tool install --reinstall printer-ai-skills"


def _is_pdf(file_path: str) -> bool:
    """True when the file starts with the %PDF header."""
    with open(file_path, "rb") as f:
        return f.read(5).startswith(b"%PDF")


def _open_printer_devmode(printer_name: str, options: Optional[WindowsPrintOptions]):
    """Open a printer and return (handle, devmode) with `options` applied.

    The DEVMODE comes from the queue's own defaults, gets the caller's dmXXX
    fields merged in and is then handed to DocumentProperties so the driver can
    validate/normalise it.

    Raises:
        Exception: whatever OpenPrinter/GetPrinter raised.
    """
    handle = win32print.OpenPrinter(printer_name)
    try:
        printer_info = win32print.GetPrinter(handle, 2)
        devmode = printer_info["pDevMode"]
        set_dev_mode(devmode, options)
    except Exception:
        win32print.ClosePrinter(handle)
        raise
    return handle, devmode


def _validate_devmode(handle, printer_name: str, devmode) -> None:
    """Let the driver validate the DEVMODE in place. Never fatal."""
    try:
        win32print.DocumentProperties(
            0,
            handle,
            printer_name,
            devmode,
            devmode,
            win32con.DM_IN_BUFFER | win32con.DM_OUT_BUFFER,
        )
    except Exception as e:
        # Not fatal: the job still goes out with the queue's own defaults
        logger.error(f"[print_file] DocumentProperties failed on {printer_name}: {e}")


def _driver_max_copies(printer_name: str, port: str) -> int:
    """How many copies the driver itself can produce (1 == none)."""
    try:
        value = win32print.DeviceCapabilities(
            printer_name, port or "FILE:", win32con.DC_COPIES
        )
    except Exception as e:
        logger.error(f"[print_file] DC_COPIES failed on {printer_name}: {e}")
        return 1
    if not isinstance(value, int) or value < 1:
        return 1
    return value


def _print_raw(printer_name: str, file_path: str,
               options: Optional[WindowsPrintOptions], note: str):
    """Spool the file verbatim with the RAW datatype (no driver rendering)."""
    try:
        handle, devmode = _open_printer_devmode(printer_name, options)
    except Exception as e:
        logger.error(f"[print_file] open printer {printer_name} failed: {e}")
        return APIResponse.error(500, f"open printer error, err: {e}").to_dict()

    _validate_devmode(handle, printer_name, devmode)

    job_id = None
    try:
        with open(file_path, "rb") as f:
            file_content = f.read()

        doc_info = (file_path, None, "RAW")
        job_id = win32print.StartDocPrinter(handle, 1, doc_info)
        win32print.StartPagePrinter(handle)
        win32print.WritePrinter(handle, file_content)
        win32print.EndPagePrinter(handle)
        win32print.EndDocPrinter(handle)
    except Exception as e:
        logger.error(f"Error printing file {file_path}: {e}")
        return APIResponse.server_error(f"Error printing file: {str(e)}").to_dict()
    finally:
        win32print.ClosePrinter(handle)

    return APIResponse.success(
        {
            "printer_name": printer_name,
            "file_path": file_path,
            "status": "submitted",
            "job_id": job_id,
            "method": "raw",
            "note": note,
        }
    ).to_dict()


def _open_pdf(file_path: str):
    """Open a PDF with pypdfium2.

    Returns:
        (document, None) on success, (None, error_response_dict) otherwise.
    """
    try:
        import pypdfium2 as pdfium
    except ImportError as e:
        logger.error(f"[print_file] pypdfium2 unavailable: {e}")
        return None, APIResponse.error(
            501,
            f"cannot render PDF without pypdfium2 ({PYPDFIUM2_HINT})",
            {"file_path": file_path, "hint": PYPDFIUM2_HINT},
        ).to_dict()

    try:
        return pdfium.PdfDocument(file_path), None
    except Exception as e:
        message = str(e)
        logger.error(f"[print_file] cannot open PDF {file_path}: {message}")
        if "password" in message.lower():
            return None, APIResponse.error(
                400,
                "PDF is password-protected; remove the password before printing",
                {"file_path": file_path, "detail": message},
            ).to_dict()
        return None, APIResponse.error(
            400,
            f"cannot read PDF: {message}",
            {"file_path": file_path},
        ).to_dict()


def _apply_auto_orientation(devmode, options: Optional[WindowsPrintOptions], page) -> bool:
    """Switch to landscape when the caller did not choose and the page is wide.

    Returns:
        bool: True when the orientation was flipped to landscape.
    """
    if options is not None and options.dmOrientation is not None:
        return False
    try:
        width, height = page.get_size()
    except Exception as e:
        logger.error(f"[print_file] page size unavailable, keeping orientation: {e}")
        return False
    if width <= height:
        return False
    devmode.Orientation = win32con.DMORIENT_LANDSCAPE
    devmode.Fields = devmode.Fields | win32con.DM_ORIENTATION
    return True


def _print_pdf_gdi(printer_name: str, port: str, file_path: str,
                   options: Optional[WindowsPrintOptions]):
    """Rasterise a PDF and draw every page onto the printer's device context.

    This is the driver-based path: the driver sees ordinary GDI drawing calls,
    so any Windows printer can print the document and the DEVMODE settings
    (colour, duplex, paper, copies) are honoured by the driver itself.
    """
    try:
        import win32gui
        import win32ui
        from PIL import ImageWin
    except ImportError as e:
        logger.error(f"[print_file] GDI dependency unavailable: {e}")
        return APIResponse.error(
            501,
            f"GDI printing needs win32ui/win32gui (pywin32) and Pillow, err: {e}",
            {"file_path": file_path},
        ).to_dict()

    pdf, error = _open_pdf(file_path)
    if pdf is None:
        return error

    handle = None
    dc = None
    hdc = None
    doc_started = False
    try:
        page_count = len(pdf)
        if page_count < 1:
            return APIResponse.error(
                400, "PDF has no pages", {"file_path": file_path}
            ).to_dict()

        try:
            handle, devmode = _open_printer_devmode(printer_name, options)
        except Exception as e:
            logger.error(f"[print_file] open printer {printer_name} failed: {e}")
            return APIResponse.error(500, f"open printer error, err: {e}").to_dict()

        landscape = _apply_auto_orientation(devmode, options, pdf[0])
        _validate_devmode(handle, printer_name, devmode)

        # Copies: the driver does them properly (and faster) whenever it can.
        requested_copies = 1
        if options is not None and options.dmCopies is not None:
            requested_copies = max(1, int(options.dmCopies))
        collate = bool(getattr(devmode, "Collate", 1))
        if _driver_max_copies(printer_name, port) > 1:
            copies_handled_by = "driver"
            client_copies = 1
        else:
            copies_handled_by = "client" if requested_copies > 1 else "driver"
            client_copies = requested_copies
            devmode.Copies = 1

        hdc = win32gui.CreateDC("WINSPOOL", printer_name, devmode)
        dc = win32ui.CreateDCFromHandle(hdc)

        logical_dpi_x = dc.GetDeviceCaps(win32con.LOGPIXELSX)
        logical_dpi_y = dc.GetDeviceCaps(win32con.LOGPIXELSY)
        printable_w = dc.GetDeviceCaps(win32con.HORZRES)
        printable_h = dc.GetDeviceCaps(win32con.VERTRES)
        physical_w = dc.GetDeviceCaps(win32con.PHYSICALWIDTH)
        physical_h = dc.GetDeviceCaps(win32con.PHYSICALHEIGHT)
        offset_x = dc.GetDeviceCaps(win32con.PHYSICALOFFSETX)
        offset_y = dc.GetDeviceCaps(win32con.PHYSICALOFFSETY)

        dpi = win_render.resolve_render_dpi(min(logical_dpi_x, logical_dpi_y))
        # A per-inch cap alone still lets a poster-sized page allocate a huge
        # bitmap; bound the total pixel count as well.
        dpi = win_render.bound_dpi_by_pixels(
            dpi,
            win_render.device_units_to_inches(physical_w, logical_dpi_x),
            win_render.device_units_to_inches(physical_h, logical_dpi_y),
        )
        scale = dpi / 72.0

        title = os.path.basename(file_path) or "printer-ai"
        job_id = dc.StartDoc(title)
        doc_started = True

        order = win_render.page_order(page_count, client_copies, collate)
        for page_index in order:
            page = pdf[page_index]
            image = page.render(scale=scale).to_pil()
            try:
                # The printer DC's origin is already the top-left of the
                # printable area, so the physical offsets are zero here; they
                # stay parameters of fit_rect for callers working in physical
                # page coordinates.
                rect = win_render.fit_rect(
                    image.width, image.height, printable_w, printable_h, 0, 0
                )
                dc.StartPage()
                ImageWin.Dib(image).draw(dc.GetHandleOutput(), rect)
                dc.EndPage()
            finally:
                # Free the bitmap before rendering the next page
                image = None

        dc.EndDoc()
        doc_started = False
    except Exception as e:
        logger.error(f"[print_file] GDI print failed for {file_path}: {e}")
        if dc is not None and doc_started:
            try:
                dc.AbortDoc()
            except Exception as abort_error:
                logger.error(f"[print_file] AbortDoc failed: {abort_error}")
        return APIResponse.server_error(f"Error printing file: {str(e)}").to_dict()
    finally:
        if dc is not None:
            try:
                dc.DeleteDC()
            except Exception as e:
                logger.error(f"[print_file] DeleteDC failed: {e}")
        if handle is not None:
            try:
                win32print.ClosePrinter(handle)
            except Exception as e:
                logger.error(f"[print_file] ClosePrinter failed: {e}")
        try:
            pdf.close()
        except Exception:
            pass

    return APIResponse.success(
        {
            "printer_name": printer_name,
            "file_path": file_path,
            "status": "submitted",
            "job_id": job_id,
            "method": "gdi",
            "pages": page_count,
            "sheets_drawn": len(order),
            "dpi": dpi,
            "copies": requested_copies,
            "copies_handled_by": copies_handled_by,
            "collate": collate,
            "auto_landscape": landscape,
            "printable_area": [printable_w, printable_h],
            "physical_page": [physical_w, physical_h],
            "physical_offset": [offset_x, offset_y],
        }
    ).to_dict()


def print_file(index: Optional[int] = None, file_path: str = "",
               options: Optional[WindowsPrintOptions] = None,
               raw: bool = False):
    """Print a file on Windows.

    By default a PDF is rasterised and drawn onto the printer's device context
    through the driver, so every Windows printer can print it and the DEVMODE
    options are honoured. `raw=True` bypasses the driver and spools the bytes
    verbatim - only useful for devices that understand the format themselves.

    Args:
        index: Printer index (1-based), or None for the default printer.
        file_path: Path of the file to print.
        options: WindowsPrintOptions (dmXXX fields), or None.
        raw: Send the file to the spooler as RAW data instead of rendering it.

    Returns:
        dict: APIResponse payload.
    """
    printer, error = resolve_printer(index)
    if printer is None:
        return error
    printer_name = printer.name

    # Pre-flight, mirroring the CUPS backend: a stopped queue (paused, in
    # error, offline or "Use Printer Offline") would only swallow the job into
    # the spooler, so refuse before any device context or spooler call.
    if printer.status == PrinterStatus.STOPPED or not printer.is_accepting:
        reasons = list(printer.status_reasons or [])
        detail = ", ".join(reasons) if reasons else "not accepting jobs"
        return APIResponse.error(
            503,
            f"Printer is stopped, cannot print ({detail})",
            {
                "printer_name": printer_name,
                "status": printer.status.value,
                "status_reasons": reasons,
                "is_accepting": printer.is_accepting,
            },
        ).to_dict()

    if raw:
        # The caller explicitly asked for the bytes to go out untouched.
        return _print_raw(printer_name, file_path, options, RAW_NOTE)

    try:
        is_pdf = _is_pdf(file_path)
    except OSError as e:
        logger.error(f"[print_file] cannot read {file_path}: {e}")
        return APIResponse.not_found(
            f"cannot read file: {e}", {"file_path": file_path}
        ).to_dict()

    if is_pdf:
        return _print_pdf_gdi(printer_name, printer.port, file_path, options)

    extension = os.path.splitext(file_path)[1].lower()
    if extension in RAW_FALLBACK_EXTENSIONS:
        return _print_raw(
            printer_name,
            file_path,
            options,
            f"{extension} is not a PDF; {RAW_NOTE}",
        )

    return APIResponse.unsupported_media_type(
        "not a PDF: convert the document to PDF first, or re-run with raw "
        "printing if the device understands this format natively",
        {"file_path": file_path, "extension": extension},
    ).to_dict()
