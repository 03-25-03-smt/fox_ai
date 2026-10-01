# Регистрирует задачи планировщика (запускать в PowerShell ОТ АДМИНИСТРАТОРА):
#   «Fox AI»       — start-fox.ps1 при входе: Ollama, gpu-stats, Docker, контейнеры
#   «Fox AI Agent» — fox-agent.ps1 с наивысшими правами: /logs, /restart, лимит мощности P100
#
#   powershell -ExecutionPolicy Bypass -File windows\install-autostart.ps1
#   powershell -ExecutionPolicy Bypass -File windows\install-autostart.ps1 -Lock -PowerLimit 200
#
# -Lock        заблокировать экран после запуска (для автологина)
# -PowerLimit  постоянный лимит мощности P100 в ваттах (0 = заводской, 250 Вт)
# Удалить: Unregister-ScheduledTask -TaskName 'Fox AI','Fox AI Agent' -Confirm:$false

param([switch]$Lock, [int]$PowerLimit = 0)

$ErrorActionPreference = 'Stop'
$admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
         ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $admin) { throw 'Запусти PowerShell от имени администратора (агенту нужны права на nvidia-smi -pl).' }

$user = "$env:USERDOMAIN\$env:USERNAME"
# Без лимита времени: gpu-stats и агент работают бесконечно
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Hours 0) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)

# 1. Запуск всего при входе
$start = Join-Path $PSScriptRoot 'start-fox.ps1'
$argLine = "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$start`""
if ($Lock) { $argLine += ' -Lock' }
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
$trigger.Delay = 'PT30S'   # дать Windows прогрузиться
Register-ScheduledTask -TaskName 'Fox AI' -Force -Settings $settings -Trigger $trigger `
    -Action (New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $argLine) `
    -Description 'Ollama + Docker + Fox AI bot' | Out-Null
Write-Host "Задача 'Fox AI': запуск при входе $user"

# 2. Агент — с наивысшими правами (лимит мощности), от имени того же пользователя (его Docker)
$agent = Join-Path $PSScriptRoot 'fox-agent.ps1'
$agentArgs = "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$agent`" -PowerLimit $PowerLimit"
$agentTrigger = New-ScheduledTaskTrigger -AtLogOn -User $user
$agentTrigger.Delay = 'PT1M'
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Highest
Register-ScheduledTask -TaskName 'Fox AI Agent' -Force -Settings $settings -Trigger $agentTrigger `
    -Principal $principal -Action (New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $agentArgs) `
    -Description 'Fox AI: /logs, /restart, лимит мощности P100' | Out-Null
Write-Host "Задача 'Fox AI Agent': с правами администратора, лимит P100: $(if ($PowerLimit) { "$PowerLimit Вт" } else { 'заводской' })"
Write-Host 'Запустить сейчас, без перезагрузки: Start-ScheduledTask -TaskName ''Fox AI Agent'''
