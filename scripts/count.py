#!/usr/bin/env python3
"""Count launcher sessions per task from their result files. Reads only.

Usage: python3 scripts/count.py <tasks folder> [--json]

Counts what the launcher's header shows: sessions, hours, results and verifier verdicts per role.
Work done inside the orchestrator's own session, or by its subagents, has no header and is not
counted; a task's own `- Cost:` line is printed beside the counts for that. Folders whose name ends
in `evidence` are skipped: they hold checkers' fixtures, which can look like results.
"""
import argparse
import json
import os
import re
import stat
from datetime import datetime
from pathlib import Path

ROLES = ("builder", "verifier", "challenger", "reviewer", "investigator")
SIDECARS = (".log", ".lock")
VERDICT = re.compile(r"^Verdict:\s*(pass|fail|inconclusive)\b(?!\s*\|)", re.M | re.I)
RESULT = re.compile(r"^Result:\s*(done|blocked|needs decision|running|launch refused|interrupted|"
                    r"killed|source check failed|[a-z]+)", re.M)
COST = re.compile(r"^- Cost: (.*)$", re.M)
WHOLE = 32_000_000


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
    hours = None
    try:
        hours = (datetime.fromisoformat(head["End UTC"])
                 - datetime.fromisoformat(head["Start UTC"])).total_seconds() / 3600
    except (KeyError, ValueError, TypeError):
        pass
    result = RESULT.search(re.sub(r"[*`]", "", text))
    verdicts = VERDICT.findall(re.sub(r"[*`]", "", body)) if head["Role"] == "verifier" else []
    return {"role": head["Role"], "family": head.get("Family", ""), "hours": hours,
            "result": result[1].strip() if result else "none",
            "verdict": verdicts[-1].lower() if verdicts else None}


def count(tasks):
    rows = []
    for task in sorted(p for p in tasks.iterdir() if p.is_dir() and not p.is_symlink()):
        row = {"task": task.name, "cost": None, "roles": {}}
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
                role["results"][found["result"]] = role["results"].get(found["result"], 0) + 1
                if found["role"] == "verifier":
                    verdict = found["verdict"] or "none"
                    role["verdicts"][verdict] = role["verdicts"].get(verdict, 0) + 1
        if row["roles"] or row["cost"]:
            rows.append(row)
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("tasks", type=Path)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    rows = count(args.tasks)
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


if __name__ == "__main__":
    main()
