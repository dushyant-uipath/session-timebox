#!/usr/bin/env python3
"""Digest recent Claude Code sessions (main threads only) into compact JSON.
Usage: sessions_digest.py [--days N] [--out PATH] [--exclude SESSION_ID]"""
import argparse
import datetime as dt
import json
import os
import sys
import time
from pathlib import Path

PROJECTS = Path.home() / ".claude" / "projects"
SKIP_DIR_MARKERS = ("observer-sessions", "-private-var-", "-tmp-")


def text_of(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            b.get("text", "") for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        )
    return ""


def is_real_prompt(text):
    t = text.strip()
    # Harness-injected messages start with these, not things the user typed
    return bool(t) and not t.startswith((
        "<", "[Request interrupted", "Caveat:", "Base directory for this skill",
        "Another Claude session sent a message", "Stop hook feedback",
        "This session is being continued", "Your response above was stopped",
    ))


def clip(s, n):
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 1] + "…"


def digest(path):
    title = custom = None
    cwd = branch = None
    prompts, prs = [], {}
    last_reply = ""
    last_speaker = None
    first_ts = last_ts = None
    with open(path, errors="replace") as f:
        for line in f:
            try:
                d = json.loads(line)
            except ValueError:
                continue
            t = d.get("type")
            ts = d.get("timestamp")
            if t == "ai-title":
                title = d.get("aiTitle") or title
            elif t == "custom-title":
                custom = d.get("customTitle") or custom
            elif t == "pr-link":
                prs[d.get("prUrl")] = d.get("prNumber")
            elif t in ("user", "assistant") and not d.get("isSidechain"):
                cwd = d.get("cwd") or cwd
                branch = d.get("gitBranch") or branch
                if ts:
                    first_ts = first_ts or ts
                    last_ts = ts
                msg = d.get("message") or {}
                if t == "user" and (d.get("origin") or {}).get("kind", "human") == "human":
                    txt = text_of(msg.get("content"))
                    if is_real_prompt(txt):
                        prompts.append(txt)
                        last_speaker = "you"
                elif t == "assistant":
                    txt = text_of(msg.get("content"))
                    if txt.strip():
                        last_reply = txt
                        last_speaker = "claude"
    if not prompts:
        return None
    return {
        "session_id": path.stem,
        "title": custom or title or clip(prompts[0], 80),
        "repo": os.path.basename(cwd) if cwd else None,
        "cwd": cwd,
        "branch": branch if branch and branch != "HEAD" else None,
        "started": first_ts,
        "last_active": last_ts,
        "prs": [u for u in prs if u][-8:],
        "first_ask": clip(prompts[0], 400),
        "recent_asks": [clip(p, 300) for p in prompts[-4:]],
        "prompt_count": len(prompts),
        "last_reply": clip(last_reply, 1500),
        "last_speaker": last_speaker,
        "asks_you": last_speaker == "claude" and last_reply.rstrip().rstrip("*_`").endswith("?"),
        "mtime": path.stat().st_mtime,
        "active_at": dt.datetime.fromisoformat(last_ts.replace("Z", "+00:00")).timestamp() if last_ts else path.stat().st_mtime,
        "resume": f"claude --resume {path.stem}",
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=float, default=7)
    ap.add_argument("--out")
    ap.add_argument("--exclude", action="append", default=[])
    a = ap.parse_args()
    cutoff = time.time() - a.days * 86400
    out = []
    for proj in PROJECTS.iterdir():
        if not proj.is_dir() or any(m in proj.name for m in SKIP_DIR_MARKERS):
            continue
        for p in proj.glob("*.jsonl"):
            if p.stem in a.exclude or p.stat().st_mtime < cutoff:
                continue
            d = digest(p)
            if d:
                out.append(d)
    out.sort(key=lambda d: d["last_active"] or "", reverse=True)
    data = json.dumps(out, indent=1, ensure_ascii=False)
    if a.out:
        Path(a.out).write_text(data)
        print(f"{len(out)} sessions -> {a.out}")
    else:
        sys.stdout.write(data)


if __name__ == "__main__":
    main()
