# Install the timba binary on Windows.
#
# Usage:
#   irm https://raw.githubusercontent.com/augustodileo/timba/main/install.ps1 | iex
#   .\install.ps1                           # from cloned repo
#   $env:VERSION="v0.1.2"; .\install.ps1   # pin version

$ErrorActionPreference = "Stop"

$Repo = "augustodileo/timba"
$InstallDir = "$env:LOCALAPPDATA\timba"
$ConfigDir = "$env:USERPROFILE\.timba"

# ── Resolve version ──────────────────────────────────────────

if (-not $env:VERSION) {
    Write-Host "Fetching latest release..."
    $Release = Invoke-RestMethod "https://api.github.com/repos/$Repo/releases/latest"
    $Version = $Release.tag_name
    if (-not $Version) {
        Write-Error "Could not determine latest version. Check https://github.com/$Repo/releases"
        exit 1
    }
} else {
    $Version = $env:VERSION
}

$PkgVersion = $Version -replace "^v", ""
$Archive = "timba-$PkgVersion-windows-x64.tar.gz"
$Url = "https://github.com/$Repo/releases/download/$Version/$Archive"

Write-Host "Installing timba $Version (windows-x64)..."

# ── Download ─────────────────────────────────────────────────

$TmpDir = Join-Path $env:TEMP "timba-install-$(Get-Random)"
New-Item -ItemType Directory -Path $TmpDir -Force | Out-Null

try {
    Invoke-WebRequest -Uri $Url -OutFile "$TmpDir\$Archive" -UseBasicParsing
} catch {
    Write-Error @"
Download failed.
  URL: $Url
  Check that $Version has a Windows binary at:
  https://github.com/$Repo/releases/tag/$Version
"@
    exit 1
}

# ── Extract ──────────────────────────────────────────────────

tar xzf "$TmpDir\$Archive" -C $TmpDir

# ── Install ──────────────────────────────────────────────────

New-Item -ItemType Directory -Path $InstallDir -Force | Out-Null
Copy-Item "$TmpDir\timba.exe" "$InstallDir\timba.exe" -Force

# ── Config ───────────────────────────────────────────────────

New-Item -ItemType Directory -Path $ConfigDir -Force | Out-Null
if (-not (Test-Path "$ConfigDir\config.yaml")) {
    if (Test-Path "$TmpDir\config.yaml") {
        Copy-Item "$TmpDir\config.yaml" "$ConfigDir\config.yaml"
        Write-Host "  Config: $ConfigDir\config.yaml"
    }
} else {
    Write-Host "  Config: kept existing $ConfigDir\config.yaml"
}

# ── PATH ─────────────────────────────────────────────────────

$UserPath = [Environment]::GetEnvironmentVariable("Path", "User")
if ($UserPath -notlike "*$InstallDir*") {
    [Environment]::SetEnvironmentVariable("Path", "$InstallDir;$UserPath", "User")
    Write-Host "  Added $InstallDir to PATH (restart your terminal)"
}

# ── Cleanup ──────────────────────────────────────────────────

Remove-Item -Recurse -Force $TmpDir

# ── Summary ──────────────────────────────────────────────────

Write-Host ""
Write-Host "Installed timba $Version"
Write-Host "  Binary: $InstallDir\timba.exe"
Write-Host "  Config: $ConfigDir\config.yaml"
Write-Host ""
Write-Host "  Get started:"
Write-Host "    timba start              set up credentials and start the bot"
Write-Host "    timba status             check bot status"
Write-Host "    timba stop               stop the bot"
Write-Host ""
Write-Host "  Data: $ConfigDir\"
