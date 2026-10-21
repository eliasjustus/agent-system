#!/usr/bin/env python3
"""Run one builder, investigator or isolated review session. Python 3.13+."""
import sys

if sys.version_info < (3, 13):
    sys.exit("Agent System launcher requires Python 3.13 or newer.")

import argparse
from collections import deque
from datetime import datetime, timezone
from functools import partial
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import stat
import subprocess
import tempfile


DEFAULT_PRIVATE = "~/dev/agent-private"  # Set by setup.
POOL_SLOTS = 5
POOL_USES = 20
GIT_CONFIG = ("-c", "core.fsmonitor=false", "-c", "core.splitIndex=false")
CHECKOUT_CONFIG = ("-c", "core.sparseCheckout=false", "-c", "core.sparseCheckoutCone=false",
                   "-c", "index.sparse=false", "-c", "core.hooksPath=/dev/null",
                   "-c", "core.untrackedCache=false", "-c", "submodule.recurse=false")
BUILD_INPUTS = {
    "package-lock.json", "npm-shrinkwrap.json", "yarn.lock", "pnpm-lock.yaml",
    "Cargo.lock", "Gemfile.lock", "composer.lock", "flake.lock",
    "bun.lock", "bun.lockb", "go.mod", "go.sum", "uv.lock", "poetry.lock", "Pipfile.lock",
    "pdm.lock", "pylock.toml", "gradle-wrapper.properties",
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
code = 143
stopping = False
def interrupted(signum, frame):
    global stopping
    if not stopping:
        stopping = True
        raise SystemExit(128 + signum)
for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
    signal.signal(sig, interrupted)
def kill_children(pid):
    try:
        children = open(f"/proc/{pid}/task/{pid}/children").read().split()
    except OSError as error:
        print(f"Warning: cannot list lease guard children for {pid}: {error}", file=sys.stderr, flush=True)
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
    stopping = True
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


def git(repo, *args, env=None, input=None, text=True, pass_fds=()):
    result = subprocess.check_output(["git", "--no-optional-locks", "-C", str(repo), *GIT_CONFIG, *args],
                                     env=env, input=input, text=text, pass_fds=pass_fds)
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


def registered_gitfile(admin):
    path = Path(os.fsdecode((admin / "gitdir").read_bytes().removesuffix(b"\n")))
    return Path(os.path.normpath(admin / path))


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
                            registered = registered_gitfile(admin)
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
    if process is not None and guarded and process.poll() is None:
        try:
            process.terminate()  # The supervisor stops and reaps all descendants.
        except ProcessLookupError:
            pass
        except OSError as error:
            cleanup_failed(f"lease guard {process.pid}", error)
        process.wait()
    if process is not None and (not unit or process.poll() is None):
        try:
            # A killed guard cannot reap its children. Without a scope, also
            # stop its process group, even when the guard has already exited.
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


def check_checkout(work, commit, pass_fds=()):
    run = partial(git, work, *CHECKOUT_CONFIG, pass_fds=pass_fds)
    # Status can report tracked files modified even immediately after a fresh
    # checkout (e.g. committed CRLF under eol=lf). Reused slots have their raw
    # content checked by checkout_mismatches; status judges only untracked files
    # and submodules.
    status = run("status", "--porcelain=v2", "-z", "--ignored=no", "--untracked-files=all",
                 "--ignore-submodules=none", text=False).split(b"\0")
    dirty = any(entry.startswith(b"? ") or (
        entry.startswith((b"1 ", b"2 ", b"u ")) and entry.split(b" ", 3)[2] != b"N...")
        for entry in status)
    if (run("rev-parse", "HEAD") != commit
            or run("rev-parse", "--abbrev-ref", "HEAD") != "HEAD" or dirty):
        raise ValueError("checkout is not clean and detached at the candidate")


def checkout_mismatches(work, pass_fds=(), paths=None, source=False):
    """Check raw content and (for slots) modes without writing the tree."""
    run = partial(git, work, *CHECKOUT_CONFIG, pass_fds=pass_fds)
    entries = []
    for entry in run("ls-files", "--stage", "-z", text=False).split(b"\0"):
        if entry:
            info, path = entry.split(b"\t", 1)
            if paths is not None and path not in paths:
                continue
            mode, blob, stage = info.split()
            if stage != b"0":
                raise ValueError("unmerged checkout index")
            if mode != b"160000":  # Submodules are checked by status, not as blobs.
                entries.append((path, mode, blob))
    symlinks = run("config", "--type=bool", "--default=true", "--get", "core.symlinks") == "true"
    umask = os.umask(0)
    os.umask(umask)
    mismatches = []
    literal = {path for path, mode, _ in entries if mode == b"120000"}
    regular = []
    for path, mode, blob in entries:
        file = work / os.fsdecode(path)
        try:
            info = file.lstat()
            actual = info.st_mode
        except (FileNotFoundError, NotADirectoryError):
            mismatches.append(path)
            continue
        if not source and info.st_nlink > 1:
            mismatches.append(path)
        elif mode == b"120000" and symlinks:
            if not stat.S_ISLNK(actual) or run("hash-object", "--stdin",
                    input=os.fsencode(os.readlink(file)), text=False) != blob:
                mismatches.append(path)
        elif (not stat.S_ISREG(actual)
              or (not source and stat.S_IMODE(actual) != (
                  (0o777 if mode == b"100755" else 0o666) & ~umask))):
            mismatches.append(path)
        else:
            regular.append((path, blob))
    if not regular:
        return mismatches
    # --stdin-paths accepts Git's C-quoted filenames, including embedded newlines.
    quoted = b"".join(b'"' + b"".join(
        bytes([byte]) if 32 <= byte < 127 and byte not in (34, 92)
        else f"\\{byte:03o}".encode() for byte in path) + b'"\n' for path, _ in regular)
    hashes = run("hash-object", "--no-filters", "--stdin-paths",
                 input=quoted, text=False).splitlines()
    if len(hashes) != len(regular):
        raise ValueError("incomplete checkout content check")
    # Raw bytes equal to a blob can still need checkout conversion after
    # attributes change. LF output is an identity conversion; CRLF, ident,
    # encodings and filters may change even those otherwise matching blobs.
    paths = b"\0".join(path for path, _ in regular) + b"\0"
    attrs = run("check-attr", "--all", "-z", "--stdin", input=paths, text=False).split(b"\0")
    attributes = {}
    for i in range(0, len(attrs) - 2, 3):
        attributes.setdefault(attrs[i], {})[attrs[i + 1]] = attrs[i + 2]
    autocrlf = run("config", "--type=bool-or-str", "--default=false", "--get", "core.autocrlf")
    eol = run("config", "--default=native", "--get", "core.eol").lower()
    for (path, blob), raw in zip(regular, hashes):
        if path in literal:  # core.symlinks=false checks out the literal link target.
            if raw != blob:
                mismatches.append(path)
            continue
        attr = attributes.get(path, {})
        converts = any(attr.get(name, b"unset") not in (b"unset", b"unspecified")
                       for name in (b"ident", b"filter", b"working-tree-encoding", b"crlf"))
        converts |= (attr.get(b"text") != b"unset" and attr.get(b"eol") != b"lf"
                     and (attr.get(b"eol") == b"crlf" or autocrlf == "true"
                          or (autocrlf != "input" and eol == "crlf"
                              and attr.get(b"text", b"unset") != b"unset")))
        if raw == blob and not converts:
            continue
        # Use a single-object response: batch --filters reports the ORIGINAL
        # blob size, so arbitrary converted binary output cannot be framed by it.
        content = subprocess.check_output([
            "git", "--no-optional-locks", "-C", str(work), *GIT_CONFIG, *CHECKOUT_CONFIG,
            "cat-file", "--filters", "--path=" + os.fsdecode(path), blob.decode()], pass_fds=pass_fds)
        digest = hashlib.new("sha1" if len(blob) == 40 else "sha256")
        digest.update(b"blob " + str(len(content)).encode() + b"\0")
        digest.update(content)
        if digest.hexdigest().encode() != raw:
            mismatches.append(path)
    return mismatches


def remove_checkout_tree(path):
    def writable_retry(function, name, error):
        if not isinstance(error, PermissionError):
            raise error
        # Removing a file needs write permission on its parent; descending into
        # a directory needs search permission on the directory itself as well.
        parent = Path(name).parent
        parent.chmod(parent.stat().st_mode | 0o700)
        if function in (os.open, os.scandir):
            if stat.S_ISDIR(Path(name).lstat().st_mode):
                os.chmod(name, os.stat(name).st_mode | 0o700)
            remove_checkout_tree(Path(name))
        else:
            function(name)
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path, onexc=writable_retry)
    else:
        try:
            path.unlink(missing_ok=True)
        except PermissionError as error:
            writable_retry(os.unlink, path, error)


def check_checkout_parents(work, paths, missing_ok=False, directory_mode=None):
    if directory_mode is not None:
        work.chmod(directory_mode)
    checked = {work}
    for raw in paths:
        parent = work
        for part in Path(os.fsdecode(raw)).parts[:-1]:
            parent /= part
            if parent in checked:
                continue
            try:
                mode = parent.lstat().st_mode
            except FileNotFoundError:
                if missing_ok:
                    break
                raise
            if not stat.S_ISDIR(mode):
                raise ValueError("checkout path has a non-directory parent")
            if directory_mode is not None and stat.S_IMODE(mode) != directory_mode:
                parent.chmod(directory_mode)
            checked.add(parent)


def clean_checkout_rules(work, paths, directory_mode, pass_fds):
    run = partial(git, work, *CHECKOUT_CONFIG, pass_fds=pass_fds)
    # Ignore rules left by an agent must not shield other untracked files.
    # Rule files within an already ignored directory affect only ignored paths
    # and belong to the warm build tree. Check parents without a trailing slash:
    # a directory's own .gitignore must not make that directory look ignored.
    leftovers = [raw for raw in run("ls-files", "--others", "-z", "--",
                 ".gitattributes", ":(glob)**/.gitattributes", ".gitignore", ":(glob)**/.gitignore",
                 text=False).split(b"\0") if raw]
    # A directory containing tracked files is not wholly ignored: its rules
    # can still alter the committed checkout's bytes.
    ignored_dirs = set()
    checked_dirs = {parent for raw in paths for parent in Path(os.fsdecode(raw)).parents}
    for raw in sorted(leftovers, key=lambda path: len(Path(os.fsdecode(path)).parts)):
        relative = Path(os.fsdecode(raw))
        for parent in reversed(relative.parents):
            if parent in ignored_dirs:
                break
            if parent == Path(".") or parent in checked_dirs:
                continue
            try:
                run("check-ignore", "-q", "--", str(parent))
            except subprocess.CalledProcessError as error:
                if error.returncode != 1:
                    raise
                checked_dirs.add(parent)
            else:
                ignored_dirs.add(parent)
                break
        else:
            check_checkout_parents(work, [raw], directory_mode=directory_mode)
            (work / os.fsdecode(raw)).unlink()


def reset_checkout(work, common, commit, deep_clean, pass_fds=()):
    run = partial(git, work, *CHECKOUT_CONFIG, pass_fds=pass_fds)
    admin = Path(run("rev-parse", "--absolute-git-dir"))
    # Check both directions before touching any Git state. A slot's .git can
    # have been replaced with a pointer to the user's main Git directory.
    if (admin.parent != common / "worktrees" or admin.is_symlink()
            or admin.resolve() != admin
            or registered_gitfile(admin) != work / ".git"):
        raise ValueError("checkout Git directory is not its own worktree registration")
    # Everything else is private state from a previous session: operation
    # state, config, attributes, refs, reflogs and locks. Keep the index's stat
    # data so unchanged files need not be rehashed by checkout.
    for path in admin.iterdir():
        if path.name in ("HEAD", "commondir", "gitdir", "locked"):
            continue
        if path.name == "index" and path.is_file() and not path.is_symlink() and path.stat().st_nlink == 1:
            continue
        if path.is_dir() and not path.is_symlink():
            remove_checkout_tree(path)
        else:
            path.unlink()
    (admin / "refs").mkdir()
    # Restore the OLD committed tree in the index first, including staged
    # deletions of paths that the new candidate ignores. Preserve stat data
    # for unchanged entries, but discard all assume-unchanged/skip-worktree bits.
    flagged = [entry[2:] for entry in run("ls-files", "-v", "-z", text=False).split(b"\0")
               if entry and (entry[:1] == b"S" or entry[:1].islower())]
    if flagged:
        for flag in ("--no-skip-worktree", "--no-assume-unchanged"):
            run("update-index", flag, "-z", "--stdin", input=b"\0".join(flagged) + b"\0", text=False)
    run("-c", "core.ignorestat=false", "read-tree", "--reset", "HEAD")
    umask = os.umask(0)
    os.umask(umask)
    directory_mode = 0o777 & ~umask
    # Git does not restore directory modes. Restore old tracked parents before
    # unlink/checkout, rejecting symlinks before Git can write through them.
    # Then restore the new tree's parents after checkout creates them.
    paths = [raw for raw in run("ls-files", "-z", text=False).split(b"\0") if raw]
    check_checkout_parents(work, paths, missing_ok=True, directory_mode=directory_mode)
    clean_checkout_rules(work, paths, directory_mode, pass_fds)
    run("-c", "core.ignorestat=false", "checkout", "-q", "--force", "--detach", commit)
    paths = [raw for raw in run("ls-files", "-z", text=False).split(b"\0") if raw]
    check_checkout_parents(work, paths, directory_mode=directory_mode)
    # Recheck against the candidate: old or locally edited ignore rules may
    # have shielded a directory that the candidate no longer ignores.
    clean_checkout_rules(work, paths, directory_mode, pass_fds)
    # Fresh worktrees have empty gitlink directories. Empty populated modules
    # directly: submodule deinit would change the shared repository config.
    gitlinks = [entry.split(b"\t", 1)[1] for entry in
                run("ls-files", "--stage", "-z", text=False).split(b"\0")
                if entry.startswith(b"160000 ")]
    check_checkout_parents(work, gitlinks, missing_ok=True)
    for raw in gitlinks:
        path = work / os.fsdecode(raw)
        remove_checkout_tree(path)
        path.mkdir(parents=True)
    mismatches = checkout_mismatches(work, pass_fds)
    if mismatches:
        check_checkout_parents(work, mismatches)
        for raw in mismatches:
            path = work / os.fsdecode(raw)
            remove_checkout_tree(path)
        run("checkout-index", "--index", "--force", "-z", "--stdin",
            input=b"\0".join(mismatches) + b"\0", text=False)
        if checkout_mismatches(work, pass_fds):
            raise ValueError("checkout bytes or modes differ from candidate")
    # Keep internal dependency links, including dangling ones. An external link
    # invalidates its whole ignored tree, so tools cannot mistake it for an
    # installed dependency tree. Never let a FIFO/socket survive as input.
    ignored = {Path(os.fsdecode(raw.rstrip(b"/"))) for raw in run(
        "ls-files", "--others", "--ignored", "--exclude-standard", "--directory", "-z",
        text=False).split(b"\0") if raw}
    ignored_parents = {parent for path in ignored for parent in (path, *path.parents)}
    tracked = set(paths)
    pending = [(work, None)]
    unsafe = set()
    while pending:
        directory, ignored_root = pending.pop()
        # scandir reuses directory-entry type information without statting each
        # build file. Errors must propagate so an unreadable tree is recreated.
        with os.scandir(directory) as entries:
            for entry in entries:
                path = Path(entry.path)
                if path == work / ".git":
                    continue
                relative = path.relative_to(work)
                root = ignored_root or (path if relative in ignored else None)
                if entry.is_dir(follow_symlinks=False):
                    pending.append((path, root))
                elif os.fsencode(relative) not in tracked:
                    if entry.is_symlink() and (root or relative in ignored_parents):
                        try:
                            internal = path.resolve().is_relative_to(work)
                        except (OSError, RuntimeError):
                            internal = False
                        if not internal:
                            unsafe.add(root or path)
                    elif not entry.is_symlink() and not entry.is_file(follow_symlinks=False):
                        path.unlink()
    for path in unsafe:
        remove_checkout_tree(path)
    run("-c", "core.excludesFile=", "clean", "-ffd" + ("x" if deep_clean else ""))
    check_checkout(work, commit, pass_fds)


def checkout_metadata(metadata):
    # Ranking reads without a lease. Never follow a symlink or wait on a FIFO,
    # including if the path changes between inspection and opening it.
    with os.fdopen(os.open(metadata, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)) as source:
        if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
            raise ValueError("non-regular metadata path")
        previous = json.load(source)
    if (type(previous["uses"]) is not int or not 0 <= previous["uses"] <= POOL_USES
            or not re.fullmatch(r"[0-9a-f]{40,64}", previous["commit"])
            or not isinstance(previous["commits"], list)
            or not 1 <= len(previous["commits"]) <= previous["uses"]
            or previous["commits"][-1] != previous["commit"]
            or any(not re.fullmatch(r"[0-9a-f]{40,64}", item) for item in previous["commits"])):
        raise ValueError("invalid metadata")
    return previous


def checkout_changes(repo, previous, commit, force_clean, untracked):
    reason = ""
    distance = float("inf")
    if previous["commit"]:
        try:
            changed = [path for path in git(repo, "diff", "--no-renames", "--name-only", "-z",
                       previous["commit"], commit, text=False).split(b"\0") if path]
            distance = len(changed)
            if any(Path(os.fsdecode(path)).name in BUILD_INPUTS
                   or Path(os.fsdecode(path)).name.startswith(("rust-toolchain", "requirements"))
                   for path in changed):
                reason = "lockfile or toolchain changed"
            if any(Path(os.fsdecode(path)).name == "Cargo.toml" for path in changed):
                reason = (reason + "; " if reason else "") + "Cargo manifest changed"
            removed = git(repo, "diff", "--no-renames", "--diff-filter=D", "--name-only", "-z",
                          previous["commit"], commit, text=False).split(b"\0")
            # Ignored output can survive several unrelated candidates. Every
            # recorded commit must still exist for the forgotten-file check.
            if git(repo, "cat-file", "--batch-check=%(objecttype)", "-z",
                   input=b"\0".join(os.fsencode(revision) for revision in previous["commits"]) + b"\0",
                   text=False) != b"\n".join(b"commit" for _ in previous["commits"]):
                raise ValueError("missing previous commit")
            objects = [os.fsencode(revision + ":" + path)
                       for revision in previous["commits"] for path in untracked]
            # Compare the entire all-missing response: missing paths can
            # contain newlines that look like successful object responses.
            if objects and git(repo, "cat-file", "--batch-check=%(objectname)", "-z",
                    input=b"\0".join(objects) + b"\0", text=False) != b"\n".join(
                        item + b" missing" for item in objects):
                reason = (reason + "; " if reason else "") + "untracked source file was previously tracked"
            if any(Path(os.fsdecode(path)).name == "main.rs"
                   or "/src/bin/" in "/" + os.fsdecode(path) for path in removed if path):
                reason = (reason + "; " if reason else "") + "Cargo executable source removed"
        except (ValueError, subprocess.CalledProcessError):
            reason = "missing previous commit"
    if force_clean:
        reason = "--clean-checkout"
    elif previous["uses"] >= POOL_USES:
        reason = f"{POOL_USES} uses"
    return reason, distance


def lease_checkout(repo, cache, commit, fresh, leases, force_clean, untracked=()):
    # Reftable keeps per-worktree refs in admin/reftable, which the pool's
    # admin-state reset cannot discard. Bypass it before creating any slots.
    if git(repo, "config", "--default=files", "--get", "extensions.refStorage") == "reftable":
        git(repo, *CHECKOUT_CONFIG, "worktree", "add", "--detach", str(fresh), commit)
        check_checkout(fresh, commit)
        return fresh, "reftable; fresh session worktree"
    common = Path(git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")).resolve()
    main = os.fsdecode(git(repo, "worktree", "list", "--porcelain", "-z",
                           text=False).split(b"\0", 1)[0]).removeprefix("worktree ")
    key = hashlib.sha256(os.fsencode(common)).hexdigest()[:12]
    pool = (cache / "am-pool" / f"{Path(main).name}-{key}").resolve()
    pool.mkdir(parents=True, exist_ok=True)
    failures = []
    ranked = []
    for number in range(1, POOL_SLOTS + 1):
        metadata = pool / f"slot-{number}.json"
        temporary = metadata.with_suffix(".json.tmp")
        try:
            if os.path.lexists(temporary) and not stat.S_ISREG(temporary.lstat().st_mode):
                raise ValueError("non-regular metadata path")
            previous = checkout_metadata(metadata)
            reason, distance = checkout_changes(repo, previous, commit, force_clean, untracked)
            ranked.append((int(bool(reason)), distance, number))
        except (OSError, ValueError, KeyError, TypeError):
            ranked.append((2, 0, number))
    # This is only a preference: metadata may change before the non-blocking
    # lease succeeds. Re-read it and recompute cleaning under that lease.
    for _, _, number in sorted(ranked):
        slot = pool / f"slot-{number}"
        lockpath = pool / f"slot-{number}.lock"
        lock = None
        try:
            # Never follow a lock symlink or block while opening a FIFO. Do
            # not replace malformed lock inodes: another launcher may already
            # have one open, and replacement would split the exclusive lease.
            if os.path.lexists(lockpath) and not stat.S_ISREG(lockpath.lstat().st_mode):
                raise ValueError("non-regular lock path")
            lock = os.fdopen(os.open(lockpath, os.O_CREAT | os.O_RDWR | os.O_APPEND
                                    | os.O_NOFOLLOW | os.O_NONBLOCK, 0o666), "a+b")
            if not stat.S_ISREG(os.fstat(lock.fileno()).st_mode):
                raise ValueError("non-regular lock path")
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if not os.path.samestat(os.fstat(lock.fileno()), lockpath.lstat()):
                raise ValueError("lock path changed while acquiring lease")
        except BlockingIOError:
            if lock is not None:
                lock.close()
            continue
        except (OSError, ValueError) as error:
            if lock is not None:
                lock.close()
            failure = f"pool slot {slot} unusable: {error}"
            print(failure, file=sys.stderr)
            failures.append(failure)
            continue
        leases.append(lock)
        metadata = pool / f"slot-{number}.json"
        temporary = metadata.with_suffix(".json.tmp")
        reason = "new slot" if not any(os.path.lexists(path) for path in (slot, metadata, temporary)) else ""
        try:
            for path in (metadata, temporary):
                if os.path.lexists(path) and not stat.S_ISREG(path.lstat().st_mode):
                    remove_checkout_tree(path)
                    reason = "non-regular metadata path"
            previous = checkout_metadata(metadata)
        except (OSError, ValueError, KeyError, TypeError):
            previous = {"uses": 0, "commit": "", "commits": []}
            reason = reason or "missing or corrupt metadata"
        changed_reason, _ = checkout_changes(repo, previous, commit, force_clean, untracked)
        reason = changed_reason or reason
        # Absence is an interrupted-reset marker.
        metadata.unlink(missing_ok=True)
        pass_fds = tuple(lease.fileno() for lease in leases)
        try:
            registered = git(repo, "worktree", "list", "--porcelain", "-z", text=False)
            if (b"worktree " + os.fsencode(slot.resolve()) + b"\0" not in registered
                    or slot.is_symlink() or not slot.is_dir()
                    or Path(git(slot, "rev-parse", "--path-format=absolute", "--git-common-dir")).resolve() != common):
                raise ValueError("missing checkout or registration")
            if previous["commit"] != git(slot, "rev-parse", "HEAD"):
                reason = reason or "checkout differs from metadata"
            reset_checkout(slot, common, commit, bool(reason), pass_fds)
        except (OSError, ValueError, subprocess.CalledProcessError) as error:
            reason = (reason + "; " if reason else "") + f"recreated ({error})"
            try:
                cleanup_worktrees(repo, slot)
                remove_checkout_tree(slot)
                cleanup_worktrees(repo, slot)
                git(repo, *CHECKOUT_CONFIG, "-c", "worktree.useRelativePaths=false",
                    "worktree", "add", "--detach", str(slot), commit, pass_fds=pass_fds)
                reset_checkout(slot, common, commit, True, pass_fds)
            except (OSError, ValueError, subprocess.CalledProcessError) as error:
                failure = f"pool slot {slot} unusable: {error}"
                print(failure, file=sys.stderr)
                failures.append(failure)
                cleanup_worktrees(repo, slot)
                leases.remove(lock)
                lock.close()
                continue
        temporary.write_text(json.dumps({
            "uses": 1 if reason else previous["uses"] + 1, "commit": commit,
            "commits": [commit] if reason else previous["commits"] + [commit]}))
        temporary.replace(metadata)
        return slot, "; ".join([*failures, f"pool {slot}",
                               f"deep clean: {reason}" if reason else "warm reset"])
    git(repo, *CHECKOUT_CONFIG, "worktree", "add", "--detach", str(fresh), commit)
    check_checkout(fresh, commit)
    return fresh, "; ".join([*failures, "pool busy or slots unusable; fresh session worktree"])


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
            staged = set(git(repo, "diff", "--cached", "--ignore-submodules=untracked", "--name-only", "-z", candidate, "--",
                             env=env, text=False).split(b"\0")) - {b""}
            changes = git(repo, "diff", "--raw", "--no-renames", "--ignore-submodules=untracked", "-z",
                          candidate, "--", env=env, text=False).split(b"\0")
            changed, structural = set(), set()
            for header, path in zip(changes[::2], changes[1::2]):
                changed.add(path)
                old_mode, new_mode = header.split()[:2]
                if old_mode[1:] != new_mode or b"160000" in (old_mode[1:], new_mode):
                    structural.add(path)
            # Some committed blobs look dirty even in a fresh checkout. Accept
            # those only when their bytes match checkout conversion. Git judges
            # source mode changes and gitlinks; full permissions/hardlink counts
            # are slot checks, not conditions on the user's working tree.
            dirty = staged or structural or (changed and checkout_mismatches(repo, paths=changed, source=True))
            if dirty:
                parser.error("commit first: verifier requires tracked files to match HEAD: "
                             + ", ".join(os.fsdecode(path) for path in sorted(dirty)))
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
    untracked_note = ""
    if args.role == "verifier":
        untracked_note = "Untracked files left out: " + (", ".join(ascii(path) for path in untracked[:20]) or "none")
        if len(untracked) > 20:
            untracked_note += f"; {len(untracked) - 20} more"
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
                work, note = lease_checkout(repo, cache, candidate, work, leases, args.clean_checkout, untracked)
                checkout_note = "Checkout: " + note
                prompt += f"\nSnapshot commit: {snapshot}\n{checkout_note}\n{untracked_note}\n"
                if before_commit:
                    before, note = lease_checkout(repo, cache, before_commit, temp / "before", leases,
                                                  args.clean_checkout, untracked)
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
                versions = "known-good" if before else "before or known-good"
                extra = "known-good" if before else "before"
                prompt += (f"\nTemporary folder: {temporary}; removed when the session ends; "
                           f"create {versions} checkouts as shared clones inside it "
                           f"(git clone --quiet --shared --no-checkout . \"$TMPDIR/{extra}\").\n")
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
                command = [sys.executable, "-I", "-c", LEASE_GUARD, str(Path(__file__).resolve()),
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
        except (OSError, ValueError) as error:
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
                          + (untracked_note + "\n" if untracked_note else "")
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
