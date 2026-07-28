#!/bin/sh
set -e

# run_server.bat의 날짜별 DB 스냅샷 백업과 동일한 동작 (systemd ExecStartPre로 이식)
SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
REPO_ROOT=$(cd "$SCRIPT_DIR/.." && pwd)
DB_PATH="$REPO_ROOT/src/server/toolwatch.db"

if [ ! -f "$DB_PATH" ]; then
    exit 0
fi

mkdir -p "$REPO_ROOT/backups"
cp "$DB_PATH" "$REPO_ROOT/backups/toolwatch-$(date +%Y%m%d).db"
