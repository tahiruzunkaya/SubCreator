# SubCreator paketini uretir:
#   dist\SubCreator\SubCreator.exe            (calistirilabilir klasor)
#   dist\SubCreator-<surum>-windows-x64.zip   (GitHub Releases'e yuklenecek dosya)
# Ceviri modeli pakete konmaz; uygulama ilk acilista Hugging Face'ten indirir (subcreator\models.py).
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$py = ".\.venv\Scripts\python.exe"
if (-not (Test-Path "vendor\llama\llama-server.exe")) {
    throw "vendor\llama\llama-server.exe yok. README'deki 'Gelistirme' bolumune bak."
}
& $py -m pip install -q -r requirements.txt -r requirements-dev.txt
& $py scripts\make_icon.py
$version = (& $py -c "import subcreator; print(subcreator.__version__)").Trim()

& $py -m PyInstaller --noconfirm --clean --windowed `
    --name SubCreator `
    --icon assets\icon.ico `
    --collect-all rapidocr_onnxruntime `
    --exclude-module PySide6.QtWebEngineCore `
    --exclude-module PySide6.QtWebEngineWidgets `
    --exclude-module PySide6.QtQml `
    --exclude-module PySide6.QtQuick `
    --exclude-module PySide6.QtPdf `
    --exclude-module tkinter `
    launcher.pyw

$dist = "dist\SubCreator"
# llama-server ve calismasi icin gereken DLL'ler (diger llama.cpp araclarinin *-impl.dll'leri gerekmiyor)
New-Item -ItemType Directory -Force "$dist\llama" | Out-Null
Copy-Item "vendor\llama\llama-server.exe", "vendor\llama\LICENSE*" "$dist\llama\"
Get-ChildItem "vendor\llama\*.dll" |
    Where-Object { $_.Name -notlike "*-impl.dll" -or $_.Name -eq "llama-server-impl.dll" } |
    Copy-Item -Destination "$dist\llama\"
# Visual C++ calisma zamani: llama.cpp DLL'leri ister. Kurulu olmayan bilgisayarlarda da calissin diye
# PyInstaller'in pakete koydugu kopyalar llama\ klasorune de konur.
foreach ($dll in "msvcp140.dll", "vcruntime140.dll", "vcruntime140_1.dll") {
    Copy-Item "$dist\_internal\$dll" "$dist\llama\"
}
Copy-Item "LICENSE", "THIRD_PARTY_NOTICES.txt" "$dist\"

$zip = "dist\SubCreator-$version-windows-x64.zip"
Remove-Item $zip -ErrorAction SilentlyContinue
# Python'un zipfile'i (Compress-Archive'in aksine) standart "/" yol ayiricisi yazar.
& $py -c "import shutil, sys; shutil.make_archive(sys.argv[1], 'zip', 'dist', 'SubCreator')" ($zip -replace '\.zip$', '')

$size = [math]::Round((Get-Item $zip).Length / 1MB)
Write-Host "Hazir: $PSScriptRoot\$dist\SubCreator.exe"
Write-Host "Zip:   $PSScriptRoot\$zip ($size MB)"
