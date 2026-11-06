# Установка clip-pipeline на Windows. Запуск один раз из PowerShell:
#   Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
#   .\tools\setup_windows.ps1
# Делает: проверяет Python и ffmpeg, создаёт .venv, ставит зависимости, создаёт pipeline.json,
# скачивает модель Whisper (с Hugging Face; если он недоступен — модель sherpa-onnx с GitHub),
# запускает doctor.

$ErrorActionPreference = "Stop"
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $root
Write-Host "clip-pipeline: установка в $root"

# 1. Python
$pyCmd = Get-Command python -ErrorAction SilentlyContinue
if (-not $pyCmd) {
    Write-Host "Python не найден. Установите: winget install Python.Python.3.12 (или python.org, отметить 'Add python.exe to PATH'),"
    Write-Host "затем откройте новое окно PowerShell и запустите скрипт снова."
    exit 1
}
$ver = & python -c "import sys; print('%d.%d' % sys.version_info[:2])"
if ([version]$ver -lt [version]"3.10") {
    Write-Host "Нужен Python 3.10 или новее, найден ${ver}"
    exit 1
}
Write-Host "Python ${ver}: OK"

# 2. ffmpeg
if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) {
    Write-Host "ffmpeg не найден — ставлю через winget (Gyan.FFmpeg)..."
    winget install --id Gyan.FFmpeg -e --accept-source-agreements --accept-package-agreements
    Write-Host "ffmpeg установлен. Откройте НОВОЕ окно PowerShell (чтобы обновился PATH) и запустите скрипт снова."
    exit 1
}
Write-Host "ffmpeg: OK ($((Get-Command ffmpeg).Source))"

# 3. Виртуальное окружение и зависимости
if (-not (Test-Path ".venv")) {
    python -m venv .venv
}
$py = Join-Path $root ".venv\Scripts\python.exe"
& $py -m pip install --upgrade pip --quiet
& $py -m pip install -r requirements.txt --quiet
Write-Host "Зависимости: OK"

# 4. Настройки и каталоги
& $py -m pipeline init

# 5. Модель Whisper: сначала faster-whisper (Hugging Face), при неудаче — sherpa-onnx с GitHub
Write-Host "Скачиваю модель Whisper small (около 500 МБ, один раз)..."
$fw = & $py -c "from faster_whisper import WhisperModel; WhisperModel('small', device='cpu', compute_type='int8'); print('ok')" 2>&1
if ($LASTEXITCODE -eq 0 -and "$fw" -match "ok") {
    Write-Host "faster-whisper: модель small загружена и закэширована"
} else {
    Write-Host "Hugging Face недоступен ($fw). Ставлю запасной бэкенд sherpa-onnx и модель с GitHub..."
    & $py -m pip install sherpa-onnx --quiet
    New-Item -ItemType Directory -Force -Path "models" | Out-Null
    $tarball = "models\sherpa-onnx-whisper-small.tar.bz2"
    if (-not (Test-Path "models\sherpa-onnx-whisper-small")) {
        Invoke-WebRequest -Uri "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-whisper-small.tar.bz2" -OutFile $tarball
        tar -xjf $tarball -C models
        Remove-Item $tarball -Force
    }
    $cfgPath = Join-Path $root "pipeline.json"
    $cfg = Get-Content $cfgPath -Raw -Encoding UTF8 | ConvertFrom-Json
    $cfg.whisper_backend = "sherpa"
    $cfg.sherpa_model_dir = (Join-Path $root "models\sherpa-onnx-whisper-small")
    $cfg | ConvertTo-Json -Depth 5 | Set-Content $cfgPath -Encoding UTF8
    Write-Host "sherpa-onnx: модель small установлена, pipeline.json переключён на backend=sherpa"
}

# 6. Проверка
& $py -m pipeline doctor
Write-Host ""
Write-Host "Готово. Дальше: .\tools\watch_jobs.ps1 — запустить исполнителя заданий (оставить окно открытым),"
Write-Host "или вручную: .\.venv\Scripts\python.exe -m pipeline clip --campaign <id> --source <файл или ссылка>"
