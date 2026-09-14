$ErrorActionPreference = "Stop"

python -m pip install --upgrade pyinstaller
python -m PyInstaller --clean --noconfirm BatchOCRStudio.spec

Write-Host "Built dist\BatchOCRStudio.exe"
