#!/usr/bin/env bash
# backup_db.sh — бэкап agent.db с ротацией и проверкой integrity
# Использование: ./scripts/backup_db.sh [метка]
# Пример: ./scripts/backup_db.sh pre-evaluations

set -euo pipefail

cd "$(dirname "$0")/.."

DB="agent.db"
LABEL="${1:-manual}"
TS=$(date +%Y%m%d-%H%M%S)
BACKUP="${DB}.bak-${LABEL}-${TS}"

if [[ ! -f "$DB" ]]; then
    echo "ERROR: $DB не найден в $(pwd)" >&2
    exit 1
fi

cp "$DB" "$BACKUP"
SIZE=$(du -h "$BACKUP" | cut -f1)
echo "OK: $BACKUP ($SIZE)"

INTEG=$(sqlite3 "$BACKUP" "PRAGMA integrity_check;")
if [[ "$INTEG" != "ok" ]]; then
    echo "ERROR: бэкап повреждён ($INTEG)" >&2
    exit 2
fi
echo "OK: integrity_check = ok"

KEEP=10
REMOVED=0
while IFS= read -r old; do
    rm -f "$old"
    echo "  removed: $old"
    REMOVED=$((REMOVED+1))
done < <(ls -t ${DB}.bak-* 2>/dev/null | tail -n +$((KEEP+1)))

if [[ $REMOVED -eq 0 ]]; then
    echo "OK: ротация не требуется (всего ≤ $KEEP бэкапов)"
fi

echo
echo "=== Последние 5 бэкапов ==="
ls -lht ${DB}.bak-* 2>/dev/null | head -5
