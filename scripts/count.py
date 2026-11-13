#!/usr/bin/env python3
"""Count launcher sessions per task from their result files. Reads only.

Usage: python3 scripts/count.py <tasks folder> [--json] [--now <time>]

Counts what the launcher's header shows: sessions, hours, results and verifier verdicts per role.
Work done inside the orchestrator's own session, or by its subagents, has no header and is not
counted; a task's own `- Cost:` line is printed beside the counts for that. Folders whose name ends
in `evidence` are skipped: they hold checkers' fixtures, which can look like results.

Also estimates how much of a task's time was waiting. Something worked during a launcher session
(its header's start to end) and in the hour before each commit that changed the task's folder, read
from the Git history of the repository holding the tasks folder (a private folder committed every
hour). Elapsed runs from the first activity to the last, or to now while the record's first
`- State:` line says active, awaiting or paused; waiting is the part with no activity. An estimate:
a whole hour counts for any change in it, and work that touched neither the folder nor a launcher
session is not seen. Whether a wait was wanted is not judged.
"""
import argparse
import json
import os
import re
import stat
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROLES = ("builder", "verifier", "challenger", "reviewer", "investigator")
SIDECARS = (".log", ".lock")
VERDICT = re.compile(r"^Verdict:\s*(pass|fail|inconclusive)\b(?!\s*\|)", re.M | re.I)
RESULT = re.compile(r"^Result:\s*(done|blocked|needs decision|running|launch refused|interrupted|"
                    r"killed|source check failed|[a-z]+)", re.M)
COST = re.compile(r"^- Cost: (.*)$", re.M)
STATE = re.compile(r"^- State: (.*)$", re.M)
OPEN = ("active", "awaiting", "paused")
WHOLE = 32_000_000
WINDOW = timedelta(hours=1)  # the interval of the snapshots whose commits show a folder changed
GIT_SECONDS = 120


def read(path):
    """A regular file's text, never through a link, never blocking. Of a file over 32 MB only the
    complete lines of its first megabyte: header and result, no verdict."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        status = os.fstat(fd)
        if not stat.S_ISREG(status.st_mode):
            return None
        if status.st_size <= WHOLE:
            data = os.pread(fd, WHOLE, 0)
        else:
            data = os.pread(fd, 1_000_000, 0)
            data = data[:data.rfind(b"\n") + 1]
        return data.decode("utf-8", errors="replace")
    except OSError:
        return None
    finally:
        os.close(fd)


def session(text):
    """A launcher result starts with its header: `Role:` first, up to a blank line."""
    block, _, body = text.partition("\n\n")
    lines = block.split("\n")
    head = dict(line.split(": ", 1) for line in lines if ": " in line)
    if not text.startswith("Role: ") or head.get("Role") not in ROLES or "Start UTC" not in head:
        return None
    hours = span = None
    try:
        start = moment(head["Start UTC"])
        span = (start, start)  # with no usable end time a session is a moment, not still running
        hours = (datetime.fromisoformat(head["End UTC"])
                 - datetime.fromisoformat(head["Start UTC"])).total_seconds() / 3600
        if hours > 0:
            span = (start, start + timedelta(hours=hours))
    except (KeyError, ValueError, TypeError, OverflowError):
        pass
    result = RESULT.search(re.sub(r"[*`]", "", text))
    verdicts = VERDICT.findall(re.sub(r"[*`]", "", body)) if head["Role"] == "verifier" else []
    return {"role": head["Role"], "family": head.get("Family", ""), "hours": hours,
            "result": result[1].strip() if result else "none",
            "verdict": verdicts[-1].lower() if verdicts else None, "span": span}


def moment(text):
    """A time from a header, Git or the command line; one without a zone is UTC."""
    value = datetime.fromisoformat(text.strip())
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def history(tasks):
    """When each task's folder changed: the commit times per task folder name, from the Git history
    of the repository holding `tasks`; None where no commit holds anything of the folder. Reads
    only, whatever Git variables the caller's environment sets."""
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    try:
        done = subprocess.run(
            ["git", "--no-pager", "-c", "core.quotepath=off", "-C", str(tasks), "log", "--relative",
             "--name-only", "--no-renames", "--no-ext-diff", "--no-textconv", "--no-show-signature",
             "-z", "--format=%x01%cI", "--", "."],
            capture_output=True, stdin=subprocess.DEVNULL, env=environment, timeout=GIT_SECONDS)
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    changed = {}
    # Each commit: \x01, its time, NUL, a newline, then the changed paths, each ended by NUL.
    for entry in done.stdout.decode("utf-8", errors="replace").split("\x01")[1:]:
        stamp, _, names = entry.partition("\0")
        try:
            when = moment(stamp)
        except ValueError:
            continue
        for name in names.lstrip("\n").split("\0"):
            task, below, _ = name.partition("/")
            if below:
                changed.setdefault(task, set()).add(when)
    return changed or None


def union(spans):
    """Hours covered by the spans, overlaps counted once."""
    seconds, edge = 0.0, None
    for start, end in sorted(spans):
        if edge is None or start > edge:
            seconds += (end - start).total_seconds()
            edge = end
        elif end > edge:
            seconds += (end - edge).total_seconds()
            edge = end
    return seconds / 3600


def spent(sessions, commits, record, now):
    """Elapsed, active and waiting time of one task as of `now`; None without any activity."""
    sessions = [(start, min(end, now)) for start, end in sessions if start <= now]
    changes = [(when - WINDOW, when) for when in commits or () if when <= now]
    both = sessions + changes
    if not both:
        return None
    state = STATE.search(record or "")
    is_open = bool(state and state[1].startswith(OPEN))
    first, last = min(start for start, _ in both), max(end for _, end in both)
    elapsed = ((now if is_open else last) - first).total_seconds() / 3600
    active = union(both)
    return {"elapsed": elapsed, "active": active, "sessions": union(sessions),
            "records": None if commits is None else union(changes),
            "waiting": 1 - active / elapsed if elapsed > 0 else None,
            "open": is_open, "idle": (now - last).total_seconds() / 3600 if is_open else None}


def count(tasks, now=None):
    now = now or datetime.now(timezone.utc)
    changed = history(tasks)
    rows = []
    for task in sorted(p for p in tasks.iterdir() if p.is_dir() and not p.is_symlink()):
        row = {"task": task.name, "cost": None, "roles": {}, "time": None}
        spans = []
        record = read(task / "task.md")
        cost = COST.findall(record) if record else []
        row["cost"] = cost[-1].strip() if cost else None
        for folder, folders, files in os.walk(task):
            folders[:] = [name for name in folders if not name.endswith("evidence")]
            for name in sorted(files):
                text = None if name.endswith(SIDECARS) else read(Path(folder) / name)
                found = session(text) if text else None
                if not found:
                    continue
                role = row["roles"].setdefault(found["role"], {"sessions": 0, "hours": 0.0,
                                                               "results": {}, "verdicts": {}})
                role["sessions"] += 1
                role["hours"] += found["hours"] or 0
                if found["span"]:
                    spans.append(found["span"])
                role["results"][found["result"]] = role["results"].get(found["result"], 0) + 1
                if found["role"] == "verifier":
                    verdict = found["verdict"] or "none"
                    role["verdicts"][verdict] = role["verdicts"].get(verdict, 0) + 1
        row["time"] = spent(spans, None if changed is None else changed.get(task.name, ()), record, now)
        if row["roles"] or row["cost"] or row["time"]:
            rows.append(row)
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("tasks", type=Path)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--now", type=moment, metavar="TIME",
                        help="count the time figures as of this moment (ISO 8601; default: now)")
    args = parser.parse_args()
    rows = count(args.tasks, args.now)
    if args.json:
        print(json.dumps(rows, indent=1))
        return
    for row in rows:
        print(row["task"])
        for name in ROLES:
            role = row["roles"].get(name)
            if not role:
                continue
            parts = [f"{role['sessions']} sessions", f"{role['hours']:.1f} h",
                     "results " + ", ".join(f"{k} {v}" for k, v in sorted(role["results"].items()))]
            if role["verdicts"]:
                parts.append("verdicts " + ", ".join(f"{k} {v}" for k, v in sorted(role["verdicts"].items())))
            print(f"  {name}: " + "; ".join(parts))
        if row["cost"]:
            print(f"  cost line: {row['cost']}")
        time = row["time"]
        if time:
            parts = (f"sessions {time['sessions']:.1f}; no Git history of the folder"
                     if time["records"] is None else
                     f"folder changes {time['records']:.1f}, sessions {time['sessions']:.1f}")
            line = f"  time: elapsed {time['elapsed']:.1f} h; active {time['active']:.1f} h ({parts})"
            if time["waiting"] is not None:
                line += f"; waiting about {time['waiting']:.0%}"
            if time["open"]:
                line += f"; open, last activity {time['idle']:.1f} h ago"
            print(line)


if __name__ == "__main__":
    main()
