<#
.SYNOPSIS
  Add a discovered network printer through Windows Settings (Printers & scanners) using UI Automation.

.DESCRIPTION
  Drives exactly the flow a person clicks: Settings -> Printers & scanners -> "Add a printer or scanner"
  -> the device entry -> "Add device". Windows then creates a Microsoft IPP Class Driver queue on a
  WSD-<guid> port with colour/duplex/media negotiated from the device. Scripted pairing through
  Windows.Devices.Enumeration returns "Failed" for printers; this UI path succeeds.

  Afterwards the script verifies the negotiated capabilities, optionally renames the queue and sets
  it as default, prints a Windows test page and prints a JSON summary to stdout.

  Needs an interactive desktop (Settings must be able to open). Renaming needs an elevated shell.
  Button names are matched in English and German.

.PARAMETER Match
  Regex matched against the device names Settings shows (e.g. "4850", "OfficeJet Pro").
.PARAMETER Name
  Optional new queue name. Requires elevation; reported (not fatal) if it cannot be applied.
.PARAMETER SetDefault
  Make the new queue the default printer.
.PARAMETER NoTestPage
  Skip the Windows test page.
.PARAMETER TimeoutSeconds
  How long to wait for discovery and for the queue to appear (default 120).
.PARAMETER KeepSettingsOpen
  Leave the Settings window open afterwards.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\win-pair-printer.ps1 -Match "4850" -Name "EPSON ET-4850 (Office)" -SetDefault
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)] [string] $Match,
    [string] $Name,
    [switch] $SetDefault,
    [switch] $NoTestPage,
    [int] $TimeoutSeconds = 120,
    [switch] $KeepSettingsOpen
)

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes

$topButton = '^(Add a printer or scanner|Drucker oder Scanner hinzufügen)$'
$addButton = '^(Add device|Gerät hinzufügen)$'
$closeButton = '^(Close Settings|Einstellungen schließen)$'
$windowName = '^(Settings|Einstellungen)$'
$result = [ordered]@{ ok = $false; matched = $Match; steps = @() }
function Step($s) { $result.steps += $s; Write-Verbose $s }
function Fail($msg) { $result.error = $msg; $result | ConvertTo-Json -Depth 4; exit 1 }

function Find-Buttons($root, $regex) {
    $cond = New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::ControlTypeProperty, [System.Windows.Automation.ControlType]::Button)
    @($root.FindAll([System.Windows.Automation.TreeScope]::Descendants, $cond) | Where-Object { $_.Current.Name -match $regex })
}
function Find-Texts($root) {
    @($root.FindAll([System.Windows.Automation.TreeScope]::Descendants, [System.Windows.Automation.Condition]::TrueCondition) | ForEach-Object { $_.Current.Name } | Where-Object { $_ -match '\S' } | Select-Object -Unique)
}
function Get-SettingsWindow {
    foreach ($w in [System.Windows.Automation.AutomationElement]::RootElement.FindAll([System.Windows.Automation.TreeScope]::Children, [System.Windows.Automation.Condition]::TrueCondition)) {
        if ($w.Current.ClassName -eq 'ApplicationFrameWindow' -and $w.Current.Name -match $windowName) { return $w }
    }
    return $null
}
function Invoke-Button($btn) { $btn.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern).Invoke() }

$before = @(Get-Printer | ForEach-Object Name)

Start-Process 'ms-settings:printers'
$win = $null; $t = 0
while ($t -lt 30 -and -not $win) { Start-Sleep -Seconds 2; $t += 2; $win = Get-SettingsWindow }
if (-not $win) { Fail 'Settings window did not open (no interactive desktop?)' }
try { $win.SetFocus() } catch {}
Step 'settings opened'

$btn = $null; $t = 0
while ($t -lt 20 -and -not $btn) { $btn = Find-Buttons $win $topButton | Select-Object -First 1; if (-not $btn) { Start-Sleep -Seconds 2; $t += 2 } }
if (-not $btn) { Fail ("'Add a printer or scanner' button not found; buttons: " + ((Find-Buttons $win '.' | ForEach-Object { $_.Current.Name }) -join ' | ')) }
Invoke-Button $btn
Step 'clicked: add a printer or scanner'

$item = $null; $t = 0
while ($t -lt $TimeoutSeconds -and -not $item) {
    Start-Sleep -Seconds 4; $t += 4
    $item = @($win.FindAll([System.Windows.Automation.TreeScope]::Descendants, [System.Windows.Automation.Condition]::TrueCondition) | Where-Object { $_.Current.Name -match $Match -and $_.Current.ControlType -eq [System.Windows.Automation.ControlType]::ListItem }) | Select-Object -First 1
}
if (-not $item) { Fail ("no discovered device matching '$Match' after ${t}s; visible: " + ((Find-Texts $win) -join ' | ')) }
$result.device = $item.Current.Name
Step "found device: $($item.Current.Name)"

$walker = [System.Windows.Automation.TreeWalker]::ControlViewWalker
$add = $null; $node = $item
for ($k = 0; $k -lt 5 -and -not $add; $k++) {
    $add = Find-Buttons $node $addButton | Select-Object -First 1
    if (-not $add) { $node = $walker.GetParent($node); if (-not $node) { break } }
}
if (-not $add) { Fail "device '$($item.Current.Name)' has no 'Add device' button (already installed?)" }
Invoke-Button $add
Step 'clicked: add device'

$queue = $null; $t = 0
while ($t -lt $TimeoutSeconds -and -not $queue) {
    Start-Sleep -Seconds 4; $t += 4
    $queue = Get-Printer | Where-Object { $before -notcontains $_.Name -and $_.Name -notmatch '\(Fax\)$' } | Select-Object -First 1
}
if (-not $queue) { Fail ("no new queue after ${t}s; Settings shows: " + ((Find-Texts $win) -join ' | ')) }
Step "queue created: $($queue.Name) on $($queue.PortName)"

if (-not $KeepSettingsOpen) {
    $w = Get-SettingsWindow
    if ($w) { $c = Find-Buttons $w $closeButton | Select-Object -First 1; if ($c) { Invoke-Button $c } }
}

$finalName = $queue.Name
if ($Name -and $Name -ne $queue.Name) {
    try { Rename-Printer -Name $queue.Name -NewName $Name; $finalName = $Name; Step "renamed to $Name" }
    catch { $result.rename_error = "could not rename (needs an elevated shell?): $($_.Exception.Message)" }
}
if ($SetDefault) {
    try {
        $w32 = Get-CimInstance Win32_Printer -Filter ("Name='" + $finalName.Replace("'", "''") + "'")
        $r = Invoke-CimMethod -InputObject $w32 -MethodName SetDefaultPrinter
        if ($r.ReturnValue -eq 0) { Step 'set as default' } else { $result.default_error = "SetDefaultPrinter returned $($r.ReturnValue)" }
    } catch { $result.default_error = $_.Exception.Message }
}

$q = Get-Printer -Name $finalName
$cfg = Get-PrintConfiguration -PrinterName $finalName
$w32 = Get-CimInstance Win32_Printer -Filter ("Name='" + $finalName.Replace("'", "''") + "'")
$result.queue = [ordered]@{
    name = $q.Name; driver = $q.DriverName; port = $q.PortName; status = [string]$q.PrinterStatus
    duplex_mode = [string]$cfg.DuplexingMode; paper = [string]$cfg.PaperSize; color = [bool]$cfg.Color
    capabilities = @($w32.CapabilityDescriptions)
}
$ds = "HKLM:\SYSTEM\CurrentControlSet\Control\Print\Printers\$finalName\DsDriver"
if (Test-Path $ds) {
    $d = Get-ItemProperty $ds
    $result.queue.duplex_supported = [bool]([int]($d.printDuplexSupported | Select-Object -First 1))
    $result.queue.color_supported = [bool]([int]($d.printColor | Select-Object -First 1))
    $result.queue.media_supported = @($d.printMediaSupported)
}

if (-not $NoTestPage) {
    $r = Invoke-CimMethod -InputObject $w32 -MethodName PrintTestPage
    Start-Sleep -Seconds 25
    $jobs = @(Get-PrintJob -PrinterName $finalName | ForEach-Object { "$($_.Id):$($_.JobStatus)" })
    $result.test_page = [ordered]@{ submitted = ($r.ReturnValue -eq 0); jobs_remaining = $jobs; queue_status = [string](Get-Printer -Name $finalName).PrinterStatus }
    if ($r.ReturnValue -eq 0 -and $jobs.Count -eq 0 -and $result.test_page.queue_status -eq 'Normal') { Step 'test page printed' } else { Step 'test page did not clear the queue - check the printer' }
}

$result.ok = $true
$result | ConvertTo-Json -Depth 4
