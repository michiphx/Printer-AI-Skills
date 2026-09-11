# Printer AI Skills

[中文文档](README_CN.md)

A cross-platform local printer CLI that lets AI assistants drive printing operations on Windows, macOS, and Linux. Works with OpenClaw / Cursor / Claude Code and other AI Skill systems.

This is a fork of [NullYing/Printer-AI-Skills](https://github.com/NullYing/Printer-AI-Skills) that adds network discovery, reachability diagnosis, driver lookup, a capability-verifying `setup` command, the Windows helper scripts an agent needs to install a network printer with all of its features, a universal "convert anything to PDF" layer in front of every print job, and driver-based (rather than raw) printing on Windows.

## Features

- 🌍 **Cross-platform**: Windows, macOS, Linux (CUPS)
- 🖨️ **Printer Management**: List printers, query status and capabilities
- 📄 **Print any file**: PDFs, images, text and code, Markdown, HTML/SVG, Office
  documents and CSV are converted to PDF automatically before printing — no
  "please convert this to PDF first"
- 🔄 **Conversion on its own**: `convert` produces the PDF without printing,
  `formats` says what this machine can handle right now and what is missing
- ⚙️ **Print options**: paper size, colour, duplex, copies, quality, tray —
  honoured by the driver on all three platforms
- 📊 **Job Management**: Track and cancel print jobs
- 🔎 **Network**: `discover` printers on the LAN, `probe` one address, `diagnose` whether installed queues are really reachable
- 🛠️ **Setup**: `setup HOST` installs with the best available driver and verifies the queue against the device's advertised capabilities; `driver-search` finds the vendor driver
- 🪟 **Windows helpers** (`scripts/`): pair a printer through Settings via UI Automation, purge stale WSD ports, run a job elevated
- 🤖 **AI-ready**: CLI + SKILL.md for seamless AI integration

## Supported file types

`print` normalises the file to PDF before it reaches the platform backend, so
you never have to convert anything by hand. `printer-ai formats` reports the
same table for *this* machine, including which converters are usable right now.

| Type | Extensions | How it is converted | Needs |
|---|---|---|---|
| PDF / PostScript / spool file | `.pdf` `.ps` `.prn` | passed through unchanged | — |
| Images | `.jpg` `.jpeg` `.jpe` `.jfif` `.png` `.gif` `.bmp` `.dib` `.tif` `.tiff` `.webp` `.ico` `.ppm` `.pgm` `.pbm` `.tga` | Pillow decodes it, every frame becomes one page, scaled to fit and centred with a 0.5″ margin | bundled (Pillow + reportlab) |
| Text, code, logs, config, data | `.txt` `.log` `.json` `.yaml` `.toml` `.ini` `.xml` `.sql` `.diff` `.py` `.js` `.ts` `.sh` `.ps1` `.c` `.cpp` `.go` `.rs` `.java` `.css` … (~75 in total) | reportlab, Courier 10 pt, hard-wrapped to the page, file name in the header and a page number in the footer | bundled (reportlab) |
| Markdown | `.md` `.markdown` `.mdown` `.mkd` `.mdtext` | rendered to styled HTML, then printed like HTML; with no HTML renderer at all the Markdown source is printed as plain text instead of failing | browser, else LibreOffice (both optional) |
| HTML / SVG | `.html` `.htm` `.xhtml` `.svg` | headless Chromium-family browser (`--headless=new --print-to-pdf`); if no browser is found, or it fails or times out, LibreOffice renders it (basic HTML/CSS only) | a Chromium-family browser, or LibreOffice |
| Office documents | `.doc` `.docx` `.docm` `.dot(x)` `.odt` `.ott` `.rtf` `.wps` `.pages` `.xls` `.xlsx` `.xlsm` `.xlt(x)` `.ods` `.ppt` `.pptx` `.pps(x)` `.pot(x)` `.odp` `.odg` `.otg` `.vsd` `.vsdx` … | Windows: Microsoft Office through COM when it is installed, LibreOffice otherwise. macOS/Linux: LibreOffice headless (always with a private profile, so an open LibreOffice window cannot break the conversion) | LibreOffice, or Microsoft Office on Windows |
| CSV / TSV | `.csv` `.tsv` | LibreOffice lays it out as a table; without a spreadsheet application it falls back to the plain-text renderer | LibreOffice optional |

The format is decided by the extension first. When the extension is missing or
unknown the file's magic bytes decide, and a generic text-ish extension hiding a
PDF, an image or an Office document is overridden by its bytes — a `.txt` that
is really a PDF prints as a PDF.

Anything that is none of the above (an unknown binary) is refused with code
`415` and a `hint`; pass `--raw` if the printer understands it natively.

## Limitations

Please read these before you rely on the tool — they are real and current.

- **Conversion depends on tools that are not bundled.** Office documents
  (`.docx`, `.xlsx`, `.pptx`, `.odt`, …) need **LibreOffice**, or Microsoft
  Office on Windows. HTML, SVG and Markdown need a **Chromium-family browser**
  (Edge ships with Windows), with LibreOffice as a fallback that understands
  only basic HTML/CSS. Without either, `print` and `convert` fail with code
  `415` and a `hint` naming what to install. Run `printer-ai formats` to see
  what is present. PDFs, images, text/code and CSV work out of the box.
- **Windows printing is raster.** PDFs are rendered to bitmaps with pypdfium2
  and drawn onto the printer's device context via GDI. Every installed printer
  can therefore print, and `--options` (DEVMODE) are handed to the driver — but
  the output is a raster image: text is slightly softer than a natively
  rasterised PDF and spool files are larger. Render resolution is the printer's
  own DPI capped at 300 (override with `PRINTER_AI_RENDER_DPI`);
  `dmPrintQuality` is passed to the driver but does **not** change that DPI.
- **A driver that ignores a DEVMODE field still ignores it.** `--options` are
  passed through, not enforced; a queue that exposes no duplex will not
  duplex. `printer-ai attrs` shows what the driver admits to.
- **Layout conversions target a single page size.** Images, text/code and
  Markdown are laid out on A4, or Letter for `en_US`-style locales; override
  with `PRINTER_AI_PAGE_SIZE=A4|Letter`. Office documents and HTML keep
  whatever page size their own renderer picks.
- **`--raw` is for printer-native data only.** It skips conversion and sends
  the bytes untouched (CUPS `-o raw`, Windows RAW spool). A device that does
  not speak the format will print garbage or nothing.
- **`setup`'s IPP-Everywhere strategy does not work on Windows 11.** IPP ports
  cannot be created there, so that strategy is skipped. Use
  `scripts/win-pair-printer.ps1` instead, which pairs the printer through
  Windows Settings.
- **`setup --vendor-lookup` contacts a manufacturer site.** Together with
  `driver-search`, it is the only command that leaves your network to a vendor;
  the printer model, your OS version and your region are sent. It is opt-in.
- **`diagnose` cannot resolve Windows WSD-port queues.** Their address is not
  stored in the port, so they are reported as `kind: unknown`.
- **`discover` only scans this machine's own /24** unless you pass `--force`.
- **macOS/Linux need CUPS headers for pycups.** Install them
  (`apt install libcups2-dev` / `brew install cups`) or the printer backend
  reports code `501`. The network commands keep working either way.

## Quick Start

### Installation

```bash
# Install globally (recommended)
uv tool install git+https://github.com/michiphx/Printer-AI-Skills.git

# Or local development
git clone https://github.com/michiphx/Printer-AI-Skills.git
cd Printer-AI-Skills
uv sync
```

Pillow, reportlab and markdown are installed as dependencies (plus pypdfium2 on
Windows), so PDFs, images, text/code and CSV print without anything else.
**Optional external tools**, only needed for the formats they cover:

- **LibreOffice** — Office documents, CSV tables, and the HTML fallback.
  `apt install libreoffice` / `dnf install libreoffice` /
  `brew install --cask libreoffice` / <https://www.libreoffice.org/download/>
- **A Chromium-family browser** (Edge, Chrome, Chromium, Brave) — HTML, SVG and
  Markdown. Microsoft Edge is preinstalled on Windows.

`printer-ai formats` tells you which of these were found.

### Usage

```bash
printer-ai printers              # List printers
printer-ai status 1              # Check printer status
printer-ai print report.docx     # Print any file (converted to PDF first)
printer-ai convert notes.md      # Only convert, write notes.pdf next to it
printer-ai formats               # What can be printed here, and what is missing
printer-ai job-status 123        # Check job status
```

## Claude Code Skill

The skill is the `SKILL.md` at the repository root plus the `scripts/` folder. Install it for all projects:

```bash
# 1. Install the CLI tool
uv tool install git+https://github.com/michiphx/Printer-AI-Skills.git

# 2. Install the skill (macOS/Linux)
git clone --depth 1 https://github.com/michiphx/Printer-AI-Skills.git /tmp/printer-ai-skills
mkdir -p ~/.claude/skills/printer-ai
cp /tmp/printer-ai-skills/SKILL.md ~/.claude/skills/printer-ai/
cp -r /tmp/printer-ai-skills/scripts ~/.claude/skills/printer-ai/
```

```powershell
# 2. Install the skill (Windows)
git clone --depth 1 https://github.com/michiphx/Printer-AI-Skills.git $env:TEMP\printer-ai-skills
New-Item -ItemType Directory -Force "$env:USERPROFILE\.claude\skills\printer-ai" | Out-Null
Copy-Item "$env:TEMP\printer-ai-skills\SKILL.md" "$env:USERPROFILE\.claude\skills\printer-ai\"
Copy-Item -Recurse -Force "$env:TEMP\printer-ai-skills\scripts" "$env:USERPROFILE\.claude\skills\printer-ai\"
```

For a single project use `.claude/skills/printer-ai/` inside the project instead. Then verify with `printer-ai printers` and ask Claude Code to "find my network printer and set it up".

## OpenClaw Skill

Use with [OpenClaw](https://openclaw.com).

⚠️ `npx clawhub install printer-ai-skills` installs the **upstream**
[NullYing/Printer-AI-Skills](https://github.com/NullYing/Printer-AI-Skills)
skill, not this fork. To get this fork, install the skill from the git clone as
described under [Claude Code Skill](#claude-code-skill) above (copy `SKILL.md`
and `scripts/` into the skill directory), then install the CLI:

```bash
# 1. Install the CLI tool
uv tool install git+https://github.com/michiphx/Printer-AI-Skills.git

# 2. Verify
printer-ai printers
```

Once installed, AI will automatically read `SKILL.md` and use CLI commands to manage your local printers — list printers, print files, check job status, and more.

## Cursor / other AI tools

1. **Install the CLI tool:**
   ```bash
   uv tool install git+https://github.com/michiphx/Printer-AI-Skills.git
   ```

2. **Add the skill to your project:**
   Copy `SKILL.md` (and `scripts/` on Windows) to your project's `.cursor/skills/` directory, or reference it from the repository.

3. **Verify installation:**
   ```bash
   printer-ai printers
   ```

## Commands

Every command prints a JSON result carrying a `code`. The process exit code is
`0` only when that `code` is `200`; any other `code` exits non-zero, so scripts
and agents can branch on the exit status alone.

| Command | Description |
|---------|-------------|
| `printers [--json]` | List all printers |
| `status [INDEX] [--json]` | Get printer status (spooler view) |
| `attrs [INDEX]` | Get printer capabilities as exposed by the driver |
| `print FILE [--index N] [--options JSON] [--raw] [--keep-pdf]` | Print a file (converted to PDF first; `--raw` sends the bytes untouched, `--keep-pdf` keeps the converted PDF and reports `pdf_path`) |
| `convert FILE [--out PATH_OR_DIR] [--json]` | Convert to PDF only, do not print (default: next to the source file; an `--out` ending in `.pdf` names the file, anything else is a directory) |
| `formats [--json]` | Which file types can be printed here: per-converter extensions, `available`, `via`, `install_hint` for the missing ones, and the effective `page_size`. Always exits `0` |
| `jobs [--printer NAME] [--json]` | List print jobs |
| `job-status JOB_ID` | Get job status |
| `cancel-job JOB_ID` | Cancel a job |
| `discover [--subnet X.Y.Z] [--fast] [--json]` | Scan the LAN for printers, read model/capabilities over IPP |
| `probe HOST` | Open ports and IPP identity of one address |
| `diagnose [--fast] [--json]` | Which installed queues are really reachable |
| `ports` / `drivers [--model M]` | Spooler ports / installed drivers ranked per model |
| `setup HOST [--name N] [--dry-run] [--no-generic]` | Install a printer with the best driver, verify capabilities |
| `driver-search [MODEL] [--host H] [--download DIR] [--open]` | Find (and verify-download) the vendor driver |
| `remove NAME --yes` / `set-default NAME` | Delete a queue / set the default printer |

### Windows helper scripts

| Script | Purpose |
|--------|---------|
| `scripts/win-pair-printer.ps1 -Match REGEX [-Name N] [-SetDefault]` | Add a discovered printer through Windows Settings (UI Automation) — yields the full-featured Microsoft IPP Class Driver queue, verifies it and prints a test page |
| `scripts/win-purge-wsd-port.ps1 -Port WSD-… -Yes` | Remove a stale WSD port that `Remove-PrinterPort` refuses (elevated, with .reg backup) |
| `scripts/win-run-elevated.ps1 -ScriptPath X` | Run a script elevated via UAC and return its output |

`SKILL.md` explains when to use which, and the Windows pitfalls (TLS-only IPP printers, orphaned WSD ports, the Device Association Service) that the scripts work around.

### What `print` reports back

A successful `print` carries `job_id`, `printer_name`, `file_path` plus, when a
conversion happened, `converter`, `converted`, `converted_from` and
`conversion_notes` (and `pdf_path` with `--keep-pdf`). On Windows it also
reports `method` (`"gdi"` or `"raw"`), `pages`, `dpi`, `copies` and
`copies_handled_by` (`"driver"` or `"client"`); on macOS/Linux it reports
`raw`. A file that cannot be converted comes back as code `415` with
`data.hint` naming the tool to install.

## Environment variables

All optional; each one overrides an auto-detected default.

| Variable | Effect |
|---|---|
| `PRINTER_AI_PAGE_SIZE` | `A4` or `Letter` — the page images, text/code and Markdown are laid out on. Default: A4, or Letter for `en_US`-style locales. Also `Legal`, `A3`, `A5` |
| `PRINTER_AI_BROWSER` | Path to (or name of) the Chromium-family browser used for HTML/SVG/Markdown. Several candidates may be given, separated by the platform's path separator |
| `PRINTER_AI_SOFFICE` | Full path to the LibreOffice `soffice` executable, when it is not on `PATH` or in a standard location |
| `PRINTER_AI_RENDER_DPI` | Windows GDI printing: rasterisation DPI, `36`–`1200`. Default: the printer's own DPI capped at 300 |

## Printing from an AI chat

The point of the tool is that a request in plain language becomes one command.
"Print `~/Downloads/invoice.docx` in black and white, double-sided" becomes:

```bash
# macOS / Linux (CUPS/IPP option names)
printer-ai print ~/Downloads/invoice.docx \
  --options '{"print_color_mode":"monochrome","sides":"two-sided-long-edge"}'
```

```powershell
# Windows (DEVMODE option names)
printer-ai print "$env:USERPROFILE\Downloads\invoice.docx" `
  --options '{"dmColor":1,"dmDuplex":2}'
```

The `.docx` is converted to PDF on the way (LibreOffice, or Microsoft Office on
Windows) — the assistant never has to ask the user to export a PDF first. If the
converter is missing, the `415` result's `hint` says what to install, and
`printer-ai formats` shows the whole picture. To keep the PDF as well, add
`--keep-pdf`; to produce only the PDF, use `printer-ai convert`.

## Development

```
uv sync --group dev
uv run pytest
```

## Security notes

- Printer names and hosts are passed to PowerShell as literals — they are never
  interpolated into a command string.
- `--download` never executes anything. It only checks the downloaded file's
  size and header, and on Windows its Authenticode signer.
- `scripts/win-run-elevated.ps1` shows a UAC prompt that the user has to
  approve; nothing runs elevated without that click.

## License

MIT License — see [LICENSE](LICENSE).

## See Also

### [Printer AI MCP](https://github.com/NullYing/printer-ai-mcp)

Need a persistent **MCP server** instead of a CLI tool? Check out [printer-ai-mcp](https://github.com/NullYing/printer-ai-mcp). It runs a FastMCP-based server that AI assistants connect to over HTTP, with Docker deployment support. Best suited for always-on environments or multi-client setups.

## Related Links

- [Printer AI MCP (server version)](https://github.com/NullYing/printer-ai-mcp)
- [CUPS Documentation](https://www.cups.org/)
