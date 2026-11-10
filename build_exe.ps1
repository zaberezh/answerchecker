<#
    Сборка screen_qa_win.py в один exe, которому не нужен Python.

    Python нужен ТОЛЬКО на этой машине и только на время сборки.
    На компьютере, где exe будет работать, не нужно ничего.

    Запуск:
        powershell -ExecutionPolicy Bypass -File build_exe.ps1
        powershell -ExecutionPolicy Bypass -File build_exe.ps1 -Langs eng,rus,pol
#>
param(
    [string]   $Tesseract = "",            # путь к папке Tesseract-OCR, если автопоиск не справился
    [string[]] $Langs     = @("eng"),      # какие языки OCR вложить
    [switch]   $NoConsole                  # собрать без окна консоли
)

$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $MyInvocation.MyCommand.Path)

function Fail($msg) { Write-Host "[!] $msg" -ForegroundColor Red; exit 1 }

# --- 1. Python для сборки -------------------------------------------------
$py = Get-Command py -ErrorAction SilentlyContinue
if ($py) { $python = "py"; $pyArgs = @("-3") }
else {
    $py = Get-Command python -ErrorAction SilentlyContinue
    if (-not $py) { Fail "Python не найден. Поставьте: winget install -e --id Python.Python.3.12" }
    $python = "python"; $pyArgs = @()
}
Write-Host "[1/5] Python: $((& $python @pyArgs --version) 2>&1)"

# --- 2. Tesseract ---------------------------------------------------------
if (-not $Tesseract) {
    foreach ($p in @("$env:ProgramFiles\Tesseract-OCR",
                     "${env:ProgramFiles(x86)}\Tesseract-OCR",
                     "$env:LOCALAPPDATA\Programs\Tesseract-OCR")) {
        if (Test-Path "$p\tesseract.exe") { $Tesseract = $p; break }
    }
}
if (-not $Tesseract -or -not (Test-Path "$Tesseract\tesseract.exe")) {
    Fail @"
Tesseract не найден. Поставьте и повторите:
      winget install -e --id UB-Mannheim.TesseractOCR
    либо укажите папку: -Tesseract "C:\Program Files\Tesseract-OCR"
"@
}
Write-Host "[2/5] Tesseract: $Tesseract"

# --- 3. Собрать вложение: exe, его DLL и выбранные языки ------------------
$stage = Join-Path $PWD "_stage\tesseract"
if (Test-Path "$PWD\_stage") { Remove-Item "$PWD\_stage" -Recurse -Force }
New-Item -ItemType Directory -Path "$stage\tessdata" -Force | Out-Null

Copy-Item "$Tesseract\tesseract.exe" $stage
Get-ChildItem "$Tesseract\*.dll" | Copy-Item -Destination $stage
foreach ($l in $Langs) {
    $src = Join-Path $Tesseract "tessdata\$l.traineddata"
    if (-not (Test-Path $src)) {
        Fail "Нет языкового файла $l.traineddata. Доустановите язык в инсталляторе Tesseract."
    }
    Copy-Item $src "$stage\tessdata"
}
# osd нужен некоторым режимам распознавания, кладём если есть
$osd = Join-Path $Tesseract "tessdata\osd.traineddata"
if (Test-Path $osd) { Copy-Item $osd "$stage\tessdata" }

$mb = [math]::Round((Get-ChildItem "$PWD\_stage" -Recurse | Measure-Object Length -Sum).Sum / 1MB, 1)
Write-Host "[3/5] Вложение готово: $mb МБ, языки: $($Langs -join ', ')"

# --- 4. Окружение сборки --------------------------------------------------
if (-not (Test-Path ".buildenv")) {
    & $python @pyArgs -m venv .buildenv
}
$venvPy = ".\.buildenv\Scripts\python.exe"
& $venvPy -m pip install --quiet --upgrade pip
& $venvPy -m pip install --quiet -r requirements.txt pyinstaller
if ($LASTEXITCODE -ne 0) { Fail "Не удалось установить зависимости" }
Write-Host "[4/5] Зависимости установлены"

# --- 5. Сборка ------------------------------------------------------------
$flags = @(
    "--noconfirm", "--clean", "--onefile",
    "--name", "screen_qa",
    "--add-data", "$stage;tesseract"
)
if ($NoConsole) { $flags += "--noconsole" }

& $venvPy -m PyInstaller @flags screen_qa_win.py
if ($LASTEXITCODE -ne 0) { Fail "Сборка не удалась" }

Remove-Item "$PWD\_stage" -Recurse -Force
$exe = Join-Path $PWD "dist\screen_qa.exe"
$size = [math]::Round((Get-Item $exe).Length / 1MB, 1)
Write-Host ""
Write-Host "[5/5] Готово: $exe  ($size МБ)" -ForegroundColor Green
Write-Host ""
Write-Host "Этот файл можно скопировать на любой компьютер с Windows."
Write-Host "Ни Python, ни Tesseract там не нужны."
Write-Host ""
Write-Host "Не забудьте положить ключ API:"
Write-Host "  mkdir `"$env:APPDATA\screen_qa`""
Write-Host "  notepad `"$env:APPDATA\screen_qa\api_key`""
