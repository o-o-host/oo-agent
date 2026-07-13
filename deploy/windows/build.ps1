# Build the Windows binaries (run from the repo root, inside a venv
# that has the package installed with the windows extras):
#
#   .venv\Scripts\pip install -e ".[gpu,docker,transport,windows]" pyinstaller
#   powershell -ExecutionPolicy Bypass -File deploy\windows\build.ps1
#
# Produces:
#   dist\oo-agent.exe          CLI / interactive runs (--dump, --enroll)
#   dist\oo-agent-service.exe  Windows service host (install/start/stop/remove)

$ErrorActionPreference = "Stop"

$common = @(
    "--onefile",
    "--collect-submodules", "oo_agent",
    "--collect-all", "HardwareMonitor",
    "--collect-all", "clr_loader",
    "--collect-all", "pythonnet",
    "--hidden-import", "win32timezone"
)

& .venv\Scripts\pyinstaller @common --name oo-agent oo_agent\__main__.py
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

& .venv\Scripts\pyinstaller @common --name oo-agent-service `
    --hidden-import servicemanager `
    oo_agent\service\windows_service.py
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "build ok:"
Get-ChildItem dist\*.exe | Format-Table Name, @{n="MB";e={[math]::Round($_.Length/1MB,1)}}
