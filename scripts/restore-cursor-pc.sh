#!/usr/bin/env bash
# Restore a Cursor PC backup created by backup-cursor-pc.sh
set -euo pipefail

ZIP=""
FORCE=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --zip) ZIP="$2"; shift 2 ;;
    --force) FORCE=1; shift ;;
    -h|--help)
      echo "Usage: ./restore-cursor-pc.sh --zip /path/to/cursor-pc-backup-....zip [--force]"
      exit 0
      ;;
    *) echo "Unknown arg: $1"; exit 1 ;;
  esac
done

[[ -n "$ZIP" ]] || { echo "Missing --zip"; exit 1; }
[[ -f "$ZIP" ]] || { echo "Zip not found: $ZIP"; exit 1; }

SHA="${ZIP%.zip}.sha256.txt"
if [[ -f "$SHA" ]]; then
  echo "==> Verifying SHA256"
  if command -v shasum >/dev/null 2>&1; then
    EXPECTED=$(awk '{print $1}' "$SHA")
    ACTUAL=$(shasum -a 256 "$ZIP" | awk '{print $1}')
  else
    EXPECTED=$(awk '{print $1}' "$SHA")
    ACTUAL=$(sha256sum "$ZIP" | awk '{print $1}')
  fi
  if [[ "${EXPECTED,,}" != "${ACTUAL,,}" ]]; then
    echo "Checksum mismatch! Refusing to restore."
    exit 1
  fi
  echo "    OK: checksum matches"
else
  echo "    WARN: no sibling .sha256.txt — skipping integrity check"
fi

if pgrep -x Cursor >/dev/null 2>&1 || pgrep -f "[C]ursor" >/dev/null 2>&1; then
  if [[ $FORCE -eq 0 ]]; then
    echo "Cursor is running. Quit it fully, then re-run (or pass --force)."
    exit 1
  fi
  pkill -x Cursor 2>/dev/null || true
  sleep 2
fi

STAMP=$(date +%Y%m%d-%H%M%S)
EXTRACT=$(mktemp -d "/tmp/cursor-restore-$STAMP.XXXX")
echo "==> Extracting to $EXTRACT"
if [[ "$ZIP" == *.tar.gz ]]; then
  tar -xzf "$ZIP" -C "$EXTRACT"
else
  unzip -q "$ZIP" -d "$EXTRACT"
fi

restore_dir() {
  local name="$1" dest="$2"
  local src="$EXTRACT/$name"
  [[ -d "$src" ]] || { echo "    WARN: not in archive: $name"; return; }
  if [[ -e "$dest" ]]; then
    mv "$dest" "${dest}.bak-$STAMP"
    echo "    Existing moved aside -> ${dest}.bak-$STAMP"
  fi
  mkdir -p "$(dirname "$dest")"
  cp -a "$src" "$dest"
  echo "    OK: $name -> $dest"
}

echo "==> Restoring Cursor folders"
if [[ "$(uname -s)" == "Darwin" ]]; then
  restore_dir "Library-Application-Support-Cursor" "$HOME/Library/Application Support/Cursor"
else
  restore_dir "config-Cursor" "$HOME/.config/Cursor"
fi
restore_dir "UserProfile-dot-cursor" "$HOME/.cursor"

if [[ -d "$EXTRACT/Projects" ]]; then
  DEST_P="$HOME/Desktop/RestoredProjects-$STAMP"
  cp -a "$EXTRACT/Projects" "$DEST_P"
  echo "    OK: projects -> $DEST_P"
  echo "    WARN: move them to the same paths as the old PC when possible (helps chat history)."
fi

rm -rf "$EXTRACT"
echo ""
echo "DONE. Start Cursor, sign in with the same account, open your projects."
echo "Repo: https://github.com/romanlanderos2006-design/lpr-reconciliation"
