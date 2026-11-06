param(
    [Parameter(Mandatory=$true)][string]$RepoRoot,
    [Parameter(Mandatory=$true)][string]$StageParent,
    [Parameter(Mandatory=$true)][string]$PythonPath,
    [switch]$ReplaceExisting
)
$ErrorActionPreference = 'Stop'
if ((Get-TimeZone).Id -ne 'Eastern Standard Time') { throw 'The approved schedule requires the host Eastern time zone.' }
foreach ($value in @($RepoRoot,$StageParent,$PythonPath)) {
    if ($value -match '["\r\n]') { throw 'Invalid task argument path.' }
}
$taskName = 'Advisor Map - Saturday Data Refresh'
$existing = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($existing -and -not $ReplaceExisting) { throw 'Task exists; inspect it before an explicit replacement.' }
New-Item -ItemType Directory -Path $StageParent -Force | Out-Null
$launcherDirectory = Join-Path $env:LOCALAPPDATA 'EIC\AdvisorRefresh\scheduler'
New-Item -ItemType Directory -Path $launcherDirectory -Force | Out-Null
$launcher = Join-Path $launcherDirectory 'run_scheduled_refresh.ps1'
Copy-Item -LiteralPath (Join-Path $RepoRoot 'scripts/run_scheduled_refresh.ps1') -Destination $launcher
$registration = Join-Path $launcherDirectory 'schedule-registration.json'
$activeFrom = (Get-Date).ToString('yyyy-MM-dd')
if (Test-Path -LiteralPath $registration) { $activeFrom = (Get-Content -LiteralPath $registration -Raw | ConvertFrom-Json).activeFrom }
@{schemaVersion=1;timezone='Eastern Standard Time';activeFrom=$activeFrom;day='Saturday';time='03:00';quarterlyMonths=@(1,4,7,10);publish=$false;repoRoot=$RepoRoot} | ConvertTo-Json | Set-Content -LiteralPath $registration -Encoding UTF8
$arguments = '-NoLogo -NoProfile -NonInteractive -WindowStyle Hidden -File "' + $launcher + '" -RepoRoot "' + $RepoRoot + '" -StageParent "' + $StageParent + '" -PythonPath "' + $PythonPath + '" -Registration "' + $registration + '"'
$action = New-ScheduledTaskAction -Execute (Join-Path $PSHOME 'powershell.exe') -Argument $arguments -WorkingDirectory $launcherDirectory
$trigger = New-ScheduledTaskTrigger -Weekly -WeeksInterval 1 -DaysOfWeek Saturday -At '03:00'
$principal = New-ScheduledTaskPrincipal -UserId ([Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable -WakeToRun -RunOnlyIfNetworkAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 48)
$description = 'ACT weekly Saturday 03:00 Eastern. First Saturday Jan/Apr/Jul/Oct runs all sources including ACT. Rebuilds dependencies and tests; prepares candidates only. Browser/source holds remain visible. Requires this Windows user logged on (locked is OK) and valid Azure CLI sign-in.'
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Description $description -Force | Select-Object TaskName,State
