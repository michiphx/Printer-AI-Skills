<#
.SYNOPSIS
  Run a PowerShell script elevated (UAC prompt) and return its output to the caller.

.DESCRIPTION
  An agent shell cannot answer UAC itself, but it can launch the job with -Verb RunAs, let the
  user click the prompt, and read the job's output back from a log file. This is that wrapper.
  The elevated job runs with -NoProfile -ExecutionPolicy Bypass; everything it writes (all
  streams) lands in a temp log that is printed here and then deleted.

  Sandboxed agent shells usually cannot show the UAC prompt at all - run this from a normal shell.
  Tell the user before calling it that a UAC prompt will appear.

.PARAMETER ScriptPath
  The .ps1 to run elevated.
.PARAMETER ArgumentList
  Arguments appended to the script call, already quoted as needed.
.PARAMETER TimeoutSeconds
  Give up waiting after this long (default 600).

.EXAMPLE
  powershell -File scripts\win-run-elevated.ps1 -ScriptPath scripts\win-purge-wsd-port.ps1 -ArgumentList '-Port','WSD-951ab23e-118c-4530-bcad-de9f8d6a5006','-Yes'
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)] [string] $ScriptPath,
    [string[]] $ArgumentList = @(),
    [int] $TimeoutSeconds = 600
)

$ErrorActionPreference = 'Stop'
$ScriptPath = (Resolve-Path $ScriptPath).Path
$log = Join-Path $env:TEMP ('elevated-' + [guid]::NewGuid().ToString('N') + '.log')
$quotedScript = "'" + $ScriptPath.Replace("'", "''") + "'"
$quotedLog = "'" + $log.Replace("'", "''") + "'"
$args_ = ($ArgumentList | ForEach-Object { if ($_ -match '^-' -or $_ -match "^'.*'$") { $_ } else { "'" + $_.Replace("'", "''") + "'" } }) -join ' '
$inner = "try { & $quotedScript $args_ *>&1 | Out-File -Encoding utf8 -FilePath $quotedLog; exit 0 } catch { `$_ | Out-File -Encoding utf8 -Append -FilePath $quotedLog; exit 1 }"
$encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($inner))

$p = Start-Process powershell -Verb RunAs -PassThru -ArgumentList @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-EncodedCommand', $encoded)
if (-not $p.WaitForExit($TimeoutSeconds * 1000)) {
    Write-Error "elevated job still running after ${TimeoutSeconds}s (UAC not answered?)"
    exit 2
}
if (Test-Path $log) { Get-Content $log; Remove-Item $log -Force } else { Write-Warning 'no output from the elevated job (UAC cancelled?)' }
exit $p.ExitCode
