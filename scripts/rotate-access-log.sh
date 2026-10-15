#!/bin/bash
# Daily anginx access-log rotation, run from lake's crontab at 00:05 PT.
# The log lives in the container's own layer (no host mount), so: rename it in
# place, tell nginx to reopen (no restart, no dropped requests), copy the day
# out to the host, compress, and prune. Retention: 90 days (owner 2026-10-14).
# archive/ (one-off historical files) is never pruned.
set -euo pipefail
C=anginx
OUT=/srv/anginx-logs
KEEP_DAYS=90
DAY=$(date -d yesterday +%F)          # rotating just after midnight: the file is mostly yesterday
mkdir -p "$OUT"

# A .rot left by an interrupted run is shipped first, never overwritten.
if docker exec "$C" test -e /var/log/nginx/access.log.rot; then
  docker cp "$C":/var/log/nginx/access.log.rot "$OUT/access-$DAY-leftover-$(date +%s).log"
  docker exec "$C" rm -f /var/log/nginx/access.log.rot
fi

if ! docker exec "$C" test -s /var/log/nginx/access.log; then
  echo "$(date '+%F %T %Z') access.log empty or missing — nothing to rotate"; exit 0
fi
docker exec "$C" sh -c 'mv /var/log/nginx/access.log /var/log/nginx/access.log.rot && nginx -s reopen'
sleep 2                                 # let workers finish writes to the old file
docker cp "$C":/var/log/nginx/access.log.rot "$OUT/access-$DAY.log"
docker exec "$C" rm -f /var/log/nginx/access.log.rot
nice gzip -f "$OUT"/access-*.log
find "$OUT" -maxdepth 1 -name 'access-*.log.gz' -mtime +"$KEEP_DAYS" -delete
echo "$(date '+%F %T %Z') rotated -> access-$DAY.log.gz ($(du -h "$OUT/access-$DAY.log.gz" | cut -f1))"
