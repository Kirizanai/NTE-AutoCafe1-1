@echo off
rem Builds the portable executable into dist\NTE-AutoCafe1-1.exe
cd /d "%~dp0"
python -m PyInstaller --noconfirm --clean --onefile --windowed ^
  --name NTE-AutoCafe1-1 --icon icono.ico --uac-admin ^
  --add-data "plantillas;plantillas" --add-data "icono.ico;." ^
  --exclude-module tkinter --exclude-module _tkinter ^
  app.py
