# CasparCG 2.5 + CEF 142: MemoryInfra SIGILL 크래시 분석과 장시간 CG 부하 시험

[English](README.en.md)

CasparCG Server 2.5.1(Ubuntu 패키지, CEF 142)에서 HTML 템플릿 CG를 장시간 반복 송출하면, 수십 분~수 시간 만에 서버 프로세스가 **SIGILL(invalid opcode in libcef.so, 스레드 `MemoryInfra`)** 로 죽는 문제가 있었습니다.

이 저장소에는 다음 내용이 들어 있습니다.

- 원인 분석 과정과 근거
- 우회 방법(`mallinfo()` clamp shim)
- 부하 시험 도구
- 약 7일간의 측정 결과

## 요약

| 항목 | 내용 |
|---|---|
| 증상 | `traps: MemoryInfra[...] trap invalid opcode ... in libcef.so[77260aa,...]` → casparcg 전체 종료 (systemd 재시작) |
| 원인 | CEF의 `MallocDumpProvider::OnMemoryDump → ReportMallinfoStats` (`base/trace_event/malloc_dump_provider.cc:202`) 가 **32bit glibc `mallinfo()`** 결과로 `checked_cast<size_t>(info.arena + info.hblkhd)` 계산. 메인 프로세스의 glibc malloc 사용량이 **2 GiB(2^31)를 넘으면 int overflow → CHECK 실패 → `ud2` 트랩** |
| 우회 | `mallinfo()` 하나만 가로채 64bit `mallinfo2()` 값을 INT_MAX 이하로 clamp하는 `LD_PRELOAD` 라이브러리 (`fix/mallinfo_clamp`). malloc/free는 건드리지 않음 |
| 검증 | shim 없이: 혼합 부하에서 16분 / 51분 / 64분 만에 크래시. shim 적용: **96시간 연속, 4,346,489 AMCP 명령, 크래시 0, 에러 0**. 이때 메인 malloc 영역(anon)은 크래시 당시(약 1.3 GB)보다 큰 1.97 GB |
| 남은 문제 | **CEF gpu-process 메모리 누수**: 약 +56 MB/h, 부하를 멈춰도 반환되지 않음. 메인 프로세스 anon도 +14 MB/h. 장기 운용 시 주기적 재시작 필요 |

## 시험 환경

| 항목 | 값 |
|---|---|
| OS | Ubuntu 26.04 LTS, headless (X/디스플레이 매니저 없음, EGL + `--ozone-platform=headless`) |
| CasparCG | `casparcg-server-2.5` 2.5.1~stable-resolute1, systemd 서비스 (`Restart=always`) |
| CEF | `casparcg-cef-142` 142.0.17~g60aac24+3 (`/usr/lib/casparcg-cef-142/libcef.so`, 바이너리 RUNPATH로 참조) |
| GPU | NVIDIA RTX 4500 Ada, 드라이버 615.71.09 |
| 출력 | DeckLink 4K Extreme 1채널, 1080i5994, external key |
| 부하 | 채널 1, 레이어 1-20(일부 1-45), 초당 약 12.6개 AMCP 명령. 텍스트는 네이버 뉴스 기사 |

## 크래시 이력 (shim 적용 전)

| 시각 | 조건 | 부하 시작 후 |
|---|---|---|
| 11/10 17:31, 11/15 23:11 | 시험 전 일반 운용 | - |
| 11/19 11:56 | 혼합, 45 레이어 | 16분 |
| 11/19 14:34 | 혼합, 20 레이어 | 64분 |
| 11/20 15:48 | 혼합, 20 레이어 (분리 시험 후 대조군) | 51분 |

모두 같은 시그니처입니다(`MemoryInfra`, libcef.so 파일 오프셋 `0x77260aa`).

### 분리 시험 (shim 없음, 레이어 1-20, 각 12시간)

| 모드 | 동작 | 명령 수 | 크래시 |
|---|---|---|---|
| update | CG ADD 후 UPDATE/INVOKE/NEXT만 반복 | 506,425 | 0 |
| churn | CG ADD/STOP/CLEAR/REMOVE만 반복 | 648,488 | 0 |
| mixed | 둘을 섞음 | 151,969 (3.4h) | **51분에 1회** |

처음에는 "update와 churn이 섞일 때 생기는 경쟁 조건"으로 추정했습니다. 그러나 심볼 분석 결과 **혼합 부하가 메인 프로세스 malloc 사용량을 가장 빨리 2 GiB 넘게 키우기 때문**인 것으로 정정했습니다.

## 원인 분석

1. **심볼 확보**
   - 배포 패키지의 libcef.so는 strip되어 있어 덤프에서 backtrace가 나오지 않았습니다.
   - 패키지 원본 tarball은 CEF 공식 minimal 빌드 `cef_binary_142.0.17+g60aac24+chromium-142.0.7444.176_linux64_minimal`과 같습니다(sha1 일치). 이 tarball의 `Release/libcef.so`는 **strip되지 않았고 `.text`가 설치본과 동일**합니다.
2. **주소 변환**
   - 커널 로그의 `77260aa`는 *파일 오프셋*이고, VMA로는 `0x77270aa`입니다. addr2line에 파일 오프셋을 넣으면 엉뚱한 심볼이 나옵니다.
   - gdb에서 `add-symbol-file libcef.so.orig -o <base>`로 덤프에 심볼을 붙였습니다.
3. **크래시 지점** (`analysis/cef_symbols/analyze.out`, `disasm.txt`)
   ```
   7726f4a: call mallinfo@plt
   7726f4f: mov  0x10(%r15),%eax      ; hblkhd
   7726f53: add  (%r15),%eax          ; + arena  (32bit int)
   7726f56: js   77270aa              ; 음수면 → ud2
   ...
   77270aa: ud2   <MallocDumpProvider::OnMemoryDump+458>
            ; base/numerics/safe_conversions.h:104 ← malloc_dump_provider.cc:202 ReportMallinfoStats
   ```
4. **덤프의 실제 값**

   | 덤프 | arena | hblkhd | 합계 |
   |---|---|---|---|
   | 11/19 11:56 | 1,523,879,936 | 942,735,360 | 2,466,615,296 (> 2^31) |
   | 11/20 15:48 | 1,763,868,672 | 387,166,208 | 2,151,034,880 (> 2^31) |

5. **재현**: `fix/mallinfo_clamp/mi_big.c`로 약 2.7 GB를 malloc하면 glibc `mallinfo()`의 `arena + hblkhd`가 음수가 됩니다.
   - 9 GB처럼 너무 크게 잡으면 int가 두 바퀴 돌아 양수로 보일 수 있으니 주의하세요.
6. **왜 2 GiB까지 커지나**
   - Chromium은 `mallinfo2`가 있으면 그것을 쓰지만, 이 CEF 빌드는 legacy `mallinfo()`로 빌드되었습니다.
   - libcef 자체 할당은 PartitionAlloc이지만, casparcg 메인 프로세스(믹서, 프레임 버퍼, ffmpeg, boost 등)의 glibc malloc 합계가 같이 집계됩니다.
   - 장시간 운용하면 이 합계가 2 GiB를 넘습니다.

## 우회 방법: mallinfo clamp shim

`fix/mallinfo_clamp/mallinfo_clamp.c`

- **바꾸는 것:** `mallinfo()` 하나만 재정의합니다. 내부에서 `mallinfo2()`를 읽어 모든 필드와 `arena + hblkhd`가 `INT_MAX`를 넘지 않게 clamp합니다.
- **효과:** 결과는 메모리 *통계* 값만 잘립니다(MemoryInfra 트레이싱 수치). 동작에는 영향이 없습니다.
- **적용 가능 근거:** libcef는 `mallinfo@GLIBC_2.2.5`를 동적 import하므로 `LD_PRELOAD`로 가로챌 수 있습니다.

```bash
cd fix/mallinfo_clamp
make            # libmallinfo_clamp.so, mi_big
make test       # glibc 기본: OVERFLOW / shim 적용: ok 확인
sudo ./apply.sh # /usr/local/lib 설치 + systemd drop-in + 재시작, 실패 시 자동 원복
```

`apply.sh`는 다음을 수행합니다.

- `/usr/local/lib/libmallinfo_clamp.so` 설치
- `/etc/systemd/system/casparcg.service.d/mallinfo-clamp.conf` 생성
  ```ini
  [Service]
  Environment=LD_PRELOAD=/usr/local/lib/libmallinfo_clamp.so
  ```
- `daemon-reload` → `restart` → 15초 뒤 shim이 메인 프로세스에 매핑됐는지 확인하고, 실패하면 원복

**확인 방법**
```bash
P=$(systemctl show casparcg -p MainPID --value)
grep -c libmallinfo_clamp /proc/$P/maps            # 1 이상
tr '\0' '\n' < /proc/$P/environ | grep LD_PRELOAD
```

**원복**: `sudo fix/mallinfo_clamp/rollback.sh`

**systemd 없이 직접 실행할 때**
```bash
LD_PRELOAD=/usr/local/lib/libmallinfo_clamp.so \
  /usr/bin/casparcg-server-2.5 /usr/share/casparcg-server-2.5/casparcg.config
```

> ⚠️ **jemalloc / tcmalloc 같은 allocator `LD_PRELOAD`는 사용하지 마세요.**
> libcef는 Chromium allocator shim을 통해 `__libc_malloc` / `__libc_free`를 직접 import합니다. 그래서 allocator를 통째로 바꾸면 할당자가 섞여 **기동 직후 `free()`에서 segfault**가 납니다(실제로 시도했다가 서비스가 StartLimit에 걸려 내려갔습니다).

## shim 적용 후 결과

### 24시간 + 72시간 (같은 프로세스 PID 564777, 혼합, 레이어 1-20)

| 항목 | 24h | 72h |
|---|---|---|
| AMCP 명령 | 1,086,604 | 3,259,885 |
| 에러 / 타임아웃 / 재접속 | 0 / 0 / 0 | 0 / 0 / 0 |
| 크래시 / 재시작 | 0 / 0 | 0 / 0 |
| 지연 p50 / p95 | 0.9 / 1.3 ms | 0.7–0.9 / 1.1–1.3 ms |
| CPU 메인 / 전체 | 83% / 280% | 83% / 275% |
| GPU 사용률 | 평균 77% | 평균 73% |

### 메모리 (72h 구간, `results/summary_clamp_72h.txt`)

| 항목 | 시작 | 72h 후 | 기울기 | 부하 종료 후 12h idle |
|---|---|---|---|---|
| 메인 anon (malloc 영역) | 975 MB | 1,969 MB | +14 MB/h | 1,984 MB |
| **CEF gpu-process anon** | 1,500 MB | 5,466 MB | **+56 MB/h** | **5,493 MB (반환 안 됨)** |
| 프로세스 트리 RSS | 10.6 GB | 15.6 GB | +72 MB/h | 12.4 GB |
| renderer RSS (20개) | 2.58 GB | 2.57 GB | 0 | 76 MB (1개) |
| 스레드 / FD | 275 / 1,698 | 275 / 1,711 | 0 | 102 / 415 |

ADD의 꼬리 지연은 조금씩 늘었습니다(p95 28 → 50 ms, p99 5.5 → 27 ms, 최대 980 ms).

### 결론

1. **MemoryInfra SIGILL 크래시는 mallinfo clamp shim으로 해결됩니다.** 메인 malloc 영역이 크래시 시점보다 50% 이상 큰 상태로 96시간 무사고였습니다.
2. **CEF gpu-process는 템플릿 생성/삭제가 누적될수록 메모리가 선형으로 늘고, idle에서도 반환되지 않습니다.**
   - 이 시험 부하(초당 12.6개 명령, 레이어 20개 상시 교체) 기준으로 하루 약 1.35 GB(트리 전체 약 1.7 GB)입니다.
   - 실제 운용 부하는 훨씬 작지만, 장기 무정지 운용이라면 **비방송 시간대 정기 재시작**(예: 주 1회 `systemctl restart casparcg`)과 메모리 경보를 권장합니다.
3. CasparCG나 CEF 패키지를 업데이트한 뒤에는 shim이 여전히 로드되는지 다시 확인하세요. 근본 해결은 CEF를 `mallinfo2` 사용으로 빌드하는 것입니다(업스트림 보고 대상).

## 부수 발견

- **템플릿 데이터 문제:** CasparCG 2.5.1은 CG UPDATE/ADD 데이터 JSON 안에 `\"`(이스케이프된 큰따옴표)가 있으면 템플릿 JS에서 `SyntaxError`가 납니다. 이 경우 업데이트가 조용히 버려지는데 AMCP 응답은 `202 OK`입니다.
- **캐시 경로:** config의 `<cache-path>/mnt/data/cef-cache</cache-path>`가 적용되지 않고, 실제로는 `--user-data-dir=/home/<user>/cef-cache`를 씁니다. CEF History DB(약 37 MB)는 템플릿을 로드할 때마다 커집니다.
- **dbus 로그:** headless 환경이라 `Failed to connect to the bus ... without a $DISPLAY` 로그가 반복되지만 동작에는 영향이 없습니다.

## 저장소 구성

```
tools/
  cg_stress.py         # 부하 발생기 (stdlib만 사용). 네이버 뉴스 수집 → CG ADD/UPDATE/INVOKE/NEXT/STOP/CLEAR/REMOVE
  caspar_monitor.py    # 서버에서 실행. casparcg + CEF 자식 프로세스 RSS/PSS/CPU/스레드/FD, malloc 영역 분류, GPU
  report.py            # 로그 분석 (MB/h 기울기, 지연 추이, 크래시/PID 변경)
  run_isolation.sh     # update → churn 분리 시험
  run_clamp_test.sh    # shim 로드 확인 후 혼합 부하 (HOST, SSH_USER, LAYERS, RATE, HOURS 환경변수)
template/stress_news.html  # 레이어 번호로 5x9 격자에 배치되는 뉴스 카드 HTML 템플릿
fix/mallinfo_clamp/        # shim 소스, Makefile, systemd drop-in, apply/rollback, 재현 프로그램
analysis/cef_symbols/      # gdb/objdump 분석 스크립트와 결과 (심볼 파일 자체는 1.5 GB라 미포함)
results/
  server_monitor/          # 서버 모니터 CSV (10초 간격, monitor_v2 = malloc 영역 분류 포함)
  load_stats/<phase>/      # 부하 발생기 1분 통계 CSV
  summary_*.txt            # report.py 결과
.claude/skills/casparcg-cef-stability/SKILL.md   # Claude Code skill (이 시험·분석 절차)
```

## 시험 재현

```bash
# 1) 템플릿을 CasparCG template 폴더로 복사 (fonts/NotoSansKR 필요)
# 2) 서버에서 모니터 실행
nohup python3 tools/caspar_monitor.py --interval 10 > monitor.out 2>&1 &
# 3) 부하 PC에서
python3 tools/cg_stress.py --host <server> --channel 1 --layers 1-20 --rate 15 --mode mixed --duration 72 --log-dir logs
# 4) 분석 (프로세스가 재시작된 경우 PID로 필터링하고 warmup은 1h 이상)
python3 tools/report.py --monitor 'logs/monitor_v2_*.csv' --stress 'logs/stress_*.csv' --warmup 1
```

`--mode`는 `mixed`(기본), `update`, `churn` 중 하나입니다. 종료는 `kill <pid>`(SIGTERM)로 하며, 사용한 레이어를 CLEAR한 뒤 끝납니다.
