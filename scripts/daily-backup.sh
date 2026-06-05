#!/bin/bash
# Daily backup: commit + push theSwiszard monorepo
set -e

REPO="/home/ziggibot/theSwiszard"
LOG="/home/ziggibot/.swiszcli/backup.log"

echo "[$(date -Iseconds)] Starting daily backup" >> "$LOG"

cd "$REPO"

# Stage everything
git add -A

# Only commit if there's something to commit
if git diff --cached --quiet; then
    echo "[$(date -Iseconds)] Nothing to commit" >> "$LOG"
    exit 0
fi

# Commit with timestamp
git commit -m "backup: daily auto-commit $(date -I)"

# Push
if git push origin main >> "$LOG" 2>&1; then
    echo "[$(date -Iseconds)] Push successful" >> "$LOG"
else
    echo "[$(date -Iseconds)] Push FAILED — check network/token" >> "$LOG"
fi
