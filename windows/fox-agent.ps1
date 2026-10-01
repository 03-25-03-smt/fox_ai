# Агент Fox AI на Windows: выполняет команды бота из белого списка.
#
# Бот (в Docker) кладёт запрос в data\host\requests\<id>.json, агент выполняет его и пишет ответ
# в data\host\responses\<id>.json. Сетевого порта нет, произвольные команды не выполняются:
#   logs <сервис> <строк>   docker compose logs
#   restart <сервис>        docker compose restart
#   ps                      docker compose ps
#   power <Вт>|default      лимит мощности P100 (nvidia-smi -pl, нужны права администратора)
#   ollama-restart          перезапуск Ollama
# Каждые 15 с агент обновляет data\host\status.json (жив, текущий лимит мощности).
#
# Запускается задачей «Fox AI Agent» с наивысшими правами (install-autostart.ps1); вручную —
# в PowerShell от администратора:
#   powershell -ExecutionPolicy Bypass -File windows\fox-agent.ps1 -PowerLimit 200

param(
    [string]$GpuName = 'P100',   # карта, для которой меняется лимит мощности
    [int]$PowerLimit = 0         # постоянный лимит в ваттах при старте (0 = не менять)
)

$ErrorActionPreference = 'Continue'
# Логи контейнеров в UTF-8: без этого PowerShell 5.1 читает их в кодировке консоли и ломает кириллицу
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$root = Resolve-Path (Join-Path $PSScriptRoot '..')
$hostDir = Join-Path $root 'data\host'
$reqDir = Join-Path $hostDir 'requests'
$respDir = Join-Path $hostDir 'responses'
New-Item -ItemType Directory -Force -Path $reqDir, $respDir | Out-Null
$maxAge = 120   # старые запросы (бот уже не ждёт) не выполняем

function Get-Gpu {
    $line = & nvidia-smi --query-gpu=index,name,power.limit,power.default_limit --format=csv,noheader,nounits 2>$null |
        Where-Object { $_ -like "*$GpuName*" } | Select-Object -First 1
    if (-not $line) { return $null }
    $p = $line -split ',\s*'
    [pscustomobject]@{ Index = $p[0]; Name = $p[1]; Limit = [double]$p[2]; Default = [double]$p[3] }
}

function Get-Services {
    Push-Location $root
    try { @(docker compose config --services 2>$null) } finally { Pop-Location }
}

function Invoke-Compose([string[]]$ComposeArgs) {
    Push-Location $root
    try {
        $out = & docker compose @ComposeArgs 2>&1 | Out-String
        @{ ok = ($LASTEXITCODE -eq 0); output = $out }
    } finally { Pop-Location }
}

function Set-Power([string]$Value) {
    $gpu = Get-Gpu
    if (-not $gpu) { return @{ ok = $false; output = "карта $GpuName не найдена" } }
    $watts = if ($Value -eq 'default') { [int]$gpu.Default } else { [int]$Value }
    if ($watts -lt 100 -or $watts -gt 300) { return @{ ok = $false; output = 'лимит вне 100–300 Вт' } }
    $out = & nvidia-smi -i $gpu.Index -pl $watts 2>&1 | Out-String
    if ($LASTEXITCODE -ne 0) {
        return @{ ok = $false; output = "nvidia-smi: $($out.Trim()) (агент запущен без прав администратора?)" }
    }
    @{ ok = $true; output = "лимит $($gpu.Name): $watts Вт" }
}

function Restart-Ollama {
    Get-Process -Name 'ollama app', 'ollama' -ErrorAction SilentlyContinue | Stop-Process -Force
    Start-Sleep -Seconds 2
    $app = Join-Path $env:LOCALAPPDATA 'Programs\Ollama\ollama app.exe'
    if (-not (Test-Path $app)) { return @{ ok = $false; output = "не найден $app" } }
    Start-Process $app
    @{ ok = $true; output = 'Ollama перезапущен' }
}

function Invoke-Request($req) {
    $a = @($req.args)
    switch ($req.cmd) {
        'ps' { return Invoke-Compose @('ps', '--format', 'table {{.Service}}\t{{.Status}}') }
        'logs' {
            if ($a[0] -notin (Get-Services)) { return @{ ok = $false; output = "нет сервиса $($a[0])" } }
            $lines = [int]$a[1]; if ($lines -lt 1 -or $lines -gt 2000) { $lines = 80 }
            return Invoke-Compose @('logs', '--no-color', '--tail', "$lines", $a[0])
        }
        'restart' {
            if ($a[0] -notin (Get-Services)) { return @{ ok = $false; output = "нет сервиса $($a[0])" } }
            return Invoke-Compose @('restart', $a[0])
        }
        'power' { return Set-Power ([string]$a[0]) }
        'ollama-restart' { return Restart-Ollama }
        default { return @{ ok = $false; output = "неизвестная команда $($req.cmd)" } }
    }
}

function Write-Json($Path, $Data) {
    # Через временный файл: бот не прочитает половину. UTF-8 без BOM
    $tmp = "$Path.tmp"
    [IO.File]::WriteAllText($tmp, ($Data | ConvertTo-Json -Compress), [Text.UTF8Encoding]::new($false))
    Move-Item -Force -Path $tmp -Destination $Path
}

if ($PowerLimit -gt 0) { $r = Set-Power "$PowerLimit"; Write-Host $r.output }

$lastStatus = [datetime]::MinValue
Write-Host "Fox AI agent: жду команды в $reqDir"
while ($true) {
    if (((Get-Date) - $lastStatus).TotalSeconds -ge 15) {
        $gpu = Get-Gpu
        $power = if ($gpu) { "$($gpu.Name): лимит $([int]$gpu.Limit) из $([int]$gpu.Default) Вт" } else { 'нет данных' }
        Write-Json (Join-Path $hostDir 'status.json') @{ time = (Get-Date).ToString('s'); power = $power }
        $lastStatus = Get-Date
    }
    foreach ($file in Get-ChildItem -Path $reqDir -Filter '*.json' -ErrorAction SilentlyContinue) {
        try {
            $req = Get-Content -Raw -Encoding UTF8 $file.FullName | ConvertFrom-Json
            Remove-Item -Force $file.FullName
            if ($req.id -notmatch '^[0-9a-f]{32}$') { continue }
            $age = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds() - [double]$req.created
            $result = if ($age -gt $maxAge) { @{ ok = $false; output = 'запрос устарел' } } else { Invoke-Request $req }
            $text = [string]$result.output
            if ($text.Length -gt 200000) { $text = $text.Substring($text.Length - 200000) }
            Write-Json (Join-Path $respDir "$($req.id).json") @{ ok = [bool]$result.ok; output = $text }
            Write-Host "$(Get-Date -Format 'HH:mm:ss') $($req.cmd) $($req.args -join ' ') -> $($result.ok)"
        } catch {
            Write-Warning "запрос $($file.Name): $_"
            Remove-Item -Force $file.FullName -ErrorAction SilentlyContinue
        }
    }
    Start-Sleep -Milliseconds 700
}
