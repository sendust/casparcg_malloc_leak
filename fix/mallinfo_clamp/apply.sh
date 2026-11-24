#!/bin/bash
# 사용: sudo fix/mallinfo_clamp/apply.sh
# jemalloc 설정을 제거하고 mallinfo clamp shim 을 적용. 기동 실패 시 자동 원복.
set -e
[ "$(id -u)" = 0 ] || { echo "sudo 로 실행하세요"; exit 1; }
D=/etc/systemd/system/casparcg.service.d
H="$(dirname "$(readlink -f "$0")")"
install -m 755 "$H/libmallinfo_clamp.so" /usr/local/lib/libmallinfo_clamp.so
install -d $D
rm -f $D/jemalloc.conf
install -m 644 "$H/mallinfo-clamp.conf" $D/mallinfo-clamp.conf
systemctl daemon-reload
systemctl reset-failed casparcg || true
systemctl restart casparcg
sleep 15
P=$(systemctl show casparcg -p MainPID --value)
if [ "$(systemctl is-active casparcg)" != active ] || [ "$P" = 0 ] || ! grep -q libmallinfo_clamp /proc/$P/maps; then
    echo "!! 기동 실패 또는 shim 미적용 -> 원복합니다"
    rm -f $D/mallinfo-clamp.conf
    systemctl daemon-reload; systemctl reset-failed casparcg || true; systemctl restart casparcg
    sleep 8; systemctl show casparcg -p MainPID -p ActiveState
    exit 1
fi
echo "OK casparcg pid=$P active, shim mapped: $(grep -c libmallinfo_clamp /proc/$P/maps)"
systemctl show casparcg -p Environment
