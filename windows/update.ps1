# Обновление Fox AI одной командой (PowerShell ОТ АДМИНИСТРАТОРА, из папки проекта):
#   powershell -ExecutionPolicy Bypass -File windows\update.ps1
#
# 1. git pull
# 2. дописывает в .env новые настройки из .env.example (твои значения не трогает),
#    сам генерирует GRAFANA_PASSWORD и SEARXNG_SECRET, если они ещё replace-me
# 3. docker compose up -d --build и чистка старых образов
# 4. задачи автозапуска «Fox AI» и «Fox AI Agent» (install-autostart.ps1) и запуск агента
#
# -NoPull       не делать git pull
# -PowerLimit   постоянный лимит мощности P100, Вт (по умолчанию 200; 0 — заводской)
# -NoLock       не блокировать экран после автозапуска

param([switch]$NoPull, [int]$PowerLimit = 200, [switch]$NoLock)

$ErrorActionPreference = 'Stop'
$root = Resolve-Path (Join-Path $PSScriptRoot '..')
Set-Location $root

function New-Secret([int]$Bytes = 32) { -join ((1..$Bytes) | ForEach-Object { '{0:x2}' -f (Get-Random -Max 256) }) }

# 1. Код
if (-not $NoPull) {
    Write-Host '== git pull'
    git pull --ff-only
    if ($LASTEXITCODE -ne 0) { throw 'git pull не удался (есть свои изменения в файлах?)' }
}

# 2. .env
Write-Host '== .env'
$envPath = Join-Path $root '.env'
if (-not (Test-Path $envPath)) {
    Copy-Item (Join-Path $root '.env.example') $envPath
    Write-Warning '.env создан из .env.example — впиши BOT_TOKEN и ADMIN_IDS (notepad .env) и запусти снова.'
    exit 1
}
$lines = [Collections.Generic.List[string]](Get-Content -Encoding UTF8 $envPath)
$have = @{}
foreach ($l in $lines) { if ($l -match '^\s*([A-Z0-9_]+)\s*=') { $have[$Matches[1]] = $true } }
$added = @()
$header = '# --- добавлено update.ps1 ' + (Get-Date -Format 'yyyy-MM-dd') + ': новые настройки по умолчанию (описание в .env.example) ---'
# Ключевые настройки не подставляем примером: их задаёшь сам
$skip = @('BOT_TOKEN', 'ADMIN_IDS')
foreach ($l in Get-Content -Encoding UTF8 (Join-Path $root '.env.example')) {
    if ($l -match '^\s*([A-Z0-9_]+)\s*=' -and -not $have.ContainsKey($Matches[1]) -and $Matches[1] -notin $skip) {
        if ($added.Count -eq 0) { $lines.Add(''); $lines.Add($header) }
        $lines.Add($l); $have[$Matches[1]] = $true; $added += $Matches[1]
    }
}
for ($i = 0; $i -lt $lines.Count; $i++) {
    if ($lines[$i] -match '^(GRAFANA_PASSWORD|SEARXNG_SECRET)=replace-me\s*$') {
        $secret = if ($Matches[1] -eq 'GRAFANA_PASSWORD') { New-Secret 8 } else { New-Secret 32 }
        $lines[$i] = "$($Matches[1])=$secret"
        Write-Host "  $($Matches[1]) сгенерирован$(if ($Matches[1] -eq 'GRAFANA_PASSWORD') { ": $secret (логин admin)" })"
    }
}
# UTF-8 без BOM: docker compose и бот читают .env как обычный текст
[IO.File]::WriteAllLines($envPath, $lines, [Text.UTF8Encoding]::new($false))
if ($added) { Write-Host "  добавлены: $($added -join ', ')" } else { Write-Host '  новых настроек нет' }
$text = Get-Content -Raw $envPath
if ($text -notmatch '(?m)^BOT_TOKEN=\d+:' -or $text -match '(?m)^BOT_TOKEN=123456:replace-me') {
    throw 'В .env не задан BOT_TOKEN (notepad .env).'
}
if ($text -notmatch '(?m)^ADMIN_IDS=\d') { throw 'В .env не задан ADMIN_IDS — твой Telegram ID (notepad .env).' }
New-Item -ItemType Directory -Force -Path (Join-Path $root 'data\gpu'), (Join-Path $root 'data\host') | Out-Null

# 3. Контейнеры
Write-Host '== docker compose up -d --build (первый раз на HDD — до 30 минут)'
docker compose up -d --build
if ($LASTEXITCODE -ne 0) { throw 'docker compose up не удался — смотри вывод выше' }
docker image prune -f | Out-Null
docker compose ps

# 4. Автозапуск и агент
$admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
         ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if ($admin) {
    Write-Host '== автозапуск'
    $installArgs = @{ PowerLimit = $PowerLimit }
    if (-not $NoLock) { $installArgs.Lock = $true }
    & (Join-Path $PSScriptRoot 'install-autostart.ps1') @installArgs
    Stop-ScheduledTask -TaskName 'Fox AI Agent' -ErrorAction SilentlyContinue
    Start-ScheduledTask -TaskName 'Fox AI Agent'
    Write-Host '  агент запущен'
} else {
    Write-Warning 'Не от администратора: автозапуск и агент не обновлены. Запусти скрипт ещё раз от администратора.'
}

Write-Host ''
Write-Host 'Готово. Проверь в Telegram: /status, /ps, /power. Grafana: http://localhost:3000, Open WebUI: http://localhost:3001'
