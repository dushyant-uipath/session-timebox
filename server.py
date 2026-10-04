#!/usr/bin/env python3
"""Local session board: live sessions, Slack items, time boxes, jump-to-terminal.
Usage: server.py [--port 8765] [--days 14]"""
import argparse
import datetime as dt
import json
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from sessions_digest import PROJECTS, SKIP_DIR_MARKERS, digest, subagent

HERE = Path(__file__).resolve().parent
STATE = HERE / "state.json"  # pre-SQLite storage, imported once
DB = HERE / "timebox.db"
SLACK = HERE / "slack.json"
SLACK_PROMPT = HERE / "slack_refresh.md"
HIDDEN_TITLES = ("slack-refresh", "slack-probe")  # background refresh sessions, kept off the board
REFRESH_TIMEOUT = 20 * 60
CLAUDE = shutil.which("claude") or "claude"
_refresh = {"job": None, "started": 0, "error": None}
LIVE_DIR = Path.home() / ".claude" / "sessions"
UUID = re.compile(r"^[0-9a-f-]{36}$")

_cache = {}  # transcript path -> (mtime, digest)
_sub_cache = {}  # subagent transcript path -> (mtime, subagent)
_latest = {}  # session_id -> digest, from the most recent scan
_lock = threading.Lock()
_state_lock = threading.Lock()


def subtasks(session_path):
    out = []
    for p in (session_path.parent / session_path.stem / "subagents").glob("*.jsonl"):
        m = p.stat().st_mtime
        hit = _sub_cache.get(p)
        if not hit or hit[0] != m:
            hit = _sub_cache[p] = (m, subagent(p))
        if hit[1]:
            out.append(hit[1])
    return sorted(out, key=lambda a: a["active_at"], reverse=True)


def db():
    conn = sqlite3.connect(DB, timeout=10)
    conn.execute("CREATE TABLE IF NOT EXISTS state (section TEXT, key TEXT, value TEXT, PRIMARY KEY (section, key))")
    return conn


# Create the database, importing state.json from the pre-SQLite version on first run
def init_db():
    with _state_lock, db() as conn:
        conn.execute("DROP TABLE IF EXISTS summaries")
        if conn.execute("SELECT COUNT(*) FROM state").fetchone()[0] or not STATE.exists():
            return
        for section, val in read_json(STATE, {}).items():
            rows = val.items() if isinstance(val, dict) else [("_", val)]
            conn.executemany("INSERT OR REPLACE INTO state VALUES (?, ?, ?)",
                             [(section, k, json.dumps(v)) for k, v in rows])


def load_state():
    state = {}
    with db() as conn:
        for section, key, value in conn.execute("SELECT section, key, value FROM state"):
            v = json.loads(value)
            if key == "_":
                state[section] = v
            else:
                state.setdefault(section, {})[key] = v
    return state


# Each op is {"path": [section, key, ...], "value": v} or {"path": [...], "delete": true}.
# One row per (section, key); a deeper path edits inside that row's JSON. All ops run in one transaction.
def apply_ops(ops):
    with _state_lock, db() as conn:
        for op in ops:
            section, *rest = op["path"]
            key, inner = (rest[0], rest[1:]) if rest else ("_", [])
            if not inner:
                if op.get("delete"):
                    conn.execute("DELETE FROM state WHERE section=? AND key=?", (section, key))
                else:
                    conn.execute("INSERT OR REPLACE INTO state VALUES (?, ?, ?)", (section, key, json.dumps(op["value"])))
                continue
            row = conn.execute("SELECT value FROM state WHERE section=? AND key=?", (section, key)).fetchone()
            node = root = json.loads(row[0]) if row else {}
            for k in inner[:-1]:
                node = node.setdefault(k, {})
            if op.get("delete"):
                node.pop(inner[-1], None)
            else:
                node[inner[-1]] = op["value"]
            conn.execute("INSERT OR REPLACE INTO state VALUES (?, ?, ?)", (section, key, json.dumps(root)))
    return load_state()


def read_json(path, default):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def live_sessions():
    """Map session id -> {pid, status, tty} for claude processes that are still running."""
    out = {}
    for f in LIVE_DIR.glob("*.json"):
        d = read_json(f, None)
        if not d or d.get("kind") != "interactive":
            continue
        try:
            os.kill(d["pid"], 0)
        except (OSError, KeyError):
            continue
        tty = subprocess.run(["ps", "-o", "tty=", "-p", str(d["pid"])],
                             capture_output=True, text=True).stdout.strip()
        out[d["sessionId"]] = {"pid": d["pid"], "status": d.get("status"),
                               "tty": f"/dev/{tty}" if tty and tty != "??" else None}
    return out


def sessions(days):
    cutoff = time.time() - days * 86400
    live = live_sessions()
    out = []
    for proj in PROJECTS.iterdir():
        if not proj.is_dir() or any(m in proj.name for m in SKIP_DIR_MARKERS):
            continue
        for p in proj.glob("*.jsonl"):
            m = p.stat().st_mtime
            if m < cutoff and p.stem not in live:
                continue
            hit = _cache.get(p)
            if not hit or hit[0] != m:
                hit = _cache[p] = (m, digest(p))
            if hit[1] and not hit[1]["title"].startswith(HIDDEN_TITLES):
                s = dict(hit[1])
                s["live"] = live.get(s["session_id"])
                s["subtasks"] = subtasks(p)
                out.append(s)
    out.sort(key=lambda d: d["active_at"], reverse=True)
    with _lock:
        _latest.clear()
        _latest.update({s["session_id"]: s for s in out})
    return out


def cleanup_job(job, delay=0):
    # The session is usually still finishing its last turn when results arrive, so retry the removal
    def run():
        time.sleep(delay)
        subprocess.run([CLAUDE, "stop", job], cwd=HERE, capture_output=True, timeout=60)
        for _ in range(8):
            if subprocess.run([CLAUDE, "rm", job], cwd=HERE, capture_output=True, timeout=60).returncode == 0:
                return
            time.sleep(20)
    threading.Thread(target=run, daemon=True).start()


def refresh_status():
    if _refresh["job"] and time.time() - _refresh["started"] > REFRESH_TIMEOUT:
        cleanup_job(_refresh["job"])
        _refresh.update(job=None, error="Slack refresh timed out after 20 minutes")
    return {"running": bool(_refresh["job"]), "started": _refresh["started"], "error": _refresh["error"]}


def start_slack_refresh(port):
    if _refresh["job"]:
        return refresh_status()
    cur = read_json(SLACK, {"items": [], "synced_at": None})
    state = load_state()
    now = time.time()
    since = max((cur.get("synced_at") or 0) - 86400, now - 8 * 86400)
    day = lambda t: dt.date.fromtimestamp(t).isoformat()
    existing = "\n".join(f"- {i['id']} | {i.get('who')} | {i.get('where')} | {i.get('headline')} | {i.get('url')}"
                         for i in cur.get("items", [])) or "(none)"
    skip = ", ".join(k[2:] for k in state.get("archived", {}) if k.startswith("k:")) or "(none)"
    prompt = SLACK_PROMPT.read_text()
    for k, v in {"USER_ID": state.get("slackUser", ""), "TODAY": day(now), "SINCE": day(since),
                 "SINCE_MINUS_1": day(since - 86400), "EXISTING": existing, "SKIP": skip, "PORT": str(port)}.items():
        prompt = prompt.replace("{{" + k + "}}", v)
    r = subprocess.run([CLAUDE, "--bg", "-n", f"slack-refresh-{int(now)}", "--model", "sonnet",
                        "--permission-mode", "bypassPermissions", prompt],
                       cwd=HERE, capture_output=True, text=True, timeout=90)
    m = re.search(r"backgrounded\W+(\w+)", r.stdout)
    if not m:
        _refresh.update(job=None, error=(r.stderr or r.stdout).strip()[-300:] or "couldn't start claude --bg")
    else:
        _refresh.update(job=m.group(1), started=now, error=None)
    return refresh_status()


def receive_slack(body):
    items = body.get("items")
    if not isinstance(items, list) or not all(isinstance(i, dict) and i.get("id") for i in items):
        return {"ok": False, "error": "items must be a list of objects with an id"}
    SLACK.write_text(json.dumps({"synced_at": time.time(), "items": items}, indent=1))
    if _refresh["job"]:
        cleanup_job(_refresh["job"], delay=30)
        _refresh.update(job=None, error=None)
    return {"ok": True, "items": len(items)}


def osascript(lines, *args):
    cmd = ["osascript"]
    for line in lines:
        cmd += ["-e", line]
    return subprocess.run(cmd + list(args), capture_output=True, text=True, timeout=20)


FOCUS_TAB = [
    "on run argv",
    'tell application "Terminal"',
    "repeat with w in windows",
    "repeat with t in tabs of w",
    "if tty of t is (item 1 of argv) then",
    "set selected of t to true",
    "set index of w to 1",
    "activate",
    'return "ok"',
    "end if",
    "end repeat",
    "end repeat",
    "end tell",
    'return "notfound"',
    "end run",
]
OPEN_TAB = ["on run argv", 'tell application "Terminal"', "activate", "do script (item 1 of argv)", "end tell", "end run"]


def focus(session_id):
    if not UUID.match(session_id or ""):
        return {"ok": False, "error": "bad id"}
    live = live_sessions().get(session_id)
    if live and live["tty"]:
        r = osascript(FOCUS_TAB, live["tty"])
        if r.stdout.strip() == "ok":
            return {"ok": True, "action": "focused", "tty": live["tty"]}
        if r.returncode:
            return {"ok": False, "error": r.stderr.strip()}
    s = _latest.get(session_id)
    if not s:
        return {"ok": False, "error": "session not found"}
    cmd = f"claude --resume {session_id}"
    if s.get("cwd") and Path(s["cwd"]).is_dir():
        cmd = f"cd {shlex.quote(s['cwd'])} && {cmd}"
    r = osascript(OPEN_TAB, cmd)
    return {"ok": r.returncode == 0, "action": "opened", "error": r.stderr.strip()}


def ics(state):
    today = dt.date.today().isoformat()
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//timebox//EN"]
    for b in state.get("boxes", {}).values():
        if b.get("date", "") < today:
            continue
        start = dt.datetime.fromisoformat(f"{b['date']}T{b['start']}")
        end = start + dt.timedelta(minutes=b.get("dur", 60))
        title = re.sub(r"[\r\n]+", " ", b.get("title", "Focus"))
        lines += ["BEGIN:VEVENT", f"UID:{b['id']}@timebox", f"DTSTAMP:{dt.datetime.now():%Y%m%dT%H%M%S}",
                  f"DTSTART:{start:%Y%m%dT%H%M%S}", f"DTEND:{end:%Y%m%dT%H%M%S}", f"SUMMARY:{title}", "END:VEVENT"]
    return "\r\n".join(lines + ["END:VCALENDAR"]) + "\r\n"


class Handler(BaseHTTPRequestHandler):
    days = 14
    port = 8765
    allowed_origins = set()

    def origin_ok(self):
        origin = self.headers.get("Origin")
        return origin is None or origin in self.allowed_origins

    def cors(self):
        origin = self.headers.get("Origin")
        if origin in self.allowed_origins:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")

    def send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.cors()
        self.end_headers()
        self.wfile.write(data)

    def do_OPTIONS(self):
        if not self.origin_ok():
            return self.send(403, "{}")
        self.send_response(204)
        self.cors()
        self.send_header("Access-Control-Allow-Methods", "GET, POST")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Private-Network", "true")
        self.end_headers()

    def body(self):
        return json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")

    def do_GET(self):
        if not self.origin_ok():
            self.send(403, "{}")
        elif self.path in ("/", "/index.html"):
            self.send(200, (HERE / "index.html").read_bytes(), "text/html; charset=utf-8")
        elif self.path == "/api/sessions":
            self.send(200, json.dumps({"now": time.time(), "sessions": sessions(self.days)}))
        elif self.path == "/api/state":
            self.send(200, json.dumps(load_state()))
        elif self.path == "/api/slack":
            self.send(200, json.dumps({**read_json(SLACK, {"items": [], "synced_at": None}), "refresh": refresh_status()}))
        elif self.path == "/today.ics":
            self.send(200, ics(load_state()), "text/calendar")
        else:
            self.send(404, "{}")

    def do_POST(self):
        if not self.origin_ok():
            self.send(403, "{}")
        elif self.path == "/api/op":
            self.send(200, json.dumps(apply_ops(self.body().get("ops", []))))
        elif self.path == "/api/slack":
            self.send(200, json.dumps(receive_slack(self.body())))
        elif self.path == "/api/slack/refresh":
            self.send(200, json.dumps(start_slack_refresh(self.port)))
        elif self.path == "/api/focus":
            self.send(200, json.dumps(focus(self.body().get("session_id"))))
        else:
            self.send(404, "{}")

    def log_message(self, *args):
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--days", type=float, default=14)
    ap.add_argument("--allow-origin", action="append", default=["https://dushyant-uipath.github.io"],
                    help="extra web origin allowed to use this server (e.g. a GitHub Pages site)")
    a = ap.parse_args()
    Handler.days = a.days
    Handler.port = a.port
    Handler.allowed_origins = {f"http://localhost:{a.port}", f"http://127.0.0.1:{a.port}", *a.allow_origin}
    init_db()
    sessions(a.days)
    print(f"Session board on http://localhost:{a.port}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", a.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
