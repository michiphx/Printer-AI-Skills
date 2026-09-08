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

Use with [OpenClaw](https://openclaw.com):

```bash
# 1. Install the Skill
npx clawhub@latest install printer-ai-skills

# 2. Install the CLI tool
uv tool install git+https://github.com/michiphx/Printer-AI-Skills.git

# 3. Verify
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

## License

MIT License

## See Also

### [Printer AI MCP](https://github.com/NullYing/printer-ai-mcp)

Need a persistent **MCP server** instead of a CLI tool? Check out [printer-ai-mcp](https://github.com/NullYing/printer-ai-mcp). It runs a FastMCP-based server that AI assistants connect to over HTTP, with Docker deployment support. Best suited for always-on environments or multi-client setups.

## Related Links

- [Printer AI MCP (server version)](https://github.com/NullYing/printer-ai-mcp)
- [CUPS Documentation](https://www.cups.org/)
