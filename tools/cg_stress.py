#!/usr/bin/env python3
"""
CasparCG CG long-run stress tester.

- Naver 뉴스(한글 기사)를 주기적으로 수집해서 기사 풀을 만든다.
- 채널 1, 레이어 1-45 에 CG ADD / UPDATE / INVOKE / NEXT / STOP / REMOVE / CLEAR
  를 상태머신으로 무한 반복 전송한다.
- 명령별 응답 지연시간, 에러, 타임아웃, 재접속을 집계하여 CSV 로 남긴다.
  (logs/stress_YYYYMMDD.csv : 1분 단위 통계, logs/events_YYYYMMDD.log : 에러/이벤트)

stdlib 만 사용 (python 3.8+).

예)
  python3 cg_stress.py --host 10.10.108.108 --channel 1 --layers 1-45 --rate 15
"""

import argparse
import html
import json
import os
import random
import re
import signal
import socket
import threading
import time
import urllib.request
import xml.etree.ElementTree as ET
from collections import defaultdict, deque
from datetime import datetime

# ----------------------------------------------------------------------------
# Logging helpers (daily rotating files)
# ----------------------------------------------------------------------------

LOG_DIR = "logs"


def _today():
    return datetime.now().strftime("%Y%m%d")


def log_event(msg):
    line = "%s %s" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3], msg)
    print(line, flush=True)
    try:
        with open(os.path.join(LOG_DIR, "events_%s.log" % _today()), "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


# ----------------------------------------------------------------------------
# Naver news fetcher
# ----------------------------------------------------------------------------

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
SECTIONS = [100, 101, 102, 103, 104, 105]  # 정치 경제 사회 생활/문화 세계 IT/과학
RE_LINK = re.compile(r"https://n\.news\.naver\.com/mnews/article/(\d+)/(\d+)")
RE_TITLE = re.compile(r'id="title_area"[^>]*>(.*?)</h2>', re.S)
RE_BODY = re.compile(r'id="dic_area"[^>]*>(.*?)</article>', re.S)
RE_PRESS = re.compile(r'<meta property="og:article:author" content="([^"]*)"')
RE_TAG = re.compile(r"<[^>]+>")
RE_DROP = re.compile(r"<(script|style)[^>]*>.*?</\1>|<em class=\"img_desc\">.*?</em>", re.S | re.I)
RE_WS = re.compile(r"\s+")

FALLBACK = [
    {"title": "기사 수집 대기 중 - 기본 한글 문장", "body": "다람쥐 헌 쳇바퀴에 타고파. 키스의 고유조건은 입술끼리 만나야 하고 특별한 기술은 필요치 않다.", "press": "local"},
    {"title": "CasparCG 장시간 안정성 테스트", "body": "채널 1 레이어 1번부터 45번까지 CG ADD, UPDATE, STOP, CLEAR 명령을 반복 전송합니다.", "press": "local"},
]


def http_get(url, timeout=10):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "ko-KR,ko;q=0.9"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def clean_text(s):
    s = RE_DROP.sub(" ", s)
    s = s.replace("<br>", " ").replace("<br/>", " ").replace("<br />", " ")
    s = RE_TAG.sub(" ", s)
    s = html.unescape(s)
    return RE_WS.sub(" ", s).strip()


class NewsPool:
    def __init__(self, refresh_sec=600, per_refresh=30, max_pool=400):
        self.refresh_sec = refresh_sec
        self.per_refresh = per_refresh
        self.pool = deque(maxlen=max_pool)
        self.seen = set()
        self.lock = threading.Lock()
        self.stop_evt = threading.Event()
        self.fetch_ok = 0
        self.fetch_fail = 0

    def start(self):
        t = threading.Thread(target=self._run, name="news", daemon=True)
        t.start()

    def _run(self):
        while not self.stop_evt.is_set():
            try:
                self.refresh()
            except Exception as e:  # never die
                log_event("NEWS refresh error: %r" % e)
            self.stop_evt.wait(self.refresh_sec)

    def refresh(self):
        links = []
        for sec in SECTIONS:
            try:
                page = http_get("https://news.naver.com/section/%d" % sec)
                for m in RE_LINK.finditer(page):
                    links.append(m.group(0))
            except Exception as e:
                log_event("NEWS section %d fail: %r" % (sec, e))
            time.sleep(0.5)
        links = [u for u in dict.fromkeys(links) if u not in self.seen]
        random.shuffle(links)
        added = 0
        for url in links[: self.per_refresh]:
            if self.stop_evt.is_set():
                break
            try:
                page = http_get(url)
                mt, mb = RE_TITLE.search(page), RE_BODY.search(page)
                if not mt or not mb:
                    continue
                title, body = clean_text(mt.group(1)), clean_text(mb.group(1))
                if not title or len(body) < 20:
                    continue
                mp = RE_PRESS.search(page)
                art = {"title": title, "body": body, "press": html.unescape(mp.group(1)).split("|")[0].strip() if mp else ""}
                with self.lock:
                    self.pool.append(art)
                self.seen.add(url)
                added += 1
                self.fetch_ok += 1
            except Exception as e:
                self.fetch_fail += 1
                log_event("NEWS article fail %s: %r" % (url, e))
            time.sleep(0.3)
        if len(self.seen) > 5000:
            self.seen.clear()
        log_event("NEWS refresh: +%d articles, pool=%d" % (added, len(self.pool)))

    def pick(self):
        with self.lock:
            if self.pool:
                return random.choice(self.pool)
        return random.choice(FALLBACK)


# ----------------------------------------------------------------------------
# AMCP client
# ----------------------------------------------------------------------------

class AmcpError(Exception):
    pass


class Amcp:
    def __init__(self, host, port, timeout):
        self.host, self.port, self.timeout = host, port, timeout
        self.sock = None
        self.buf = b""

    def connect(self):
        self.close()
        s = socket.create_connection((self.host, self.port), timeout=self.timeout)
        s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        s.settimeout(self.timeout)
        self.sock = s
        self.buf = b""

    def close(self):
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
        self.sock = None

    def _readline(self, deadline):
        while b"\r\n" not in self.buf:
            remain = deadline - time.monotonic()
            if remain <= 0:
                raise socket.timeout("amcp response timeout")
            self.sock.settimeout(remain)
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("server closed connection")
            self.buf += chunk
        line, self.buf = self.buf.split(b"\r\n", 1)
        return line.decode("utf-8", "replace")

    def cmd(self, command):
        """Send one AMCP command, return (code:int, lines:list[str], latency_ms)."""
        if not self.sock:
            raise ConnectionError("not connected")
        t0 = time.monotonic()
        deadline = t0 + self.timeout
        self.sock.sendall(command.encode("utf-8") + b"\r\n")
        first = self._readline(deadline)
        try:
            code = int(first[:3])
        except ValueError:
            raise AmcpError("bad response: %r" % first[:200])
        lines = [first]
        if code == 200:           # multi-line, terminated by empty line
            while True:
                ln = self._readline(deadline)
                if ln == "":
                    break
                lines.append(ln)
        elif code == 201:         # exactly one data line
            lines.append(self._readline(deadline))
        return code, lines, (time.monotonic() - t0) * 1000.0


def amcp_quote(s):
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


# ----------------------------------------------------------------------------
# Statistics
# ----------------------------------------------------------------------------

class Stats:
    FIELDS = ["time", "elapsed_h", "cmds", "ok", "err", "timeouts", "reconnects",
              "active_layers", "info_layers", "news_pool",
              "lat_avg_ms", "lat_p50_ms", "lat_p95_ms", "lat_p99_ms", "lat_max_ms",
              "add_p95_ms", "update_p95_ms", "clear_p95_ms", "info_ms", "err_codes"]

    def __init__(self):
        self.t_start = time.time()
        self.total = defaultdict(int)
        self.reset()

    def reset(self):
        self.lat = []
        self.lat_by = defaultdict(list)
        self.cnt = defaultdict(int)
        self.err_codes = defaultdict(int)
        self.info_ms = ""
        self.info_layers = ""

    def add(self, kind, code, lat_ms):
        self.cnt["cmds"] += 1
        self.total["cmds"] += 1
        if code is not None and code < 300:
            self.cnt["ok"] += 1
            self.lat.append(lat_ms)
            self.lat_by[kind].append(lat_ms)
        else:
            self.cnt["err"] += 1
            self.total["err"] += 1
            self.err_codes["%s:%s" % (kind, code)] += 1

    def event(self, name):
        self.cnt[name] += 1
        self.total[name] += 1

    @staticmethod
    def pct(v, p):
        if not v:
            return ""
        v = sorted(v)
        return "%.1f" % v[min(len(v) - 1, int(len(v) * p / 100.0))]

    def flush(self, active_layers, news_pool):
        row = {
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "elapsed_h": "%.3f" % ((time.time() - self.t_start) / 3600.0),
            "cmds": self.cnt["cmds"], "ok": self.cnt["ok"], "err": self.cnt["err"],
            "timeouts": self.cnt["timeouts"], "reconnects": self.cnt["reconnects"],
            "active_layers": active_layers, "info_layers": self.info_layers, "news_pool": news_pool,
            "lat_avg_ms": "%.1f" % (sum(self.lat) / len(self.lat)) if self.lat else "",
            "lat_p50_ms": self.pct(self.lat, 50), "lat_p95_ms": self.pct(self.lat, 95),
            "lat_p99_ms": self.pct(self.lat, 99),
            "lat_max_ms": "%.1f" % max(self.lat) if self.lat else "",
            "add_p95_ms": self.pct(self.lat_by["ADD"], 95),
            "update_p95_ms": self.pct(self.lat_by["UPDATE"], 95),
            "clear_p95_ms": self.pct(self.lat_by["CLEAR"] + self.lat_by["CG_CLEAR"], 95),
            "info_ms": self.info_ms,
            "err_codes": " ".join("%s=%d" % kv for kv in sorted(self.err_codes.items())),
        }
        path = os.path.join(LOG_DIR, "stress_%s.csv" % _today())
        new = not os.path.exists(path)
        with open(path, "a", encoding="utf-8") as f:
            if new:
                f.write(",".join(self.FIELDS) + "\n")
            f.write(",".join('"%s"' % str(row[k]).replace('"', "'") for k in self.FIELDS) + "\n")
        print("[%s] %.2fh cmds=%s ok=%s err=%s to=%s rc=%s active=%s p50=%s p95=%s max=%s info=%sms total=%d"
              % (row["time"], float(row["elapsed_h"]), row["cmds"], row["ok"], row["err"], row["timeouts"],
                 row["reconnects"], active_layers, row["lat_p50_ms"], row["lat_p95_ms"], row["lat_max_ms"],
                 row["info_ms"], self.total["cmds"]), flush=True)
        self.reset()


# ----------------------------------------------------------------------------
# Layer state machine
# ----------------------------------------------------------------------------

EMPTY, ACTIVE, STOPPED, REMOVED = "EMPTY", "ACTIVE", "STOPPED", "REMOVED"


class Layer:
    def __init__(self, n):
        self.n = n
        self.state = EMPTY
        self.updates_left = 0
        self.not_before = 0.0
        self.seq = 0


def parse_range(s):
    out = []
    for part in s.split(","):
        if "-" in part:
            a, b = part.split("-")
            out.extend(range(int(a), int(b) + 1))
        elif part.strip():
            out.append(int(part))
    return out


class Stress:
    def __init__(self, args, news):
        self.a = args
        self.news = news
        self.amcp = Amcp(args.host, args.port, args.timeout)
        self.layers = [Layer(n) for n in parse_range(args.layers)]
        self.stats = Stats()
        self.running = True

    # -- payload --------------------------------------------------------------
    def payload(self, layer):
        art = self.news.pick()
        layer.seq += 1
        body = art["body"]
        maxlen = random.choice([80, 200, 500, self.a.max_body])
        if len(body) > maxlen:
            start = random.randint(0, len(body) - maxlen)
            body = body[start:start + maxlen]
        d = {"layer": layer.n, "seq": layer.seq, "ts": datetime.now().strftime("%H:%M:%S"),
             "press": art["press"], "f0": art["title"], "f1": body}
        return amcp_quote(json.dumps(d, ensure_ascii=False))

    # -- send with bookkeeping ------------------------------------------------
    def send(self, kind, command):
        try:
            code, lines, lat = self.amcp.cmd(command)
        except (socket.timeout, TimeoutError):
            self.stats.event("timeouts")
            self.stats.add(kind, None, 0)
            log_event("TIMEOUT %s :: %s" % (kind, command[:160]))
            self.reconnect()
            return None, []
        except (ConnectionError, OSError, AmcpError) as e:
            self.stats.add(kind, None, 0)
            log_event("CONN ERROR %s: %r :: %s" % (kind, e, command[:160]))
            self.reconnect()
            return None, []
        self.stats.add(kind, code, lat)
        if code >= 300:
            log_event("ERR %d %s :: %s :: %s" % (code, kind, command[:160], " | ".join(lines)[:200]))
        elif lat > self.a.slow_ms:
            log_event("SLOW %.0fms %s :: %s" % (lat, kind, command[:120]))
        return code, lines

    def reconnect(self):
        self.stats.event("reconnects")
        delay = 1.0
        while self.running:
            try:
                self.amcp.connect()
                log_event("CONNECTED %s:%d" % (self.a.host, self.a.port))
                # 연결이 끊긴 동안 상태를 모르므로 모든 레이어 초기화
                for L in self.layers:
                    L.state = EMPTY
                return
            except OSError as e:
                log_event("reconnect failed: %r (retry in %.0fs)" % (e, delay))
                time.sleep(delay)
                delay = min(delay * 2, 30)

    # -- one step on one layer ------------------------------------------------
    def step(self, L):
        ch, cl, tpl = self.a.channel, L.n, self.a.template
        now = time.monotonic()
        cgl = 1  # cg (flash) layer inside the video layer
        if L.state in (EMPTY, REMOVED):
            if sum(1 for x in self.layers if x.state == ACTIVE) >= self.a.max_active:
                return
            code, _ = self.send("ADD", "CG %d-%d ADD %d %s 1 %s" % (ch, cl, cgl, amcp_quote(tpl), self.payload(L)))
            if code is not None and code < 300:
                L.state = ACTIVE
                if self.a.mode == "churn":       # 생성/삭제만: UPDATE 없이 바로 STOP->CLEAR
                    L.updates_left = 0
                elif self.a.mode == "update":    # UPDATE 만: 한 번 ADD 후 STOP/CLEAR 하지 않음
                    L.updates_left = 1 << 62
                else:
                    L.updates_left = random.randint(self.a.min_updates, self.a.max_updates)
                L.not_before = now + 0.5
        elif L.state == ACTIVE:
            if L.updates_left > 0:
                r = random.random()
                if r < 0.85:
                    self.send("UPDATE", "CG %d-%d UPDATE %d %s" % (ch, cl, cgl, self.payload(L)))
                elif r < 0.93:
                    self.send("INVOKE", 'CG %d-%d INVOKE %d "flash"' % (ch, cl, cgl))
                else:
                    self.send("NEXT", "CG %d-%d NEXT %d" % (ch, cl, cgl))
                L.updates_left -= 1
                L.not_before = now + random.uniform(0.2, self.a.update_interval)
            else:
                self.send("STOP", "CG %d-%d STOP %d" % (ch, cl, cgl))
                L.state = STOPPED
                L.not_before = now + 0.6  # out animation
        elif L.state == STOPPED:
            r = random.random()
            if r < 0.45:
                self.send("CG_CLEAR", "CG %d-%d CLEAR" % (ch, cl))
                L.state = EMPTY
            elif r < 0.75:
                self.send("CLEAR", "CLEAR %d-%d" % (ch, cl))
                L.state = EMPTY
            else:
                # producer 는 남기고 템플릿만 제거 -> 같은 producer 에 재 ADD
                self.send("REMOVE", "CG %d-%d REMOVE %d" % (ch, cl, cgl))
                L.state = REMOVED
            L.not_before = now + random.uniform(0.2, 2.0)

    def health(self):
        code, lines = self.send("INFO", "INFO %d" % self.a.channel)
        if code == 201 and len(lines) > 1:
            xml = lines[1]
            mine = set(L.n for L in self.layers)
            busy = 0
            try:

                root = ET.fromstring(xml.encode("utf-8"))
                for el in root.iter():
                    m = re.match(r"layer_(\d+)$", el.tag)
                    if m and int(m.group(1)) in mine:
                        prod = el.findtext("foreground/producer") or "empty"
                        busy += prod != "empty"
            except ET.ParseError as e:
                log_event("INFO xml parse error: %r" % e)
            self.stats.info_layers = busy
            # 마지막 INFO latency 기록
            self.stats.info_ms = "%.1f" % self.stats.lat_by["INFO"][-1] if self.stats.lat_by["INFO"] else ""

    def clear_all(self):
        for L in self.layers:
            try:
                self.amcp.cmd("CLEAR %d-%d" % (self.a.channel, L.n))
            except Exception:
                break

    # -- main loop ------------------------------------------------------------
    def run(self):
        self.reconnect()
        self.stats.cnt["reconnects"] = self.stats.total["reconnects"] = 0
        if self.a.clear_on_start:
            self.clear_all()
        period = 1.0 / self.a.rate
        next_tick = time.monotonic()
        next_stats = time.monotonic() + self.a.stats_interval
        next_health = time.monotonic() + self.a.health_interval
        t_end = time.monotonic() + self.a.duration * 3600 if self.a.duration > 0 else None
        while self.running:
            now = time.monotonic()
            if t_end and now >= t_end:
                log_event("duration reached")
                break
            ready = [L for L in self.layers if L.not_before <= now]
            if ready:
                self.step(random.choice(ready))
            if now >= next_health:
                self.health()
                next_health = now + self.a.health_interval
            if now >= next_stats:
                active = sum(1 for L in self.layers if L.state == ACTIVE)
                self.stats.flush(active, len(self.news.pool))
                next_stats += self.a.stats_interval
            next_tick += period
            sl = next_tick - time.monotonic()
            if sl > 0:
                time.sleep(sl)
            else:
                next_tick = time.monotonic()  # behind schedule, don't burst
        if self.a.clear_on_exit:
            log_event("clearing layers on exit")
            self.clear_all()
        self.amcp.close()


def main():
    global LOG_DIR
    p = argparse.ArgumentParser(description="CasparCG CG AMCP long-run stress test (Naver news)")
    p.add_argument("--host", default="10.10.108.108")
    p.add_argument("--port", type=int, default=5250)
    p.add_argument("--channel", type=int, default=1)
    p.add_argument("--layers", default="1-45", help="예: 1-45 또는 1-10,30-45")
    p.add_argument("--template", default="stress_news", help="template 폴더 기준 경로(확장자 제외)")
    p.add_argument("--rate", type=float, default=15.0, help="초당 명령 수 (전체)")
    p.add_argument("--mode", choices=["mixed", "update", "churn"], default="mixed",
                   help="mixed=ADD/UPDATE/STOP/CLEAR 혼합, update=ADD 후 UPDATE 만 반복, churn=ADD->STOP->CLEAR 만 반복")
    p.add_argument("--max-active", type=int, default=45, help="동시에 ACTIVE 인 레이어 최대 수")
    p.add_argument("--min-updates", type=int, default=3)
    p.add_argument("--max-updates", type=int, default=30)
    p.add_argument("--update-interval", type=float, default=3.0, help="같은 레이어 UPDATE 간 최대 간격(초)")
    p.add_argument("--max-body", type=int, default=1500, help="f1 본문 최대 글자 수")
    p.add_argument("--timeout", type=float, default=10.0, help="AMCP 응답 타임아웃(초)")
    p.add_argument("--slow-ms", type=float, default=500.0, help="이 이상 걸린 명령은 이벤트 로그에 기록")
    p.add_argument("--stats-interval", type=float, default=60.0)
    p.add_argument("--health-interval", type=float, default=30.0)
    p.add_argument("--news-refresh", type=float, default=600.0, help="뉴스 재수집 주기(초)")
    p.add_argument("--duration", type=float, default=0, help="실행 시간(시간), 0=무한")
    p.add_argument("--log-dir", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs"))
    p.add_argument("--no-clear-on-start", dest="clear_on_start", action="store_false")
    p.add_argument("--no-clear-on-exit", dest="clear_on_exit", action="store_false")
    a = p.parse_args()

    LOG_DIR = a.log_dir
    os.makedirs(LOG_DIR, exist_ok=True)
    log_event("START %s" % vars(a))

    news = NewsPool(refresh_sec=a.news_refresh)
    news.start()
    # 첫 기사 수집을 잠시 기다림 (실패해도 FALLBACK 으로 진행)
    for _ in range(60):
        if news.pool:
            break
        time.sleep(0.5)

    st = Stress(a, news)

    def _sig(signum, _frame):
        log_event("signal %d -> stopping" % signum)
        st.running = False
        news.stop_evt.set()

    signal.signal(signal.SIGINT, _sig)
    signal.signal(signal.SIGTERM, _sig)
    st.run()
    log_event("STOP total=%s" % dict(st.stats.total))


if __name__ == "__main__":
    main()
