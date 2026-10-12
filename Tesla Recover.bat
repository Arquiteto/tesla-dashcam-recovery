@echo off
rem Double-click to start Tesla Dashcam Recovery.
cd /d "%~dp0"

rem uv may not be on PATH yet right after installing it.
set "UV=uv"
where uv >nul 2>nul || set "UV=%USERPROFILE%\.local\bin\uv.exe"
if not "%UV%"=="uv" if not exist "%UV%" (
  echo.
  echo uv is not installed. Install it first - see "Install" in README.md:
  echo   powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 ^| iex"
  echo.
  pause
  exit /b 1
)

echo Starting Tesla Dashcam Recovery... (the first start downloads Python and takes a minute or two)
"%UV%" run python -m teslarecover %*
if errorlevel 1 pause
