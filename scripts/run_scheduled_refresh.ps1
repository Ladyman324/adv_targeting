param(
    [Parameter(Mandatory=$true)][string]$RepoRoot,
    [Parameter(Mandatory=$true)][string]$StageParent,
    [Parameter(Mandatory=$true)][string]$PythonPath,
    [Parameter(Mandatory=$true)][string]$Registration
)
$ErrorActionPreference = 'Stop'
if ((Get-TimeZone).Id -ne 'Eastern Standard Time') { throw 'Host time zone changed; stop the scheduled refresh.' }
$logDirectory = Join-Path $env:LOCALAPPDATA 'EIC\AdvisorRefresh\schedule-logs'
New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null
$logPath = Join-Path $logDirectory ('refresh-' + (Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ') + '.log')
& $PythonPath -E -s -B -u (Join-Path $RepoRoot 'src/refresh_schedule.py') --root $RepoRoot --stage-parent $StageParent --registration $Registration --execute --allow-network *> $logPath
exit $LASTEXITCODE
