<#
.SYNOPSIS
    Register MinerWatch as a Windows scheduled task that starts at boot.

.DESCRIPTION
    Creates a task that launches scripts\run.ps1 shortly after startup, logs to
    logs\minerwatch.log, and restarts itself if it ever exits unexpectedly.

    Defaults to the SYSTEM account, which needs no stored password and survives
    the operator logging out. Pass -User to run as somebody else; you will be
    prompted for that account's password, because a task that runs while nobody
    is logged on has to store one.

    Run this from an elevated PowerShell — registering a task for SYSTEM
    requires administrator rights.

.PARAMETER Mode
    rehearse       Poll and log only. Forces a rehearsal even where miners.yaml
                   sets sleep.dry_run: false, so nothing reaches a miner. (default)
    as-configured  Let each miner's own sleep.dry_run decide. Some miners may be
                   live; the installer lists which.
    live-sleep     Real sleep/wake; watchdog restarts still rehearsed.
    live           Real everything, restarts included.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\install-task.ps1

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\install-task.ps1 -Mode live-sleep

.EXAMPLE
    # Remove it again.
    powershell -ExecutionPolicy Bypass -File scripts\install-task.ps1 -Uninstall
#>
[CmdletBinding()]
param(
    [ValidateSet("rehearse", "as-configured", "live-sleep", "live")]
    [string]$Mode = "rehearse",
    [string]$TaskName = "MinerWatch",
    [string]$Config = "miners.yaml",
    [string]$User = "SYSTEM",
    # Minutes to wait after boot before starting. The network stack and any
    # switch the miners sit behind are frequently not ready at second zero, and
    # a fleet that all reads as unreachable on the first poll is noise.
    [int]$StartupDelayMinutes = 2,
    # Skip the confirmation prompt shown when a mode will actuate hardware.
    [switch]$Force,
    [switch]$Uninstall
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

function Assert-Elevated {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($id)
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw ("This script must run from an elevated PowerShell. " +
               "Right-click PowerShell -> Run as administrator, then re-run it.")
    }
}

Assert-Elevated

# ---------------------------------------------------------------------------
# Uninstall
# ---------------------------------------------------------------------------
if ($Uninstall) {
    $existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if (-not $existing) {
        Write-Host "No scheduled task named '$TaskName' found." -ForegroundColor DarkGray
        return
    }
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Removed scheduled task '$TaskName'." -ForegroundColor Green
    return
}

# ---------------------------------------------------------------------------
# Preflight: fail here, not silently at 3am after a reboot
# ---------------------------------------------------------------------------
$runScript = Join-Path $root "scripts\run.ps1"
$venvPython = Join-Path $root ".venv\Scripts\python.exe"
$configPath = Join-Path $root $Config

foreach ($required in @($runScript, $venvPython, $configPath)) {
    if (-not (Test-Path $required)) {
        throw "Missing $required. Run scripts\setup.ps1 first, and check -Config."
    }
}

# Parse the config now. A task whose config has a typo just dies at boot with
# exit code 2 and no console to say why.
Write-Host "Validating $Config ..." -ForegroundColor Cyan
& $venvPython -m minerwatch -c $configPath status *> $null
if ($LASTEXITCODE -ne 0) {
    throw ("$Config was rejected. Fix it first - run this to see the error:`n" +
           "    .\.venv\Scripts\python.exe -m minerwatch -c $Config status")
}

$logDir  = Join-Path $root "logs"
$logFile = Join-Path $logDir "minerwatch.log"
New-Item -ItemType Directory -Path $logDir -Force | Out-Null

# ---------------------------------------------------------------------------
# Say plainly which miners this mode will actuate, before registering anything
# ---------------------------------------------------------------------------
# A mode name alone is not enough: "as-configured" actuates whichever miners set
# sleep.dry_run: false, which is invisible unless somebody goes and reads the
# YAML. Name them here, at the moment the decision is being made.
# The probe is a file, not an inline `python -c` string. PowerShell re-splits a
# string on its way to a native executable and eats the double quotes inside, so
# the inline version reached the interpreter as `print(ENABLED: + ,.join(...))`
# and died with a SyntaxError - after which this block read nothing and reported
# "will NOT send anything to any miner" for a config that was live.
$probeScript = Join-Path $PSScriptRoot "probe_sleep.py"
if (-not (Test-Path $probeScript)) {
    throw "Missing $probeScript. Re-extract the release; the installer cannot verify what it would actuate without it."
}
$probeOut = & $venvPython $probeScript $configPath 2>&1
if ($LASTEXITCODE -ne 0) {
    throw ("Could not determine which miners $Config would actuate, so nothing was registered.`n" +
           "    " + ($probeOut -join "`n    "))
}

$enabledLine = ($probeOut | Select-String '^ENABLED:').Line
$liveLine    = ($probeOut | Select-String '^LIVE:').Line
# Refuse to guess. An absent line is not an empty fleet, and reporting it as one
# is exactly the failure this whole section is meant to prevent.
if ($null -eq $enabledLine -or $null -eq $liveLine) {
    throw ("The sleep probe returned unusable output, so nothing was registered.`n" +
           "    " + ($probeOut -join "`n    "))
}
$enabled = (($enabledLine -replace '^ENABLED:', '') -split ',') | Where-Object { $_ }
$liveByConfig = (($liveLine -replace '^LIVE:', '') -split ',') | Where-Object { $_ }

$willActuate = switch ($Mode) {
    "rehearse"      { @() }
    "as-configured" { $liveByConfig }
    "live-sleep"    { $enabled }
    "live"          { $enabled }
}

Write-Host ""
if ($willActuate.Count -eq 0) {
    Write-Host "This task will NOT send anything to any miner." -ForegroundColor Green
    if ($Mode -eq "rehearse" -and $liveByConfig.Count -gt 0) {
        Write-Host ("  (note: $Config marks " + ($liveByConfig -join ", ") +
                    " as live, but -Mode rehearse overrides that)") -ForegroundColor DarkGray
    }
} else {
    Write-Host "This task WILL send real commands to:" -ForegroundColor Yellow
    Write-Host ("  sleep/wake : " + ($willActuate -join ", ")) -ForegroundColor Yellow
    if ($Mode -eq "live") {
        Write-Host "  restarts   : every miner, when one fails inside its window" -ForegroundColor Yellow
    }
    if (-not $Force -and [Environment]::UserInteractive) {
        $answer = Read-Host "Type 'yes' to register this task"
        if ($answer -ne "yes") {
            Write-Host "Cancelled. Nothing was registered." -ForegroundColor DarkGray
            return
        }
    }
}

# ---------------------------------------------------------------------------
# Build the task
# ---------------------------------------------------------------------------
# "rehearse" forces --dry-run rather than passing nothing. Passing nothing
# means "honour each miner's configured dry_run", which for a config containing
# dry_run: false is the opposite of a rehearsal - and a mode named rehearse must
# never actuate hardware.
$modeFlag = switch ($Mode) {
    "rehearse"      { " -DryRun" }
    "as-configured" { "" }
    "live-sleep"    { " -LiveSleep" }
    "live"          { " -Live" }
}

$arguments = ('-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden ' +
              '-File "{0}" -Config "{1}" -LogFile "{2}"{3}' -f
              $runScript, $Config, $logFile, $modeFlag)

$action = New-ScheduledTaskAction -Execute "powershell.exe" `
                                  -Argument $arguments `
                                  -WorkingDirectory $root

$trigger = New-ScheduledTaskTrigger -AtStartup
$trigger.Delay = "PT{0}M" -f $StartupDelayMinutes

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartInterval (New-TimeSpan -Minutes 5) `
    -RestartCount 999 `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Seconds 0)   # 0 = never kill it

if ($User -eq "SYSTEM") {
    $principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" `
                                            -LogonType ServiceAccount `
                                            -RunLevel Highest
    $register = @{ TaskName = $TaskName; Action = $action; Trigger = $trigger;
                   Settings = $settings; Principal = $principal }
} else {
    # A task that runs while nobody is logged on must store a password.
    $cred = Get-Credential -UserName $User -Message "Password for $User (the task runs as this account)"
    $register = @{ TaskName = $TaskName; Action = $action; Trigger = $trigger;
                   Settings = $settings; User = $cred.UserName;
                   Password = $cred.GetNetworkCredential().Password; RunLevel = "Highest" }
}

if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Write-Host "Replacing existing task '$TaskName' ..." -ForegroundColor Yellow
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

Register-ScheduledTask @register `
    -Description "MinerWatch: Antminer schedule, software sleep, and watchdog." | Out-Null

Write-Host ""
Write-Host "Registered '$TaskName'." -ForegroundColor Green
Write-Host "  Mode        : $Mode"
if ($willActuate.Count -eq 0 -and $Mode -eq "as-configured") {
    # Distinguish "this mode cannot actuate" from "nothing is live *today*".
    # -Mode as-configured re-reads miners.yaml every time the task starts, so a
    # later edit setting dry_run: false goes live without anyone re-running this
    # installer. Reporting that as a flat "rehearsal only" would be a promise
    # the mode does not make.
    Write-Host "  Sends       : nothing yet - no miner in $Config sets dry_run: false" -ForegroundColor Green
    Write-Host "                (this mode follows the config, so an edit can make it live)" -ForegroundColor DarkGray
} elseif ($willActuate.Count -eq 0) {
    Write-Host "  Sends       : nothing - rehearsal only" -ForegroundColor Green
} else {
    Write-Host ("  Sends       : sleep/wake to " + ($willActuate -join ", ")) -ForegroundColor Yellow
}
Write-Host "  Account     : $User"
Write-Host "  Starts      : at boot, after a $StartupDelayMinutes minute delay"
Write-Host "  On failure  : restarts every 5 minutes"
Write-Host "  Log         : $logFile"
Write-Host ""
Write-Host "Start it now without rebooting:" -ForegroundColor Cyan
Write-Host "  Start-ScheduledTask -TaskName '$TaskName'"
Write-Host "Then check it is alive:" -ForegroundColor Cyan
Write-Host "  Get-ScheduledTask -TaskName '$TaskName' | Get-ScheduledTaskInfo"
Write-Host "  Get-Content '$logFile' -Tail 20 -Wait"
