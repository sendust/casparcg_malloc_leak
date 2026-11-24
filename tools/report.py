#!/usr/bin/env python3
"""
장기 테스트 결과 분석.

  python3 report.py --monitor 'logs/monitor_*.csv' --stress 'logs/stress_*.csv' [--warmup 1] [--plot out.png]

- 메모리(RSS/PSS/GPU), 스레드, FD 의 선형 증가율(시간당)을 계산 -> 누수 의심 판단
- CPU 평균/최대, PID 변경(크래시/재시작) 횟수
- AMCP 명령 수, 에러율, 타임아웃, 재접속, 지연시간 추이(처음 vs 마지막 구간)
"""

import argparse
import csv
import glob
from datetime import datetime


def load(pattern):
    rows = []
    for p in sorted(glob.glob(pattern)):
        with open(p, encoding="utf-8") as f:
            rows.extend(csv.DictReader(f))
    for r in rows:
        r["_t"] = datetime.strptime(r["time"], "%Y-%m-%d %H:%M:%S")
    rows.sort(key=lambda r: r["_t"])
    return rows


def fnum(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def series(rows, key, t0):
    out = []
    for r in rows:
        v = fnum(r.get(key))
        if v is not None:
            out.append(((r["_t"] - t0).total_seconds() / 3600.0, v))
    return out


def slope(pts):
    n = len(pts)
    if n < 3:
        return None
    mx = sum(p[0] for p in pts) / n
    my = sum(p[1] for p in pts) / n
    den = sum((p[0] - mx) ** 2 for p in pts)
    return sum((p[0] - mx) * (p[1] - my) for p in pts) / den if den else None


def window_avg(pts, lo, hi):
    v = [y for x, y in pts if lo <= x < hi]
    return sum(v) / len(v) if v else None


def fmt(v, nd=1):
    return "-" if v is None else ("%." + str(nd) + "f") % v


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--monitor", default="logs/monitor_*.csv")
    ap.add_argument("--stress", default="logs/stress_*.csv")
    ap.add_argument("--warmup", type=float, default=1.0, help="증가율 계산에서 제외할 초기 시간(h)")
    ap.add_argument("--plot", help="matplotlib 가 있으면 PNG 저장")
    a = ap.parse_args()

    mon = load(a.monitor)
    st = load(a.stress)

    if mon:
        t0, t1 = mon[0]["_t"], mon[-1]["_t"]
        hours = (t1 - t0).total_seconds() / 3600
        print("=== CasparCG 리소스 (%s ~ %s, %.1f h, %d samples) ===" % (t0, t1, hours, len(mon)))
        events = [(r["time"], r["event"]) for r in mon if r.get("event")]
        pidchg = [e for e in events if not e[1].startswith("START")]
        print("PID 변경/프로세스 소실 이벤트: %d" % len(pidchg))
        for e in pidchg[:20]:
            print("   ", *e)
        print("%-18s %10s %10s %10s %10s %14s" % ("metric", "first", "last", "min", "max", "slope/h(warm)"))
        for key in ["main_rss_mb", "tree_rss_mb", "tree_pss_mb", "gpu_proc_mem_mb", "gpu_mem_mb",
                    "threads", "fds", "nproc", "sys_mem_used_mb", "swap_used_mb",
                    "main_anon_mb", "main_heap_mb", "main_anon_big_mb", "main_anon_mid_mb", "main_anon_small_mb",
                    "cefgpu_rss_mb", "cefgpu_anon_mb", "renderer_rss_mb"]:
            pts = series(mon, key, t0)
            if not pts:
                continue
            warm = [p for p in pts if p[0] >= a.warmup] or pts
            first = window_avg(pts, a.warmup, a.warmup + 1) or pts[0][1]
            last = window_avg(pts, hours - 1, hours + 1)
            print("%-18s %10s %10s %10s %10s %14s" % (key, fmt(first), fmt(last), fmt(min(y for _, y in pts)),
                                                     fmt(max(y for _, y in pts)), fmt(slope(warm), 3)))
        for key in ["main_cpu_pct", "tree_cpu_pct", "gpu_util", "load1", "gpu_temp"]:
            pts = series(mon, key, t0)
            if pts:
                v = [y for _, y in pts]
                print("%-18s avg=%s max=%s" % (key, fmt(sum(v) / len(v)), fmt(max(v))))

        rss = [p for p in series(mon, "tree_rss_mb", t0) if p[0] >= a.warmup]
        s = slope(rss)
        if s is not None:
            per_day = s * 24
            verdict = "OK (증가 경향 미미)" if per_day < 50 else ("주의 (하루 %.0f MB 증가)" % per_day if per_day < 300
                                                         else "누수 의심 (하루 %.0f MB 증가)" % per_day)
            print("\n메모리 판정(tree RSS, warmup 이후): %.2f MB/h -> %s" % (s, verdict))

    if st:
        t0 = st[0]["_t"]
        hours = (st[-1]["_t"] - t0).total_seconds() / 3600
        tot = lambda k: sum(int(fnum(r.get(k)) or 0) for r in st)
        cmds, err = tot("cmds"), tot("err")
        print("\n=== AMCP 부하 (%.1f h) ===" % hours)
        print("명령 %d, 에러 %d (%.4f%%), 타임아웃 %d, 재접속 %d, 평균 %.1f cmd/s"
              % (cmds, err, 100.0 * err / max(cmds, 1), tot("timeouts"), tot("reconnects"),
                 cmds / max(hours * 3600, 1)))
        for key in ["lat_p50_ms", "lat_p95_ms", "lat_p99_ms", "lat_max_ms", "add_p95_ms", "update_p95_ms",
                    "clear_p95_ms", "info_ms"]:
            pts = series(st, key, t0)
            if not pts:
                continue
            print("%-14s 처음1h=%7s  마지막1h=%7s  최대=%8s  증가율/h=%s"
                  % (key, fmt(window_avg(pts, 0, 1)), fmt(window_avg(pts, hours - 1, hours + 1)),
                     fmt(max(y for _, y in pts)), fmt(slope(pts), 3)))
        errs = {}
        for r in st:
            for tok in (r.get("err_codes") or "").split():
                k, v = tok.rsplit("=", 1)
                errs[k] = errs.get(k, 0) + int(v)
        if errs:
            print("에러 코드:", ", ".join("%s=%d" % kv for kv in sorted(errs.items(), key=lambda x: -x[1])))

    if a.plot and mon:
        try:
            import matplotlib
            matplotlib.use("Agg")
            import matplotlib.pyplot as plt
        except ImportError:
            print("matplotlib 없음 - plot 생략")
            return
        t0 = mon[0]["_t"]
        fig, ax = plt.subplots(4, 1, figsize=(14, 12), sharex=True)
        for k in ["main_rss_mb", "tree_rss_mb", "tree_pss_mb"]:
            p = series(mon, k, t0)
            if p:
                ax[0].plot(*zip(*p), label=k, lw=0.8)
        ax[0].set_ylabel("MB"); ax[0].legend()
        for k in ["main_cpu_pct", "tree_cpu_pct", "gpu_util"]:
            p = series(mon, k, t0)
            if p:
                ax[1].plot(*zip(*p), label=k, lw=0.6)
        ax[1].set_ylabel("%"); ax[1].legend()
        for k in ["threads", "fds", "gpu_proc_mem_mb"]:
            p = series(mon, k, t0)
            if p:
                ax[2].plot(*zip(*p), label=k, lw=0.8)
        ax[2].legend()
        if st:
            for k in ["lat_p95_ms", "lat_p99_ms"]:
                p = series(st, k, t0)
                if p:
                    ax[3].plot(*zip(*p), label=k, lw=0.8)
            ax[3].set_ylabel("AMCP ms"); ax[3].legend()
        ax[3].set_xlabel("hours")
        fig.tight_layout()
        fig.savefig(a.plot, dpi=100)
        print("plot saved:", a.plot)


if __name__ == "__main__":
    main()
