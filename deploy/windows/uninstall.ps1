# oo-agent uninstaller for Windows.
# Stops and removes the service, then deletes the install directory,
# config, token and state. Run from an elevated PowerShell:
#   powershell -NoProfile -ExecutionPolicy Bypass -File uninstall.ps1 [-Yes]

param(
    [switch]$Yes
)

$ErrorActionPreference = "SilentlyContinue"

$principal = New-Object Security.Principal.WindowsPrincipal(
    [Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Error "Run this script from an elevated (Administrator) PowerShell."
    exit 2
}

if (-not $Yes) {
    $answer = Read-Host ("This removes the oo-agent service, config and " +
        "token from this machine. Continue? [y/N]")
    if ($answer -notin @("y", "Y", "yes")) {
        Write-Host "aborted"
        exit 1
    }
}

$service = "oo-agent"
$installDir = Join-Path $env:ProgramFiles "oo-agent"
$stateDir = Join-Path $env:ProgramData "oo-agent"

sc.exe stop $service | Out-Null
Start-Sleep -Seconds 2
sc.exe delete $service | Out-Null
Write-Host "service removed"

if (Test-Path $stateDir) {
    Remove-Item -Recurse -Force $stateDir
    Write-Host "removed $stateDir"
}
if (Test-Path $installDir) {
    Remove-Item -Recurse -Force $installDir
    Write-Host "removed $installDir"
}
Write-Host "done."
