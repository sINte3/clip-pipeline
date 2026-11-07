# Исполнитель заданий. Запуск: .\tools\watch_jobs.ps1  (окно оставить открытым; Ctrl+C — остановить)
#
# Каждые 20 секунд читает файлы jobs\*.job. В файле — по одной команде на строку, строки начинаются
# с «python -m pipeline». Остальные строки отклоняются: исполнитель не выполняет произвольные команды.
# Лог каждой команды пишется в jobs\done\<имя>.log; выполненное задание переносится в jobs\done,
# упавшее — в jobs\failed. Так Claude может запускать конвейер на этом ПК (все 16 потоков),
# записывая задания в папку, а не управляя компьютером напрямую.

param([int]$IntervalSeconds = 20)

$ErrorActionPreference = "Continue"
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
# Вывод Python в UTF-8 читаем как UTF-8, иначе в логах кириллица превращается в кракозябры
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $root
$py = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) {
    Write-Host "Нет .venv — сначала запустите .\tools\setup_windows.ps1"
    exit 1
}
foreach ($d in @("jobs", "jobs\done", "jobs\failed")) {
    New-Item -ItemType Directory -Force -Path $d | Out-Null
}
Write-Host "watch_jobs: жду задания в $root\jobs (проверка каждые $IntervalSeconds с, Ctrl+C — стоп)"

while ($true) {
    $jobs = Get-ChildItem -Path "jobs" -Filter "*.job" -File -ErrorAction SilentlyContinue | Sort-Object LastWriteTime
    foreach ($job in $jobs) {
        $log = Join-Path "jobs\done" ($job.BaseName + ".log")
        $ok = $true
        "=== $($job.Name) начало $(Get-Date -Format s) ===" | Out-File -FilePath $log -Encoding utf8
        $lines = Get-Content -Path $job.FullName -Encoding UTF8 | Where-Object { $_.Trim() -ne "" -and -not $_.Trim().StartsWith("#") }
        foreach ($line in $lines) {
            $cmd = $line.Trim()
            if ($cmd -notmatch '^python -m pipeline(\s|$)') {
                "ОТКЛОНЕНО (разрешены только команды 'python -m pipeline ...'): $cmd" | Out-File -FilePath $log -Append -Encoding utf8
                $ok = $false
                continue
            }
            "> $cmd" | Out-File -FilePath $log -Append -Encoding utf8
            $argsStr = $cmd.Substring("python ".Length)
            $started = Get-Date
            try {
                $output = & cmd.exe /c "`"$py`" $argsStr 2>&1"
                $code = $LASTEXITCODE
            } catch {
                $output = $_.Exception.Message
                $code = 1
            }
            $output | Out-File -FilePath $log -Append -Encoding utf8
            $elapsed = [int]((Get-Date) - $started).TotalSeconds
            "код возврата $code, $elapsed с" | Out-File -FilePath $log -Append -Encoding utf8
            if ($code -ne 0) { $ok = $false }
        }
        "=== конец $(Get-Date -Format s) ===" | Out-File -FilePath $log -Append -Encoding utf8
        $dest = if ($ok) { "jobs\done" } else { "jobs\failed" }
        Move-Item -Path $job.FullName -Destination (Join-Path $dest $job.Name) -Force
        Write-Host "$(Get-Date -Format T) $($job.Name) -> $dest"
    }
    Start-Sleep -Seconds $IntervalSeconds
}
