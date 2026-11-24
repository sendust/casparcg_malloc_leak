# CasparCG 2.5 + CEF 142: MemoryInfra SIGILL crash analysis and long-run CG stress test

[한국어](README.md)

CasparCG Server 2.5.1 (Ubuntu packages, CEF 142) died after tens of minutes to a few hours of continuous HTML-template CG playout. The cause was **SIGILL (invalid opcode in libcef.so, thread `MemoryInfra`)**.

This repository contains:

- the root-cause analysis and the evidence for it
- a workaround: a `mallinfo()` clamp shim
- the load-test tools
- about 7 days of measurements

## TL;DR

| | |
|---|---|
| Symptom | `traps: MemoryInfra[...] trap invalid opcode ... in libcef.so[77260aa,...]` → the whole casparcg process exits; systemd restarts it |
| Root cause | CEF's `MallocDumpProvider::OnMemoryDump → ReportMallinfoStats` (`base/trace_event/malloc_dump_provider.cc:202`) calls the **legacy 32-bit glibc `mallinfo()`** and does `checked_cast<size_t>(info.arena + info.hblkhd)`. Once the main process's glibc malloc total exceeds **2 GiB (2^31)**, the int overflows, the CHECK fails, and the `ud2` trap fires |
| Workaround | An `LD_PRELOAD` library that overrides **only `mallinfo()`**. It reads the 64-bit `mallinfo2()` and clamps every value to INT_MAX (`fix/mallinfo_clamp`). malloc/free are untouched |
| Verification | Without the shim, mixed load crashed after 16 / 51 / 64 min. With the shim: **96 h continuous, 4,346,489 AMCP commands, 0 crashes, 0 errors**. During that run the main malloc anon reached 1.97 GB, more than at the crashes (~1.3 GB) |
| Remaining issue | **The CEF gpu-process leaks memory**: about +56 MB/h, not released after the load stops. Main-process anon also grows about +14 MB/h. Periodic restarts are needed for long unattended operation |

## Environment

| | |
|---|---|
| OS | Ubuntu 26.04 LTS, headless (no X or display manager; EGL + `--ozone-platform=headless`) |
| CasparCG | `casparcg-server-2.5` 2.5.1~stable-resolute1, systemd service (`Restart=always`) |
| CEF | `casparcg-cef-142` 142.0.17~g60aac24+3 (`/usr/lib/casparcg-cef-142/libcef.so`, resolved through the binary's RUNPATH) |
| GPU | NVIDIA RTX 4500 Ada, driver 615.71.09 |
| Output | one DeckLink 4K Extreme channel, 1080i5994, external key |
| Load | channel 1, layers 1-20 (1-45 in early runs), about 12.6 AMCP commands/s, text from Korean news articles |

## Crash history (before the shim)

| Time | Condition | Time into load |
|---|---|---|
| 11/10 17:31, 11/15 23:11 | normal use, before testing | - |
| 11/19 11:56 | mixed, 45 layers | 16 min |
| 11/19 14:34 | mixed, 20 layers | 64 min |
| 11/20 15:48 | mixed, 20 layers (control run after isolation) | 51 min |

All crashes have the same signature: thread `MemoryInfra`, libcef.so file offset `0x77260aa`.

### Isolation test (no shim, layers 1-20, 12 h each)

| Mode | Actions | Commands | Crashes |
|---|---|---|---|
| update | CG ADD, then only UPDATE/INVOKE/NEXT | 506,425 | 0 |
| churn | only CG ADD/STOP/CLEAR/REMOVE | 648,488 | 0 |
| mixed | both | 151,969 (3.4 h) | **1, at 51 min** |

We first suspected a race between updates and create/destroy. Symbol analysis corrected this: **mixed load simply drives the main-process malloc total past 2 GiB the fastest**.

## Analysis

1. **Getting symbols**
   - The packaged libcef.so is stripped, so the core dumps gave no backtrace.
   - The package's orig tarball matches the official CEF minimal build `cef_binary_142.0.17+g60aac24+chromium-142.0.7444.176_linux64_minimal` (same sha1).
   - That build's `Release/libcef.so` is **unstripped and has an identical `.text`** to the installed library.
2. **Translating the address**
   - The kernel's `77260aa` is a *file offset*; the VMA is `0x77270aa`. Feeding the file offset to addr2line gives a wrong symbol.
   - In gdb, we attached symbols to the dump with `add-symbol-file libcef.so.orig -o <base>`.
3. **Crash site** (`analysis/cef_symbols/analyze.out`, `disasm.txt`):
   ```
   7726f4a: call mallinfo@plt
   7726f4f: mov  0x10(%r15),%eax      ; hblkhd
   7726f53: add  (%r15),%eax          ; + arena  (32-bit int)
   7726f56: js   77270aa              ; negative → ud2
   ...
   77270aa: ud2   <MallocDumpProvider::OnMemoryDump+458>
            ; base/numerics/safe_conversions.h:104 ← malloc_dump_provider.cc:202 ReportMallinfoStats
   ```
4. **Values in the core dumps**

   | Dump | arena | hblkhd | sum |
   |---|---|---|---|
   | 11/19 11:56 | 1,523,879,936 | 942,735,360 | 2,466,615,296 (> 2^31) |
   | 11/20 15:48 | 1,763,868,672 | 387,166,208 | 2,151,034,880 (> 2^31) |

5. **Reproduction**: `fix/mallinfo_clamp/mi_big.c` allocates about 2.7 GB, and glibc `mallinfo()` then returns a negative `arena + hblkhd`.
   - Do not allocate too much: at 9 GB, for example, the int wraps twice and looks positive again.
6. **Why the total gets that large**
   - Chromium uses `mallinfo2` when it is available, but this CEF build was compiled to use legacy `mallinfo()`.
   - libcef's own allocations go to PartitionAlloc, but the counter includes all glibc malloc in the casparcg main process (mixer, frame buffers, ffmpeg, boost, and so on).
   - Over a long run, that total crosses 2 GiB.

## Workaround: mallinfo clamp shim

`fix/mallinfo_clamp/mallinfo_clamp.c`

- **What it changes:** it redefines only `mallinfo()`. It reads `mallinfo2()` and clamps every field, and `arena + hblkhd`, to `INT_MAX`.
- **Effect:** only the MemoryInfra *statistics* are truncated; behaviour is unchanged.
- **Why it works:** libcef imports `mallinfo@GLIBC_2.2.5` dynamically, so `LD_PRELOAD` can interpose it.

```bash
cd fix/mallinfo_clamp
make            # libmallinfo_clamp.so, mi_big
make test       # plain glibc: OVERFLOW / with shim: ok
sudo ./apply.sh # install to /usr/local/lib + systemd drop-in + restart; auto-rollback on failure
```

`apply.sh` does the following:

- installs `/usr/local/lib/libmallinfo_clamp.so`
- creates `/etc/systemd/system/casparcg.service.d/mallinfo-clamp.conf`:
  ```ini
  [Service]
  Environment=LD_PRELOAD=/usr/local/lib/libmallinfo_clamp.so
  ```
- runs `daemon-reload`, then `restart`, then checks after 15 s that the shim is mapped in the main process, and rolls back if it is not

**Check that it is loaded:**
```bash
P=$(systemctl show casparcg -p MainPID --value)
grep -c libmallinfo_clamp /proc/$P/maps            # >= 1
tr '\0' '\n' < /proc/$P/environ | grep LD_PRELOAD
```

**Roll back:** `sudo fix/mallinfo_clamp/rollback.sh`

**Run without systemd:**
```bash
LD_PRELOAD=/usr/local/lib/libmallinfo_clamp.so \
  /usr/bin/casparcg-server-2.5 /usr/share/casparcg-server-2.5/casparcg.config
```

> ⚠️ **Do not `LD_PRELOAD` a whole allocator (jemalloc, tcmalloc, ...).**
> libcef imports `__libc_malloc` / `__libc_free` directly through Chromium's allocator shim. Swapping the allocator mixes allocators, and casparcg **segfaults in `free()` right at startup**. We tried it, and the service hit its StartLimit and stayed down.

## Results with the shim

### 24 h + 72 h (same process, PID 564777, mixed, layers 1-20)

| | 24 h | 72 h |
|---|---|---|
| AMCP commands | 1,086,604 | 3,259,885 |
| errors / timeouts / reconnects | 0 / 0 / 0 | 0 / 0 / 0 |
| crashes / restarts | 0 / 0 | 0 / 0 |
| latency p50 / p95 | 0.9 / 1.3 ms | 0.7–0.9 / 1.1–1.3 ms |
| CPU main / tree | 83% / 280% | 83% / 275% |
| GPU util | avg 77% | avg 73% |

### Memory (72 h run, `results/summary_clamp_72h.txt`)

| | start | after 72 h | slope | 12 h idle after load |
|---|---|---|---|---|
| main anon (malloc) | 975 MB | 1,969 MB | +14 MB/h | 1,984 MB |
| **CEF gpu-process anon** | 1,500 MB | 5,466 MB | **+56 MB/h** | **5,493 MB (not released)** |
| process tree RSS | 10.6 GB | 15.6 GB | +72 MB/h | 12.4 GB |
| renderer RSS (20) | 2.58 GB | 2.57 GB | 0 | 76 MB (1) |
| threads / FDs | 275 / 1,698 | 275 / 1,711 | 0 | 102 / 415 |

ADD tail latency crept up: p95 went from 28 to 50 ms, p99 from 5.5 to 27 ms, and the max reached 980 ms.

### Conclusions

1. **The mallinfo clamp shim fixes the MemoryInfra SIGILL crash.** The process ran 96 h without a crash while its main malloc area was more than 50% larger than at the crashes.
2. **The CEF gpu-process grows linearly with the number of template create/destroy cycles and does not release memory when idle.**
   - Under this test load (12.6 cmd/s, 20 layers constantly recycled) that is about 1.35 GB/day, or about 1.7 GB/day for the whole tree.
   - Real playout is much lighter, but for long unattended operation we recommend a **scheduled restart outside air time** (for example weekly `systemctl restart casparcg`) plus a memory alert.
3. After upgrading the CasparCG or CEF packages, re-check that the shim is still loaded. The proper fix is to build CEF to use `mallinfo2`; this should be reported upstream.

## Side findings

- **Template data:** CasparCG 2.5.1 HTML templates throw a JS `SyntaxError` when the CG UPDATE/ADD JSON contains an escaped quote (`\"`). The update is silently dropped, but AMCP still answers `202 OK`.
- **Cache path:** `<cache-path>/mnt/data/cef-cache</cache-path>` in the config is not applied; CEF uses `--user-data-dir=/home/<user>/cef-cache` instead. The CEF History DB (about 37 MB) grows with every template load.
- **dbus log noise:** because the server is headless, `Failed to connect to the bus ... without a $DISPLAY` is logged repeatedly. It does not affect operation.

## Repository layout

```
tools/
  cg_stress.py         # load generator (stdlib only): news text → CG ADD/UPDATE/INVOKE/NEXT/STOP/CLEAR/REMOVE
  caspar_monitor.py    # runs on the server: casparcg + CEF children RSS/PSS/CPU/threads/FDs, malloc breakdown, GPU
  report.py            # log analysis (MB/h slopes, latency trend, crashes/PID changes)
  run_isolation.sh     # update-only then churn-only isolation test
  run_clamp_test.sh    # verifies the shim is loaded, then runs mixed load (env: HOST, SSH_USER, LAYERS, RATE, HOURS)
template/stress_news.html  # news-card HTML template placed on a 5x9 grid by layer number
fix/mallinfo_clamp/        # shim source, Makefile, systemd drop-in, apply/rollback, reproducer
analysis/cef_symbols/      # gdb/objdump script and output (the 1.5 GB symbol file is not included)
results/
  server_monitor/          # server monitor CSVs (10 s; monitor_v2 has the malloc breakdown)
  load_stats/<phase>/      # load generator per-minute stats
  summary_*.txt            # report.py output
.claude/skills/casparcg-cef-stability/SKILL.md   # Claude Code skill describing this procedure
```

## Reproducing the test

```bash
# 1) copy template/stress_news.html into the CasparCG template folder (uses fonts/NotoSansKR)
# 2) on the server
nohup python3 tools/caspar_monitor.py --interval 10 > monitor.out 2>&1 &
# 3) on the load PC
python3 tools/cg_stress.py --host <server> --channel 1 --layers 1-20 --rate 15 --mode mixed --duration 72 --log-dir logs
# 4) analyse (filter by PID if the process restarted; use a warmup of at least 1 h)
python3 tools/report.py --monitor 'logs/monitor_v2_*.csv' --stress 'logs/stress_*.csv' --warmup 1
```

`--mode` is one of `mixed` (default), `update` or `churn`. Stop the generator with `kill <pid>` (SIGTERM); it clears the layers it used before exiting.
