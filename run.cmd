@echo off
rem One script to check, set up and run the whole trading platform from Windows (repo root).
rem The Windows twin of run.sh, with the same commands: run.cmd help lists them.
rem The work is done by tools\run.ps1 (PowerShell 7 if installed, else Windows PowerShell 5.1).
setlocal
set "PS_EXE=powershell"
where pwsh >nul 2>nul && set "PS_EXE=pwsh"
"%PS_EXE%" -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\run.ps1" %*
exit /b %ERRORLEVEL%
