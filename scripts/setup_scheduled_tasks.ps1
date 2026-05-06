# setup_scheduled_tasks.ps1 — Register the arb-bot's nightly seeders
# as Windows Scheduled Tasks. Run ONCE on the bot-hosting machine.
#
# Tasks created:
#   ArbBot-Seed-OddsAPI   — daily 02:00, accumulates Odds API snapshots
#   ArbBot-Seed-Kalshi    — daily 02:30, captures Kalshi candles for
#                            recently-settled trades before they age off
#
# Idempotent: re-running with -Force overwrites existing task definitions.
# Tasks run as the current user under their normal session.
#
# Usage:
#   .\scripts\setup_scheduled_tasks.ps1            # register both
#   .\scripts\setup_scheduled_tasks.ps1 -WhatIf    # preview without registering
#   .\scripts\setup_scheduled_tasks.ps1 -Remove    # unregister both

[CmdletBinding(SupportsShouldProcess=$true)]
param(
    [switch]$Remove
)

# Resolve paths anchored to the project root (parent of scripts/)
$ScriptDir  = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectDir = Split-Path -Parent $ScriptDir
$LogDir     = Join-Path $ProjectDir "data\scheduled_tasks"

# Locate python.exe — prefer the one currently on PATH for consistency
$Python = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $Python) {
    Write-Error "python.exe not found on PATH. Activate your environment or fix PATH first."
    exit 1
}

$Tasks = @(
    @{
        Name        = "ArbBot-Seed-OddsAPI"
        Description = "Nightly Odds API historical snapshot seeder for arb-bot."
        Time        = "02:00"
        Args        = "scripts\seed_odds_api_historical.py --max-snapshots 12"
        LogFile     = "ArbBot-Seed-OddsAPI.log"
    },
    @{
        Name        = "ArbBot-Seed-Kalshi"
        Description = "Nightly Kalshi candlestick seeder for arb-bot's settled trades."
        Time        = "02:30"
        Args        = "scripts\seed_kalshi_historical.py --from-trades"
        LogFile     = "ArbBot-Seed-Kalshi.log"
    }
)

# ── Removal mode ────────────────────────────────────────────────────────
if ($Remove) {
    foreach ($t in $Tasks) {
        if (Get-ScheduledTask -TaskName $t.Name -ErrorAction SilentlyContinue) {
            if ($PSCmdlet.ShouldProcess($t.Name, "Unregister")) {
                Unregister-ScheduledTask -TaskName $t.Name -Confirm:$false
                Write-Host "Removed: $($t.Name)"
            }
        } else {
            Write-Host "Not present: $($t.Name)"
        }
    }
    exit 0
}

# ── Registration mode ──────────────────────────────────────────────────
if (-not (Test-Path $LogDir)) {
    New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
}

Write-Host ""
Write-Host "Registering arb-bot scheduled tasks..."
Write-Host "  Project dir: $ProjectDir"
Write-Host "  Python:      $Python"
Write-Host "  Logs:        $LogDir"
Write-Host ""

foreach ($t in $Tasks) {
    $logPath = Join-Path $LogDir $t.LogFile
    # Wrap in cmd /c so we can redirect stdout+stderr to the per-task log.
    # Quote the python path in case Program Files is in there.
    $cmd     = "cmd.exe"
    $cmdArgs = "/c `"`"$Python`" $($t.Args) >> `"$logPath`" 2>&1`""

    $action    = New-ScheduledTaskAction `
                    -Execute $cmd `
                    -Argument $cmdArgs `
                    -WorkingDirectory $ProjectDir
    $trigger   = New-ScheduledTaskTrigger -Daily -At $t.Time
    $settings  = New-ScheduledTaskSettingsSet `
                    -AllowStartIfOnBatteries `
                    -DontStopIfGoingOnBatteries `
                    -StartWhenAvailable `
                    -ExecutionTimeLimit (New-TimeSpan -Hours 1)
    $principal = New-ScheduledTaskPrincipal `
                    -UserId $env:USERNAME `
                    -LogonType Interactive `
                    -RunLevel Limited

    if ($PSCmdlet.ShouldProcess($t.Name, "Register-ScheduledTask")) {
        Register-ScheduledTask `
            -TaskName    $t.Name `
            -Description $t.Description `
            -Action      $action `
            -Trigger     $trigger `
            -Settings    $settings `
            -Principal   $principal `
            -Force | Out-Null

        $next = (Get-ScheduledTaskInfo -TaskName $t.Name).NextRunTime
        Write-Host ("  + {0,-22}  daily {1}  next: {2}" -f $t.Name, $t.Time, $next)
    }
}

Write-Host ""
Write-Host "Done. Verify with:"
Write-Host '  Get-ScheduledTask -TaskName "ArbBot-*" | Select-Object TaskName, State'
Write-Host ""
Write-Host "To remove later:"
Write-Host "  .\scripts\setup_scheduled_tasks.ps1 -Remove"
