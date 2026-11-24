#!/bin/bash
# 사용: sudo fix/mallinfo_clamp/rollback.sh
set -e
[ "$(id -u)" = 0 ] || { echo "sudo 로 실행하세요"; exit 1; }
rm -f /etc/systemd/system/casparcg.service.d/mallinfo-clamp.conf /etc/systemd/system/casparcg.service.d/jemalloc.conf
systemctl daemon-reload
systemctl reset-failed casparcg || true
systemctl restart casparcg
sleep 8
systemctl show casparcg -p Environment -p MainPID -p ActiveState
