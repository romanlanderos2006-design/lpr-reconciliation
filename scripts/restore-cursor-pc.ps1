#Requires -Version 5.1
<#
.SYNOPSIS
  Restore a Cursor PC backup zip created by backup-cursor-pc.ps1

.EXAMPLE
  .\restore-cursor-pc.ps1 -BackupZip "$env:USERPROFILE\Desktop\CursorPCBackup\cursor-pc-backup-....zip"
#>
[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)]
  [string]$BackupZip,
  [switch]$Force
)

$ErrorActionPreference = "Stop"

function Write-Step($msg) { Write-Host "`n==> $msg" -ForegroundColor Cyan }
function Write-Ok($msg)   { Write-Host "    OK: $msg" -ForegroundColor Green }
function Write-Warn($msg) { Write-Host "    WARN: $msg" -ForegroundColor Yellow }

if (-not (Test-Path -LiteralPath $BackupZip)) {
  throw "Backup zip not found: $BackupZip"
}

# Verify checksum if sibling .sha256.txt exists
$shaFile = $BackupZip -replace '\.zip$', '.sha256.txt'
if (Test-Path -LiteralPath $shaFile) {
  Write-Step "Verifying SHA256"
  $expected = ((Get-Content $shaFile -Raw) -split '\s+')[0].Trim()
  $actual = (Get-FileHash -Algorithm SHA256 -Path $BackupZip).Hash
  if ($expected.ToUpperInvariant() -ne $actual.ToUpperInvariant()) {
    throw "Checksum mismatch! Expected $expected got $actual — do not restore a corrupt backup."
  }
  Write-Ok "Checksum matches"
} else {
  Write-Warn "No .sha256.txt next to the zip — skipping integrity check"
}

# Cursor must be closed
$cursorProcs = Get-Process -Name "Cursor","Cursor Helper" -ErrorAction SilentlyContinue
if ($cursorProcs) {
  if (-not $Force) {
    throw "Cursor is still running. Quit Cursor completely (check system tray), then re-run. Or pass -Force to kill it."
  }
  Write-Warn "Stopping Cursor processes (-Force)"
  $cursorProcs | Stop-Process -Force
  Start-Sleep -Seconds 2
}

$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$extractRoot = Join-Path $env:TEMP "cursor-restore-$stamp"
New-Item -ItemType Directory -Force -Path $extractRoot | Out-Null

Write-Step "Extracting backup"
Expand-Archive -LiteralPath $BackupZip -DestinationPath $extractRoot -Force
Write-Ok $extractRoot

function Backup-ExistingThenCopy($fromName, $toPath) {
  $from = Join-Path $extractRoot $fromName
  if (-not (Test-Path -LiteralPath $from)) {
    Write-Warn "Not in archive: $fromName"
    return
  }
  if (Test-Path -LiteralPath $toPath) {
    $bak = "$toPath.bak-$stamp"
    Write-Host "    Existing folder moved aside -> $bak"
    Move-Item -LiteralPath $toPath -Destination $bak
  }
  $parent = Split-Path $toPath -Parent
  if (-not (Test-Path $parent)) {
    New-Item -ItemType Directory -Force -Path $parent | Out-Null
  }
  Copy-Item -LiteralPath $from -Destination $toPath -Recurse -Force
  Write-Ok "$fromName -> $toPath"
}

Write-Step "Restoring Cursor folders"
Backup-ExistingThenCopy "AppData-Roaming-Cursor" (Join-Path $env:APPDATA "Cursor")
Backup-ExistingThenCopy "AppData-Local-Cursor" (Join-Path $env:LOCALAPPDATA "Cursor")
Backup-ExistingThenCopy "UserProfile-dot-cursor" (Join-Path $env:USERPROFILE ".cursor")

$projects = Join-Path $extractRoot "Projects"
if (Test-Path $projects) {
  $destProjects = Join-Path $env:USERPROFILE "Desktop\RestoredProjects-$stamp"
  Copy-Item -LiteralPath $projects -Destination $destProjects -Recurse -Force
  Write-Ok "Project copies placed at $destProjects"
  Write-Warn "Move them to your preferred paths (ideally the SAME paths as the old PC for chat history)."
}

if (Test-Path (Join-Path $extractRoot "MANIFEST.txt")) {
  Write-Host ""
  Get-Content (Join-Path $extractRoot "MANIFEST.txt") | Select-Object -First 40
}

Remove-Item -Recurse -Force $extractRoot

Write-Host ""
Write-Host "DONE. Start Cursor, sign in with the same account, and open your projects." -ForegroundColor Green
Write-Host "Clone from GitHub if needed: https://github.com/romanlanderos2006-design/lpr-reconciliation"
