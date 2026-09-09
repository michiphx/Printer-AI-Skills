# Printer AI Skills

[中文文档](README_CN.md)

A cross-platform local printer CLI that lets AI assistants drive printing operations on Windows, macOS, and Linux. Works with OpenClaw / Cursor / Claude Code and other AI Skill systems.

This is a fork of [NullYing/Printer-AI-Skills](https://github.com/NullYing/Printer-AI-Skills) that adds network discovery, reachability diagnosis, driver lookup, a capability-verifying `setup` command, and the Windows helper scripts an agent needs to install a network printer with all of its features.

## Features

- 🌍 **Cross-platform**: Windows, macOS, Linux (CUPS)
- 🖨️ **Printer Management**: List printers, query status and capabilities
- 📄 **File Printing**: Print with customizable options (paper, color, duplex, etc.)
- 📊 **Job Management**: Track and cancel print jobs
- 🔎 **Network**: `discover` printers on the LAN, `probe` one address, `diagnose` whether installed queues are really reachable
- 🛠️ **Setup**: `setup HOST` installs with the best available driver and verifies the queue against the device's advertised capabilities; `driver-search` finds the vendor driver
- 🪟 **Windows helpers** (`scripts/`): pair a printer through Settings via UI Automation, purge stale WSD ports, run a job elevated
- 🤖 **AI-ready**: CLI + SKILL.md for seamless AI integration

## Limitations

Please read these before you rely on the tool — they are real and current.

- **Windows printing is raw.** `print` hands the file to the spooler as raw
  data. Only printers that understand PDF or PostScript themselves will print a
  PDF. Other formats are refused with code `415` — convert to PDF first. The
  `--options` (DEVMODE) values are best-effort on this path.
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

### Usage

```bash
printer-ai printers              # List printers
printer-ai status 1              # Check printer status
printer-ai print file.pdf        # Print a file
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
| `print FILE [--index N] [--options JSON]` | Print a file |
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
