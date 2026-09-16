"""
Cups Printer Operations Module
"""

# NOTE: pycups is a source-only distribution that needs a compiler and the CUPS
# headers. The ImportError is deliberately NOT swallowed here - main.py imports
# this module lazily and turns the failure into a 501 with an install hint, so
# the stdlib-only commands keep working on machines without pycups.
import cups
from typing import List, Dict, Any, Optional, Tuple
from models.model import (
    Printer,
    APIResponse,
    PrinterStatus,
    PrintJob,
    LinuxPrintOptions,
)
from utils.logger import logger


def print_file_prompt():
    return """
    CUPS Printer Operations Module
    
    This module provides comprehensive printer management capabilities for Linux/macOS systems using CUPS (Common UNIX Printing System) and IPP (Internet Printing Protocol).
    
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
    - Automatic format conversion between generic and CUPS-specific options
    
    Print Options Format:
    CUPS uses IPP standard parameters. Option names are hyphenated and every
    value is passed to pycups as a string (pycups raises TypeError on anything
    else). LinuxPrintOptions.to_dict() does the translation, so a caller may
    still use the snake_case field names and ints; the dict that reaches
    printFile() looks like {"copies": "2", "media": "A4", "print-color-mode": "color"}.
    - copies: Number of copies (e.g., "2")
    - media: Paper size (e.g., "A4", "Letter", "Legal")
    - orientation-requested: "3"=Portrait, "4"=Landscape
    - print-color-mode: "monochrome" or "color"
    - sides: "one-sided", "two-sided-long-edge", "two-sided-short-edge"
    - print-quality: "3"=Draft, "4"=Normal, "5"=High
    - page-ranges: Specific pages (e.g., "1-5,10-15")
    - number-up: Pages per sheet (e.g., "2")
    - fit-to-page: "true" or "false"

    Usage Tips:
    - Always check printer status before printing
    - Use printer attributes to determine available options
    - Handle errors gracefully (printer offline, paper out, etc.)
    - Monitor print jobs for completion status
    - Printer attributes contain all available options and current settings
    
    """


def get_print_options_format():
    """Get Linux/CUPS-specific print options format

    Returns:
        dict: Linux/CUPS print options format and examples
    """
    response = {
        "platform": "Linux/macOS",
        "format": "CUPS/IPP (Internet Printing Protocol)",
        "description": "Linux/macOS uses CUPS with IPP standard options. Names are hyphenated (print-color-mode); snake_case aliases (print_color_mode) are accepted too. All values are sent to CUPS as strings.",
        "documentation": {
            "cups_options": "https://www.cups.org/doc/options.html",
            "ipp_attributes": "https://www.iana.org/assignments/ipp-registrations/ipp-registrations.xhtml",
        },
        "options": {
            "copies": {
                "type": "str",
                "description": "Number of copies to print",
            },
            "media": {
                "type": "str",
                "description": "Paper size name, see: https://www.cups.org/doc/spec-ppd.html",
                "common_values": ["A3", "A4", "A5", "Letter", "Legal", "Executive"],
            },
            "orientation-requested": {
                "type": "str",
                "description": "Paper orientation (IPP enum)",
                "values": {"3": "Portrait", "4": "Landscape", "5": "Reverse Landscape", "6": "Reverse Portrait"},
            },
            "print-color-mode": {
                "type": "str",
                "description": "Color printing mode",
                "values": ["monochrome", "color"],
            },
            "sides": {
                "type": "str",
                "description": "Duplex (double-sided) printing mode",
                "values": ["one-sided", "two-sided-long-edge", "two-sided-short-edge"],
            },
            "print-quality": {
                "type": "str",
                "description": "Print quality (IPP enum)",
                "values": {"3": "Draft", "4": "Normal", "5": "High"},
            },
            "page-ranges": {
                "type": "str",
                "description": "Page ranges to print, e.g. '1-5,10-15'",
            },
            "number-up": {
                "type": "str",
                "description": "Number of pages per sheet",
                "values": ["1", "2", "4", "6", "9", "16"],
            },
            "fit-to-page": {
                "type": "str",
                "description": "Scale pages to fit the selected media size",
                "values": ["true", "false"],
            },
            "media-source": {
                "type": "str",
                "description": "Paper source/tray, device-specific values",
            },
            "media-type": {
                "type": "str",
                "description": "Media type, device-specific values",
            },
            "resolution": {
                "type": "str",
                "description": "Print resolution, e.g. '600dpi' or '600x600dpi'",
            },
        },
        "examples": {
            "basic_print": {
                "copies": "1",
                "media": "A4",
                "orientation-requested": "3",
                "print-color-mode": "monochrome",
            },
            "advanced_print": {
                "copies": "2",
                "media": "A4",
                "orientation-requested": "4",
                "print-color-mode": "color",
                "sides": "two-sided-long-edge",
                "print-quality": "4",
                "page-ranges": "1-5,10-15",
                "number-up": "2",
            },
        },
    }
    return response





def get_printer_list() -> Dict[str, Any]:
    """Get the list of printers on macOS (using CUPS)"""
    try:
        # Connect to CUPS server
        conn = cups.Connection()

        # Get all printers
        printers_dict = conn.getPrinters()

        # Get default printer
        default_printer = None
        try:
            default_printer = conn.getDefault()
        except:
            # No default printer set
            pass

        # Build printer list
        printers: List[Printer] = []
        index = 0
        for printer_name, printer_attrs in printers_dict.items():
            index += 1
            # Get printer state
            # printer_attrs['printer-state'] values:
            # 3 = idle, 4 = processing, 5 = stopped
            state = printer_attrs.get("printer-state", 3)
            state_reasons = printer_attrs.get("printer-state-reasons", [])

            # Determine status using PrinterStatus enum
            status = PrinterStatus.from_cups_state(state)

            # Check if printer is accepting jobs
            is_accepting = printer_attrs.get("printer-is-accepting-jobs", True)

            # Check if this is the default printer
            is_default = printer_name == default_printer

            printer = Printer(
                index=index,
                name=printer_name,
                status=status,
                status_reasons=state_reasons,
                is_accepting=is_accepting,
                type="CUPS",
                is_default=is_default,
                location=printer_attrs.get("printer-location", ""),
                model=printer_attrs.get("printer-make-and-model", ""),
                uri=printer_attrs.get("device-uri", ""),
            )
            printers.append(printer)

        # Convert printers to dict for response
        printers_data = [printer.to_dict() for printer in printers]

        response = APIResponse.success(
            {
                "printers": printers_data,
                "count": len(printers),
                "default_printer": default_printer,
            }
        )
        return response.to_dict()

    except RuntimeError as e:
        # CUPS connection error
        response = APIResponse.server_error(
            f"CUPS connection error: {str(e)}", {"printers": [], "count": 0}
        )
        return response.to_dict()
    except Exception as e:
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
    """Get printer by index (1-based), or the default printer when index is None."""
    printer, _error = resolve_printer(index)
    return printer


def get_printer_status(index: Optional[int] = None) -> Dict[str, Any]:
    """Get printer status"""
    printer, error = resolve_printer(index)
    if printer is None:
        return error

    try:
        # Connect to CUPS server
        conn = cups.Connection()

        # Get printer status
        printer_name = printer.name
        printer_attrs = conn.getPrinterAttributes(printer_name)

        state = printer_attrs.get("printer-state", 3)
        status = PrinterStatus.from_cups_state(state)

        response = APIResponse.success(
            {
                "index": printer.index,
                "name": printer_name,
                "is_accepting_jobs": printer_attrs.get(
                    "printer-is-accepting-jobs", True
                ),
                "status_reasons": printer_attrs.get("printer-state-reasons", []),
                "status": status.value,
            }
        )
        return response.to_dict()
    except Exception as e:
        response = APIResponse.server_error(f"Error getting printer status: {str(e)}")
        return response.to_dict()


def get_printer_attrs(index: Optional[int] = None) -> Dict[str, Any]:
    """Get printer attributes"""
    printer_result = get_printer_status(index)
    if printer_result.get("code") != 200:
        return printer_result

    try:
        conn = cups.Connection()
        printer_name = printer_result["data"]["name"]
        printer_attrs = conn.getPrinterAttributes(printer_name)

        response = APIResponse.success(printer_attrs)
        return response.to_dict()
    except Exception as e:
        response = APIResponse.server_error(
            f"Error getting printer attributes: {str(e)}"
        )
        return response.to_dict()


def print_file(
    index: Optional[int] = None, file_path: str = "",
    options: Optional[LinuxPrintOptions] = None, raw: bool = False
) -> dict:
    """
    Print file using CUPS

    Args:
        index: Printer index
        file_path: Path to the file to print
        options: LinuxPrintOptions instance (optional)
        raw: Send the file unfiltered (the `lp -o raw` equivalent). Use it only
            for data the device itself understands - CUPS will not convert it.

    Returns:
        dict: Response with job_id if successful
    """
    import os

    # Check if file exists
    if not os.path.exists(file_path):
        response = APIResponse.not_found(f"File not found: {file_path}")
        return response.to_dict()

    printer, error = resolve_printer(index)
    if printer is None:
        return error

    try:
        conn = cups.Connection()
        printer_name = printer.name

        # Get printer attributes and check status
        printer_attrs = conn.getPrinterAttributes(printer_name)
        state = printer_attrs.get("printer-state", 3)
        is_accepting = printer_attrs.get("printer-is-accepting-jobs", True)

        # Check if printer is accepting jobs
        if not is_accepting:
            response = APIResponse.server_error(
                "Printer is not accepting jobs",
                {"printer_name": printer_name, "is_accepting": is_accepting},
            )
            return response.to_dict()

        # Warn if printer is not idle (but still try to print)
        if state == 5:  # stopped
            response = APIResponse.server_error(
                "Printer is stopped, cannot print",
                {
                    "printer_name": printer_name,
                    "state": state,
                    "state_reasons": printer_attrs.get("printer-state-reasons", []),
                },
            )
            return response.to_dict()

        # Convert options to dict for CUPS API
        print_options = options.to_dict() if options else {}

        # `raw` tells CUPS to skip its filter chain and hand the bytes to the
        # device untouched - the IPP equivalent of `lp -o raw`.
        if raw:
            print_options["raw"] = "true"

        # Set default title if not provided
        title = os.path.basename(file_path)

        # Send print job to CUPS
        logger.info("Printing file: %s", file_path)
        logger.info("Print options: %s", print_options)
        logger.info("Printer name: %s", printer_name)
        logger.info("Title: %s", title)

        job_id = conn.printFile(printer_name, file_path, title, print_options)

        logger.info("Job ID: %s", job_id)

        response = APIResponse.success(
            {
                "job_id": job_id,
                "printer_name": printer_name,
                "file_path": file_path,
                "title": title,
                "status": "submitted",
                "raw": bool(raw),
            }
        )
        return response.to_dict()

    except cups.IPPError as e:
        # CUPS specific errors
        response = APIResponse.server_error(f"CUPS IPP error: {str(e)}")
        return response.to_dict()
    except Exception as e:
        logger.exception("Error printing file: %s", e)
        response = APIResponse.server_error(f"Error printing file: {str(e)}")
        return response.to_dict()


# Job state values (IPP job-state enum):
# 3 = pending, 4 = pending-held, 5 = processing,
# 6 = processing-stopped, 7 = canceled, 8 = aborted, 9 = completed
JOB_STATE_MAP = {
    3: "pending",
    4: "pending-held",
    5: "processing",
    6: "processing-stopped",
    7: "canceled",
    8: "aborted",
    9: "completed",
}

# CUPS' getJobs() only returns a tiny default attribute set (in practice just
# job-uri), so everything we want has to be requested explicitly. Note that
# CUPS does NOT return `job-printer-name` for jobs at all - the queue name has
# to be parsed out of job-printer-uri (see job_printer_name()).
JOB_REQUESTED_ATTRIBUTES = [
    "job-id",
    "job-name",
    "job-printer-uri",
    "job-state",
    "job-state-reasons",
    "job-priority",
    "job-k-octets",
    "job-impressions",
    "job-impressions-completed",
    "job-originating-user-name",
    "time-at-creation",
    "time-at-processing",
    "time-at-completed",
    "document-name-supplied",
]


def job_printer_name(job_info: Dict[str, Any]) -> str:
    """Derive the queue name from a job record.

    CUPS reports the owning queue as a URI like
    `ipp://localhost/printers/ET-4850`; there is no `job-printer-name`
    attribute. Returns "" when the URI is missing or unparsable.
    """
    printer_uri = job_info.get("job-printer-uri") or job_info.get("printer-uri") or ""
    if isinstance(printer_uri, (list, tuple)):
        printer_uri = printer_uri[0] if printer_uri else ""
    if printer_uri and "/printers/" in printer_uri:
        return printer_uri.split("/printers/")[-1].split("/")[0]
    return ""


def job_display_name(job_info: Dict[str, Any]) -> str:
    """Human-readable job name: the supplied document name, else job-name."""
    return job_info.get("document-name-supplied") or job_info.get("job-name") or ""


def _get_jobs_with_attributes(conn, which_jobs: str = "all", my_jobs: bool = False):
    """Call getJobs() asking for the attributes we actually need.

    `requested_attributes` needs pycups >= 1.9.72; older builds raise TypeError,
    in which case we fall back to the bare call rather than failing outright.
    """
    try:
        return conn.getJobs(
            which_jobs=which_jobs,
            my_jobs=my_jobs,
            requested_attributes=list(JOB_REQUESTED_ATTRIBUTES),
        )
    except TypeError:
        logger.warning(
            "pycups getJobs() does not support requested_attributes; "
            "job details will be incomplete"
        )
        return conn.getJobs(which_jobs=which_jobs, my_jobs=my_jobs)


def get_print_jobs(printer_name: str = None) -> Dict[str, Any]:
    """Get print jobs for a specific printer or all printers

    Args:
        printer_name: Printer name. If None, get jobs from all printers

    Returns:
        dict: Print jobs information
    """
    try:
        conn = cups.Connection()

        # Get all jobs
        jobs = _get_jobs_with_attributes(conn, which_jobs="all", my_jobs=False)

        all_jobs = []
        for job_id, job_info in jobs.items():
            job_info = job_info or {}
            derived_printer = job_printer_name(job_info)

            # Filter by printer name if specified
            if printer_name and derived_printer != printer_name:
                continue

            job_state = job_info.get("job-state", 0)

            k_octets = job_info.get("job-k-octets", 0) or 0
            impressions_done = job_info.get("job-impressions-completed", 0) or 0

            print_job = PrintJob(
                job_id=job_info.get("job-id", job_id),
                printer_name=derived_printer,
                job_name=job_display_name(job_info),
                status=JOB_STATE_MAP.get(job_state, "unknown"),
                priority=job_info.get("job-priority", 0) or 0,
                size=k_octets * 1024,  # Convert KB to bytes
                pages=impressions_done,
                user=job_info.get("job-originating-user-name", "") or "",
                submitted_time=job_info.get("time-at-creation", 0) or 0,
                total_pages=job_info.get("job-impressions", 0) or 0,
                pages_printed=impressions_done,
            )
            all_jobs.append(print_job.to_dict())

        response = APIResponse.success({"jobs": all_jobs, "count": len(all_jobs)})
        return response.to_dict()

    except Exception as e:
        response = APIResponse.server_error(f"Error getting print jobs: {str(e)}")
        return response.to_dict()


def get_print_job_status(job_id: int) -> Dict[str, Any]:
    """
    Get print job status

    Args:
        job_id: Print job ID

    Returns:
        dict: Job status information
    """
    try:
        conn = cups.Connection()

        # Use getJobAttributes to fetch the full job record
        try:
            job = conn.getJobAttributes(job_id)
        except cups.IPPError:
            response = APIResponse.not_found(f"Print job {job_id} not found")
            return response.to_dict()

        job_state = job.get("job-state", 0)

        # Parse the printer name out of job-printer-uri
        # Format: ipp://localhost/printers/Canon_G5080_series_2
        printer_name = job_printer_name(job)

        response = APIResponse.success(
            {
                "job_id": job_id,
                "printer_name": printer_name,
                "job_name": job_display_name(job),
                "job_state": JOB_STATE_MAP.get(job_state, "unknown"),
                "job_state_reasons": job.get("job-state-reasons", []),
                "time_at_creation": job.get("time-at-creation", 0),
                "time_at_processing": job.get("time-at-processing", 0),
                "time_at_completed": job.get("time-at-completed", 0),
            }
        )
        return response.to_dict()

    except Exception as e:
        response = APIResponse.server_error(f"Error getting job status: {str(e)}")
        return response.to_dict()


def cancel_print_job(job_id: int) -> Dict[str, Any]:
    """
    Cancel a print job

    Args:
        job_id: Print job ID to cancel

    Returns:
        dict: Response indicating success or failure
    """
    try:
        conn = cups.Connection()
        conn.cancelJob(job_id)

        response = APIResponse.success({"job_id": job_id, "status": "canceled"})
        return response.to_dict()

    except cups.IPPError as e:
        response = APIResponse.server_error(f"CUPS IPP error: {str(e)}")
        return response.to_dict()
    except Exception as e:
        response = APIResponse.server_error(f"Error canceling job: {str(e)}")
        return response.to_dict()
