#!/bin/bash
# mallinfo clamp shim(LD_PRELOAD) 적용 casparcg 에 대한 혼합 부하 장시간 테스트
# 서버에서 sudo fix/mallinfo_clamp/apply.sh 실행 후 사용
#   setsid nohup ./run_clamp_test.sh > clamp_test.out 2>&1 &
cd "$(dirname "$0")"
HOST=${HOST:-10.10.108.108}
SSH_USER=${SSH_USER:-$USER}
LAYERS=${LAYERS:-1-45}
RATE=${RATE:-15}
HOURS=${HOURS:-24}

P=$(ssh -n $SSH_USER@$HOST 'systemctl show casparcg -p MainPID --value')
N=$(ssh -n $SSH_USER@$HOST "grep -c libmallinfo_clamp /proc/$P/maps")
echo "$(date '+%F %T') casparcg pid=$P shim_mappings=$N"
if [ "${N:-0}" -lt 1 ]; then
    echo "mallinfo clamp shim 이 casparcg 메인 프로세스에 로드되지 않았습니다. apply.sh 를 먼저 실행하세요."
    exit 1
fi

echo "$(date '+%F %T') START mode=mixed layers=$LAYERS rate=$RATE hours=$HOURS"
python3 cg_stress.py --mode mixed --layers "$LAYERS" --rate "$RATE" --duration "$HOURS" \
    --log-dir logs_clamp > stress_clamp.out 2>&1
echo "$(date '+%F %T') END exit=$?"
