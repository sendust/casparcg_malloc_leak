---
name: casparcg-cef-stability
description: Long-run stability testing and crash diagnosis for CasparCG Server 2.5 with CEF (HTML templates) on Linux. Use when asked to stress-test CasparCG CG (CG ADD/UPDATE/INVOKE/NEXT/STOP/CLEAR on many layers), monitor casparcg/CEF memory/CPU, analyse a casparcg crash ("traps: MemoryInfra ... invalid opcode in libcef.so", SIGILL), apply or verify the mallinfo clamp shim, or investigate CEF gpu-process memory growth. 트리거 - casparcg 장시간 테스트, CG 부하, casparcg 크래시/덤프 분석, libcef 심볼, mallinfo shim, CEF 메모리 누수.
---

# CasparCG + CEF stability testing and crash diagnosis

The tools live in this repo: `tools/`, `template/`, `fix/mallinfo_clamp/`, `analysis/cef_symbols/`.

Read `README.md` (Korean) or `README.en.md` for the full findings before drawing conclusions.

## Known facts (verified, do not re-derive)

- **The MemoryInfra SIGILL crash is caused by `mallinfo()` overflowing.**
  - Signature: `traps: MemoryInfra ... invalid opcode ... in libcef.so[77260aa]` (CEF 142.0.17).
  - Location: CEF `MallocDumpProvider::OnMemoryDump` → `ReportMallinfoStats`, which computes `checked_cast<size_t>(arena + hblkhd)` from the legacy 32-bit `mallinfo()`.
  - It fires once the main-process glibc malloc total exceeds 2^31.
  - Fix: `fix/mallinfo_clamp` (an `LD_PRELOAD` that overrides only `mallinfo()`). Verified: 96 h with 0 crashes.
- **Never `LD_PRELOAD` jemalloc or tcmalloc into casparcg.**
  - libcef imports `__libc_malloc` and `__libc_free` directly, so the allocators get mixed.
  - Result: a segfault in `free()` at startup, then the systemd StartLimit trips and the service stays down.
- **The CEF gpu-process leaks about +56 MB/h** under heavy template churn and does not release it when idle. Main-process anon grows about +14 MB/h. Recommend scheduled restarts.
- **Escaped quotes in template data are silently dropped.** In CasparCG 2.5.1, JSON data containing `\"` gives a JS SyntaxError in the template and the update is discarded, yet AMCP returns 202. Do not count 202 as "applied".
- **The packaged libcef.so is stripped.** Use the unstripped `Release/libcef.so` from the official CEF minimal build with the same version string; its `.text` is identical.

## Workflow

1. **Preflight on the server**
   - Find the process: `systemctl show casparcg -p MainPID -p NRestarts`.
   - Check the shim is loaded: `grep -c libmallinfo_clamp /proc/$P/maps`.
   - Check for old crash reports in `ls /var/crash`. apport keeps only one report per executable, so move old `.crash` files out first or the next crash will not be captured.
2. **Monitor**: run `tools/caspar_monitor.py --interval 10` on the server with `setsid nohup ... < /dev/null &`.
   - It writes `logs/monitor_v2_YYYYMMDD.csv` with per-process-tree metrics.
   - `main_anon_big/mid/small` split the malloc arenas from the 8–64 MB frame buffers.
   - `cefgpu_*` and `renderer_*` columns cover the CEF children.
3. **Load**: install `template/stress_news.html` into the template folder, then run `tools/cg_stress.py --mode mixed|update|churn --layers 1-20 --rate 15 --duration <h>`.
   - Use `ssh -n` and `setsid nohup` for anything started remotely, otherwise the SSH session hangs.
   - Stop it with `pkill -f "^python3 cg_stress.py"`. The `^` matters: without it the pattern also kills the invoking shell.
4. **Analyse**: `tools/report.py --monitor <csv> --stress '<dir>/stress_*.csv' --warmup 1`.
   - **Filter monitor rows by PID.** Restarts mix instances, and a `PID_CHANGE` event in the monitor CSV means a crash.
   - Report slopes in MB/h after warmup, plus the error, timeout and reconnect totals and the latency trend.
5. **After a crash**
   - Confirm the signature with `journalctl -k | grep -E "traps|segfault"`.
   - Move `/var/crash/*.crash` to an archive directory.
   - Unpack it with `apport-unpack`.
   - Debug it:
     ```
     gdb casparcg-server CoreDump
     add-symbol-file libcef.so.orig -o <libcef load base>
     ```
   - The kernel trap offset is a **file offset**. Convert it to a VMA, and use `analysis/cef_symbols/analyze.sh` as a template.
   - To read `mallinfo` values from the dump, inspect the `struct mallinfo` on the faulting frame (`arena` at +0, `hblkhd` at +0x10).

## Applying the fix

```bash
cd fix/mallinfo_clamp && make && make test   # expect OVERFLOW without the shim and ok with it (~2.7 GB test)
sudo ./apply.sh                              # drop-in + restart + verify; auto-rollback on failure
sudo ./rollback.sh                           # undo
```

Applying the fix restarts casparcg, which interrupts on-air output. Confirm with the user before running `apply.sh` or `rollback.sh`, before restarting the service, and before attaching gdb to the live process (attaching pauses it).

## Reporting

- Report results in the user's language.
- Include:
  - crashes and restarts
  - command, error, timeout and reconnect totals
  - latency p50/p95/p99 trends
  - memory slopes for main anon, cefgpu anon and tree RSS
- State plainly when evidence is not yet conclusive. For example, the shim is not proven until the main malloc area has grown past the old crash level of about 1.3 GB anon, which corresponded to more than 2 GiB of malloc.
