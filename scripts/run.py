#!/usr/bin/env python3
"""Run one builder, investigator or isolated review session. Python 3.13+."""
import sys

if sys.version_info < (3, 13):
    sys.exit("Agent System launcher requires Python 3.13 or newer.")

import argparse
from collections import deque
from datetime import datetime, timezone
import fcntl
import hashlib
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
POOL_SLOTS = 3
POOL_USES = 20
GIT_CONFIG = ("-c", "core.fsmonitor=false", "-c", "core.splitIndex=false")
CHECKOUT_CONFIG = ("-c", "core.sparseCheckout=false", "-c", "core.sparseCheckoutCone=false",
                   "-c", "index.sparse=false", "-c", "core.attributesFile=")
BUILD_INPUTS = {
    "package-lock.json", "npm-shrinkwrap.json", "yarn.lock", "pnpm-lock.yaml",
    "Cargo.lock", "Gemfile.lock", "composer.lock", "flake.lock",
    "bun.lock", "bun.lockb", ".java-version", "go.mod", "go.sum", "uv.lock", "poetry.lock", "Pipfile.lock",
    "pdm.lock", "pylock.toml", "gradle.lockfile", "gradle-wrapper.properties",
    "gradle.properties", "settings.gradle", "settings.gradle.kts",
    "build.gradle", "build.gradle.kts", "libs.versions.toml",
    ".nvmrc", ".node-version", ".tool-versions", ".python-version",
    "pyproject.toml", "setup.py", "setup.cfg",
}

# This supervisor, outside systemd-run, owns the inherited leases. Linux's
# subreaper keeps even double-forked children attached to it if the launcher dies.
# Do not pass the descriptors to the agent: programs routinely close extra fds.
LEASE_GUARD = r'''
import ctypes, os, signal, subprocess, sys
launcher = int(sys.argv[4])
libc = ctypes.CDLL(None, use_errno=True)
if libc.prctl(36, 1, 0, 0, 0):
    raise OSError(ctypes.get_errno(), "cannot become lease subreaper")
def interrupted(signum, frame):
    raise SystemExit(128 + signum)
for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
    signal.signal(sig, interrupted)
def kill_children(pid):
    try:
        children = open(f"/proc/{pid}/task/{pid}/children").read().split()
    except FileNotFoundError:
        return
    for child in children:
        kill_children(int(child))
        try:
            os.kill(int(child), signal.SIGKILL)
        except ProcessLookupError:
            pass
try:
    # Arrange this after installing handlers, then close the race where the
    # launcher died before prctl. Scope membership does not change ancestry.
    if libc.prctl(1, signal.SIGTERM, 0, 0, 0):
        raise OSError(ctypes.get_errno(), "cannot watch launcher death")
    if os.getppid() != launcher:
        raise SystemExit(143)
    process = subprocess.Popen(sys.argv[5:])
    code = process.wait()
finally:
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, signal.SIG_IGN)
    # Reap orphaned descendants too. Repeat because killing a child may adopt
    # its children. The locks stay held until there are no children left.
    while True:
        kill_children(os.getpid())
        try:
            os.waitpid(-1, 0)
        except ChildProcessError:
            break
    if os.getppid() != launcher:
        import pathlib, runpy, shutil
        helpers = runpy.run_path(sys.argv[1])
        helpers["cleanup_worktrees"](pathlib.Path(sys.argv[2]), pathlib.Path(sys.argv[3]))
        shutil.rmtree(sys.argv[3], ignore_errors=True)
sys.exit(code if code >= 0 else 128 - code)
'''


def git(repo, *args, env=None, input=None, text=True):
    result = subprocess.check_output(["git", "--no-optional-locks", "-C", str(repo), *GIT_CONFIG, *args],
                                     env=env, input=input, text=text)
    return result.strip() if text else result.rstrip(b"\n")


def utc():
    return datetime.now(timezone.utc).isoformat()


def write_bytes():
    try:
        values = dict(line.split(":") for line in Path("/proc/self/io").read_text().splitlines())
        return int(values["write_bytes"])
    except (OSError, ValueError, KeyError, UnicodeError):
        return None


def cleanup_failed(what, error):
    print(f"Cleanup failed: {what}: {error}", file=sys.stderr)


def cleanup_worktrees(repo, session):
    try:
        probe = subprocess.run(["git", "--no-optional-locks", "-C", str(repo), *GIT_CONFIG,
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


def stop_session(unit, process, guarded=False):
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
            if guarded:
                process.terminate()  # The supervisor stops and reaps all descendants.
            else:
                os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError as error:
            cleanup_failed(f"process group {process.pid}", error)
    if process is not None:
        process.wait()


def interrupted(signum, frame):
    raise KeyboardInterrupt(signum)


def discover_servers(executable, cwd, env, timeout, log):
    process = None
    try:
        process = subprocess.Popen([executable, "mcp", "list", "--json"], cwd=cwd, env=env,
                                   stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=log, start_new_session=True)
        stdout, _ = process.communicate(timeout=timeout)
        if process.returncode:
            return [], f"discovery exited {process.returncode}"
        try:
            servers = json.loads(stdout)
        except (ValueError, UnicodeError):
            return [], "discovery returned invalid JSON"
        if not isinstance(servers, list) or any(
                not isinstance(server, dict) or not isinstance(server.get("name"), str)
                or not isinstance(server.get("enabled"), bool) for server in servers):
            return [], "discovery returned an invalid server list"
        return [server["name"] for server in servers if server["enabled"]], ""
    except subprocess.TimeoutExpired:
        return [], f"discovery timed out after {timeout:g} seconds"
    except (OSError, UnicodeError) as error:
        return [], f"discovery failed: {error}"
    finally:
        stop_session("", process)


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


def review_leave_out(repo, private, head, snapshot):
    path = repo / "AGENTS.md"
    try:
        if not path.is_file():
            return []
        first = path.read_text(encoding="utf-8").split("\n", 1)[0]
        match = re.match(r"<!-- Agent System entry for project ([\w.-]+)\.(?: |$)", first)
        if not match or match[1] in (".", ".."):
            return []
        path = private / "projects" / match[1] / "review-leave-out.txt"
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return []
    except (OSError, UnicodeError) as error:
        print(f"Warning: cannot read {path}: {error}; using a full checkout", file=sys.stderr)
        return []
    entries = []
    for line in lines:
        if not line.strip() or line.startswith("#"):
            continue
        entry = os.path.normpath(line)
        if (line.startswith("/") or ".." in line.split("/") or entry == "." or "\0" in line
                or entry.split("/", 1)[0] in ("AGENTS.md", "CLAUDE.md", ".claude", ".codex")):
            print(f"Warning: ignoring invalid leave-out path {line!r}", file=sys.stderr)
        elif entry not in entries:
            entries.append(entry)
    if not entries:
        return []
    # HEAD keeps deleted paths eligible for the full-checkout fallback.
    files = set()
    for revision in (snapshot, *([head] if head else [])):
        files.update(os.fsdecode(raw) for raw in git(
            repo, "--literal-pathspecs", "ls-tree", "--full-tree", "-r", "--name-only", "-z", revision,
            "--", *entries, text=False).split(b"\0") if raw)
    return [entry for entry in entries
            if any(name == entry or name.startswith(entry + "/") for name in files)]


def check_checkout(work, commit):
    if (git(work, "rev-parse", "HEAD") != commit
            or git(work, "rev-parse", "--abbrev-ref", "HEAD") != "HEAD"
            or git(work, *CHECKOUT_CONFIG, "-c", "status.showUntrackedFiles=all",
                   "status", "--porcelain", "--ignored=no", "--untracked-files=all")):
        raise ValueError("checkout is not clean and detached at the candidate")


def reset_checkout(work, commit, deep_clean):
    admin = Path(git(work, "rev-parse", "--absolute-git-dir"))
    attributes = admin / "info/attributes"
    rewrite = os.path.lexists(attributes)
    attributes.unlink(missing_ok=True)
    # This belongs only to the slot. Previous agents can leave filters, excludes,
    # hooks or fsmonitor commands here, not just sparse-checkout settings.
    (admin / "config.worktree").unlink(missing_ok=True)
    worktree_config = git(work, "config", "--type=bool", "--default=false", "--get",
                         "extensions.worktreeConfig") == "true"
    if worktree_config:
        # Only write the slot's config, never the repository's shared config.
        for setting in ("core.sparseCheckout", "core.sparseCheckoutCone", "index.sparse"):
            git(work, "config", "--worktree", setting, "false")
    (admin / "info/sparse-checkout").unlink(missing_ok=True)
    for state in ("MERGE_HEAD", "MERGE_MSG", "MERGE_MODE", "MERGE_AUTOSTASH", "AUTO_MERGE",
                  "CHERRY_PICK_HEAD", "REVERT_HEAD", "REBASE_HEAD", "SQUASH_MSG",
                  "rebase-merge", "rebase-apply", "sequencer",
                  *(path.name for path in admin.glob("BISECT_*"))):
        path = admin / state
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink(missing_ok=True)
    # Rebuild from the OLD committed tree first: even an index deletion of a
    # now-ignored file must be restored so checkout can remove it. A new index
    # also discards skip-worktree, assume-unchanged and staged state.
    index = Path(git(work, "rev-parse", "--path-format=absolute", "--git-path", "index"))
    index.unlink(missing_ok=True)
    git(work, *CHECKOUT_CONFIG, "-c", "core.ignorestat=false", "read-tree", "HEAD")
    # Include ignored attributes, and use the rebuilt index so staged additions
    # cannot masquerade as committed attributes. Remove them before any refresh.
    for raw in git(work, *CHECKOUT_CONFIG, "ls-files", "--others", "-z", "--",
                   ".gitattributes", ":(glob)**/.gitattributes", text=False).split(b"\0"):
        if raw:
            (work / os.fsdecode(raw)).unlink()
            rewrite = True
    changed = git(work, *CHECKOUT_CONFIG, "diff", "--no-renames", "--name-only", "-z",
                  "HEAD", commit, text=False).split(b"\0")
    rewrite |= any(Path(os.fsdecode(path)).name == ".gitattributes" for path in changed if path)
    rewrite |= bool(git(work, *CHECKOUT_CONFIG, "diff", "--name-only", "--", ".gitattributes",
                        ":(glob)**/.gitattributes"))
    # Refresh stat data without writing working files, preserving warm leases'
    # unchanged files. Exit 1 means modified files; checkout below restores them.
    if not rewrite:
        refresh = subprocess.run(["git", "--no-optional-locks", "-C", str(work),
                                  *GIT_CONFIG, *CHECKOUT_CONFIG,
                                  "-c", "core.ignorestat=false", "update-index", "--refresh"],
                                 capture_output=True)
        if refresh.returncode not in (0, 1):
            refresh.check_returncode()
    git(work, *CHECKOUT_CONFIG, "-c", "core.ignorestat=false", "checkout", "--force", "--detach", commit)
    if rewrite:
        # Checkout must use the NEW attributes even for unchanged blobs. Its
        # transition above also removes paths tracked only by the previous tree.
        # Discard stat data again: diff/checkout can cache paths as up to date
        # under the old attributes, and checkout-index --force trusts that data.
        index.unlink()
        git(work, *CHECKOUT_CONFIG, "-c", "core.ignorestat=false", "read-tree", commit)
        git(work, *CHECKOUT_CONFIG, "checkout-index", "--all", "--force")
    git(work, *CHECKOUT_CONFIG, "-c", "core.excludesFile=", "clean", "-ffd" + ("x" if deep_clean else ""))
    check_checkout(work, commit)


def lease_checkout(repo, cache, commit, fresh, leases, force_clean):
    common = Path(git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")).resolve()
    main = os.fsdecode(git(repo, "worktree", "list", "--porcelain", "-z",
                           text=False).split(b"\0", 1)[0]).removeprefix("worktree ")
    key = hashlib.sha256(os.fsencode(common)).hexdigest()[:12]
    pool = (cache / "am-pool" / f"{Path(main).name}-{key}").resolve()
    pool.mkdir(parents=True, exist_ok=True)
    for number in range(1, POOL_SLOTS + 1):
        slot = pool / f"slot-{number}"
        lock = (pool / f"slot-{number}.lock").open("a+b")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lock.close()
            continue
        leases.append(lock)
        metadata = pool / f"slot-{number}.json"
        reason = ""
        try:
            previous = json.loads(metadata.read_text())
            if (type(previous["uses"]) is not int or not 0 <= previous["uses"] <= POOL_USES
                    or not re.fullmatch(r"[0-9a-f]{40,64}", previous["commit"])):
                raise ValueError("invalid metadata")
        except (OSError, ValueError, KeyError, TypeError):
            previous = {"uses": 0, "commit": ""}
            reason = "missing or corrupt metadata"
        if force_clean:
            reason = "--clean-checkout"
        elif previous["uses"] >= POOL_USES:
            reason = f"{POOL_USES} uses"
        elif previous["commit"]:
            try:
                changed = git(repo, "diff", "--no-renames", "--name-only", "-z",
                              previous["commit"], commit, text=False).split(b"\0")
                if any(Path(os.fsdecode(path)).name in BUILD_INPUTS
                       or Path(os.fsdecode(path)).name.startswith(("rust-toolchain", "requirements"))
                       or os.fsdecode(path).endswith(".lockfile") for path in changed if path):
                    reason = "lockfile or toolchain changed"
                if any(Path(os.fsdecode(path)).name == "Cargo.toml" for path in changed if path):
                    reason = (reason + "; " if reason else "") + "Cargo manifest changed"
                removed = git(repo, "diff", "--no-renames", "--diff-filter=D", "--name-only", "-z",
                              previous["commit"], commit, text=False).split(b"\0")
                if any(Path(os.fsdecode(path)).name == "main.rs"
                       or "/src/bin/" in "/" + os.fsdecode(path) for path in removed if path):
                    reason = (reason + "; " if reason else "") + "Cargo executable source removed"
            except subprocess.CalledProcessError:
                reason = "missing previous commit"
        # Absence is an interrupted-reset marker, including when checkout hooks fail.
        metadata.unlink(missing_ok=True)
        try:
            registered = git(repo, "worktree", "list", "--porcelain", "-z", text=False)
            if (b"worktree " + os.fsencode(slot.resolve()) + b"\0" not in registered
                    or slot.is_symlink() or not slot.is_dir()
                    or Path(git(slot, "rev-parse", "--path-format=absolute", "--git-common-dir")).resolve() != common):
                raise ValueError("missing checkout or registration")
            if previous["commit"] != git(slot, "rev-parse", "HEAD"):
                reason = reason or "checkout differs from metadata"
            reset_checkout(slot, commit, bool(reason))
        except (OSError, ValueError, subprocess.CalledProcessError) as error:
            reason = (reason + "; " if reason else "") + f"recreated ({error})"
            try:
                cleanup_worktrees(repo, slot)
                if slot.is_symlink():
                    slot.unlink()
                elif slot.exists():
                    shutil.rmtree(slot)
                cleanup_worktrees(repo, slot)
                git(repo, *CHECKOUT_CONFIG, "worktree", "add", "--detach", str(slot), commit)
                reset_checkout(slot, commit, True)
            except (OSError, ValueError, subprocess.CalledProcessError):
                cleanup_worktrees(repo, slot)
                git(repo, *CHECKOUT_CONFIG, "worktree", "add", "--detach", str(fresh), commit)
                check_checkout(fresh, commit)
                return fresh, "pool slot broken; fresh session worktree"
        metadata.with_suffix(".json.tmp").write_text(json.dumps({
            "uses": 1 if reason else previous["uses"] + 1, "commit": commit}))
        metadata.with_suffix(".json.tmp").replace(metadata)
        return slot, f"pool {slot}; " + (f"deep clean: {reason}" if reason else "warm reset")
    git(repo, *CHECKOUT_CONFIG, "worktree", "add", "--detach", str(fresh), commit)
    check_checkout(fresh, commit)
    return fresh, "pool busy; fresh session worktree"


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
    parser.add_argument("--full-checkout", action="store_true",
                        help="include all paths even when a review leave-out list is configured")
    parser.add_argument("--before", help="lease a second verifier checkout at this commit")
    parser.add_argument("--clean-checkout", action="store_true",
                        help="deep-clean verifier checkouts, including ignored build output")
    parser.add_argument("--timeout", type=float)
    parser.add_argument("--writable", action="append", default=[], type=Path, metavar="DIR")
    parser.add_argument("--network", action="store_true")
    local = Path(__file__).resolve().parent
    parser.add_argument("--method", default=local if (local / "roles").is_dir()
                        else Path("~/.local/share/agent-method"), type=Path)
    args = parser.parse_args()
    if (args.before or args.clean_checkout) and args.role != "verifier":
        parser.error("--before and --clean-checkout are allowed only for verifiers")
    if args.network and args.role not in ("builder", "verifier"):
        parser.error("--network is allowed for only builder and verifier roles")
    if args.network and args.family != "codex":
        parser.error("--network is supported only for the codex family")
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
    candidate, before_commit, untracked = "", "", []
    if args.role == "verifier":
        try:
            repo = Path(git(repo, "rev-parse", "--show-toplevel")).resolve()
            candidate = git(repo, "rev-parse", "--verify", "HEAD^{commit}")
        except subprocess.CalledProcessError:
            parser.error("commit first: verifier requires a committed candidate")
        # Check both paths: the index may differ even if the worktree matches HEAD.
        # diff can refresh the index even with --no-optional-locks. Use a copy
        # outside the source tree, including for refusal before leasing a slot.
        with tempfile.NamedTemporaryFile(prefix="agent-index-") as check:
            index = Path(git(repo, "rev-parse", "--path-format=absolute", "--git-path", "index"))
            env = dict(os.environ, GIT_INDEX_FILE=check.name)
            if index.exists():
                shutil.copyfile(index, env["GIT_INDEX_FILE"])
            else:
                Path(check.name).unlink()
                git(repo, "read-tree", "--empty", env=env)
            flagged = []
            for entry in git(repo, "ls-files", "-v", "-z", env=env, text=False).split(b"\0"):
                if not entry:
                    continue
                flag, path = entry[:1], entry[2:]
                if (flag == b"S" or flag.islower()) and (
                        flag.lower() != b"s" or os.path.lexists(repo / os.fsdecode(path))):
                    flagged.append(path)
            if flagged:
                # Clear flags only in the copy. Absent sparse files stay skipped.
                for flag in ("--no-skip-worktree", "--no-assume-unchanged"):
                    git(repo, "update-index", flag, "-z", "--stdin", env=env,
                        input=b"\0".join(flagged) + b"\0", text=False)
            if (git(repo, "diff", "--name-only", candidate, "--", env=env)
                    or git(repo, "diff", "--cached", "--name-only", candidate, "--", env=env)):
                parser.error("commit first: verifier requires tracked files to match HEAD")
        untracked = [os.fsdecode(path) for path in git(
            repo, "ls-files", "--others", "--exclude-standard", "--full-name", "-z", text=False).split(b"\0") if path]
        if args.before:
            before_commit = git(repo, "rev-parse", "--verify", args.before + "^{commit}")
    cache = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    if not cache.is_absolute():
        cache = Path.home() / ".cache"
    roots = [path.expanduser().resolve() for path in args.writable]
    for path in roots:
        if (path in (Path("/"), Path.home().resolve()) or path.is_relative_to(private)
                or private.is_relative_to(path)):
            parser.error(f"unsafe --writable root {path}: must not open home or the private folder")
        if args.role == "verifier" and (path.is_relative_to(repo) or repo.is_relative_to(path)):
            parser.error(f"unsafe --writable root {path}: must not open the verifier's repository")
        if any(path.is_relative_to(root) or root.is_relative_to(path)
               for root in ((cache / "am").resolve(), (cache / "am-pool").resolve())):
            parser.error(f"unsafe --writable root {path}: must not open launcher sessions or checkout pools")
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
    found = shutil.which(executable)
    if found is None:
        print(f"Cannot find agent executable: {executable}", file=sys.stderr)
        return 127
    executable = os.path.abspath(found)
    restricted = args.family == "codex" and args.role in ("challenger", "reviewer")
    environment = ("restricted; discovery did not run"
                   if restricted else "user configuration")
    start, snapshot, code = utc(), "", 1
    skipped = []
    checkout_note, before_note = "", ""
    before = None
    leases = []
    root = cache / "am"
    try:
        root.mkdir(parents=True, exist_ok=True)
        initial_io = write_bytes()
        session = tempfile.TemporaryDirectory(prefix="", dir=root)
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
        length = len(os.fsencode(temporary))
        if length > 62:
            print(f"Warning: TMPDIR is {length} bytes; programs such as Chromium may fail "
                  "to create Unix sockets in it above 62 bytes; starting anyway",
                  file=sys.stderr)
        agent_env = dict(os.environ, TMPDIR=str(temporary), TMP=str(temporary), TEMP=str(temporary))
        work, answer, source = temp / "work", temp / "answer", temp / "evidence"
        process = None
        systemd = shutil.which("systemd-run")
        unit = f"agent-{args.role}-{temp.name}.scope" if systemd else ""
        try:
            cwd = repo
            if args.role == "verifier":
                snapshot = candidate
                work, note = lease_checkout(repo, cache, candidate, work, leases, args.clean_checkout)
                checkout_note = "Checkout: " + note
                if before_commit:
                    before, note = lease_checkout(repo, cache, before_commit, temp / "before", leases,
                                                  args.clean_checkout)
                    before_note = f"Before checkout: {before} ({before_commit}); {note}"
                    prompt += "\n" + before_note + "\nUse this writable checkout for the before state.\n"
                cwd = work
            elif review:
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
                    ["git", "-C", str(repo), *GIT_CONFIG, "ls-files", "--others", "--exclude-standard", "-z"],
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
                git_only = args.family == "codex"
                leave_out = (review_leave_out(repo, private, head, snapshot)
                             if not git_only and not args.full_checkout else [])
                if git_only:
                    checkout_note = "Checkout: Git-only (root AGENTS.md and CLAUDE.md)"
                elif args.full_checkout:
                    checkout_note = "Checkout: full (--full-checkout)"
                elif leave_out:
                    base = head or git(repo, "hash-object", "-t", "tree", "--stdin", input="")
                    changed = [os.fsdecode(raw) for raw in git(
                        repo, "diff", "--no-renames", "--name-only", "-z", base, snapshot,
                        text=False).split(b"\0") if raw]
                    touched = [name for name in changed if any(
                        name == path or name.startswith(path + "/") for path in leave_out)]
                    if touched:
                        checkout_note = (f"Checkout: full because snapshot changes touch leave-out paths "
                                         f"{leave_out!r}: {touched!r}")
                        leave_out = []
                    else:
                        checkout_note = f"Checkout: left-out paths {leave_out!r}"
                if leave_out or git_only:
                    git(repo, "worktree", "add", "--no-checkout", "--detach", str(work), snapshot)
                    git(work, "read-tree", snapshot)
                    if git_only:
                        omitted = b"\0".join(path for path in git(work, "ls-files", "-z", text=False).split(b"\0")
                                             if path and path not in (b"AGENTS.md", b"CLAUDE.md")) + b"\0"
                    else:
                        omitted = git(work, "--literal-pathspecs", "ls-files", "-z", "--",
                                      *leave_out, text=False)
                    git(work, "update-index", "-z", "--skip-worktree", "--stdin",
                        input=omitted, text=False)
                    git(work, "checkout-index", "-a")
                    if any(os.path.lexists(work / path) for path in leave_out):
                        checkout_note = ("Checkout: full because left-out paths were checked out anyway "
                                         "(Git older than 2.37?)")
                        leave_out = []
                else:
                    git(repo, "worktree", "add", "--detach", str(work), snapshot)
                if checkout_note:
                    prompt += "\n" + checkout_note + "\n"
                if git_only:
                    prompt += (f"Read the code through Git: `git show {snapshot}:<path>`, "
                               f"`git grep <pattern> {snapshot}` and `git diff`.\n")
                if leave_out:
                    hint = f" (`git show {snapshot}:<path>`)" if args.family == "codex" else ""
                    prompt += (f"These paths are absent from the file system but present in Git{hint}. "
                               "If your check needs them, say so in your answer.\n")
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
                             *([before] if before else []),
                             *([temporary] if writable else [])]:
                    command += ["--add-dir", str(path)]
                if args.network:
                    command += ["-c", "sandbox_workspace_write.network_access=true"]
                if restricted:
                    command += ["--disable", "plugins", "--disable", "apps",
                                "-c", "skills.include_instructions=false"]
                    out.parent.mkdir(parents=True, exist_ok=True)
                    with out.with_suffix(".log").open("wb") as log:
                        environment = "restricted with MCP servers not disabled: discovery did not complete"
                        servers, reason = discover_servers(executable, cwd, agent_env,
                                                           min(30, args.timeout or 30), log)
                    environment = "restricted"
                    if reason:
                        environment += f" with MCP servers not disabled: {reason}"
                        print(f"Warning: MCP servers were not disabled: {reason}", file=sys.stderr)
                    unsupported = []
                    for name in servers:
                        if re.fullmatch(r"[A-Za-z0-9_-]+", name):
                            command += ["-c", f"mcp_servers.{name}.enabled=false"]
                        else:
                            unsupported.append(ascii(name))
                    if unsupported:
                        reason = f"unsupported server names: {', '.join(unsupported)}"
                        environment += f" with MCP servers not all disabled: {reason}"
                        print(f"Warning: MCP servers were not all disabled: {reason}", file=sys.stderr)
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
                    if before:
                        command += ["--add-dir", str(before)]
                if writable:
                    command += ["--add-dir", str(temporary)]
            if unit:
                command = [systemd, "--user", "--scope", "--quiet", "--collect",
                           "--slice=agents.slice", f"--unit={unit}", "--", *command]
            else:
                print("systemd-run unavailable; running without a scope", file=sys.stderr)
            if leases:
                command = [sys.executable, "-c", LEASE_GUARD, str(Path(__file__).resolve()),
                           str(repo), str(temp), str(os.getpid()), *command]
            with (answer.open("wb") if args.family == "claude" else open(os.devnull, "wb")) as stdout:
                out.parent.mkdir(parents=True, exist_ok=True)
                with out.with_suffix(".log").open("ab" if restricted else "wb") as log:
                    process = subprocess.Popen(command, cwd=cwd, stdin=subprocess.PIPE, stdout=stdout,
                                               stderr=log, env=agent_env, start_new_session=True,
                                               pass_fds=tuple(lock.fileno() for lock in leases))
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
            stop_session(unit, process, guarded=bool(leases))
            try:
                answer_bytes = answer.read_bytes() if answer.exists() else b""
                if not answer_bytes.decode("utf-8", errors="replace").strip():
                    print("Session failed: agent produced no non-empty answer.", file=sys.stderr)
                    code = code or 1
                if code and out.with_suffix(".log").exists():
                    with out.with_suffix(".log").open(errors="replace") as log:
                        sys.stderr.writelines(deque(log, maxlen=20))
                end = utc()
                final_io = write_bytes()
                writes = (f"{(final_io - initial_io) / 1024**2:.1f} MiB"
                          if initial_io is not None and final_io is not None else "unavailable")
                header = (f"Role: {args.role}\nFamily: {args.family}\nModel: {model}\n"
                          f"Executable: {executable}\n"
                          + (f"Requested sandbox: {'workspace-write' if args.role in ('builder', 'verifier') else 'read-only'}\n"
                             f"Network: {'on' if args.network else 'not requested (user config applies)'}\n"
                             f"Requested environment: {environment}\n" if args.family == "codex" else "")
                          + f"Author family: {args.author_family or ''}\n"
                          f"Same-family reason: {args.same_family_reason or ''}\n"
                          f"Scope: {unit or 'none'}\n"
                          + (f"Snapshot commit: {snapshot}\n" if review else "")
                          + (checkout_note + "\n" if checkout_note else "")
                          + (before_note + "\n" if before_note else "")
                          + ("Untracked files left out: " + ", ".join(ascii(path) for path in untracked[:20])
                             + (f"; {len(untracked) - 20} more" if len(untracked) > 20 else "") + "\n"
                             if args.role == "verifier" else "")
                          + (f"Skipped untracked (>5 MB): {', '.join(skipped)}\n" if skipped else "")
                          + f"Write bytes: {writes}\nStart UTC: {start}\nEnd UTC: {end}\nExit code: {code}\n\n")
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
        for lock in leases:
            lock.close()
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
