<#
.SYNOPSIS
  Installs or updates Stripes from the latest GitHub release.

.DESCRIPTION
  Meant to run as SYSTEM from a Group Policy startup script or scheduled task.
  Safe to run repeatedly: it only downloads when a new release exists, checks
  the SHA-256 checksum, and skips the update if Stripes is open. At startup the
  network may not be up yet, so downloads are retried for a few minutes.
  Log: %ProgramData%\Stripes\update.log

  To run it by hand, use an administrator PowerShell window:
    powershell -NoProfile -ExecutionPolicy Bypass -File "\\server\share\Stripes\Update-Stripes.ps1"
#>
param(
    [string]$Repo = "spudandroot/CS_Stripes",
    [string]$InstallDir = (Join-Path $env:ProgramFiles "Stripes"),
    [string]$LogDir = (Join-Path $env:ProgramData "Stripes"),
    [int]$Attempts = 6,       # downloads are retried while the network comes up
    [int]$RetrySeconds = 30
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"  # the progress bar makes downloads very slow in Windows PowerShell
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$log = $null
function Write-Log($Message) {
    $line = "$(Get-Date -Format s)  $Message"
    Write-Host $line
    if ($log) {
        try { Add-Content -Path $log -Value $line } catch { }
    }
}

try {
    New-Item -ItemType Directory -Force $LogDir | Out-Null
    $log = Join-Path $LogDir "update.log"
    if ((Test-Path $log) -and (Get-Item $log).Length -gt 1MB) { Remove-Item $log }
} catch {
    $log = Join-Path ([IO.Path]::GetTempPath()) "Stripes-update.log"
}

$exe = Join-Path $InstallDir "Stripes.exe"
$versionFile = Join-Path $InstallDir "version.txt"
$tmp = Join-Path ([IO.Path]::GetTempPath()) "Stripes-update-$PID"

function Get-File($Url, $OutFile) {
    for ($attempt = 1; ; $attempt++) {
        try {
            Invoke-WebRequest -Uri $Url -OutFile $OutFile -UseBasicParsing -TimeoutSec 120
            return
        } catch {
            if ($attempt -ge $Attempts) { throw }
            Write-Log "Download failed ($($_.Exception.Message)); trying again in $RetrySeconds seconds."
            Start-Sleep -Seconds $RetrySeconds
        }
    }
}

try {
    try {
        New-Item -ItemType Directory -Force $InstallDir, $tmp | Out-Null
    } catch {
        throw "Can't create $InstallDir ($($_.Exception.Message)). Run this as administrator (at startup it runs as SYSTEM)."
    }

    Get-File "https://github.com/$Repo/releases/latest/download/version.txt" "$tmp\version.txt"
    $latest = (Get-Content "$tmp\version.txt" -Raw).Trim()
    $current = if (Test-Path $versionFile) { (Get-Content $versionFile -Raw).Trim() } else { "" }
    if ($latest -eq $current -and (Test-Path $exe)) {
        Write-Host "Stripes $current is up to date."
        exit 0
    }

    Write-Log "Updating Stripes '$current' -> '$latest'"
    $base = "https://github.com/$Repo/releases/download/$latest"
    Get-File "$base/Stripes.exe" "$tmp\Stripes.exe"
    Get-File "$base/Stripes.exe.sha256" "$tmp\Stripes.exe.sha256"

    $expected = (Get-Content "$tmp\Stripes.exe.sha256" -Raw).Trim()
    $actual = (Get-FileHash "$tmp\Stripes.exe" -Algorithm SHA256).Hash
    if ($actual -ne $expected) { throw "Checksum mismatch: expected $expected, got $actual" }

    if (Get-Process -Name Stripes -ErrorAction SilentlyContinue) {
        Write-Log "Stripes is open on this PC; will try again next run."
        exit 0
    }
    try {
        Copy-Item "$tmp\Stripes.exe" $exe -Force
    } catch {
        throw "Can't write $exe ($($_.Exception.Message)). Run this as administrator (at startup it runs as SYSTEM)."
    }
    Set-Content -Path $versionFile -Value $latest
    Write-Log "Installed Stripes $latest"
} catch {
    Write-Log "Update failed: $($_.Exception.Message)"
    exit 1
} finally {
    Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
}
