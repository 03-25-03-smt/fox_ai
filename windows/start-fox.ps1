# Запуск Fox AI после входа в Windows: Ollama, Docker Desktop, контейнеры, gpu-stats.
# Сам по себе запускается задачей планировщика (install-autostart.ps1); вручную:
#   powershell -ExecutionPolicy Bypass -File windows\start-fox.ps1

param([switch]$Lock)   # заблокировать экран, когда всё запущено (для автологина)

$root = Resolve-Path (Join-Path $PSScriptRoot '..')
$logDir = Join-Path $root 'data'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
Start-Transcript -Path (Join-Path $logDir 'start-fox.log') -Append | Out-Null

# 1. Ollama (у приложения есть свой автозапуск, но подстрахуемся)
if (-not (Get-Process -Name 'ollama' -ErrorAction SilentlyContinue)) {
    $app = Join-Path $env:LOCALAPPDATA 'Programs\Ollama\ollama app.exe'
    if (Test-Path $app) { Start-Process $app; Write-Host 'Ollama запущен' }
    else { Write-Warning 'Ollama не установлен' }
}

# 2. Снимки температуры GPU — отдельным скрытым процессом, если ещё не запущены
$stats = Join-Path $PSScriptRoot 'gpu-stats.ps1'
$running = Get-CimInstance Win32_Process -Filter "Name = 'powershell.exe'" |
    Where-Object { $_.CommandLine -like '*gpu-stats.ps1*' }
if (-not $running) {
    Start-Process powershell -WindowStyle Hidden -ArgumentList @(
        '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', "`"$stats`"")
    Write-Host 'gpu-stats запущен'
}

# 3. Docker Desktop: запустить и дождаться движка (до 5 минут, на HDD бывает долго)
$desktop = Join-Path $env:ProgramFiles 'Docker\Docker\Docker Desktop.exe'
if (-not (Get-Process -Name 'Docker Desktop' -ErrorAction SilentlyContinue) -and (Test-Path $desktop)) {
    Start-Process $desktop
}
$deadline = (Get-Date).AddMinutes(5)
do {
    Start-Sleep -Seconds 5
    docker info *> $null
} until ($LASTEXITCODE -eq 0 -or (Get-Date) -gt $deadline)

if ($LASTEXITCODE -ne 0) {
    Write-Warning 'Docker не запустился за 5 минут'
} else {
    # 4. Контейнеры (у них restart: unless-stopped, но после `down` нужен up)
    Push-Location $root
    docker compose up -d
    Pop-Location
}

Stop-Transcript | Out-Null
if ($Lock) { rundll32.exe user32.dll, LockWorkStation }
