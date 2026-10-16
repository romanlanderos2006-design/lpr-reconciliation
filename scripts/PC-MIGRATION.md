# PC migration — save everything

This folder automates backing up Cursor local data so a PC wipe does not lose chats, settings, extensions, or unpushed project files.

## Already safe (no action needed)

| Item | Status |
|------|--------|
| GitHub repo `lpr-reconciliation` | On `origin/main`, working tree clean |
| Cursor account / plan | Tied to your login |
| Cloud Agents | At [cursor.com/agents](https://cursor.com/agents) |

## What these scripts save (local-only data)

- Cursor settings, keybindings, UI state
- Extensions list + extension data under `~/.cursor`
- Chat / agent history stored on this machine
- Optional copies of your project folders (including uncommitted files)

## Old PC — run once before wipe

### Windows (PowerShell)

```powershell
cd path\to\lpr-reconciliation\scripts
Set-ExecutionPolicy -Scope Process Bypass
.\backup-cursor-pc.ps1
```

Or with explicit project folders:

```powershell
.\backup-cursor-pc.ps1 -ProjectPaths @("$env:USERPROFILE\Documents\GitHub")
```

### macOS / Linux

```bash
cd path/to/lpr-reconciliation/scripts
chmod +x backup-cursor-pc.sh restore-cursor-pc.sh
./backup-cursor-pc.sh
# or:
./backup-cursor-pc.sh --project "$HOME/code"
```

**Required after the script finishes:** copy both of these off the PC (USB / OneDrive / Google Drive):

1. `Desktop/CursorPCBackup/cursor-pc-backup-….zip`
2. `Desktop/CursorPCBackup/cursor-pc-backup-….sha256.txt`

Do not wipe until those two files are on another drive and the checksum looks correct.

## New PC — restore

1. Install Cursor from https://cursor.com and sign in with the **same** account.
2. Quit Cursor completely.
3. Run the matching restore script against the zip:

```powershell
# Windows
.\restore-cursor-pc.ps1 -BackupZip "$env:USERPROFILE\Desktop\CursorPCBackup\cursor-pc-backup-….zip"
```

```bash
# macOS / Linux
./restore-cursor-pc.sh --zip ~/Desktop/CursorPCBackup/cursor-pc-backup-….zip
```

4. Clone your repo (same folder path as before helps chat history come back):

```bash
git clone https://github.com/romanlanderos2006-design/lpr-reconciliation.git
```

5. Open Cursor and confirm chats / extensions / settings.
6. Only then discard the backup zip.

## Extra belt-and-suspenders (optional)

In Cursor on the old PC: Command Palette → `Preferences: Open Profiles (UI)` → Export Profile → save that file next to the zip.
