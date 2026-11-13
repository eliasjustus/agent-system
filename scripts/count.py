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
from datetime import datetime
from pathlib import Path

ROLES = ("builder", "verifier", "challenger", "reviewer", "investigator")
HEADER = re.compile(r"^(Role|Family|Start UTC|End UTC): ?(.*)$")
RESULT = re.compile(r"^\**Result:\**\s*([a-z ]+)", re.M)
VERDICT = re.compile(r"^\**Verdict:\**\s*\**(pass|fail|inconclusive)\b", re.M | re.I)
COST = re.compile(r"^- Cost: (.*)$", re.M)
LIMIT = 3_000_000


def read(path):
    try:
        if path.is_symlink() or path.stat().st_size > LIMIT:
            return None
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def session(text):
    head = {}
    for line in text.split("\n", 40)[:40]:
        match = HEADER.match(line)
        if match:
            head.setdefault(match[1], match[2].strip())
    if head.get("Role") not in ROLES or "Start UTC" not in head:
        return None
    hours = None
    try:
        hours = (datetime.fromisoformat(head["End UTC"])
                 - datetime.fromisoformat(head["Start UTC"])).total_seconds() / 3600
    except (KeyError, ValueError):
        pass
    result = RESULT.search(text)
    verdicts = VERDICT.findall(text) if head["Role"] == "verifier" else []
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
                text = read(Path(folder) / name) if name.endswith(".md") else None
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
