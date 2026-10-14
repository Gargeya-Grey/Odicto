"""Per-capture timings from the live dictation.log (read-only, stdlib only).

Usage:
    python log_report.py --root W:\\AI-Projects\\Odicto [--since "2026-10-14 17:00:00"] [--out DIR]

For each capture it prints the mode, the lag between the app's state change and
the HUD seeing it, STT seconds, paste seconds and the mic overflows logged while
it ran. It also counts idle overflows and restarts. The log holds lengths and
timings only, never transcript text (LOG_TRANSCRIPTS=false), so the report is
safe to keep as evidence. With --out it writes report.txt and report.json there.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime

STAMP = re.compile(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) ")
# A HUD print can land in the middle of another line, so every pattern is searched, not matched.
REC = re.compile(r">>> Recording \((raw dictation|AI refined)\)")
HUD = re.compile(r"\[HUD\] -> (\w+)")
PROC = re.compile(r">>> Processing transcription")
STT = re.compile(r"\(STT ([\d.]+)s\)")
PASTE = re.compile(r"pasted successfully in ([\d.]+) seconds")
END = re.compile(r"System Idle\. Ready\.|Cancelled\. Nothing inserted|Failed to start recorder")
OVERFLOW = re.compile(r"input overflow")
SESSION = re.compile(r"----- session (\S+ \S+) pid=(\d+)")
LAG_FLAG_S = 1.0  # timestamps have 1 s resolution; 1 s or less is noise


def parse(path: str, since: datetime | None) -> dict:
    captures, cur = [], None
    idle_overflows, sessions = 0, []
    last_t = None
    with open(path, encoding="utf-8", errors="replace") as f:
        for raw in f:
            m = STAMP.search(raw)
            if m:
                last_t = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
            t = last_t
            if t is None or (since and t < since):
                continue
            s = SESSION.search(raw)
            if s:
                sessions.append({"at": s.group(1), "pid": int(s.group(2))})
                cur = None
                continue
            r, h = REC.search(raw), HUD.search(raw)
            if r or (h and h.group(1) == "RECORDING"):
                if cur is None or cur.get("end"):
                    cur = {"start": t}
                    captures.append(cur)
                if r:
                    cur["mode"] = "AI" if r.group(1) == "AI refined" else "raw"
                    cur["rec_log"] = t
                if h and h.group(1) == "RECORDING":
                    cur.setdefault("hud_rec", t)
            if cur is None or cur.get("end"):
                if OVERFLOW.search(raw):
                    idle_overflows += 1
                continue
            if h and h.group(1) == "PROCESSING":
                cur.setdefault("hud_proc", t)
            if PROC.search(raw):
                cur.setdefault("proc_log", t)
            if (m2 := STT.search(raw)):
                cur["stt_s"] = float(m2.group(1))
            if (m3 := PASTE.search(raw)):
                cur["paste_s"] = float(m3.group(1))
            if OVERFLOW.search(raw):
                cur["overflows"] = cur.get("overflows", 0) + 1
            if END.search(raw):
                cur["end"] = t
    rows = []
    for c in captures:
        def gap(a, b):
            return abs((c[a] - c[b]).total_seconds()) if a in c and b in c else None
        lag = [g for g in (gap("hud_rec", "rec_log"), gap("hud_proc", "proc_log")) if g is not None]
        rows.append({
            "start": c["start"].strftime("%Y-%m-%d %H:%M:%S"),
            "mode": c.get("mode", "?"),
            "hud_lag_s": max(lag) if lag else None,
            "stt_s": c.get("stt_s"),
            "paste_s": c.get("paste_s"),
            "overflows": c.get("overflows", 0),
        })
    lagged = [r for r in rows if r["hud_lag_s"] is not None and r["hud_lag_s"] > LAG_FLAG_S]
    return {"captures": rows, "idle_overflows": idle_overflows, "sessions": sessions,
            "lagged_captures": len(lagged), "total_captures": len(rows)}


def render(rep: dict) -> str:
    out = [f"{'start':19}  mode  hud_lag  stt_s  paste_s  overflows"]
    for r in rep["captures"]:
        lag = "-" if r["hud_lag_s"] is None else f"{r['hud_lag_s']:.0f}s"
        flag = "  LAG" if r["hud_lag_s"] and r["hud_lag_s"] > LAG_FLAG_S else ""
        stt = "-" if r["stt_s"] is None else f"{r['stt_s']:.2f}"
        paste = "-" if r["paste_s"] is None else f"{r['paste_s']:.2f}"
        out.append(f"{r['start']}  {r['mode']:4}  {lag:>7}  {stt:>5}  {paste:>7}  {r['overflows']:>9}{flag}")
    out.append("")
    out.append(f"captures: {rep['total_captures']}, HUD lag > {LAG_FLAG_S:g}s: {rep['lagged_captures']}, "
               f"idle overflows: {rep['idle_overflows']}, restarts: {len(rep['sessions'])}")
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", required=True, help="install root that holds dictation.log")
    ap.add_argument("--since", help='"YYYY-MM-DD HH:MM:SS"; default: the whole log')
    ap.add_argument("--out", help="directory for report.txt and report.json")
    a = ap.parse_args()
    path = os.path.join(a.root, "dictation.log")
    if not os.path.isfile(path):
        print(f"no log at {path}", file=sys.stderr)
        return 2
    since = datetime.strptime(a.since, "%Y-%m-%d %H:%M:%S") if a.since else None
    rep = parse(path, since)
    text = render(rep)
    print(text)
    if a.out:
        os.makedirs(a.out, exist_ok=True)
        with open(os.path.join(a.out, "report.txt"), "w", encoding="utf-8") as f:
            f.write(text + "\n")
        with open(os.path.join(a.out, "report.json"), "w", encoding="utf-8") as f:
            json.dump(rep, f, indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
