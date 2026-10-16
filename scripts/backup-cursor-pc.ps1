#Requires -Version 5.1
<#
.SYNOPSIS
  One-shot Cursor + local project backup before a PC wipe / new machine.

.DESCRIPTION
  Copies Cursor app data, .cursor config, and optionally your project folders
  into a single timestamped zip with a checksum + restore instructions.
  Designed to minimize human error: defaults are safe; only asks for project roots.

.EXAMPLE
  # Double-click or run in PowerShell from this folder:
  .\backup-cursor-pc.ps1

.EXAMPLE
  # Also zip specific project folders (recommended):
  .\backup-cursor-pc.ps1 -ProjectPaths @("$env:USERPROFILE\Documents\GitHub","$env:USERPROFILE\code")
#>
[CmdletBinding()]
param(
  [string]$OutputRoot = "$env:USERPROFILE\Desktop\CursorPCBackup",
  [string[]]$ProjectPaths = @(),
  [switch]$SkipProjects
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

function Write-Step($msg) { Write-Host "`n==> $msg" -ForegroundColor Cyan }
function Write-Ok($msg)   { Write-Host "    OK: $msg" -ForegroundColor Green }
function Write-Warn($msg) { Write-Host "    WARN: $msg" -ForegroundColor Yellow }
function Write-Fail($msg) { Write-Host "    FAIL: $msg" -ForegroundColor Red }

$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$workDir = Join-Path $OutputRoot "staging-$stamp"
$zipPath = Join-Path $OutputRoot "cursor-pc-backup-$stamp.zip"
$manifestPath = Join-Path $workDir "MANIFEST.txt"
$shaPath = Join-Path $OutputRoot "cursor-pc-backup-$stamp.sha256.txt"

Write-Host "Cursor PC Backup" -ForegroundColor White
Write-Host "This will create: $zipPath"

New-Item -ItemType Directory -Force -Path $workDir | Out-Null
New-Item -ItemType Directory -Force -Path $OutputRoot | Out-Null

$sources = @(
  @{ Name = "AppData-Roaming-Cursor"; Path = Join-Path $env:APPDATA "Cursor" },
  @{ Name = "AppData-Local-Cursor";   Path = Join-Path $env:LOCALAPPDATA "Cursor" },
  @{ Name = "UserProfile-dot-cursor"; Path = Join-Path $env:USERPROFILE ".cursor" }
)

$copied = @()
$missing = @()

Write-Step "Copying Cursor data folders"
foreach ($src in $sources) {
  if (Test-Path -LiteralPath $src.Path) {
    $dest = Join-Path $workDir $src.Name
    Write-Host "    Copying $($src.Path) ..."
    # Robocopy is more reliable for large trees than Copy-Item
    $null = & robocopy $src.Path $dest /E /COPY:DAT /R:1 /W:1 /NFL /NDL /NJH /NJS /NP
    $code = $LASTEXITCODE
    if ($code -ge 8) {
      throw "Robocopy failed for $($src.Path) with exit code $code"
    }
    $copied += $src
    Write-Ok $src.Name
  } else {
    $missing += $src
    Write-Warn "Not found (skipped): $($src.Path)"
  }
}

if ($copied.Count -eq 0) {
  Write-Fail "No Cursor folders found. Is Cursor installed on this PC?"
  exit 1
}

# Common project discovery if user did not pass -ProjectPaths
if (-not $SkipProjects -and $ProjectPaths.Count -eq 0) {
  $guesses = @(
    (Join-Path $env:USERPROFILE "Documents\GitHub"),
    (Join-Path $env:USERPROFILE "Documents\Projects"),
    (Join-Path $env:USERPROFILE "source"),
    (Join-Path $env:USERPROFILE "code"),
    (Join-Path $env:USERPROFILE "dev"),
    (Join-Path $env:USERPROFILE "repos"),
    "C:\dev",
    "C:\code",
    "C:\src"
  ) | Where-Object { Test-Path -LiteralPath $_ }

  if ($guesses.Count -gt 0) {
    Write-Step "Found possible project folders"
    $guesses | ForEach-Object { Write-Host "    - $_" }
    Write-Host ""
    Write-Host "Include these in the backup? (Y/n)" -ForegroundColor Yellow
    $ans = Read-Host
    if ($ans -eq "" -or $ans -match '^[Yy]') {
      $ProjectPaths = $guesses
    }
  }
}

if (-not $SkipProjects -and $ProjectPaths.Count -gt 0) {
  Write-Step "Copying project folders"
  $projRoot = Join-Path $workDir "Projects"
  New-Item -ItemType Directory -Force -Path $projRoot | Out-Null
  foreach ($p in $ProjectPaths) {
    if (-not (Test-Path -LiteralPath $p)) {
      Write-Warn "Project path missing: $p"
      continue
    }
    $leaf = Split-Path $p -Leaf
    $dest = Join-Path $projRoot $leaf
    # Avoid name collisions
    if (Test-Path -LiteralPath $dest) {
      $dest = Join-Path $projRoot ($leaf + "_" + [Guid]::NewGuid().ToString("N").Substring(0, 8))
    }
    Write-Host "    Copying $p ..."
    $null = & robocopy $p $dest /E /COPY:DAT /R:1 /W:1 /XD node_modules .git __pycache__ .venv venv dist build .next /NFL /NDL /NJH /NJS /NP
    # Note: we skip .git for size; remotes are on GitHub. Uncommitted files ARE copied.
    # If you need full git history locally too, remove .git from /XD above.
    if ($LASTEXITCODE -ge 8) {
      throw "Robocopy failed for $p"
    }
    Write-Ok $leaf
  }
}

Write-Step "Writing machine inventory"
$inventory = [ordered]@{
  backupCreatedUtc = (Get-Date).ToUniversalTime().ToString("o")
  computerName     = $env:COMPUTERNAME
  userName         = $env:USERNAME
  userProfile      = $env:USERPROFILE
  powershell       = $PSVersionTable.PSVersion.ToString()
  os               = [System.Environment]::OSVersion.VersionString
  cursorFoldersCopied = @($copied | ForEach-Object { $_.Path })
  cursorFoldersMissing = @($missing | ForEach-Object { $_.Path })
  projectPaths     = @($ProjectPaths)
}
$inventory | ConvertTo-Json -Depth 5 | Set-Content -Encoding UTF8 (Join-Path $workDir "inventory.json")

# List installed Cursor extensions if present
$extDir = Join-Path $env:USERPROFILE ".cursor\extensions"
if (Test-Path $extDir) {
  Get-ChildItem $extDir -Directory | Select-Object -ExpandProperty Name |
    Set-Content -Encoding UTF8 (Join-Path $workDir "extensions-list.txt")
  Write-Ok "extensions-list.txt"
}

$restoreInstructions = @"
CURSOR PC BACKUP — RESTORE ON NEW PC
====================================
Created: $($inventory.backupCreatedUtc)
From PC: $($inventory.computerName) / $($inventory.userName)

WHAT THIS ARCHIVE CONTAINS
- AppData-Roaming-Cursor  -> %APPDATA%\Cursor
- AppData-Local-Cursor    -> %LOCALAPPDATA%\Cursor  (if present)
- UserProfile-dot-cursor  -> %USERPROFILE%\.cursor
- Projects\               -> your local project copies (if included)
- inventory.json, extensions-list.txt, this MANIFEST

BEFORE YOU WIPE THE OLD PC
1. Keep this zip + the .sha256.txt file on an external drive / OneDrive / Google Drive.
2. Confirm the sha256 matches (see VERIFY below).
3. Sign into https://cursor.com and https://github.com to confirm cloud account access.

ON THE NEW PC
1. Install Cursor from https://cursor.com
2. Sign in with the SAME Cursor account.
3. Close Cursor completely (system tray too).
4. Run:  .\restore-cursor-pc.ps1 -BackupZip "FULL\PATH\TO\cursor-pc-backup-....zip"
   OR manually:
   - Extract the zip
   - Copy AppData-Roaming-Cursor  -> %APPDATA%\Cursor
   - Copy AppData-Local-Cursor    -> %LOCALAPPDATA%\Cursor  (if folder exists in zip)
   - Copy UserProfile-dot-cursor  -> %USERPROFILE%\.cursor
5. Re-clone GitHub repos to the SAME folder paths you used before when possible
   (chat history is keyed to workspace paths).
   Your repo: https://github.com/romanlanderos2006-design/lpr-reconciliation
6. Open Cursor and confirm chats / extensions / settings.

VERIFY (on either PC, PowerShell)
  Get-FileHash -Algorithm SHA256 "PATH\TO\cursor-pc-backup-....zip"

CLOUD SIDE (already account-backed — no zip needed)
- Cursor plan / billing
- Cloud Agents at https://cursor.com/agents
- GitHub repos (push any uncommitted work before wipe)

NOTES
- Profile export is also available in Cursor: Command Palette -> "Preferences: Open Profiles (UI)" -> Export Profile
- Do NOT delete the backup until you have confirmed the new PC works.
"@

Set-Content -Encoding UTF8 -Path $manifestPath -Value $restoreInstructions

Write-Step "Creating zip (this can take a few minutes)"
if (Test-Path $zipPath) { Remove-Item -Force $zipPath }
Compress-Archive -Path (Join-Path $workDir "*") -DestinationPath $zipPath -CompressionLevel Optimal
Write-Ok $zipPath

Write-Step "Writing checksum"
$hash = (Get-FileHash -Algorithm SHA256 -Path $zipPath).Hash
"$hash  $(Split-Path $zipPath -Leaf)" | Set-Content -Encoding ASCII $shaPath
Write-Ok $shaPath
Write-Host "    SHA256: $hash"

Write-Step "Cleaning staging folder"
Remove-Item -Recurse -Force $workDir

$sizeMB = [math]::Round((Get-Item $zipPath).Length / 1MB, 1)
Write-Host ""
Write-Host "DONE. Backup ready." -ForegroundColor Green
Write-Host "  Zip:      $zipPath  ($sizeMB MB)"
Write-Host "  Checksum: $shaPath"
Write-Host ""
Write-Host "NEXT: Copy BOTH files to OneDrive / Google Drive / USB before resetting this PC." -ForegroundColor Yellow
Write-Host "Then on the new PC run restore-cursor-pc.ps1 against the zip."
