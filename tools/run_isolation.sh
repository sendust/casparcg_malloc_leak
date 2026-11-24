#!/bin/bash
# CEF 크래시 원인 분리 테스트: UPDATE 전용 -> CHURN(생성/삭제) 전용 순서로 실행
# 사용: setsid nohup ./run_isolation.sh > isolation.out 2>&1 &
cd "$(dirname "$0")"
HOURS=${HOURS:-12}
LAYERS=${LAYERS:-1-20}
RATE=${RATE:-15}

for mode in update churn; do
    echo "$(date '+%F %T') PHASE START mode=$mode hours=$HOURS layers=$LAYERS rate=$RATE"
    python3 cg_stress.py --mode "$mode" --layers "$LAYERS" --rate "$RATE" --duration "$HOURS" \
        --log-dir "logs_iso_$mode" > "stress_iso_$mode.out" 2>&1
    echo "$(date '+%F %T') PHASE END mode=$mode exit=$?"
done
