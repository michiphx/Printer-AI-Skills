<#
.SYNOPSIS
  Run a PowerShell script elevated (UAC prompt) and return its output to the caller.

.DESCRIPTION
  An agent shell cannot answer UAC itself, but it can launch the job with -Verb RunAs, let the
  user click the prompt, and read the job's output back from a log file. This is that wrapper.
  The elevated job runs with -NoProfile -ExecutionPolicy Bypass; everything it writes (all
  streams) lands in a temp log that is printed here and then deleted.

  Arguments are never spliced into the elevated command as text. They travel as a JSON array
  inside the encoded command and are rebuilt there into a splatting hashtable, so a value
  containing $(...), a backtick or a quote is bound as a literal string and can never be
  executed - the elevated shell is running as administrator, so this is the difference between
  passing a printer name and handing over the machine.

  Sandboxed agent shells usually cannot show the UAC prompt at all - run this from a normal shell.
  Tell the user before calling it that a UAC prompt will appear.

.PARAMETER ScriptPath
  The .ps1 to run elevated.
.PARAMETER ArgumentList
  Arguments for the script, one element per token: a parameter name ('-Port'), then its value
  ('WSD-...'), switches as their own element ('-Yes'). Do not pre-quote anything - quoting is
  this script's job. An element that is not shaped like a parameter name is passed positionally.
  (A value that itself begins with a dash cannot be distinguished from a switch; pass such a
  value positionally, or extend the target script to accept it another way.)
.PARAMETER TimeoutSeconds
  Give up waiting after this long (default 600).

.EXAMPLE
  powershell -File scripts\win-run-elevated.ps1 -ScriptPath scripts\win-purge-wsd-port.ps1 -ArgumentList '-Port','WSD-951ab23e-118c-4530-bcad-de9f8d6a5006','-Yes'

.NOTES
  Exit codes: 0/1 from the elevated script, 2 = timeout (UAC never answered), 3 = UAC cancelled.
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

# Single-quoted PowerShell literals: no $, backtick or $(...) expansion, and the only
# escape needed is a doubled quote. Every value below is embedded this way.
function ConvertTo-PsLiteral([string] $Value) { "'" + $Value.Replace("'", "''") + "'" }

$argsJson = ConvertTo-Json -InputObject @($ArgumentList) -Compress

$template = @'
$ErrorActionPreference = 'Stop'
$parsed = ConvertFrom-Json @@ARGSJSON@@
[string[]] $raw = @(); if ($null -ne $parsed) { [string[]] $raw = $parsed }
$named = @{}; $pos = @()
for ($i = 0; $i -lt $raw.Count; $i++) {
    $e = [string]$raw[$i]
    if ($e -match '^-([A-Za-z][A-Za-z0-9]*)$') {
        $n = $Matches[1]
        if (($i + 1) -lt $raw.Count -and ([string]$raw[$i + 1]) -notmatch '^-[A-Za-z][A-Za-z0-9]*$') {
            $named[$n] = [string]$raw[$i + 1]; $i++
        } else { $named[$n] = $true }
    } else { $pos += $e }
}
try { & @@SCRIPT@@ @named @pos *>&1 | Out-File -Encoding utf8 -FilePath @@LOG@@; exit 0 }
catch { $_ | Out-File -Encoding utf8 -Append -FilePath @@LOG@@; exit 1 }
'@

$inner = $template.
    Replace('@@ARGSJSON@@', (ConvertTo-PsLiteral $argsJson)).
    Replace('@@SCRIPT@@', (ConvertTo-PsLiteral $ScriptPath)).
    Replace('@@LOG@@', (ConvertTo-PsLiteral $log))
$encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($inner))

try {
    $p = Start-Process powershell -Verb RunAs -PassThru -ArgumentList @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-EncodedCommand', $encoded)
} catch {
    # "The operation was canceled by the user" - UAC dismissed. Not an error worth a stack trace.
    Write-Warning 'no output from the elevated job (UAC cancelled?)'
    if (Test-Path $log) { Remove-Item $log -Force -ErrorAction SilentlyContinue }
    exit 3
}
if (-not $p) {
    Write-Warning 'no output from the elevated job (UAC cancelled?)'
    if (Test-Path $log) { Remove-Item $log -Force -ErrorAction SilentlyContinue }
    exit 3
}
if (-not $p.WaitForExit($TimeoutSeconds * 1000)) {
    Write-Error "elevated job still running after ${TimeoutSeconds}s (UAC not answered?)"
    exit 2
}
if (Test-Path $log) { Get-Content $log; Remove-Item $log -Force } else { Write-Warning 'no output from the elevated job (UAC cancelled?)' }
exit $p.ExitCode
