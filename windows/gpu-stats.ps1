# Раз в 20 секунд пишет снимок nvidia-smi в data\gpu\gpu.csv.
# Бот в Docker не видит P100 (она в режиме TCC), поэтому температуры всех карт
# для /status и предупреждений о перегреве он берёт из этого файла.
# Обычно запускается из start-fox.ps1; вручную:
#   powershell -ExecutionPolicy Bypass -File windows\gpu-stats.ps1

param([int]$Interval = 20)

$query = 'index,name,temperature.gpu,utilization.gpu,memory.used,memory.total,power.draw,fan.speed'
$dir = Join-Path $PSScriptRoot '..\data\gpu'
New-Item -ItemType Directory -Force -Path $dir | Out-Null
$file = Join-Path $dir 'gpu.csv'
$tmp = "$file.tmp"

while ($true) {
    $out = & nvidia-smi "--query-gpu=$query" --format=csv,noheader,nounits 2>$null
    if ($LASTEXITCODE -eq 0 -and $out) {
        # Через временный файл: бот никогда не прочитает наполовину записанный снимок
        Set-Content -Path $tmp -Value $out -Encoding UTF8
        Move-Item -Path $tmp -Destination $file -Force
    }
    Start-Sleep -Seconds $Interval
}
