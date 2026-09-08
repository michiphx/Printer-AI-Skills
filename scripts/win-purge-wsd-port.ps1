<#
.SYNOPSIS
  Remove a stale WSD-<guid> printer port that Remove-PrinterPort refuses to delete.

.DESCRIPTION
  Ports of deleted WSD/IPP printers linger in the spooler and cannot be removed through
  Remove-PrinterPort ("in use" or a generic error, even after a spooler restart). The port is
  only a registry key under the WSD Port monitor: this script refuses if a queue still uses
  the port, exports the key to a .reg backup, stops the spooler, deletes the key and starts
  the spooler again.

  Must run elevated.

.PARAMETER Port
  Port name, e.g. WSD-951ab23e-118c-4530-bcad-de9f8d6a5006.
.PARAMETER Yes
  Required confirmation switch.
.PARAMETER BackupDir
  Where to write the .reg backup (default: %TEMP%).

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\win-purge-wsd-port.ps1 -Port WSD-951ab23e-118c-4530-bcad-de9f8d6a5006 -Yes
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)] [ValidatePattern('^WSD-[0-9a-fA-F-]+$')] [string] $Port,
    [switch] $Yes,
    [string] $BackupDir = $env:TEMP
)

$ErrorActionPreference = 'Stop'
$result = [ordered]@{ port = $Port; ok = $false }
function Fail($msg) { $result.error = $msg; $result | ConvertTo-Json; exit 1 }

if (-not $Yes) { Fail 'refusing without -Yes' }
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) { Fail 'must run elevated (registry under HKLM and spooler control)' }

$users = @(Get-Printer | Where-Object { $_.PortName -eq $Port } | ForEach-Object Name)
if ($users.Count -gt 0) { Fail ("port is used by: " + ($users -join ', ') + " - remove those queues first") }

$key = "HKLM\SYSTEM\CurrentControlSet\Control\Print\Monitors\WSD Port\Ports\$Port"
if (-not (Test-Path "Registry::$key")) { Fail "registry key not found: $key" }

$backup = Join-Path $BackupDir ("$Port-" + (Get-Date -Format 'yyyyMMdd-HHmmss') + '.reg')
& reg.exe export "$key" "$backup" /y | Out-Null
if (-not (Test-Path $backup)) { Fail 'registry export failed, nothing changed' }
$result.backup = $backup

Stop-Service Spooler -Force
Start-Sleep -Seconds 3
try {
    $out = & reg.exe delete "$key" /f 2>&1
    $result.reg_delete = [string]$out
} finally {
    Start-Service Spooler
    Start-Sleep -Seconds 5
}
$result.spooler = [string](Get-Service Spooler).Status
$result.port_gone = -not (Get-PrinterPort -Name $Port -ErrorAction SilentlyContinue)
$result.ok = $result.port_gone
$result | ConvertTo-Json
if (-not $result.ok) { exit 1 }
