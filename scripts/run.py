#!/usr/bin/env python3
"""Run one builder, investigator or isolated review session. Python 3.13+."""
import sys

if sys.version_info < (3, 13):
    sys.exit("Agent System launcher requires Python 3.13 or newer.")

import argparse
from collections import deque
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import subprocess
import tempfile


DEFAULT_PRIVATE = "~/dev/agent-private"  # Set by setup.


def git(repo, *args, env=None, input=None, text=True):
    return subprocess.check_output(["git", "--no-optional-locks", "-C", str(repo), *args],
                                   env=env, input=input, text=text).strip()


def utc():
    return datetime.now(timezone.utc).isoformat()


def cleanup_failed(what, error):
    print(f"Cleanup failed: {what}: {error}", file=sys.stderr)


def cleanup_worktrees(repo, session):
    try:
        probe = subprocess.run(["git", "--no-optional-locks", "-C", str(repo),
                                "rev-parse", "--is-inside-work-tree"],
                               text=True, capture_output=True)
        if probe.returncode or probe.stdout.strip() != "true":
            return
        entries = git(repo, "worktree", "list", "--porcelain", "-z", text=False).split(b"\0")
        for entry in entries:
            if not entry.startswith(b"worktree "):
                continue
            path = Path(os.path.normpath(os.fsdecode(entry.removeprefix(b"worktree "))))
            if not path.is_relative_to(session):
                continue
            try:
                git(repo, "worktree", "remove", "--force", "--force", str(path))
            except (OSError, subprocess.CalledProcessError) as error:
                cleanup_failed(f"worktree remove {path}", error)
                if path.exists():
                    continue
                # A missing checkout may no longer be removable through Git. Remove
                # only its matching registration, including in a linked repository.
                try:
                    common = Path(os.fsdecode(git(repo, "rev-parse", "--git-common-dir", text=False)))
                    if not common.is_absolute():
                        common = repo / common
                    for admin in (common / "worktrees").iterdir():
                        try:
                            registered = Path(os.path.normpath(os.fsdecode(
                                (admin / "gitdir").read_bytes().removesuffix(b"\n"))))
                            if registered == path / ".git":
                                shutil.rmtree(admin)
                                break
                        except (OSError, UnicodeError) as error:
                            cleanup_failed(f"worktree registration {admin / 'gitdir'}", error)
                except (OSError, subprocess.CalledProcessError) as error:
                    cleanup_failed(f"worktree registration for {path}", error)
    except (OSError, subprocess.CalledProcessError) as error:
        cleanup_failed(f"session worktrees under {session}", error)


def stop_session(unit, process):
    if unit:
        try:
            result = subprocess.run(["systemctl", "--user", "stop", unit],
                                    text=True, capture_output=True)
            gone = f"unit {unit} not loaded" in result.stderr.lower()
            if result.returncode and not gone:
                cleanup_failed(f"scope {unit}", result.stderr.strip() or f"exit {result.returncode}")
        except OSError as error:
            cleanup_failed(f"scope {unit}", error)
    if process is not None and (not unit or process.poll() is None):
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError as error:
            cleanup_failed(f"process group {process.pid}", error)
    if process is not None:
        process.wait()


def interrupted(signum, frame):
    raise KeyboardInterrupt(signum)


def check_entries(repo, method, private):
    entry = repo / "AGENTS.md"
    path = entry
    try:
        if not entry.is_file():
            return
        first, _, body = entry.read_text(encoding="utf-8", newline="").partition("\n")
        match = re.match(r"<!-- Agent System entry for project ([\w.-]+)\.", first)
        if not match:
            return
        project_id = match[1]
        installed = method.expanduser().resolve()
        path = installed / "AGENTS.md"
        if not path.is_file():
            raise FileNotFoundError(f"missing {path}")
        expected = path.read_text(encoding="utf-8", newline="").replace("{{PROJECT}}", project_id).partition("\n")[2]
        differences = [entry] if body != expected else []
        path = installed / "SOURCE"
        source = path.read_text(encoding="utf-8") if path.is_file() else ""
        source_path = next((line.removeprefix("Path: ") for line in source.splitlines()
                            if line.startswith("Path: ")), "")
        setup = str(Path(source_path) / "scripts/setup.py") if source_path else "setup.py"
        command = shlex.join(["python3", setup, "--project", str(repo), "--id", project_id,
                              "--method", str(method), "--private", private, "--dry-run"])
        path = repo / ".claude/agents/builder.md"
        if path.is_file():
            builder = path
            actual = path.read_text(encoding="utf-8", newline="")
            path = installed / "claude-builder.md"
            if path.is_file() and actual != path.read_text(encoding="utf-8", newline=""):
                differences.append(builder)
    except (OSError, ValueError) as error:
        print(f"Warning: cannot check {path}: {error}", file=sys.stderr)
        return
    for path in differences:
        print(f"Warning: {path} differs from the installed method; see {command}", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--role", required=True, choices=("builder", "investigator", "verifier", "challenger", "reviewer"))
    parser.add_argument("--family", required=True, choices=("claude", "codex"))
    parser.add_argument("--brief", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--repo", default=Path.cwd(), type=Path)
    parser.add_argument("--author-family", choices=("claude", "codex"))
    parser.add_argument("--same-family-reason")
    parser.add_argument("--private", default=DEFAULT_PRIVATE)
    parser.add_argument("--timeout", type=float)
    parser.add_argument("--writable", action="append", default=[], type=Path, metavar="DIR")
    local = Path(__file__).resolve().parent
    parser.add_argument("--method", default=local if (local / "roles").is_dir()
                        else Path("~/.local/share/agent-method"), type=Path)
    args = parser.parse_args()
    if args.writable and args.role not in ("builder", "verifier"):
        parser.error("--writable is allowed for only builder and verifier roles")
    if args.writable and args.family != "codex":
        parser.error("--writable is supported only for the codex family")
    review = args.role not in ("builder", "investigator")
    # Investigators may read the private records; checkers and builders may not.
    shielded = args.role != "investigator"
    if review and not args.author_family:
        parser.error("review roles require --author-family")
    if review and args.author_family == args.family and not (args.same_family_reason or "").strip():
        parser.error("same-family review requires --same-family-reason")
    if args.timeout is not None and args.timeout <= 0:
        parser.error("--timeout must be positive")
    brief = args.brief.expanduser().read_text()
    private = Path(args.private).expanduser().resolve()
    repo, out = args.repo.expanduser().resolve(), args.out.expanduser().resolve()
    roots = [path.expanduser().resolve() for path in args.writable]
    for path in roots:
        if (path in (Path("/"), Path.home().resolve()) or path.is_relative_to(private)
                or private.is_relative_to(path)):
            parser.error(f"unsafe --writable root {path}: must not open home or the private folder")
        if args.role == "verifier" and (path.is_relative_to(repo) or repo.is_relative_to(path)):
            parser.error(f"unsafe --writable root {path}: must not open the verifier's repository")
    private_forms = {args.private, str(Path(args.private).expanduser()), str(private)}
    if private.is_relative_to(Path.home()):
        private_forms.add("~/" + str(private.relative_to(Path.home())))
    if shielded and any(path in brief for path in private_forms):
        parser.error("brief contains the private folder path")
    method = args.method.expanduser().resolve()
    check_entries(repo, args.method, args.private)
    prompt = (method / "roles" / f"{args.role}.md").read_text()
    if args.role == "verifier":
        prompt += "\n" + (method / "guides/verification.md").read_text()
    prompt += "\n---\n" + brief
    model = (os.environ.get(f"AGENT_{args.family.upper()}_MODEL")
             or ("claude-opus-5-5" if args.family == "claude" else "gpt-6.1-sol"))
    executable = os.environ.get(f"AGENT_{args.family.upper()}_CMD") or args.family
    if shutil.which(executable) is None:
        print(f"Cannot find agent executable: {executable}", file=sys.stderr)
        return 127
    start, snapshot, code = utc(), "", 1
    skipped = []
    cache = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    if not cache.is_absolute():
        cache = Path.home() / ".cache"
    root = cache / "agent-method/sessions"
    try:
        root.mkdir(parents=True, exist_ok=True)
        session = tempfile.TemporaryDirectory(prefix="agent-session-", dir=root)
    except OSError as error:
        print(f"Cannot create session folder under {root}: {error}", file=sys.stderr)
        return 1
    previous_sigterm = signal.signal(signal.SIGTERM, interrupted)
    previous_sighup = signal.getsignal(signal.SIGHUP)
    if previous_sighup != signal.SIG_IGN:
        signal.signal(signal.SIGHUP, interrupted)
    previous_sigint = signal.getsignal(signal.SIGINT)
    try:
        temp = Path(session.name).resolve()
        temporary = temp / "tmp"
        try:
            temporary.mkdir()
        except OSError as error:
            print(f"Cannot create session temporary folder {temporary}: {error}", file=sys.stderr)
            return 1
        agent_env = dict(os.environ, TMPDIR=str(temporary), TMP=str(temporary), TEMP=str(temporary))
        work, answer, source = temp / "work", temp / "answer", temp / "evidence"
        process = None
        systemd = shutil.which("systemd-run")
        unit = f"agent-{args.role}-{temp.name.removeprefix('agent-session-')}.scope" if systemd else ""
        try:
            cwd = repo
            if review:
                env = dict(os.environ, GIT_INDEX_FILE=str(temp / "index"))
                try:
                    head = git(repo, "rev-parse", "--verify", "-q", "HEAD")
                except subprocess.CalledProcessError:
                    head = ""
                index = Path(git(repo, "rev-parse", "--git-path", "index"))
                if not index.is_absolute():
                    index = repo / index
                if index.exists():
                    shutil.copyfile(index, temp / "index")
                else:
                    git(repo, "read-tree", head or "--empty", env=env)
                git(repo, "add", "-u", env=env)
                untracked = subprocess.check_output(
                    ["git", "-C", str(repo), "ls-files", "--others", "--exclude-standard", "-z"],
                    env=env)
                paths = []
                for raw in untracked.split(b"\0"):
                    if not raw:
                        continue
                    path = os.fsdecode(raw)
                    if (repo / path).lstat().st_size > 5 * 1024 * 1024:
                        skipped.append(path)
                    else:
                        paths.append(path)
                if paths:
                    git(repo, "--literal-pathspecs", "add", "--pathspec-from-file=-",
                        "--pathspec-file-nul", env=env, input="\0".join(paths) + "\0")
                tree = git(repo, "write-tree", env=env)
                for kind in ("AUTHOR", "COMMITTER"):
                    env.setdefault(f"GIT_{kind}_NAME", "Agent snapshot")
                    env.setdefault(f"GIT_{kind}_EMAIL", "snapshot@localhost")
                snapshot = git(repo, "commit-tree", tree, *(["-p", head] if head else []),
                               env=env, input="Review snapshot\n")
                git(repo, "worktree", "add", "--detach", str(work), snapshot)
                cwd = work
            if args.role == "verifier":
                source.mkdir()
                prompt += (f"\nEvidence folder: {source}; only this folder is kept; "
                           "nothing else in the session folder is kept.\n")
            for path in roots:
                path.mkdir(parents=True, exist_ok=True)
            writable = args.role in ("builder", "verifier")
            if writable:
                prompt += (f"\nTemporary folder: {temporary}; removed when the session ends; "
                           "create before or known-good checkouts as shared clones inside it "
                           "(git clone --quiet --shared --no-checkout . \"$TMPDIR/before\").\n")
            if args.family == "codex":
                effort = os.environ.get("AGENT_CODEX_EFFORT") or "high"
                command = [executable, "exec", "-m", model, "-c", f"model_reasoning_effort={effort}",
                           "-s", "workspace-write" if writable else "read-only",
                           "-C", str(cwd), "-o", str(answer)]
                for path in [*roots, *([source] if args.role == "verifier" else []),
                             *([temporary] if writable else [])]:
                    command += ["--add-dir", str(path)]
                command += ["-"]
            else:
                settings = {"permissions": {"deny": [f"{tool}(/{private}/**)"
                                                     for tool in ("Read", "Edit")] if shielded else []}}
                tools = "Read,Grep,Glob,Bash,Write,Edit" if writable else "Read,Grep,Glob"
                command = [executable, "-p", "--model", model, "--output-format", "text",
                           "--settings", json.dumps(settings), "--allowedTools",
                           tools, "--tools", tools, "--strict-mcp-config"]
                if args.role == "verifier":
                    command += ["--add-dir", str(source)]
                if writable:
                    command += ["--add-dir", str(temporary)]
            if unit:
                command = [systemd, "--user", "--scope", "--quiet", "--collect",
                           "--slice=agents.slice", f"--unit={unit}", "--", *command]
            else:
                print("systemd-run unavailable; running without a scope", file=sys.stderr)
            with (answer.open("wb") if args.family == "claude" else open(os.devnull, "wb")) as stdout:
                out.parent.mkdir(parents=True, exist_ok=True)
                with out.with_suffix(".log").open("wb") as log:
                    process = subprocess.Popen(command, cwd=cwd, stdin=subprocess.PIPE, stdout=stdout,
                                               stderr=log, env=agent_env, start_new_session=True)
                try:
                    process.communicate(prompt.encode(), timeout=args.timeout)
                    code = process.returncode
                except subprocess.TimeoutExpired:
                    code = 124
        except KeyboardInterrupt as error:
            code = 128 + (error.args[0] if error.args else signal.SIGINT)
        except OSError as error:
            print(f"Session failed: {error}", file=sys.stderr)
            code = 127 if isinstance(error, FileNotFoundError) else 1
        finally:
            # Finish saving and cleanup even if another termination signal arrives.
            signal.signal(signal.SIGINT, signal.SIG_IGN)
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            signal.signal(signal.SIGHUP, signal.SIG_IGN)
            stop_session(unit, process)
            try:
                answer_bytes = answer.read_bytes() if answer.exists() else b""
                if not answer_bytes.decode("utf-8", errors="replace").strip():
                    print("Session failed: agent produced no non-empty answer.", file=sys.stderr)
                    code = code or 1
                if code and out.with_suffix(".log").exists():
                    with out.with_suffix(".log").open(errors="replace") as log:
                        sys.stderr.writelines(deque(log, maxlen=20))
                end = utc()
                header = (f"Role: {args.role}\nFamily: {args.family}\nModel: {model}\n"
                          f"Author family: {args.author_family or ''}\n"
                          f"Same-family reason: {args.same_family_reason or ''}\n"
                          f"Scope: {unit or 'none'}\n"
                          + (f"Snapshot commit: {snapshot}\n" if review else "")
                          + (f"Skipped untracked (>5 MB): {', '.join(skipped)}\n" if skipped else "")
                          + f"Start UTC: {start}\nEnd UTC: {end}\nExit code: {code}\n\n")
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_bytes(header.encode() + answer_bytes)
                evidence = out.with_name(out.stem + "-evidence")
                try:
                    if evidence.is_symlink() or evidence.is_file():
                        evidence.unlink()
                    elif evidence.exists():
                        shutil.rmtree(evidence)
                    if args.role == "verifier" and source.is_dir() and any(source.iterdir()):
                        shutil.copytree(source, evidence, symlinks=True)
                except OSError as error:
                    print(f"Evidence failed: {evidence}: {error}", file=sys.stderr)
            finally:
                cleanup_worktrees(repo, temp)
    finally:
        try:
            session.cleanup()
        except OSError as error:
            cleanup_failed(f"temporary folder {session.name}", error)
        signal.signal(signal.SIGTERM, previous_sigterm)
        signal.signal(signal.SIGHUP, previous_sighup)
        signal.signal(signal.SIGINT, previous_sigint)
    return code


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, subprocess.CalledProcessError) as error:
        print(f"Launcher failed: {error}", file=sys.stderr)
        sys.exit(1)
