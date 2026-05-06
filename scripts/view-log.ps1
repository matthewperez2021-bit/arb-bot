# view-log.ps1 — Read data/sports_scheduler.log with the right encoding
# and an optional regex filter. Replaces the multi-flag PowerShell
# invocation that frequently gets the encoding wrong.
#
# After commits ae8e276 (NO_COLOR) + this helper:
#   - No ANSI color codes left in the log
#   - Box-drawing characters (─╭╰) render correctly via -Encoding UTF8
#
# Usage:
#   .\scripts\view-log.ps1                            # last 100 lines, default filter
#   .\scripts\view-log.ps1 -Tail 300                  # last 300 lines
#   .\scripts\view-log.ps1 -Tail 200 -Match "v2|v4"   # custom regex
#   .\scripts\view-log.ps1 -All                       # full file
#   .\scripts\view-log.ps1 -All -Match ""             # full file, no filter

[CmdletBinding()]
param(
    [int]    $Tail  = 100,
    [string] $Match = "STRATEGY|filters dropped|Trades placed|Run #",
    [switch] $All
)

$ScriptDir  = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectDir = Split-Path -Parent $ScriptDir
$LogPath    = Join-Path $ProjectDir "data\sports_scheduler.log"

if (-not (Test-Path $LogPath)) {
    Write-Error "Log not found: $LogPath. Has the scheduler run yet?"
    exit 1
}

$content = if ($All) {
    Get-Content -Path $LogPath -Encoding UTF8
} else {
    Get-Content -Path $LogPath -Encoding UTF8 -Tail $Tail
}

if ([string]::IsNullOrEmpty($Match)) {
    $content
} else {
    $content | Select-String -Pattern $Match
}
