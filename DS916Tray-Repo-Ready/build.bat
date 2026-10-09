@echo off
setlocal
cd /d "%~dp0"

echo ==========================================
echo  DS916Tray - build Windows executable
echo ==========================================

where py >nul 2>nul
if errorlevel 1 (
  echo ERROR: Python launcher "py" not found. Install Python 3.10+ and enable Add Python to PATH.
  pause
  exit /b 1
)

py -m pip install --upgrade pip
if errorlevel 1 goto :error
py -m pip install -r requirements.txt
if errorlevel 1 goto :error

if not exist assets\ds916tray.ico (
  echo ERROR: assets\ds916tray.ico is missing.
  pause
  exit /b 1
)

py -m PyInstaller --noconfirm --clean --onefile --windowed --name DS916Tray --icon assets\ds916tray.ico --add-data "assets;assets" --collect-all winsdk ds916_tray.py
if errorlevel 1 goto :error

set "APPDATA_DS916=%APPDATA%\DS916Tray"
if not exist "%APPDATA_DS916%\Themes" mkdir "%APPDATA_DS916%\Themes"
if not exist "%APPDATA_DS916%\Themes\tbrggreen_3_Screens.ds916theme" copy /Y "Themes\tbrggreen_3_Screens.ds916theme" "%APPDATA_DS916%\Themes\tbrggreen_3_Screens.ds916theme" >nul
if exist theme_builder.html copy /Y theme_builder.html "%APPDATA_DS916%\theme_builder.html" >nul

powershell -NoProfile -ExecutionPolicy Bypass -Command "$target = Join-Path (Get-Location) 'dist\DS916Tray.exe'; $desktop = [Environment]::GetFolderPath('Desktop'); $shortcut = (New-Object -ComObject WScript.Shell).CreateShortcut((Join-Path $desktop 'DS916Tray.lnk')); $shortcut.TargetPath = $target; $shortcut.WorkingDirectory = (Split-Path $target); $shortcut.IconLocation = $target + ',0'; $shortcut.Save()"

echo.
echo Build completed: dist\DS916Tray.exe
echo Desktop shortcut created. Test on the target PC before publishing.
echo The included tbrggreen theme is copied only if it does not already exist.
pause
exit /b 0

:error
echo.
echo BUILD FAILED. Read the error above, then check Python and package installation.
pause
exit /b 1
