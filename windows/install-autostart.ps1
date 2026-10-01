# Регистрирует задачу планировщика «Fox AI»: start-fox.ps1 при входе в Windows.
#   powershell -ExecutionPolicy Bypass -File windows\install-autostart.ps1          # просто автозапуск
#   powershell -ExecutionPolicy Bypass -File windows\install-autostart.ps1 -Lock    # + блокировка экрана
# Удалить: Unregister-ScheduledTask -TaskName 'Fox AI' -Confirm:$false

param([switch]$Lock)

$ErrorActionPreference = 'Stop'
$script = Join-Path $PSScriptRoot 'start-fox.ps1'
$argLine = "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$script`""
if ($Lock) { $argLine += ' -Lock' }

$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $argLine
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$trigger.Delay = 'PT30S'   # дать Windows прогрузиться
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Hours 0)   # gpu-stats работает бесконечно — без лимита

Register-ScheduledTask -TaskName 'Fox AI' -Action $action -Trigger $trigger -Settings $settings `
    -Description 'Ollama + Docker + Fox AI bot' -Force | Out-Null
Write-Host "Задача 'Fox AI' создана: запуск при входе пользователя $env:USERNAME"
