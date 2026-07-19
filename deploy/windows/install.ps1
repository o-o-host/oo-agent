# oo-agent installer for Windows hosts (PowerShell 5.1+, run elevated).
#
# Installs the agent into "C:\Program Files\oo-agent" (own venv), puts the
# config in C:\ProgramData\oo-agent, registers and starts the Windows
# service. Safe to re-run: existing config and token are never overwritten.
#
# One-liner (elevated PowerShell):
#   powershell -NoProfile -ExecutionPolicy Bypass -Command ^
#     "iwr -useb https://monitor.o-o.host/dl/install.ps1 -OutFile $env:TEMP\oo-install.ps1; ^
#      & $env:TEMP\oo-install.ps1 -Server https://monitor.o-o.host/api -Enroll CODE"

param(
    [string]$Server = "https://monitor.o-o.host/api",
    [string]$Enroll = "",
    [string]$Source = "https://monitor.o-o.host/dl/oo-agent.tar.gz",
    [switch]$NoStart,
    [switch]$Force
)

$ErrorActionPreference = "Stop"

function Fail($msg) { Write-Host "error: $msg" -ForegroundColor Red; exit 2 }

# ── elevation ────────────────────────────────────────────────────────────
$identity = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
if (-not $identity.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Fail "run from an elevated (Administrator) PowerShell"
}

$Prefix  = Join-Path $env:ProgramFiles "oo-agent"
$ConfDir = Join-Path $env:ProgramData "oo-agent"

# ── python >= 3.10 ───────────────────────────────────────────────────────
function Find-Python {
    foreach ($cmd in @("py -3", "python")) {
        try {
            $v = & $cmd.Split()[0] $cmd.Split()[1..99] -c "import sys;print('%d.%d' % sys.version_info[:2])" 2>$null
            if ($v -and [version]$v -ge [version]"3.10") { return $cmd }
        } catch { }
    }
    return $null
}

$py = Find-Python
if (-not $py) {
    Write-Host "python >= 3.10 not found - installing via winget ..."
    try {
        winget install -e --id Python.Python.3.12 --scope machine `
            --accept-source-agreements --accept-package-agreements --silent
    } catch { Fail "winget install failed - install Python 3.12 from python.org and re-run" }
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
                [Environment]::GetEnvironmentVariable("Path", "User")
    $py = Find-Python
    if (-not $py) { Fail "python still not found after install - open a new console and re-run" }
}
Write-Host "python: $py"

# ── fetch + unpack source ────────────────────────────────────────────────
$tmp = Join-Path $env:TEMP "oo-agent-src"
if (Test-Path $tmp) { Remove-Item -Recurse -Force $tmp }
New-Item -ItemType Directory -Path $tmp | Out-Null
$tarball = Join-Path $tmp "oo-agent.tar.gz"
Write-Host "downloading $Source ..."
Invoke-WebRequest -UseBasicParsing -Uri $Source -OutFile $tarball
tar -xzf $tarball -C $tmp
$src = Get-ChildItem $tmp -Directory | Where-Object { Test-Path (Join-Path $_.FullName "pyproject.toml") } | Select-Object -First 1
if (-not $src) { $src = Get-Item $tmp }
if (-not (Test-Path (Join-Path $src.FullName "pyproject.toml"))) { Fail "downloaded archive does not look like an oo-agent source tree" }

# ── venv + install ───────────────────────────────────────────────────────
Write-Host "installing into $Prefix ..."
New-Item -ItemType Directory -Force -Path $Prefix, $ConfDir | Out-Null
$venvPy = Join-Path $Prefix "venv\Scripts\python.exe"
if (-not (Test-Path $venvPy)) {
    & $py.Split()[0] $py.Split()[1..99] -m venv (Join-Path $Prefix "venv")
}
& $venvPy -m pip install --quiet --upgrade pip
& $venvPy -m pip install --quiet "$($src.FullName)[gpu,docker,transport,windows]"
if ($LASTEXITCODE -ne 0) { Fail "pip install failed" }

# pywin32: register service DLLs
$post = Join-Path $Prefix "venv\Scripts\pywin32_postinstall.py"
if (Test-Path $post) { & $venvPy $post -install -quiet 2>$null | Out-Null }

# ── config ───────────────────────────────────────────────────────────────
$ini = Join-Path $ConfDir "agent.ini"
if (-not (Test-Path $ini)) {
    Copy-Item (Join-Path $src.FullName "agent.ini.example") $ini
    Write-Host "config: created $ini"
} else {
    Write-Host "config: keeping existing $ini"
}
if ($Server) {
    $text = Get-Content $ini -Raw
    if ($text -match "(?m)^\s*;?\s*server\s*=") {
        $text = [regex]::new("^\s*;?\s*server\s*=.*$", "Multiline").Replace($text, "server = $Server", 1)
    } else {
        $text += "`n[transport]`nserver = $Server`n"
    }
    Set-Content -Path $ini -Value $text -Encoding UTF8
    Write-Host "config: server = $Server"
}

# ── enroll ───────────────────────────────────────────────────────────────
if ($Enroll) {
    $tokenFile = Join-Path $ConfDir "agent.token"
    if ((Test-Path $tokenFile) -and $Force) {
        # Reinstall over an existing agent: stop the old service so it
        # does not keep pushing with the token we are about to replace.
        sc.exe stop oo-agent 2>$null | Out-Null
        Remove-Item -Force $tokenFile
        Write-Host "enroll: discarded the previous token (-Force)"
    }
    if (Test-Path $tokenFile) {
        Write-Host "enroll: token already present, skipping (use -Force to re-enroll)"
    } else {
        & (Join-Path $Prefix "venv\Scripts\oo-agent.exe") --enroll $Enroll
        if ($LASTEXITCODE -ne 0) { Fail "enroll failed - check the code and server URL" }
    }
}

# ── windows service ──────────────────────────────────────────────────────
& $venvPy -m oo_agent.service.windows_service --startup auto install 2>$null
if (-not $NoStart) {
    & $venvPy -m oo_agent.service.windows_service start
    Write-Host "service: oo-agent installed and started"
} else {
    Write-Host "service: installed but not started (-NoStart)"
}

Write-Host "done." -ForegroundColor Green
