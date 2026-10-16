#!/usr/bin/env bash
# One-shot Cursor + local project backup before a PC wipe / new machine (macOS / Linux).
set -euo pipefail

OUTPUT_ROOT="${OUTPUT_ROOT:-$HOME/Desktop/CursorPCBackup}"
SKIP_PROJECTS=0
PROJECT_PATHS=()

usage() {
  cat <<'EOF'
Usage: ./backup-cursor-pc.sh [--output DIR] [--project PATH]... [--skip-projects]

Creates a timestamped zip of Cursor data (+ optional project folders) with SHA256.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --output) OUTPUT_ROOT="$2"; shift 2 ;;
    --project) PROJECT_PATHS+=("$2"); shift 2 ;;
    --skip-projects) SKIP_PROJECTS=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown arg: $1"; usage; exit 1 ;;
  esac
done

STAMP=$(date +%Y%m%d-%H%M%S)
WORK="$OUTPUT_ROOT/staging-$STAMP"
ZIP="$OUTPUT_ROOT/cursor-pc-backup-$STAMP.zip"
SHA="$OUTPUT_ROOT/cursor-pc-backup-$STAMP.sha256.txt"

mkdir -p "$WORK" "$OUTPUT_ROOT"
echo "==> Cursor PC Backup -> $ZIP"

copy_if_exists() {
  local name="$1" path="$2"
  if [[ -d "$path" ]]; then
    echo "    Copying $path"
    mkdir -p "$WORK/$name"
    # Prefer rsync; fall back to cp
    if command -v rsync >/dev/null 2>&1; then
      rsync -a "$path"/ "$WORK/$name"/
    else
      cp -a "$path"/. "$WORK/$name"/
    fi
    echo "    OK: $name"
  else
    echo "    WARN: missing $path"
  fi
}

if [[ "$(uname -s)" == "Darwin" ]]; then
  copy_if_exists "Library-Application-Support-Cursor" "$HOME/Library/Application Support/Cursor"
else
  copy_if_exists "config-Cursor" "$HOME/.config/Cursor"
fi
copy_if_exists "UserProfile-dot-cursor" "$HOME/.cursor"

# Discover common project roots if none given
if [[ $SKIP_PROJECTS -eq 0 && ${#PROJECT_PATHS[@]} -eq 0 ]]; then
  GUESSES=()
  for g in "$HOME/Documents/GitHub" "$HOME/Documents/Projects" "$HOME/code" "$HOME/dev" "$HOME/repos" "$HOME/src"; do
    [[ -d "$g" ]] && GUESSES+=("$g")
  done
  if [[ ${#GUESSES[@]} -gt 0 ]]; then
    echo "==> Found possible project folders:"
    printf '    - %s\n' "${GUESSES[@]}"
    read -r -p "Include these in the backup? [Y/n] " ans || true
    if [[ -z "${ans:-}" || "$ans" =~ ^[Yy]$ ]]; then
      PROJECT_PATHS=("${GUESSES[@]}")
    fi
  fi
fi

if [[ $SKIP_PROJECTS -eq 0 && ${#PROJECT_PATHS[@]} -gt 0 ]]; then
  mkdir -p "$WORK/Projects"
  for p in "${PROJECT_PATHS[@]}"; do
    if [[ ! -d "$p" ]]; then
      echo "    WARN: missing project $p"
      continue
    fi
    leaf=$(basename "$p")
    dest="$WORK/Projects/$leaf"
    echo "    Copying $p"
    if command -v rsync >/dev/null 2>&1; then
      rsync -a --exclude node_modules --exclude .git --exclude __pycache__ --exclude .venv --exclude venv --exclude dist --exclude build --exclude .next "$p"/ "$dest"/
    else
      cp -a "$p" "$dest"
    fi
    echo "    OK: $leaf"
  done
fi

# Inventory
python3 - <<PY
import json, os, platform, socket
from datetime import datetime, timezone
inv = {
  "backupCreatedUtc": datetime.now(timezone.utc).isoformat(),
  "computerName": socket.gethostname(),
  "userName": os.environ.get("USER", ""),
  "home": os.path.expanduser("~"),
  "os": platform.platform(),
  "projectPaths": $(printf '%s\n' "${PROJECT_PATHS[@]+"${PROJECT_PATHS[@]}"}" | python3 -c 'import sys,json; print(json.dumps([l.strip() for l in sys.stdin if l.strip()]))'),
}
open("$WORK/inventory.json","w").write(json.dumps(inv, indent=2))
print("    OK: inventory.json")
PY

EXT_DIR="$HOME/.cursor/extensions"
if [[ -d "$EXT_DIR" ]]; then
  ls -1 "$EXT_DIR" > "$WORK/extensions-list.txt" || true
  echo "    OK: extensions-list.txt"
fi

cat > "$WORK/MANIFEST.txt" <<EOF
CURSOR PC BACKUP — RESTORE ON NEW PC
Created: $(date -u +%Y-%m-%dT%H:%M:%SZ)
From: $(hostname) / ${USER:-unknown}

ON THE NEW PC
1. Install Cursor, sign in with the SAME account, quit Cursor fully.
2. Run: ./restore-cursor-pc.sh --zip /path/to/$(basename "$ZIP")
3. Re-clone GitHub repos to the same folder paths when possible.
   Repo: https://github.com/romanlanderos2006-design/lpr-reconciliation
4. Keep this zip + .sha256.txt until the new PC is confirmed working.

CLOUD (already safe with your account)
- https://cursor.com/agents
- GitHub remotes
EOF

echo "==> Creating zip"
(
  cd "$WORK"
  if command -v zip >/dev/null 2>&1; then
    zip -r -q "$ZIP" .
  else
    tar -czf "${ZIP%.zip}.tar.gz" .
    ZIP="${ZIP%.zip}.tar.gz"
    SHA="${ZIP}.sha256.txt"
  fi
)

echo "==> Writing checksum"
if command -v shasum >/dev/null 2>&1; then
  (cd "$(dirname "$ZIP")" && shasum -a 256 "$(basename "$ZIP")") > "$SHA"
elif command -v sha256sum >/dev/null 2>&1; then
  (cd "$(dirname "$ZIP")" && sha256sum "$(basename "$ZIP")") > "$SHA"
fi

rm -rf "$WORK"
SIZE=$(du -h "$ZIP" | awk '{print $1}')
echo ""
echo "DONE. Backup ready: $ZIP ($SIZE)"
echo "Checksum: $SHA"
echo "Copy BOTH files off this PC before resetting."
