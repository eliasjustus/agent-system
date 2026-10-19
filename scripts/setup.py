#!/usr/bin/env python3
"""Install the method and a project's entry files. Python 3.13+."""
import sys

if sys.version_info < (3, 13):
    sys.exit("Agent System setup requires Python 3.13 or newer.")

import argparse
import difflib
import hashlib
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile


ROOT = Path(__file__).resolve().parents[1]


def git(repo, *args):
    return subprocess.check_output(["git", "--no-optional-locks", "-C", str(repo), *args],
                                   text=True).strip()


def display(path):
    return "~/" + str(path.relative_to(Path.home())) if path.is_relative_to(Path.home()) else str(path)


def tree(path):
    return {str(p.relative_to(path)): p.read_bytes() if p.is_file() else None
            for p in path.rglob("*")} if path.is_dir() else None


def diff(path, old, new):
    for line in difflib.unified_diff(old.splitlines(keepends=True), new.splitlines(keepends=True),
                                     fromfile=str(path), tofile=str(path)):
        sys.stdout.write(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n")


def without_metadata(text):
    first, separator, body = text.partition("\n")
    return re.sub(r" (?:Method commit: [0-9a-f]{12}(?:-dirty)?|Checksum: [0-9a-f]{12})\.",
                  "", first) + separator + body


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--id", required=True)
    parser.add_argument("--private", default="~/dev/agent-private")
    parser.add_argument("--method", default="~/.local/share/agent-method")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    project, private, method = (Path(p).expanduser().resolve()
                                for p in (args.project, args.private, args.method))
    if not re.fullmatch(r"[\w.-]+", args.id) or args.id in (".", ".."):
        parser.error("--id must be a single project directory name")
    if (ROOT.is_relative_to(method) or method.is_relative_to(ROOT)
            or project.is_relative_to(method) or private.is_relative_to(method)):
        parser.error("method replacement would overlap source, project or private files")
    entry = project / "AGENTS.md"
    if entry.exists() and "Agent System entry" not in entry.read_text(encoding="utf-8").split("\n", 1)[0] and not args.force:
        print(f"Refused foreign {entry}; use --force to replace it.", file=sys.stderr)
        return 1
    claude = project / "CLAUDE.md"
    if claude.exists() and "@AGENTS.md" not in claude.read_text(encoding="utf-8").splitlines():
        print(f"Warning: Claude Code may not load AGENTS.md; add a line @AGENTS.md to {claude}.",
              file=sys.stderr)
    # Resolve repository metadata before making any changes.
    exclude = Path(git(project, "rev-parse", "--path-format=absolute", "--git-path", "info/exclude"))
    tracked = subprocess.run(["git", "-C", str(project), "ls-files", "--error-unmatch",
                              "--", ".claude/agents/builder.md"], capture_output=True).returncode == 0
    source = git(ROOT, "rev-parse", "HEAD")
    dirty = bool(git(ROOT, "status", "--porcelain"))
    replacements = {"{{METHOD}}": display(method), "{{PRIVATE}}": display(private)}

    def fill(text, project_id=False):
        for key, value in replacements.items():
            text = text.replace(key, value)
        return text.replace("{{PROJECT}}", args.id) if project_id else text

    content = fill((ROOT / "product/AGENTS.md").read_text(encoding="utf-8"), True)
    first, separator, body = content.partition("\n")
    checksum = hashlib.sha256(body.encode("utf-8")).hexdigest()[:12]
    content = (first.removesuffix(" -->")
               + f" Method commit: {source[:12]}{'-dirty' if dirty else ''}. Checksum: {checksum}. -->"
               + separator + body)
    edited = False
    stored_checksum = None
    if entry.exists():
        old = entry.read_bytes()
        old_first_line, _, old_body = old.partition(b"\n")
        stored_checksum = re.search(rb" Checksum: ([^. ]+)\.", old_first_line)
        edited = bool(stored_checksum and stored_checksum[1]
                      != hashlib.sha256(old_body).hexdigest()[:12].encode("utf-8"))
        if edited and not args.force:
            diff(entry, old.decode("utf-8"), content)
            print(f"Refused {entry}: edited by hand; use --force to replace it.", file=sys.stderr)
            return 1

    def write(path, content, keep=False):
        if path.exists() and (keep or path.read_text(encoding="utf-8") == content):
            print(f"Kept {path}")
            return
        print(f"{'Would write' if args.dry_run else 'Wrote'} {path}")
        if args.dry_run:
            diff(path, path.read_text(encoding="utf-8") if path.exists() else "", content)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")

    def directory(path):
        print(f"{'Kept' if path.is_dir() else 'Would create' if args.dry_run else 'Created'} {path}/")
        if not args.dry_run:
            path.mkdir(parents=True, exist_ok=True)

    builder = ("---\nname: builder\ndescription: Implements one bounded assignment from the "
               "orchestrator's brief; never judges its own work\n"
               f"model: {os.environ.get('AGENT_CLAUDE_MODEL') or 'claude-opus-5-5'}\n---\n"
               + fill((ROOT / "product/roles/builder.md").read_text(encoding="utf-8")))
    installed = tree(ROOT / "product")
    installed = {name: fill(data.decode("utf-8")).encode("utf-8") if name.endswith(".md") else data
                 for name, data in installed.items()}
    installed["run.py"] = (ROOT / "scripts/run.py").read_text(encoding="utf-8").replace(
        'DEFAULT_PRIVATE = "~/dev/agent-private"  # Set by setup.',
        f"DEFAULT_PRIVATE = {str(private)!r}  # Set by setup.").encode("utf-8")
    installed["claude-builder.md"] = builder.encode("utf-8")
    installed["SOURCE"] = f"Commit: {source}\nDirty: {'yes' if dirty else 'no'}\nPath: {ROOT}\n".encode("utf-8")
    previous = tree(method)
    if previous == installed:
        print(f"Kept method {method}")
    else:
        print(f"{'Would replace' if args.dry_run else 'Installing'} method {method}")
        if args.dry_run:
            previous = previous or {}
            for name in sorted(previous.keys() | installed.keys()):
                if previous.get(name) is None and installed.get(name) is None:
                    continue
                if name not in previous:
                    print(f"A {method / name}")
                elif name not in installed:
                    print(f"D {method / name}")
                elif previous[name] != installed[name]:
                    print(f"M {method / name}")
        else:
            method.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix=f".{method.name}-", dir=method.parent) as temp:
                stage, backup = Path(temp) / "new", Path(temp) / "old"
                shutil.copytree(ROOT / "product", stage)
                shutil.copy2(ROOT / "scripts/run.py", stage / "run.py")
                for name, data in installed.items():
                    if data is not None:
                        (stage / name).write_bytes(data)
                if method.exists():
                    method.rename(backup)
                try:
                    stage.rename(method)
                except OSError:
                    if backup.exists():
                        backup.rename(method)
                    raise
    write(entry, content, keep=bool(stored_checksum) and not edited
          and without_metadata(entry.read_text(encoding="utf-8")) == without_metadata(content))
    project_private = private / "projects" / args.id
    for name in ("tasks", "findings"):
        directory(project_private / name)
    parts = re.split(r"(?m)^=== file: (.+)\n", (ROOT / "product/templates/project.md").read_text(encoding="utf-8"))
    for name, body in zip(parts[1::2], parts[2::2]):
        if name not in ("goals.md", "knowledge.md", "checks.md"):
            raise ValueError(f"Unexpected project template file: {name}")
        write(project_private / name, fill(body, True), keep=True)
    write(private / "user/preferences.md", fill((ROOT / "product/templates/preferences.md").read_text(encoding="utf-8")), keep=True)
    write(project / ".claude/agents/builder.md", builder)
    if not tracked:
        existing = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
        pattern = "/.claude/agents/builder.md"
        if pattern not in existing.splitlines():
            write(exclude, existing + ("\n" if existing and not existing.endswith("\n") else "") + pattern + "\n")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        print(f"Setup failed: {error}", file=sys.stderr)
        sys.exit(1)
