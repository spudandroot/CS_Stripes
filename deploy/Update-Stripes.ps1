<#
.SYNOPSIS
  Installs or updates Stripes from the latest GitHub release.

.DESCRIPTION
  Meant to run as SYSTEM from a Group Policy scheduled task (at startup and
  daily). Safe to run repeatedly: it only downloads when a new release exists,
  checks the SHA-256 checksum, and skips the update if Stripes is open.
  Log: %ProgramData%\Stripes\update.log
#>
param(
    [string]$Repo = "spudandroot/CS_Stripes",
    [string]$InstallDir = (Join-Path $env:ProgramFiles "Stripes"),
    [string]$LogDir = (Join-Path $env:ProgramData "Stripes")
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"  # the progress bar makes downloads very slow in Windows PowerShell
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

New-Item -ItemType Directory -Force $InstallDir, $LogDir | Out-Null
$log = Join-Path $LogDir "update.log"
if ((Test-Path $log) -and (Get-Item $log).Length -gt 1MB) { Remove-Item $log }
function Write-Log($Message) { "$(Get-Date -Format s)  $Message" | Add-Content $log }

$exe = Join-Path $InstallDir "Stripes.exe"
$versionFile = Join-Path $InstallDir "version.txt"
$tmp = Join-Path ([IO.Path]::GetTempPath()) "Stripes-update-$PID"

function Get-File($Url, $OutFile) {
    Invoke-WebRequest -Uri $Url -OutFile $OutFile -UseBasicParsing -TimeoutSec 120
}

try {
    New-Item -ItemType Directory -Force $tmp | Out-Null

    Get-File "https://github.com/$Repo/releases/latest/download/version.txt" "$tmp\version.txt"
    $latest = (Get-Content "$tmp\version.txt" -Raw).Trim()
    $current = if (Test-Path $versionFile) { (Get-Content $versionFile -Raw).Trim() } else { "" }
    if ($latest -eq $current -and (Test-Path $exe)) { exit 0 }

    Write-Log "Updating Stripes '$current' -> '$latest'"
    $base = "https://github.com/$Repo/releases/download/$latest"
    Get-File "$base/Stripes.exe" "$tmp\Stripes.exe"
    Get-File "$base/Stripes.exe.sha256" "$tmp\Stripes.exe.sha256"

    $expected = (Get-Content "$tmp\Stripes.exe.sha256" -Raw).Trim()
    $actual = (Get-FileHash "$tmp\Stripes.exe" -Algorithm SHA256).Hash
    if ($actual -ne $expected) { throw "Checksum mismatch: expected $expected, got $actual" }

    try {
        Copy-Item "$tmp\Stripes.exe" $exe -Force
    } catch {
        Write-Log "Stripes is open on this PC; will try again next run."
        exit 0
    }
    Set-Content -Path $versionFile -Value $latest
    Write-Log "Installed Stripes $latest"
} catch {
    Write-Log "Update failed: $($_.Exception.Message)"
    exit 1
} finally {
    Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
}
