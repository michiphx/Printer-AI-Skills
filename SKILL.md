---
name: printer-ai
description: "Cross-platform local printer CLI - Manage and print to local printers (Windows/macOS/Linux) via the printer-ai CLI. Use when the user needs to print files, query printer status, or manage print jobs. NOT for: cloud/remote printers."
metadata: {"openclaw":{"emoji":"🖨️","requires":{"bins":["printer-ai"]},"install":[{"id":"uv","kind":"uv","package":"git+https://github.com/NullYing/printer-ai-skills.git","bins":["printer-ai"],"label":"Install printer-ai (uv)"}]}}
---

# Cross-Platform Local Printer Skill

Operate local printers via the `printer-ai` CLI. Supports Windows, macOS, and Linux.

## When to Use

✅ **USE this skill when:**

- User wants to print local files (PDF, images, Office documents, etc.)
- Query local printer list and status (查询本地打印机列表和状态)
- Manage print jobs: check status, cancel jobs (管理打印任务)
- Get detailed printer attributes / capabilities (获取打印机属性/能力)

## When NOT to Use

❌ **DON'T use this skill when:**

- Operating cloud / remote print boxes (use `lianke-print-box` skill instead)
- The printer is not locally connected

## Setup

```bash
# Install
uv tool install git+https://github.com/NullYing/printer-ai-skills.git

# Verify
printer-ai printers
```

## Printing Workflow

### 1. List Printers

```bash
# Human-friendly format
printer-ai printers

# JSON format (recommended for parsing)
printer-ai printers --json
```

Get the printer `index` from output (needed for subsequent commands). ⭐ marks the default printer.

### 2. Check Printer Status

```bash
printer-ai status INDEX
# Or JSON format
printer-ai status INDEX --json
```

Status meanings:
- 🟢 `idle` = Ready (空闲可用)
- 🟡 `processing` = Busy (处理中)
- 🔴 `stopped` = Stopped (已停止)

### 3. Get Printer Attributes (optional, to discover capabilities)

```bash
printer-ai attrs INDEX
```

Returns all supported options (paper size, color mode, duplex, etc.).

### 4. Print a File

```bash
# Print with default printer
printer-ai print /path/to/file.pdf

# Specify printer by index
printer-ai print /path/to/file.pdf --index 2

# With print options — macOS/Linux (CUPS/IPP format)
printer-ai print /path/to/file.pdf --options '{"copies":"2","media":"A4","orientation_requested":"3","print_color_mode":"color"}'

# With print options — Windows (DEVMODE format)
printer-ai print /path/to/file.pdf --options '{"dmCopies":2,"dmPaperSize":9,"dmOrientation":1,"dmColor":2}'
```

### 5. Query Job Status

```bash
printer-ai job-status JOB_ID
```

### 6. List All Jobs

```bash
printer-ai jobs
printer-ai jobs --printer "Printer Name"
printer-ai jobs --json
```

### 7. Cancel a Job

```bash
printer-ai cancel-job JOB_ID
```

## Print Options Quick Reference

### macOS / Linux (CUPS/IPP format)

| Option | Example Value | Description |
|--------|--------------|-------------|
| `copies` | `"2"` | Number of copies (打印份数) |
| `media` | `"A4"`, `"Letter"` | Paper size (纸张大小) |
| `orientation_requested` | `"3"`=portrait, `"4"`=landscape | Orientation (方向) |
| `print_color_mode` | `"monochrome"`, `"color"` | Color mode (颜色模式) |
| `sides` | `"one-sided"`, `"two-sided-long-edge"` | Duplex (双面打印) |
| `print_quality` | `"3"`=draft, `"4"`=normal, `"5"`=high | Quality (质量) |
| `page_ranges` | `"1-5,10-15"` | Page range (页面范围) |
| `number_up` | `"2"`, `"4"` | Pages per sheet (每页合并页数) |

### Windows (DEVMODE format)

| Option | Example Value | Description |
|--------|--------------|-------------|
| `dmCopies` | `2` | Number of copies (打印份数) |
| `dmPaperSize` | `9`=A4, `1`=Letter | Paper size (纸张大小) |
| `dmOrientation` | `1`=portrait, `2`=landscape | Orientation (方向) |
| `dmColor` | `1`=mono, `2`=color | Color mode (颜色模式) |
| `dmDuplex` | `1`=simplex, `2`=long-edge, `3`=short-edge | Duplex (双面打印) |
| `dmPrintQuality` | `-4`=default | Quality (质量) |

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
reachable".**

### `discover` — find printers on the LAN

```bash
printer-ai discover                      # scan the local /24
printer-ai discover --subnet 192.168.1   # a specific /24
printer-ai discover --json
```

Probes ports 9100/631/515, then asks each hit over IPP for its model, state and
real capabilities (duplex, media, colour modes). Adds the MAC from the ARP cache.

### `probe HOST` — inspect one address

```bash
printer-ai probe 192.168.1.72
```

Returns open ports and the device's own IPP identity. Note: many printers (Epson
among them) accept TCP on 631 but **reset anything that is not TLS** — the query
tries plain IPP, then IPPS, then 443.

### `setup HOST` — install with the best available driver ⭐

```bash
printer-ai setup 192.168.1.72 --dry-run          # show the plan, change nothing
printer-ai setup 192.168.1.72 --name "Buero"     # install
printer-ai setup 192.168.1.72 --no-generic       # fail rather than degrade
```

Identifies the device over IPP, then walks a strategy ladder from best to worst:

1. **Vendor driver** — installed, or pulled from the in-box Windows INF store
2. **IPP Everywhere** — Microsoft IPP Class Driver on a real IPP port, which
   negotiates duplex/media/colour live from the device *(needs admin)*
3. **Raw 9100 fallback** — always prints, but exposes only generic capabilities

Afterwards it **verifies the installed queue against the device's own advertised
capabilities** and reports what was lost:

```json
"missing": ["duplex (device supports two-sided printing)",
            "media types (0 exposed, 9 on device)"],
"full_featured": false
```

`--no-generic` refuses step 3 entirely — better no printer than a crippled one.

⚠️ Steps 1 and 2 need an **elevated shell**. Without admin only the raw fallback
succeeds; the output says so in `note`/`hint` rather than failing silently.

### `driver-search` — find the manufacturer driver online ⭐

When no driver is installed and Windows ships none in-box, the only real source
is the vendor. `setup` calls this automatically and puts the result in
`vendor_driver_lookup` + `driver_hint`; you can also run it directly:

```bash
printer-ai driver-search --host 192.168.1.72        # read the model over IPP
printer-ai driver-search "EPSON ET-4850 Series"     # or name it
printer-ai driver-search --host 1.2.3.4 --region DE --os WIN1164
printer-ai driver-search --host 1.2.3.4 --download C:\Temp   # opt-in, see below
```

OS and region are auto-detected (Windows build → `WIN1164` for 11 x64, region
from the system locale). Region matters: the EU/CH build of the ET-4850 driver
is **3.80.05**, the US one **3.80.00**.

**Epson** is implemented against the Download Center API. That API sits behind a
WAF that answers browsers but returns 403 to plain HTTP clients, so the call is
best-effort and always degrades to a **verified deep link** to the filtered
download page — `api_reachable` says which happened, `page_verified` is true
either way.

For **other vendors** the tool reports `supported: false` plus the official
support hub, explicitly marked `site_verified: false`. It does not invent
download URLs.

⚠️ `--download` fetches the installer and **nothing else** — it is never
executed, and only ever runs when you pass the flag. Install it yourself, then
re-run `printer-ai setup` so the vendor driver gets picked up.

### Inventory & management

```bash
printer-ai ports                              # spooler ports
printer-ai drivers --model "EPSON ET-4850"    # ranked driver candidates
printer-ai set-default "Printer Name"
printer-ai remove "Printer Name" --yes        # --yes is required
```

## Notes

- Check printer status with `printer-ai status` before printing to confirm it is online
- `status` reflects the spooler cache — use `diagnose` to confirm real reachability
- Print option formats differ by platform: macOS/Linux uses CUPS/IPP string format, Windows uses DEVMODE integer format
- Use `printer-ai attrs` to discover actual supported options for a specific printer
- The `--json` flag returns pure JSON output for easy programmatic parsing
