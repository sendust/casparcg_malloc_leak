#!/usr/bin/env python3
"""
CasparCG 서버 리소스 모니터 (CasparCG 가 돌고 있는 리눅스 서버에서 실행).

- casparcg 메인 프로세스 + 모든 자식 프로세스(CEF renderer/gpu 등) 합계
  RSS / PSS(가능하면) / CPU% / 스레드 수 / FD 수 / 프로세스 수
- 시스템 메모리, load average
- nvidia-smi 가 있으면 GPU 사용률, GPU 메모리, 온도
- casparcg PID 변경(재시작/크래시) 감지
- logs/monitor_YYYYMMDD.csv 로 기록 (기본 10초 간격)

stdlib 만 사용, root 권한 불필요 (같은 사용자 또는 root 로 실행하면 FD/PSS 까지 수집).

예)  python3 caspar_monitor.py --interval 10 --name casparcg
"""

import argparse
import os
import shutil
import subprocess
import time
from datetime import datetime

CLK = os.sysconf("SC_CLK_TCK")
PAGE = os.sysconf("SC_PAGE_SIZE")
NCPU = os.cpu_count() or 1


def read(path):
    try:
        with open(path, "rb") as f:
            return f.read().decode("utf-8", "replace")
    except OSError:
        return None


def all_procs():
    """pid -> (ppid, comm, cmdline, utime+stime ticks, rss_bytes, threads)"""
    out = {}
    for d in os.listdir("/proc"):
        if not d.isdigit():
            continue
        st = read("/proc/%s/stat" % d)
        if not st:
            continue
        r = st.rfind(")")
        comm = st[st.find("(") + 1:r]
        f = st[r + 2:].split()
        # f[0]=state f[1]=ppid ... f[11]=utime f[12]=stime f[17]=num_threads f[21]=rss(pages)
        try:
            cmd = (read("/proc/%s/cmdline" % d) or "").replace("\0", " ").strip()
            out[int(d)] = (int(f[1]), comm, cmd, int(f[11]) + int(f[12]), int(f[21]) * PAGE, int(f[17]))
        except (IndexError, ValueError):
            pass
    return out


def find_tree(procs, name):
    """root = name 이 들어간 프로세스 중 부모가 name 이 아닌 것. 자손 전체 반환."""
    match = lambda p: name in procs[p][1].lower() or name in procs[p][2].split(" ")[0].lower()
    cands = [p for p in procs if match(p)]
    roots = [p for p in cands if procs[p][0] not in procs or not match(procs[p][0])]
    # 스크립트(wrapper)가 아닌 실제 바이너리를 우선 - RSS 가장 큰 root
    roots.sort(key=lambda p: -procs[p][4])
    if not roots:
        return None, []
    root = roots[0]
    children = {}
    for p, v in procs.items():
        children.setdefault(v[0], []).append(p)
    tree, stack = [], [root]
    while stack:
        p = stack.pop()
        tree.append(p)
        stack.extend(children.get(p, []))
    return root, tree


def pss_kb(pid):
    s = read("/proc/%d/smaps_rollup" % pid)
    if not s:
        return None
    for ln in s.splitlines():
        if ln.startswith("Pss:"):
            return int(ln.split()[1])
    return None


def smaps_breakdown(pid):
    """main 프로세스 Anonymous 메모리를 매핑 종류/크기로 분류 (MB)."""
    s = read("/proc/%d/smaps" % pid)
    if not s:
        return None
    out = {"anon": 0, "heap": 0, "big": 0, "mid": 0, "small": 0, "file_anon": 0}
    size, name = 0, ""
    for ln in s.splitlines():
        c = ln[0]
        if c in "0123456789abcdef" and "-" in ln.split(" ", 1)[0]:
            parts = ln.split()
            a, b = parts[0].split("-")
            size = int(b, 16) - int(a, 16)
            name = parts[5] if len(parts) > 5 else ""
        elif ln.startswith("Anonymous:"):
            kb = int(ln.split()[1])
            if not kb:
                continue
            out["anon"] += kb
            if name == "[heap]":
                out["heap"] += kb
            elif name == "":
                if size >= 64 << 20:
                    out["big"] += kb          # malloc arena 등 대형 mmap
                elif size >= 8 << 20:
                    out["mid"] += kb          # 프레임 버퍼 크기대 (1080 BGRA ~ 8MB)
                else:
                    out["small"] += kb
            else:
                out["file_anon"] += kb        # 파일/디바이스 매핑의 COW 페이지 등
    return {k: v / 1024.0 for k, v in out.items()}


def rollup(pid, key):
    s = read("/proc/%d/smaps_rollup" % pid)
    if not s:
        return None
    for ln in s.splitlines():
        if ln.startswith(key + ":"):
            return int(ln.split()[1]) / 1024.0
    return None


def fd_count(pid):
    try:
        return len(os.listdir("/proc/%d/fd" % pid))
    except OSError:
        return None


def meminfo():
    m = {}
    for ln in (read("/proc/meminfo") or "").splitlines():
        k, v = ln.split(":", 1)
        m[k] = int(v.split()[0])
    return m


def cpu_total_ticks():
    f = (read("/proc/stat") or "").splitlines()[0].split()[1:]
    return sum(int(x) for x in f)


def gpu_stats():
    if not shutil.which("nvidia-smi"):
        return {}
    try:
        o = subprocess.run(
            ["nvidia-smi", "--query-gpu=utilization.gpu,memory.used,memory.total,temperature.gpu,encoder.stats.sessionCount",
             "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=5).stdout
        first = o.strip().splitlines()[0].split(",")
        return {"gpu_util": first[0].strip(), "gpu_mem_mb": first[1].strip(),
                "gpu_mem_total_mb": first[2].strip(), "gpu_temp": first[3].strip()}
    except Exception:
        return {}


def gpu_proc_mem(pids):
    if not shutil.which("nvidia-smi"):
        return ""
    try:
        o = subprocess.run(["nvidia-smi", "--query-compute-apps=pid,used_memory", "--format=csv,noheader,nounits"],
                           capture_output=True, text=True, timeout=5).stdout
        tot = 0
        for ln in o.strip().splitlines():
            p, m = ln.split(",")
            if int(p) in pids:
                tot += int(m)
        return str(tot)
    except Exception:
        return ""


FIELDS = ["time", "pid", "nproc", "main_rss_mb", "tree_rss_mb", "tree_pss_mb", "main_cpu_pct", "tree_cpu_pct",
          "threads", "fds", "sys_mem_used_mb", "sys_mem_avail_mb", "swap_used_mb", "load1",
          "gpu_util", "gpu_mem_mb", "gpu_proc_mem_mb", "gpu_temp",
          "main_anon_mb", "main_heap_mb", "main_anon_big_mb", "main_anon_mid_mb", "main_anon_small_mb",
          "main_anon_file_mb", "cefgpu_rss_mb", "cefgpu_anon_mb", "renderer_rss_mb", "renderer_count", "event"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", default="casparcg", help="프로세스 이름(소문자 부분일치)")
    ap.add_argument("--interval", type=float, default=10.0)
    ap.add_argument("--log-dir", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs"))
    ap.add_argument("--no-pss", action="store_true", help="PSS 수집 생략(프로세스 많을 때 부하 감소)")
    a = ap.parse_args()
    os.makedirs(a.log_dir, exist_ok=True)

    prev = None  # (wall_ticks, {pid: ticks})
    last_root = None
    print("monitoring '%s' every %.0fs -> %s" % (a.name, a.interval, a.log_dir), flush=True)
    while True:
        t0 = time.monotonic()
        procs = all_procs()
        root, tree = find_tree(procs, a.name)
        total_ticks = cpu_total_ticks()
        event = ""
        if root != last_root:
            event = "START pid=%s" % root if last_root is None else "PID_CHANGE %s->%s" % (last_root, root)
            if root is None:
                event = "PROCESS_NOT_FOUND"
            print(datetime.now().strftime("%F %T"), event, flush=True)
            last_root = root
            prev = None

        row = {k: "" for k in FIELDS}
        row["time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        row["event"] = event
        if root:
            ticks = {p: procs[p][3] for p in tree}
            if prev:
                dwall = (total_ticks - prev[0]) / NCPU  # ticks of one cpu
                if dwall > 0:
                    dmain = ticks[root] - prev[1].get(root, ticks[root])
                    dtree = sum(t - prev[1].get(p, t) for p, t in ticks.items())
                    row["main_cpu_pct"] = "%.1f" % (100.0 * dmain / dwall)   # 100% = 코어 1개
                    row["tree_cpu_pct"] = "%.1f" % (100.0 * dtree / dwall)
            prev = (total_ticks, ticks)
            row["pid"] = root
            row["nproc"] = len(tree)
            row["main_rss_mb"] = "%.1f" % (procs[root][4] / 1048576)
            row["tree_rss_mb"] = "%.1f" % (sum(procs[p][4] for p in tree) / 1048576)
            if not a.no_pss:
                ps = [pss_kb(p) for p in tree]
                if all(x is not None for x in ps):
                    row["tree_pss_mb"] = "%.1f" % (sum(ps) / 1024)
            row["threads"] = sum(procs[p][5] for p in tree)
            fds = [fd_count(p) for p in tree]
            if all(x is not None for x in fds):
                row["fds"] = sum(fds)
            row["gpu_proc_mem_mb"] = gpu_proc_mem(set(tree))
            sb = smaps_breakdown(root)
            if sb:
                row["main_anon_mb"] = "%.1f" % sb["anon"]
                row["main_heap_mb"] = "%.1f" % sb["heap"]
                row["main_anon_big_mb"] = "%.1f" % sb["big"]
                row["main_anon_mid_mb"] = "%.1f" % sb["mid"]
                row["main_anon_small_mb"] = "%.1f" % sb["small"]
                row["main_anon_file_mb"] = "%.1f" % sb["file_anon"]
            rend = [p for p in tree if "--type=renderer" in procs[p][2]]
            row["renderer_count"] = len(rend)
            row["renderer_rss_mb"] = "%.1f" % (sum(procs[p][4] for p in rend) / 1048576)
            gp = [p for p in tree if "--type=gpu-process" in procs[p][2]]
            if gp:
                row["cefgpu_rss_mb"] = "%.1f" % (procs[gp[0]][4] / 1048576)
                v = rollup(gp[0], "Anonymous")
                row["cefgpu_anon_mb"] = "" if v is None else "%.1f" % v
        else:
            prev = None

        m = meminfo()
        row["sys_mem_used_mb"] = "%.0f" % ((m["MemTotal"] - m["MemAvailable"]) / 1024)
        row["sys_mem_avail_mb"] = "%.0f" % (m["MemAvailable"] / 1024)
        row["swap_used_mb"] = "%.0f" % ((m.get("SwapTotal", 0) - m.get("SwapFree", 0)) / 1024)
        row["load1"] = (read("/proc/loadavg") or "").split(" ")[0]
        row.update({k: v for k, v in gpu_stats().items() if k in row})

        path = os.path.join(a.log_dir, "monitor_v2_%s.csv" % datetime.now().strftime("%Y%m%d"))
        new = not os.path.exists(path)
        with open(path, "a") as f:
            if new:
                f.write(",".join(FIELDS) + "\n")
            f.write(",".join(str(row[k]) for k in FIELDS) + "\n")

        time.sleep(max(0.5, a.interval - (time.monotonic() - t0)))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
