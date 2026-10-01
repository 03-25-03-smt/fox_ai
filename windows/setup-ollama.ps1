# Настройка Ollama на Windows: модели на отдельном диске, только Tesla P100.
#
# Запуск (PowerShell, из папки проекта):
#   powershell -ExecutionPolicy Bypass -File windows\setup-ollama.ps1
#   powershell -ExecutionPolicy Bypass -File windows\setup-ollama.ps1 -ModelsDir 'L:\ollama'
#
# Ключ -ListenAll нужен, только если бот из Docker не достучался до Ollama
# (см. README, «Если что-то не работает»). Требует запуска от администратора.

param(
    [string]$ModelsDir = 'L:\ollama',
    [string]$GpuName = 'P100',     # часть имени карты для LLM из `nvidia-smi -L`
    [int]$Parallel = 2,            # = MAX_CONCURRENT в .env
    [int]$MaxLoaded = 3,
    [string]$KeepAlive = '30m',    # сколько держать модель в VRAM после запроса
    [switch]$ListenAll
)

$ErrorActionPreference = 'Stop'

function Set-UserEnv([string]$Name, [string]$Value) {
    [Environment]::SetEnvironmentVariable($Name, $Value, 'User')
    Set-Item -Path "Env:$Name" -Value $Value
    Write-Host "  $Name = $Value"
}

# --- Карта для LLM -------------------------------------------------------
$gpus = & nvidia-smi --query-gpu=index,name,uuid --format=csv,noheader
if ($LASTEXITCODE -ne 0) { throw 'nvidia-smi не работает: сначала поставь драйвер (README, шаг 3).' }
Write-Host 'Видеокарты:'
$gpus | ForEach-Object { Write-Host "  $_" }

$llm = $gpus | Where-Object { $_ -like "*$GpuName*" } | Select-Object -First 1
if (-not $llm) { throw "Карта '$GpuName' не найдена. Укажи другую: -GpuName '3070'" }
$uuid = ($llm -split ',')[2].Trim()

# --- Папка моделей -------------------------------------------------------
New-Item -ItemType Directory -Force -Path $ModelsDir | Out-Null

Write-Host 'Переменные Ollama (для текущего пользователя):'
Set-UserEnv 'OLLAMA_MODELS' $ModelsDir
# UUID, а не номер: нумерация CUDA на Windows может не совпадать с nvidia-smi
Set-UserEnv 'CUDA_VISIBLE_DEVICES' $uuid
Set-UserEnv 'OLLAMA_NUM_PARALLEL' "$Parallel"
Set-UserEnv 'OLLAMA_MAX_LOADED_MODELS' "$MaxLoaded"
Set-UserEnv 'OLLAMA_KEEP_ALIVE' $KeepAlive

if ($ListenAll) {
    $admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
             ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    if (-not $admin) { throw '-ListenAll требует PowerShell от имени администратора.' }
    Set-UserEnv 'OLLAMA_HOST' '0.0.0.0:11434'
    # У Ollama нет авторизации: пускаем только Docker/WSL (172.16.0.0/12), локальную сеть
    # блокируем явно (правило Block в брандмауэре Windows сильнее любых Allow)
    Get-NetFirewallRule -DisplayName 'Ollama*' -ErrorAction SilentlyContinue | Remove-NetFirewallRule
    New-NetFirewallRule -DisplayName 'Ollama: Docker/WSL' -Direction Inbound -Protocol TCP `
        -LocalPort 11434 -RemoteAddress '172.16.0.0/12' -Action Allow | Out-Null
    New-NetFirewallRule -DisplayName 'Ollama: block LAN' -Direction Inbound -Protocol TCP `
        -LocalPort 11434 -RemoteAddress '10.0.0.0/8', '192.168.0.0/16' -Action Block | Out-Null
    Write-Host '  Брандмауэр: 11434 открыт только для Docker/WSL.'
} else {
    [Environment]::SetEnvironmentVariable('OLLAMA_HOST', $null, 'User')
}

# --- Перезапуск Ollama, чтобы подхватил переменные ------------------------
Get-Process -Name 'ollama app', 'ollama' -ErrorAction SilentlyContinue | Stop-Process -Force
Start-Sleep -Seconds 2
$app = Join-Path $env:LOCALAPPDATA 'Programs\Ollama\ollama app.exe'
if (Test-Path $app) {
    Start-Process $app
    Start-Sleep -Seconds 5
    Write-Host "Ollama перезапущен. LLM-карта: $($llm.Trim())"
    Write-Host 'Проверка: ollama run qwen2.5:7b "Привет"; затем ollama ps -> PROCESSOR = 100% GPU'
} else {
    Write-Warning "Не нашёл $app — запусти Ollama вручную из меню Пуск."
}
