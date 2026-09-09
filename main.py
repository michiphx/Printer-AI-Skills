#!/usr/bin/env python3
"""
Cross-platform local printer CLI.

Drives local printers on Windows, macOS and Linux, and is meant to be called
by an AI skill system (OpenClaw / Cursor / Claude and friends) as well as by
humans.

Usage:
    printer-ai printers              # list printers
    printer-ai status [INDEX]        # printer status (default printer if omitted)
    printer-ai attrs [INDEX]         # printer attributes
    printer-ai print FILE            # print a file
    printer-ai jobs                  # list print jobs
    printer-ai job-status JOB_ID     # query one job
    printer-ai cancel-job JOB_ID     # cancel a job
"""

import argparse
import json
import os
import sys
from sys import platform

# The platform backends are imported lazily by _backend(). Importing them at
# module load would make every command depend on the backend: on macOS/Linux
# `import cups` (pycups) needs a compiler and the CUPS headers at install time,
# and a failure there must not take down stdlib-only commands such as
# discover / probe / driver-search.
_INSTALL_HINT = (
    "On macOS/Linux install CUPS headers and pycups "
    "(e.g. apt install libcups2-dev; uv tool install --reinstall printer-ai-skills)"
)

_BACKEND_CACHE = None


class _Backend:
    """Thin holder for the platform-specific printer functions."""

    def __init__(self, module, options_class):
        self.get_printer_list = module.get_printer_list
        self.get_printer_status = module.get_printer_status
        self.get_printer_attrs = module.get_printer_attrs
        self.print_file = module.print_file
        self.get_print_jobs = module.get_print_jobs
        self.get_print_job_status = module.get_print_job_status
        self.cancel_print_job = module.cancel_print_job
        self.PrintOptions = options_class


def _unavailable(msg):
    """Build the 501 result used when no printer backend can be loaded."""
    return {"code": 501, "msg": msg, "data": {}}


def _backend():
    """Import the platform printer backend on first use.

    Returns:
        (backend, None) when the backend loaded, (None, result_dict) otherwise.
        The result dict is a normal API response with code 501.
    """
    global _BACKEND_CACHE
    if _BACKEND_CACHE is not None:
        return _BACKEND_CACHE

    if platform == "win32":
        try:
            from local_printer import windows as module
            from models.model import WindowsPrintOptions as options_class
        except ImportError as e:
            _BACKEND_CACHE = (
                None,
                _unavailable(f"printer backend unavailable: {e}. {_INSTALL_HINT}"),
            )
            return _BACKEND_CACHE
    elif platform in ("linux", "darwin"):
        try:
            from local_printer import cups as module
            from models.model import LinuxPrintOptions as options_class
        except ImportError as e:
            _BACKEND_CACHE = (
                None,
                _unavailable(f"printer backend unavailable: {e}. {_INSTALL_HINT}"),
            )
            return _BACKEND_CACHE
    else:
        _BACKEND_CACHE = (
            None,
            _unavailable(
                f"printer backend unavailable: unsupported platform '{platform}'. "
                f"{_INSTALL_HINT}"
            ),
        )
        return _BACKEND_CACHE

    _BACKEND_CACHE = (_Backend(module, options_class), None)
    return _BACKEND_CACHE


def output_json(data):
    """Print a result as JSON (easy for an AI caller to parse)."""
    print(json.dumps(data, indent=2, ensure_ascii=False))


def finish(result, as_json):
    """Emit a result and exit: status 0 only when the result code is 200.

    In JSON mode the whole result is printed. In human mode the caller has
    already printed its own text, so only failures add a line (on stderr).
    """
    code = result.get("code") if isinstance(result, dict) else None
    if as_json:
        output_json(result)
    elif code != 200:
        msg = result.get("msg", "failed") if isinstance(result, dict) else "failed"
        print(f"Error {code}: {msg}", file=sys.stderr)
    sys.exit(0 if code == 200 else 1)


def _status_label(status):
    """Plain-text status marker, ASCII only."""
    return {
        "idle": "[ok]",
        "processing": "[busy]",
        "stopped": "[stopped]",
    }.get(status, "[?]")


# ==================== printer commands ====================


def cmd_printers(args):
    """List printers."""
    backend, error = _backend()
    if error:
        finish(error, args.json)

    result = backend.get_printer_list()
    if args.json or result.get("code") != 200:
        finish(result, args.json)

    printers = result.get("data", {}).get("printers", [])
    if not printers:
        print("No printers installed")
        finish(result, args.json)

    print(f"Found {len(printers)} printer(s):\n")
    for p in printers:
        default_mark = " (default)" if p.get("is_default") else ""
        status = p.get("status", "unknown")
        print(f"  [{p.get('index')}] {p.get('name', 'unknown')}{default_mark}")
        print(
            f"      status: {_status_label(status)} {status}  |  "
            f"model: {p.get('model') or 'unknown'}"
        )
        if p.get("location"):
            print(f"      location: {p.get('location')}")
    finish(result, args.json)


def cmd_status(args):
    """Show the status of one printer (the default printer when no index given)."""
    backend, error = _backend()
    if error:
        finish(error, args.json)

    result = backend.get_printer_status(args.index)
    if args.json or result.get("code") != 200:
        finish(result, args.json)

    data = result.get("data", {})
    status = data.get("status", "unknown")
    print(f"Printer: {data.get('name', 'unknown')}")
    print(f"Status:  {_status_label(status)} {status}")
    print(f"Accepting jobs: {'yes' if data.get('is_accepting_jobs') else 'no'}")
    reasons = data.get("status_reasons") or []
    if reasons:
        print(f"Reasons: {', '.join(str(r) for r in reasons)}")
    finish(result, args.json)


def cmd_attrs(args):
    """Show printer attributes (always JSON)."""
    backend, error = _backend()
    if error:
        finish(error, True)
    finish(backend.get_printer_attrs(args.index), True)


def cmd_print(args):
    """Print a file."""
    backend, error = _backend()
    if error:
        finish(error, False)

    if not os.path.exists(args.file_path):
        finish(
            {
                "code": 404,
                "msg": f"file not found: {args.file_path}",
                "data": {"file_path": args.file_path},
            },
            False,
        )

    # Parse print options
    print_options = None
    if args.options and backend.PrintOptions:
        try:
            options_dict = json.loads(args.options)
            print_options = backend.PrintOptions.from_dict(options_dict)
        except json.JSONDecodeError as e:
            finish({"code": 400, "msg": f"invalid --options JSON: {e}", "data": {}}, False)
        except TypeError as e:
            finish({"code": 400, "msg": f"invalid print options: {e}", "data": {}}, False)

    result = backend.print_file(args.index, args.file_path, print_options)

    if result.get("code") != 200:
        # Dump the whole result: it carries the reason and any hint
        finish(result, True)

    data = result.get("data", {})
    job_id = data.get("job_id", "")
    print(f"Print job submitted  job_id: {job_id}")
    print(f"  printer: {data.get('printer_name', '')}")
    print(f"  file:    {data.get('file_path', '')}")
    if data.get("note"):
        print(f"  note:    {data['note']}")
    print(f"  check with: printer-ai job-status {job_id}")
    finish(result, False)


def cmd_jobs(args):
    """List print jobs."""
    backend, error = _backend()
    if error:
        finish(error, args.json)

    result = backend.get_print_jobs(args.printer)
    if args.json or result.get("code") != 200:
        finish(result, args.json)

    jobs = result.get("data", {}).get("jobs", [])
    if not jobs:
        print("No print jobs")
        finish(result, args.json)

    print(f"{len(jobs)} print job(s):\n")
    for job in jobs:
        status = job.get("status", "unknown")
        print(f"  [{job.get('job_id')}] {job.get('job_name', 'unknown')}  - {status}")
        print(f"      printer: {job.get('printer_name', '')}")
    finish(result, args.json)


def cmd_job_status(args):
    """Query a print job (and the state of the printer running it)."""
    backend, error = _backend()
    if error:
        finish(error, True)

    result = backend.get_print_job_status(args.job_id)

    # Merge in the printer status when we know which queue owns the job
    printer_name = result.get("data", {}).get("printer_name", "")
    if printer_name and result.get("code") == 200:
        printer_list_result = backend.get_printer_list()
        if printer_list_result.get("code") == 200:
            for p in printer_list_result["data"].get("printers", []):
                if p.get("name") == printer_name:
                    status_result = backend.get_printer_status(p["index"])
                    if status_result.get("code") == 200:
                        result["data"]["printer_status"] = status_result["data"]
                    break

    finish(result, True)


def cmd_cancel_job(args):
    """Cancel a print job."""
    backend, error = _backend()
    if error:
        finish(error, True)
    finish(backend.cancel_print_job(args.job_id), True)


# ==================== network discovery / install commands ====================
#
# The network commands live in local_printer.commands_net, which owns its own
# handlers (cmd_discover, cmd_setup, ...) and the same "exit 0 only on code 200"
# rule. main.py just parses the arguments and hands them over; the local
# implementations below are used only when that module has no handler for a
# command, so the CLI keeps working either way.


def _run_net_command(name, args, fallback):
    """Dispatch a network subcommand to commands_net, else to the fallback."""
    from local_printer import commands_net

    handler = getattr(commands_net, f"cmd_{name}", None)
    if handler is None:
        return fallback(args)
    result = handler(args)
    # These handlers normally exit by themselves; honour a returned result too.
    if isinstance(result, dict):
        finish(result, getattr(args, "json", False))
    sys.exit(0)


def _net(name, fallback):
    """Build the argparse callback for a network subcommand."""

    def runner(args):
        return _run_net_command(name, args, fallback)

    runner.__name__ = f"cmd_{name}"
    return runner


def cmd_discover(args):
    """Scan the LAN for printers."""
    from local_printer import commands_net

    extra = {}
    if getattr(args, "force", False):
        extra["force"] = True
    result = commands_net.discover(
        subnet=args.subnet, timeout=args.timeout, deep=not args.fast, **extra
    )
    if args.json or result.get("code") != 200:
        finish(result, args.json)

    data = result["data"]
    printers = data["printers"]
    print(f"Scanned {data['subnet']} - found {data['count']} printer(s)\n")
    for entry in printers:
        ipp = entry.get("ipp") or {}
        model = ipp.get("make_and_model") or entry.get("vendor_hint") or "unknown model"
        state = ipp.get("state")
        print(f"  {entry['host']}  {_status_label(state)} {model}")
        print(f"      ports: {', '.join(entry['open_ports'])}")
        if entry.get("mac"):
            print(f"      mac:   {entry['mac']}")
        if ipp.get("supports_duplex") is not None:
            duplex = "yes" if ipp["supports_duplex"] else "no"
            print(
                f"      duplex: {duplex}  |  default media: "
                f"{ipp.get('media_default', '?')}"
            )
    if not printers:
        print("  (none found - try --subnet)")
    finish(result, args.json)


def cmd_probe(args):
    """Probe one host."""
    from local_printer import commands_net

    finish(commands_net.probe(args.host, timeout=args.timeout), True)


def cmd_diagnose(args):
    """Verify installed printers against the network."""
    from local_printer import commands_net

    result = commands_net.diagnose(deep=not args.fast, timeout=args.timeout)
    if args.json or result.get("code") != 200:
        finish(result, args.json)

    data = result["data"]
    print(
        f"{data['count']} printer(s): {data['online']} online, "
        f"{data['offline']} offline, {data.get('unknown', 0)} unknown\n"
    )
    for entry in data["printers"]:
        online = entry.get("really_online")
        mark = "[unknown]" if online is None else ("[online]" if online else "[offline]")
        default = " (default)" if entry.get("is_default") else ""
        print(f"  [{entry['index']}] {entry['name']}{default}")
        print(f"      spooler: {entry['spooler_status']}  |  {mark} {entry['verdict']}")
        if entry.get("model"):
            print(f"      device:  {entry['model']} ({entry.get('device_state', '?')})")
    finish(result, args.json)


def cmd_ports(args):
    """List printer ports (always JSON)."""
    from local_printer import commands_net

    finish(commands_net.ports(), True)


def cmd_drivers(args):
    """List installed printer drivers (always JSON)."""
    from local_printer import commands_net

    finish(commands_net.drivers(model=args.model), True)


def cmd_setup(args):
    """Install a network printer, best driver first."""
    from local_printer import commands_net

    extra = {}
    if getattr(args, "vendor_lookup", False):
        extra["vendor_lookup"] = True
    result = commands_net.setup(
        args.host,
        name=args.name,
        dry_run=args.dry_run,
        allow_generic=not args.no_generic,
        **extra,
    )
    if args.json or args.dry_run or result.get("code") != 200:
        finish(result, True)

    data = result["data"]
    strategy = data.get("installed_with") or {}
    print(f"Printer installed: {data.get('printer')}")
    print(f"  method: {strategy.get('kind')}  |  driver: {strategy.get('driver')}")
    print(f"  port:   {strategy.get('port')}")

    verification = data.get("verification") or {}
    if verification.get("full_featured"):
        print("  all device features available")
    elif verification.get("full_featured") is None:
        print(f"  features unverified: {verification.get('note', 'no device answer')}")
    else:
        print("  limited features:")
        for item in verification.get("missing", []):
            print(f"    - {item}")
    if data.get("hint"):
        print(f"  hint: {data['hint']}")
    finish(result, False)


def cmd_driver_search(args):
    """Look up a manufacturer driver online."""
    from local_printer import commands_net

    result = commands_net.driver_search(
        model=args.model, host=args.host, region=args.region,
        os_code=args.os, download_dir=args.download, open_browser=args.open,
    )
    if args.json or result.get("code") != 200:
        finish(result, args.json)

    data = result["data"]
    print(f"Model:  {data['model']}")
    print(f"Vendor: {data['vendor']}  |  region: {data.get('region')}  |  "
          f"os: {data['os']['release']} {data['os']['arch']}")

    if not data.get("supported"):
        print(f"\nWarning: {data.get('hint', '')}")
        if data.get("support_site"):
            print(f"  support site (unverified): {data['support_site']}")
        finish(result, args.json)

    downloads = data.get("downloads") or []
    if downloads:
        print(f"\n{len(downloads)} driver package(s) found:\n")
        for item in downloads:
            size = f"{item['size_mb']} MB" if item.get("size_mb") else "?"
            print(f"  [{item['category']}] v{item['version']}  ({size})")
            print(f"      {item['filename']}")
            print(f"      {item['url']}")
    else:
        print(f"\nWarning: {data.get('note', 'No direct download links available.')}")
    print(f"\nDownload page: {data['download_page']}")

    dl = data.get("download")
    if dl:
        if dl.get("ok"):
            print(f"\nSaved: {dl['path']}")
            print(f"  {dl['size_bytes']} bytes  |  SHA-256 {dl['sha256']}")
            print(f"  {dl['note']}")
        elif dl.get("blocked"):
            print(f"\nBlocked: {dl.get('error')}")
            print("  The vendor only serves this file to real browser sessions.")
            print(f"  Open it in a browser: printer-ai driver-search {_echo_args(args)} --open")
        else:
            print(f"\nDownload failed: {dl.get('error')}")

    opened = data.get("opened")
    if opened:
        if opened.get("ok"):
            print(f"\nOpened in the browser: {opened['url']}")
            print("  Finish the download there, then: printer-ai setup <IP> --no-generic")
        else:
            print(f"\nCould not open a browser: {opened.get('error')}")
    finish(result, args.json)


def _echo_args(args):
    """Rebuild the identifying part of the command for a follow-up hint."""
    if args.host:
        return f"--host {args.host}"
    return f'"{args.model}"'


def cmd_remove(args):
    """Delete a printer queue (always JSON)."""
    from local_printer import commands_net

    if not args.yes:
        finish(
            {"code": 400, "msg": "refusing to remove a printer without --yes", "data": {}},
            True,
        )
    finish(commands_net.remove(args.name), True)


def cmd_set_default(args):
    """Set the default printer (always JSON)."""
    from local_printer import commands_net

    finish(commands_net.set_default(args.name), True)


# ==================== entry point ====================


def build_parser():
    parser = argparse.ArgumentParser(
        prog="printer-ai",
        description="Cross-platform local printer CLI - let an AI drive local printing",
    )
    subparsers = parser.add_subparsers(dest="command", help="available commands")

    # printers
    p_printers = subparsers.add_parser("printers", help="list installed printers")
    p_printers.add_argument("--json", action="store_true", help="output JSON")
    p_printers.set_defaults(func=cmd_printers)

    # status
    p_status = subparsers.add_parser("status", help="show printer status")
    p_status.add_argument("index", type=int, nargs="?", default=None,
                          help="printer index (1-based; default: the default printer)")
    p_status.add_argument("--json", action="store_true", help="output JSON")
    p_status.set_defaults(func=cmd_status)

    # attrs
    p_attrs = subparsers.add_parser("attrs", help="show printer attributes as JSON")
    p_attrs.add_argument("index", type=int, nargs="?", default=None,
                         help="printer index (1-based; default: the default printer)")
    p_attrs.set_defaults(func=cmd_attrs)

    # print
    p_print = subparsers.add_parser("print", help="print a file")
    p_print.add_argument("file_path", help="path of the file to print")
    p_print.add_argument("--index", type=int, default=None,
                         help="printer index (1-based; default: the default printer)")
    p_print.add_argument("--options", help="print options as a JSON string")
    p_print.set_defaults(func=cmd_print)

    # jobs
    p_jobs = subparsers.add_parser("jobs", help="list print jobs")
    p_jobs.add_argument("--printer", default=None, help="only jobs of this printer name")
    p_jobs.add_argument("--json", action="store_true", help="output JSON")
    p_jobs.set_defaults(func=cmd_jobs)

    # job-status
    p_js = subparsers.add_parser("job-status", help="query one print job")
    p_js.add_argument("job_id", type=int, help="job id")
    p_js.set_defaults(func=cmd_job_status)

    # cancel-job
    p_cj = subparsers.add_parser("cancel-job", help="cancel a print job")
    p_cj.add_argument("job_id", type=int, help="job id")
    p_cj.set_defaults(func=cmd_cancel_job)

    # discover
    p_disc = subparsers.add_parser("discover", help="scan the local network for printers")
    p_disc.add_argument("--subnet", default=None,
                        help="the /24 subnet to scan, e.g. 192.168.1")
    p_disc.add_argument("--timeout", type=float, default=0.6,
                        help="per-port timeout in seconds")
    p_disc.add_argument("--fast", action="store_true", help="skip the IPP identity query")
    p_disc.add_argument("--force", action="store_true",
                        help="allow scanning a subnet that is not one of this machine's "
                             "own /24 networks")
    p_disc.add_argument("--json", action="store_true", help="output JSON")
    p_disc.set_defaults(func=_net("discover", cmd_discover))

    # probe
    p_probe = subparsers.add_parser("probe", help="check whether one host is a printer")
    p_probe.add_argument("host", help="IP address")
    p_probe.add_argument("--timeout", type=float, default=1.0, help="timeout in seconds")
    p_probe.set_defaults(func=_net("probe", cmd_probe))

    # diagnose
    p_diag = subparsers.add_parser(
        "diagnose", help="check whether installed printers are really online")
    p_diag.add_argument("--timeout", type=float, default=1.0, help="timeout in seconds")
    p_diag.add_argument("--fast", action="store_true", help="skip the IPP identity query")
    p_diag.add_argument("--json", action="store_true", help="output JSON")
    p_diag.set_defaults(func=_net("diagnose", cmd_diagnose))

    # ports
    p_ports = subparsers.add_parser("ports", help="list printer ports")
    p_ports.set_defaults(func=_net("ports", cmd_ports))

    # drivers
    p_drv = subparsers.add_parser("drivers", help="list installed printer drivers")
    p_drv.add_argument("--model", default=None, help="match candidate drivers for a model")
    p_drv.set_defaults(func=_net("drivers", cmd_drivers))

    # setup
    p_setup = subparsers.add_parser(
        "setup", help="install a network printer, best driver first")
    p_setup.add_argument("host", help="printer IP address")
    p_setup.add_argument("--name", default=None,
                         help="queue name (default: the device model)")
    p_setup.add_argument("--dry-run", action="store_true",
                         help="only show the plan, change nothing")
    p_setup.add_argument("--no-generic", action="store_true",
                         help="refuse the generic driver fallback (fail instead)")
    p_setup.add_argument("--vendor-lookup", action="store_true",
                         help="query the manufacturer's download portal for a driver "
                              "(sends model/OS/region to the vendor)")
    p_setup.add_argument("--json", action="store_true", help="output JSON")
    p_setup.set_defaults(func=_net("setup", cmd_setup))

    # driver-search
    p_ds = subparsers.add_parser(
        "driver-search", help="look up a driver on the manufacturer's site")
    p_ds.add_argument("model", nargs="?", default=None,
                      help="printer model, e.g. 'EPSON ET-4850 Series'")
    p_ds.add_argument("--host", default=None,
                      help="read the model from this IP over IPP instead")
    p_ds.add_argument("--region", default=None,
                      help="two-letter region code, e.g. DE / US (default: system region)")
    p_ds.add_argument("--os", default=None,
                      help="vendor OS code (default: auto-detected)")
    p_ds.add_argument("--download", default=None, metavar="DIR",
                      help="download the installer into this directory "
                           "(downloads and checksums only, never executes)")
    p_ds.add_argument("--open", action="store_true",
                      help="open the download link in the default browser "
                           "(for vendors that refuse scripted downloads)")
    p_ds.add_argument("--json", action="store_true", help="output JSON")
    p_ds.set_defaults(func=_net("driver_search", cmd_driver_search))

    # remove
    p_rm = subparsers.add_parser("remove", help="delete a printer queue")
    p_rm.add_argument("name", help="printer name")
    p_rm.add_argument("--yes", action="store_true", help="confirm the deletion")
    p_rm.set_defaults(func=_net("remove", cmd_remove))

    # set-default
    p_sd = subparsers.add_parser("set-default", help="set the default printer")
    p_sd.add_argument("name", help="printer name")
    p_sd.set_defaults(func=_net("set_default", cmd_set_default))

    return parser


def main():
    # Console code pages differ wildly across the machines this ships to;
    # never let an unencodable character turn into a crash.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass

    parser = build_parser()
    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(0)

    try:
        args.func(args)
    except SystemExit:
        raise
    except KeyboardInterrupt:
        print("Aborted", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
