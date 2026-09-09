---
name: printer-ai
description: "Cross-platform local printer CLI - discover, diagnose, set up and print to local printers (Windows/macOS/Linux) via the printer-ai CLI. Use when the user needs to print files, find or install a network printer, look up or download a manufacturer driver, check whether a printer is really online, or manage print jobs. NOT for: cloud/remote printers."
metadata: {"openclaw":{"emoji":"🖨️","requires":{"bins":["printer-ai"]},"install":[{"id":"uv","kind":"uv","package":"git+https://github.com/michiphx/Printer-AI-Skills.git","bins":["printer-ai"],"label":"Install printer-ai (uv)"}]}}
---

# Cross-Platform Local Printer Skill

Operate local printers via the `printer-ai` CLI. Supports Windows, macOS, and Linux.

Helper scripts for the Windows-only chores that the CLI cannot do yet live in a
`scripts/` folder **inside this skill's own directory** — the base directory
Claude Code reports when it loads this skill. Always invoke them by their full
path (`<skill base directory>\scripts\<name>.ps1`); a relative `scripts\…` path
resolves against the current working directory and will not find them.

Every command exits **non-zero when it fails** (exit `0` only when the JSON
`code` is `200`), so you can branch on `$?` / `$LASTEXITCODE` instead of parsing
the JSON just to detect failure.

## When to Use

✅ **USE this skill when:**

- User wants to print local files (PDF, images, Office documents, etc.)
- Query local printer list and status
- Manage print jobs: check status, cancel jobs
- Get detailed printer attributes / capabilities
- Find a printer on the LAN, check whether an installed one is really reachable,
  install a network printer, or look up the manufacturer driver

## When NOT to Use

❌ **DON'T use this skill when:**

- Operating cloud / remote print boxes (use `lianke-print-box` skill instead)
- The printer is not locally connected

## Setup

```bash
# Install the CLI (needs uv: https://docs.astral.sh/uv/)
uv tool install git+https://github.com/michiphx/Printer-AI-Skills.git

# Verify
printer-ai printers
```

Windows notes:

- `uv` puts `printer-ai.exe` into `%USERPROFILE%\.local\bin`. A shell that was
  opened before the install does not see it yet: prepend
  `$env:PATH = "$env:USERPROFILE\.local\bin;$env:PATH"`.
- If the launcher fails with `uv trampoline failed to canonicalize script path`
  (seen in some elevated shells), call the venv directly:
  `& "$env:APPDATA\uv\tools\printer-ai-skills\Scripts\python.exe" <checkout>\main.py …`
  or re-run `uv tool install --reinstall …` from that shell.

## Printing Workflow

### 1. List Printers

```bash
printer-ai printers          # human-friendly
printer-ai printers --json   # recommended for parsing
```

Get the printer `index` from output (needed for subsequent commands). ⭐ marks the default printer.

### 2. Check Printer Status

```bash
printer-ai status              # no INDEX → the default printer
printer-ai status INDEX
printer-ai status INDEX --json
```

- 🟢 `idle` = ready
- 🟡 `processing` = busy
- 🔴 `stopped` = stopped

`status` is the spooler's cached view. For "is it actually reachable" use `diagnose` (below).

### 3. Get Printer Attributes (optional)

```bash
printer-ai attrs INDEX
```

Returns all options the *driver* exposes (paper size, color mode, duplex, …).

### 4. Print a File

On **Windows** the file is sent to the spooler as raw data, so only
**PDF, PS, PRN and TXT** are accepted. Anything else is refused with code `415`
— convert it to PDF first (e.g. print it to *Microsoft Print to PDF*, or use the
source application's own "Export/Save as PDF") and print the PDF. Note that the
target printer must itself understand PDF/PostScript for a PDF to come out
right; `--options` are best-effort on this path.

```bash
printer-ai print /path/to/file.pdf                # no --index → the default printer
printer-ai print /path/to/file.pdf --index 2      # a specific printer

# macOS/Linux (CUPS/IPP option names)
printer-ai print /path/to/file.pdf --options '{"copies":"2","media":"A4","orientation_requested":"3","print_color_mode":"color"}'

# Windows (DEVMODE option names)
printer-ai print /path/to/file.pdf --options '{"dmCopies":2,"dmPaperSize":9,"dmOrientation":1,"dmColor":2}'
```

### 5. Track Jobs

```bash
printer-ai job-status JOB_ID                 # one job + its printer status
printer-ai jobs [--printer "Printer Name"] [--json]
printer-ai cancel-job JOB_ID
```

## Print Options Quick Reference

### macOS / Linux (CUPS/IPP format)

| Option | Example Value | Description |
|--------|--------------|-------------|
| `copies` | `"2"` | Number of copies |
| `media` | `"A4"`, `"Letter"` | Paper size |
| `orientation_requested` | `"3"`=portrait, `"4"`=landscape | Orientation |
| `print_color_mode` | `"monochrome"`, `"color"` | Color mode |
| `sides` | `"one-sided"`, `"two-sided-long-edge"` | Duplex |
| `print_quality` | `"3"`=draft, `"4"`=normal, `"5"`=high | Quality |
| `page_ranges` | `"1-5,10-15"` | Page range |
| `number_up` | `"2"`, `"4"` | Pages per sheet |

### Windows (DEVMODE format)

| Option | Example Value | Description |
|--------|--------------|-------------|
| `dmCopies` | `2` | Number of copies |
| `dmPaperSize` | `9`=A4, `1`=Letter | Paper size |
| `dmOrientation` | `1`=portrait, `2`=landscape | Orientation |
| `dmColor` | `1`=mono, `2`=color | Color mode |
| `dmDuplex` | `1`=simplex, `2`=long-edge, `3`=short-edge | Duplex |
| `dmPrintQuality` | `-4`=default | Quality |

## Command Reference

| Command | Purpose | Key flags |
|---|---|---|
| `printers` | List installed queues | `--json` |
| `status [INDEX]` | Spooler status of one queue (default printer without `INDEX`) | `--json` |
| `attrs [INDEX]` | Driver capabilities + DevMode | — |
| `print FILE` | Submit a print job (default printer without `--index`) | `--index`, `--options` |
| `jobs` | List print jobs | `--printer`, `--json` |
| `job-status JOB_ID` | One job + its printer status | — |
| `cancel-job JOB_ID` | Cancel a job | — |
| `discover` | Scan the LAN for printers | `--subnet`, `--force`, `--timeout`, `--fast`, `--json` |
| `probe HOST` | Inspect one address over IPP | `--timeout` |
| `diagnose` | Are the installed printers *really* online | `--timeout`, `--fast`, `--json` |
| `ports` | Spooler ports | — |
| `drivers` | Installed drivers, ranked per model | `--model` |
| `setup HOST` | Install a printer, best driver first | `--name`, `--dry-run`, `--no-generic`, `--vendor-lookup`, `--json` |
| `driver-search` | Find the vendor driver online | `--host`, `--region`, `--os`, `--download`, `--open`, `--json` |
| `remove NAME` | Delete a queue | `--yes` (required) |
| `set-default NAME` | Set the default printer | — |

Indexes are 1-based and come from `printers`; they shift when queues are added
or removed, so re-read them rather than caching.

## Network Discovery & Setup

The spooler reports a *cached* state: a printer that is switched off, or stranded
on an old subnet, still shows up as `idle`. These commands go to the wire instead.

### `diagnose` — which printers are really online ⭐

```bash
printer-ai diagnose            # human-readable
printer-ai diagnose --json     # for parsing
printer-ai diagnose --fast     # skip the IPP identity query
```

Resolves each queue's host, probes it, and reports `really_online` plus the live
device state. **Use this instead of `status` when the question is "is it actually
reachable".** Limitation: queues on Windows `WSD-*` ports cannot be resolved,
because their address is not stored in the port; they are reported as
`kind: unknown`. Check those with a test page instead
(`<skill base directory>\scripts\win-pair-printer.ps1` prints one after
installing).

### `discover` — find printers on the LAN

```bash
printer-ai discover                              # scan this machine's own /24
printer-ai discover --subnet 192.168.1           # another /24 → needs --force
printer-ai discover --subnet 192.168.1 --force
printer-ai discover --json
```

Probes ports 9100/631/515, then asks each hit over IPP for its model, state and
real capabilities (duplex, media, colour modes). Adds the MAC from the ARP cache.

By default only this machine's own /24 is scanned. A `--subnet` that is not a
local one is refused unless you add `--force` — tell the user you are about to
scan a network they are not on, and only then pass `--force`.

### `probe HOST` — inspect one address

```bash
printer-ai probe 192.168.1.72
```

Returns open ports and the device's own IPP identity. Many printers (Epson among
them) accept TCP on 631 but only *serve* IPPS — the query tries plain IPP, then
IPPS, then 443.

### Before installing a network printer — flag IP stability once

A network printer's IP is usually DHCP-assigned and can silently change (lease
renewal, router reboot, someone redoing its WiFi from the front panel). A queue
built against the old IP then goes stale and printing just stops — the most
common reason a printer "disappears" weeks after a correct setup.

Before running `setup HOST` on a *network* printer (skip for USB/local queues),
explain this in plain language and offer two fixes:

1. **DHCP reservation on the router (recommended)** — bind the printer's MAC
   (from `discover`/`probe`) to its current IP in the router's DHCP settings.
   Needs the user's own router login — guide them, never enter credentials.
2. **Static IP on the printer** — a fixed IP in the printer's own network menu,
   outside the DHCP range. Simpler, but a factory reset or "reconnect to WiFi"
   silently undoes it.

Ask once. If they decline, proceed against the current IP without blocking.
Queues created by Windows pairing (`WSD-*` ports) track the printer by its UUID
rather than its IP, so there the reservation is belt-and-braces only.

### `setup HOST` — install with the best available driver ⭐

```bash
printer-ai setup 192.168.1.72 --dry-run          # show the plan, change nothing
printer-ai setup 192.168.1.72 --name "Buero"     # install
printer-ai setup 192.168.1.72 --no-generic       # fail rather than degrade
printer-ai setup 192.168.1.72 --vendor-lookup    # opt-in: ask the vendor site
```

Identifies the device over IPP, then walks a strategy ladder from best to worst:

1. **Vendor driver** — installed, or pulled from the in-box Windows INF store
2. **IPP Everywhere** — Microsoft IPP Class Driver on a real IPP port, which
   negotiates duplex/media/colour live from the device *(needs admin)*
3. **Raw 9100 fallback** — always prints, but exposes only generic capabilities

Afterwards it **checks the installed queue against the device's own advertised
capabilities** — a heuristic comparison, not a print test — and reports what was
lost:

```json
"missing": ["duplex (device supports two-sided printing)",
            "media types (0 exposed, 9 on device)"],
"full_featured": false
```

`--no-generic` refuses step 3 entirely — better no printer than a crippled one.

⚠️ Steps 1 and 2 need an **elevated shell**. Without admin only the raw fallback
succeeds; the output says so in `note`/`hint` rather than failing silently.

⚠️ On **Windows 11** step 2 is **skipped** (`Add-PrinterPort` cannot create IPP
ports there). `setup` marks that strategy as skipped and returns a
`recommended` field in its JSON, e.g.

```json
"recommended": "scripts/win-pair-printer.ps1 -Match ... -Name ..."
```

**Follow that recommendation**: run the named script from this skill's own
folder (full path — see the next section), which delivers exactly the
full-featured class-driver queue that step 2 promises.

**`--vendor-lookup` is opt-in and leaves the network.** It queries the
manufacturer's site and sends the printer model, your OS version and your
region. `setup` does **not** do this unless you pass the flag. Use it only when
no usable driver was found locally and the user has agreed to a vendor lookup;
otherwise leave it off and work from what is installed in-box.

### Windows 11: installing a network printer that actually works

Recommended order for a network printer on Windows 11:

1. `printer-ai discover --json` / `printer-ai probe HOST` — confirm the device,
   its model, MAC and advertised capabilities (`supports_duplex`, `media_types`).
2. **Pair it through Windows Settings** — the same thing a person does under
   *Settings → Bluetooth & devices → Printers & scanners → Add a printer or
   scanner → Add device*, driven by UI Automation so the agent can do it. The
   script lives in this skill's own folder — use the base directory Claude Code
   reported when it loaded this skill and call it with the **full path**:

   ```powershell
   powershell -ExecutionPolicy Bypass -File "<skill base directory>\scripts\win-pair-printer.ps1" -Match "4850" -Name "EPSON ET-4850 (Office)" -SetDefault
   ```

   `-Match` is a regex against the names Settings shows. The result is a
   Microsoft IPP Class Driver queue on a `WSD-<guid>` port with colour, duplex,
   trays and media negotiated from the device, tracked by the printer's UUID.
   The script checks the capabilities, prints a Windows test page and reports
   JSON. Needs an interactive desktop; renaming needs an elevated shell (the
   script says so if it cannot rename). Do **not** try to script
   `Windows.Devices.Enumeration` pairing (`PairAsync`) instead — it returns
   `Failed` for printers while the Settings flow succeeds.
3. **Vendor driver** — `printer-ai driver-search --host HOST --open`, let the
   user run the installer, then `printer-ai setup HOST --no-generic` (elevated),
   which now picks the vendor driver on a standard TCP/IP port. Best print
   quality and vendor UI, but IP-bound: pair it with a DHCP reservation.
4. **Raw 9100** (`setup` without `--no-generic`) — last resort, generic
   capabilities only.

Running something elevated from an agent shell: UAC cannot be answered by the
agent, but `<skill base directory>\scripts\win-run-elevated.ps1 -ScriptPath job.ps1`
launches the job with `-Verb RunAs`, the user clicks the prompt, and the script
returns the job's output. Tell the user before each call that a UAC prompt will
appear.

### `driver-search` — find the manufacturer driver online ⭐

When no driver is installed and Windows ships none in-box, the only real source
is the vendor. `setup --vendor-lookup` runs this lookup and puts the result in
`vendor_driver_lookup` + `driver_hint` (without the flag `setup` stays offline
and reports `vendor_driver_lookup.skipped: true`); you can also run it directly:

```bash
printer-ai driver-search --host 192.168.1.72        # read the model over IPP
printer-ai driver-search "EPSON ET-4850 Series"     # or name it
printer-ai driver-search --host 1.2.3.4 --region DE --os WIN1164
printer-ai driver-search --host 1.2.3.4 --download C:\Temp   # opt-in, see below
```

This command contacts the manufacturer's site and sends the model, OS and
region. OS and region are auto-detected (Windows build → `WIN1164` for 11 x64,
region from the system locale); region matters, vendors ship different builds
per region.

**Epson** is implemented against the Download Center API, which sits behind a
WAF that answers browsers but returns 403 to plain HTTP clients. The call is
best-effort and always degrades to a **checked deep link** to the filtered
download page — `api_reachable` says which happened, `page_verified` is true
either way. For **other vendors** the tool reports `supported: false` plus the
official support hub, marked `site_verified: false`. It never invents URLs.

**Getting the file — `--download` vs `--open`**

```bash
printer-ai driver-search --host 192.168.1.72 --download C:\Temp   # fetch + check
printer-ai driver-search --host 192.168.1.72 --open              # hand it to the browser
```

`--download` fetches the installer, then **checks** it before keeping it — a
heuristic check, not proof of authenticity: Content-Length match, expected size
from the vendor API, and an `MZ` header check for `.exe`. It writes to a `.part`
file and only renames on success, so a blocked or truncated attempt leaves
nothing behind. It reports SHA-256.

The result carries a **`verification`** field (the size/header checks) and a
**`signature`** field (the Authenticode signer, on Windows). Read both and
report them to the user. `--download` **never executes anything, and neither may
you: never run the installer yourself.** Hand the file to the user, let *them*
run it, then re-run `printer-ai setup` so the vendor driver is picked up.

When the vendor's CDN answers HTTP 403/406/429 the result is `blocked: true`
with `open_in_browser_url` — not a bare failure. That is the normal case for
Epson: **their WAF rejects urllib, PowerShell and curl alike, on both the API
and the file host.** `--open` then hands the exact URL to the default browser,
which the WAF does serve.

⚠️ Neither flag ever runs an installer, and `--open` only opens URLs over HTTPS.

### Inventory & management

```bash
printer-ai ports                              # spooler ports
printer-ai drivers --model "EPSON ET-4850"    # ranked driver candidates
printer-ai set-default "Printer Name"
printer-ai remove "Printer Name" --yes        # --yes is required
```

Stale `WSD-*` ports left behind by deleted queues (Windows):

```powershell
# elevated; exports the registry key to a .reg backup, then purges the port.
# The script lives in this skill's own folder — use the base directory Claude
# Code reported when it loaded this skill, and give the full path:
powershell -ExecutionPolicy Bypass -File "<skill base directory>\scripts\win-purge-wsd-port.ps1" -Port WSD-951ab23e-118c-4530-bcad-de9f8d6a5006 -Yes
```

## Gotchas

Things that cost time once; check them before debugging further.

**A queue reporting `idle` proves nothing.** The Windows spooler caches state.
A printer that is powered off, or whose WSD entry still points at an address
from a previous network, reports `idle` forever. Use `diagnose`, or a test page
for `WSD-*` queues.

**Stale WSD ports carry no usable IPv4.** A WSD queue may hold only a
link-local IPv6 address (`fe80::…`), which is useless for reachability checks.
The EUI-64 in such an address encodes the MAC — flip bit 1 of the first octet
(`fe80::6a55:d4ff:fe7d:8b68` → MAC `68:55:D4:7D:8B:68`) and match it against the
ARP table to find the device's current IPv4.

**Some printers answer plain IPP with HTTP 426 Upgrade Required.** They may even
advertise `uri-security-supported: tls, none`, yet only IPPS actually serves
requests. Windows' IPP pairing provider cannot follow the TLS upgrade, so the
`IPP#…` entry in Settings never installs; the WSD entry of the same printer
(port 80, WS-Print 2.0) is unaffected and still yields the full-featured class
driver queue. `probe` reports `ipp_tls: true` in that case. Alternatively the
printer's web config usually has "IPP → allow non-secure communication" (user
must log in themselves).

**Never `Add-Printer` onto a leftover `WSD-*` port.** After a queue is deleted
its port stays behind without a device node. A queue placed on it installs with
the generic Letter/no-duplex profile and every job ends in `Error`. Purge the
port (`scripts/win-purge-wsd-port.ps1`) and pair again.

**Stale `WSD-*` ports cannot be removed with `Remove-PrinterPort`** ("in use"
or a generic error, even after a spooler restart). Stop the spooler, delete
`HKLM\SYSTEM\CurrentControlSet\Control\Print\Monitors\WSD Port\Ports\<name>`
(export it first), start the spooler — that is what the purge script does.

**Never `Restart-Service DeviceAssociationService`.** It hangs in STOP_PENDING
(NOT_STOPPABLE) and blocks every discovery and pairing until the `dasHost.exe`
workers are killed; Windows respawns them and the service starts cleanly.

**Setup needs elevation.** Standard TCP/RAW ports can often be created without
admin, but IPP and WSD ports and `Add-PrinterDriver` cannot. Without elevation
only the degraded raw-9100 path succeeds; `setup` says so in `note`/`hint`
instead of failing silently.

**A generic driver on a raw port silently loses features.** The Microsoft IPP
Class Driver over port 9100 negotiates nothing: no duplex, no trays, no media
types, Letter as default. Always read `verification.missing` after `setup`, or
use `--no-generic` to refuse the downgrade outright.

**Vendor portals block scripted clients.** Expect 403 from urllib, PowerShell
and curl on both API and file host. Fall back to `--open`, never assume a
scrape will work.

## Notes

- Check printer status with `printer-ai status` before printing to confirm it is online
- `status` reflects the spooler cache — use `diagnose` to confirm real reachability
- Print option formats differ by platform: macOS/Linux uses CUPS/IPP strings,
  Windows uses DEVMODE integers
- `attrs` reports what the *driver* exposes; `probe`/`discover` report what the
  *device* advertises. When they disagree, the device is right and the driver is
  the limitation.
- The `--json` flag returns pure JSON output for easy programmatic parsing
- Credentials (printer web config, router admin) stay with the user: guide them,
  never type passwords for them.
