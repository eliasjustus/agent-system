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
import math
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import stat
import subprocess
import tempfile
import time


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
import ctypes, os, runpy, signal, subprocess, sys
launcher = int(sys.argv[4])
helpers = runpy.run_path(sys.argv[1])
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
        helpers["diagnostic"](f"Warning: cannot list lease guard children for {pid}: {error}")
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
        import pathlib, shutil
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


CLEANUP_FAILURES = None


def diagnostic(message, *, end="\n"):
    if sys.stderr is None:
        return False
    contents = message if isinstance(message, bytes) else str(message).encode("utf-8", errors="backslashreplace")
    try:
        remaining = memoryview(contents + end.encode())
        while remaining:
            remaining = remaining[os.write(sys.stderr.fileno(), remaining):]
    except (OSError, ValueError):
        return False
    return True


def header_value(value):
    return "".join(char if char.isascii() and char.isprintable() else ascii(char)[1:-1]
                   for char in str(value))


def open_result(path, flags, mode):
    fd = os.open(path, flags | os.O_NOFOLLOW | os.O_NONBLOCK, 0o666)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(f"non-regular result file: {path}")
        return os.fdopen(fd, mode)
    except BaseException:
        os.close(fd)
        raise


def cleanup_failed(what, error):
    if CLEANUP_FAILURES is not None:
        CLEANUP_FAILURES.append(header_value(f"{what}: {error}"))
    diagnostic(f"Cleanup failed: {what}: {error}")


def cleanup_step(what, action, *args, **kwargs):
    try:
        action(*args, **kwargs)
    except Exception as error:
        cleanup_failed(what, error)


class ResultDestination:
    def __init__(self, args):
        requested = args.out.expanduser()
        self.out = requested.parent.resolve() / requested.name
        self.saved = None
        self.code = None
        self.rendered = None
        self.written = None
        self.pending_cleanup = False
        self.lock = None
        self.claimed = False
        self.finalizing = False
        self.signal_code = None
        self.spawning = False
        self.keep_session = False
        mask = os.umask(0)
        os.umask(mask)
        self.mode = 0o666 & ~mask

    def claim(self, args):
        if self.out == self.out.with_name(self.out.stem + ".lock"):
            raise ValueError("--out must differ from its stem lock path")
        if self.out == self.out.with_suffix(".log"):
            raise ValueError("--out must differ from its sibling .log path")
        if any(part in self.out.name for part in (".saving-", ".attempt-")):
            raise ValueError("--out name must not contain .saving- or .attempt-")
        if self.out.stem.endswith("-evidence"):
            raise ValueError("--out stem must not end in -evidence")
        if self.out.is_dir():
            raise ValueError("--out must not be an existing directory")
        self.out.parent.mkdir(parents=True, exist_ok=True)
        lockpath = self.out.with_name(self.out.stem + ".lock")
        self.lock = os.fdopen(os.open(lockpath, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW
                                      | os.O_NONBLOCK, 0o666), "a")
        try:
            if not stat.S_ISREG(os.fstat(self.lock.fileno()).st_mode):
                raise OSError(f"non-regular result lock: {lockpath}")
            try:
                fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise BlockingIOError(f"another launcher owns {self.out}") from None
            if not os.path.samestat(os.fstat(self.lock.fileno()), lockpath.lstat()):
                raise OSError(f"result lock changed: {lockpath}")
            self.start = utc()
            self.header = ((f"Role: {args.role}\n" if args.role is not None else "")
                           + f"Family: {args.family}\n"
                           f"Author family: {args.author_family or ''}\n"
                           f"Same-family reason: {header_value(args.same_family_reason or '')}\n"
                           f"Start UTC: {self.start}\n")
            # Under the stem lock, no writer of these temporary files is alive.
            for path in self.out.parent.iterdir():
                if (re.fullmatch(re.escape(self.out.stem) + r"\.saving-[a-z0-9_]{8}", path.name)
                        and not path.is_dir()):
                    path.unlink()
            paths = (self.out, self.out.with_suffix(".log"),
                     self.out.with_name(self.out.stem + "-evidence"))
            # All launchers sharing these siblings hold the same stem lock.
            if any(os.path.lexists(path) for path in paths):
                number = 1
                while True:
                    stem = self.out.stem + f".attempt-{number}"
                    targets = tuple(self.out.with_name(stem + suffix)
                                    for suffix in (self.out.suffix, ".log", "-evidence"))
                    if any(os.path.lexists(path) for path in targets):
                        number += 1
                        continue
                    for path, target in zip(paths, targets):
                        if os.path.lexists(path):
                            path.rename(target)
                    break
            self.write(self.header.encode("utf-8", errors="backslashreplace") + b"Result: running\n")
            self.claimed = True
        except BaseException:
            if not self.claimed:
                self.lock.close()
            raise

    def write(self, contents):
        # A sibling temporary file keeps replacement on the same filesystem.
        name = None
        try:
            with tempfile.NamedTemporaryFile(dir=self.out.parent,
                                             prefix=self.out.stem + ".saving-", delete=False) as stream:
                name = stream.name
                os.fchmod(stream.fileno(), self.mode)
                stream.write(contents)
                written = os.fstat(stream.fileno())
            os.replace(name, self.out)
            self.written = written
            name = None
        finally:
            if name is not None:
                Path(name).unlink(missing_ok=True)

    def refused(self, reason, output=""):
        self.finalizing = True
        if self.saved is not None:
            return self.code
        self.write_terminal((self.header + f"Result: launch refused\nReason: {header_value(reason)}\n"
                             f"End UTC: {utc()}\nExit code: 2\n"
                             + ("\n" + output if output else "")).encode("utf-8", errors="backslashreplace"), 2)
        diagnostic(reason)
        return 2

    def write_terminal(self, contents, code):
        self.finalizing = True
        if self.saved is not None:
            return
        try:
            self.write(contents)
        except Exception:
            try:
                # Replacement may fail while the already claimed file is writable.
                with open_result(self.out, os.O_WRONLY, "wb") as stream:
                    info = os.fstat(stream.fileno())
                    if (info.st_nlink != 1 or self.written is None
                            or not os.path.samestat(info, self.written)):
                        raise OSError(f"result file changed: {self.out}")
                    os.ftruncate(stream.fileno(), 0)
                    stream.write(contents)
            except Exception as report_error:
                # Remove only our stale running marker if neither write succeeds.
                try:
                    if (self.written is not None
                            and os.path.samestat(self.out.lstat(), self.written)):
                        self.out.unlink(missing_ok=True)
                    else:
                        diagnostic(f"Result path {self.out} holds a file the launcher does not own")
                except FileNotFoundError:
                    pass
                except Exception as remove_error:
                    diagnostic(f"Cannot remove stale result {self.out}: {remove_error}")
                diagnostic(f"Cannot record result in {self.out}: {report_error}")
                diagnostic(contents, end="")
        self.saved = (contents.decode("utf-8"), b"")
        self.code = code
        self.rendered = contents

    def cannot_save(self, error, answer):
        self.finalizing = True
        if self.saved is not None:
            cleanup_failed("answer saving", error)
            return self.code
        self.keep_session = True
        reason = f"failed: cannot save answer: {error}; answer left in {answer.parent}"
        contents = (self.header + f"Result: {header_value(reason)}\nEnd UTC: {utc()}\nExit code: 1\n").encode(
            "utf-8", errors="backslashreplace")
        self.write_terminal(contents, 1)
        diagnostic(reason)
        return 1

    def artifact(self, path):
        if not path.is_relative_to(self.out.parent):
            return False
        name = path.relative_to(self.out.parent).parts[0]
        return (name in (self.out.name, self.out.with_suffix(".log").name,
                         self.out.stem + "-evidence", self.out.stem + ".lock")
                or name.startswith((self.out.stem + ".attempt-", self.out.stem + ".saving-")))

    def save(self, header, answer, code):
        if self.saved is not None:
            return
        before, separator, after = header.rpartition("\nExit code:")
        contents = (before + "\nCleanup: pending" + separator + after).encode(
            "utf-8", errors="backslashreplace") + answer
        self.write(contents)
        self.saved = (header, answer)
        self.code = code
        self.rendered = contents
        self.pending_cleanup = True

    def finish(self):
        if self.saved is not None:
            header, answer = self.saved
            failures = 0
            while True:
                status = ""
                if self.pending_cleanup or CLEANUP_FAILURES:
                    cleanup = "failed: " + "; ".join(CLEANUP_FAILURES) if CLEANUP_FAILURES else "ok"
                    status += f"\nCleanup: {cleanup}"
                if self.signal_code is not None:
                    status += f"\nSignal: {self.signal_code - 128}"
                before, separator, after = header.rpartition("\nExit code:")
                contents = (before + status + separator + after).encode("utf-8", errors="backslashreplace") + answer
                if contents == self.rendered:
                    return
                try:
                    self.write(contents)
                    self.rendered = contents
                except Exception as report_error:
                    failures += 1
                    if failures == 2:
                        diagnostic(f"Cannot update cleanup status in {self.out}: {report_error}")
                        return
                    cleanup_failed("result finalization", report_error)


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
            except Exception as error:
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
                        except Exception as error:
                            cleanup_failed(f"worktree registration {admin / 'gitdir'}", error)
                except Exception as error:
                    cleanup_failed(f"worktree registration for {path}", error)
    except Exception as error:
        cleanup_failed(f"session worktrees under {session}", error)


def stop_session(unit, process, guarded=False):
    if unit:
        try:
            result = subprocess.run(["systemctl", "--user", "stop", unit],
                                    text=True, capture_output=True)
            gone = f"unit {unit} not loaded" in result.stderr.lower()
            if result.returncode and not gone:
                cleanup_failed(f"scope {unit}", result.stderr.strip() or f"exit {result.returncode}")
        except Exception as error:
            cleanup_failed(f"scope {unit}", error)
    if process is not None and guarded and process.poll() is None:
        try:
            process.terminate()  # The supervisor stops and reaps all descendants.
        except ProcessLookupError:
            pass
        except Exception as error:
            cleanup_failed(f"lease guard {process.pid}", error)
        cleanup_step(f"wait for process {process.pid}", process.wait)
    if process is not None and (not unit or process.poll() is None):
        try:
            # A killed guard cannot reap its children. Without a scope, also
            # stop its process group, even when the guard has already exited.
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except Exception as error:
            cleanup_failed(f"process group {process.pid}", error)
    if process is not None:
        cleanup_step(f"wait for process {process.pid}", process.wait)


def interrupted(signum, frame):
    raise KeyboardInterrupt(signum)


def discover_servers(executable, cwd, env, timeout, log, result):
    process = None
    try:
        result.spawning = True
        try:
            process = subprocess.Popen([executable, "mcp", "list", "--json"], cwd=cwd, env=env,
                                       stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                       stderr=log, start_new_session=True)
        finally:
            result.spawning = False
        if result.signal_code is not None:
            raise KeyboardInterrupt(result.signal_code - 128)
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
        diagnostic(f"Warning: cannot check {path}: {error}")
        return
    for path in differences:
        diagnostic(f"Warning: {path} differs from the installed method; see {command}")


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
        diagnostic(f"Warning: cannot read {path}: {error}; using a full checkout")
        return []
    entries = []
    for line in lines:
        if not line.strip() or line.startswith("#"):
            continue
        entry = os.path.normpath(line)
        if (line.startswith("/") or ".." in line.split("/") or entry == "." or "\0" in line
                or entry.split("/", 1)[0] in ("AGENTS.md", "CLAUDE.md", ".claude", ".codex")):
            diagnostic(f"Warning: ignoring invalid leave-out path {line!r}")
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


def nonregular_metadata_paths(metadata, temporary):
    return [path for path in (metadata, temporary)
            if os.path.lexists(path) and not stat.S_ISREG(path.lstat().st_mode)]


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


def checkout_changes(repo, previous, commit, force_clean, untracked=None):
    reasons = []
    if force_clean:
        reasons.append("--clean-checkout")
    if previous["uses"] >= POOL_USES:
        reasons.append(f"{POOL_USES} uses")
    distance = float("inf")
    if previous["commit"]:
        try:
            changes = [] if previous["commit"] == commit else git(
                repo, "diff", "--no-renames", "--name-status", "-z",
                previous["commit"], commit, text=False).split(b"\0")[:-1]
            changed = changes[1::2]
            distance = len(changed)
            if any(Path(os.fsdecode(path)).name in BUILD_INPUTS
                   or Path(os.fsdecode(path)).name.startswith(("rust-toolchain", "requirements"))
                   for path in changed):
                reasons.append("lockfile or toolchain changed")
            if any(Path(os.fsdecode(path)).name == "Cargo.toml" for path in changed):
                reasons.append("Cargo manifest changed")
            if any(Path(os.fsdecode(path)).name == "main.rs"
                   or "/src/bin/" in "/" + os.fsdecode(path)
                   for status, path in zip(changes[::2], changed) if status == b"D"):
                reasons.append("Cargo executable source removed")
            # Ranking is only a preference. History and untracked-file checks
            # belong to the authoritative decision after acquiring the lease.
            if untracked is not None:
                if previous["commits"] and git(
                        repo, "cat-file", "--batch-check=%(objecttype)", "-z",
                        input=b"\0".join(os.fsencode(revision) for revision in previous["commits"]) + b"\0",
                        text=False) != b"\n".join(b"commit" for _ in previous["commits"]):
                    reasons.append("missing previous commit")
                objects = [os.fsencode(revision + ":" + path)
                           for revision in previous["commits"] for path in untracked]
                # Compare the entire all-missing response: missing paths can
                # contain newlines that look like successful object responses.
                if objects and git(repo, "cat-file", "--batch-check=%(objectname)", "-z",
                        input=b"\0".join(objects) + b"\0", text=False) != b"\n".join(
                            item + b" missing" for item in objects):
                    reasons.append("untracked source file was previously tracked")
        except (ValueError, subprocess.CalledProcessError):
            reasons.append("missing previous commit")
    return "; ".join(reasons), distance


def lease_checkout(repo, cache, commit, fresh, leases, force_clean, untracked=(), before_commit="",
                   reserved=None):
    # Reftable keeps per-worktree refs in admin/reftable, which the pool's
    # admin-state reset cannot discard. Bypass it before creating any slots.
    if git(repo, "config", "--default=files", "--get", "extensions.refStorage") == "reftable":
        git(repo, *CHECKOUT_CONFIG, "worktree", "add", "--detach", str(fresh), commit)
        check_checkout(fresh, commit)
        return fresh, "reftable; fresh session worktree", None
    common = Path(git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")).resolve()
    main = os.fsdecode(git(repo, "worktree", "list", "--porcelain", "-z",
                           text=False).split(b"\0", 1)[0]).removeprefix("worktree ")
    key = hashlib.sha256(os.fsencode(common)).hexdigest()[:12]
    pool = (cache / "am-pool" / f"{Path(main).name}-{key}").resolve()
    pool.mkdir(parents=True, exist_ok=True)
    failures = []
    reservation = None
    excluded = set()

    def candidates():
        nonlocal reservation
        if reserved is not None:
            number, paths, lock = reserved
            yield number, paths, lock
            excluded.add(number)
        ranked = []
        before_ranked = []
        for number in range(1, POOL_SLOTS + 1):
            if number in excluded:
                continue
            slot = pool / f"slot-{number}"
            metadata = slot.with_suffix(".json")
            temporary = metadata.with_suffix(".json.tmp")
            paths = (slot, metadata, temporary)
            if not any(os.path.lexists(path) for path in paths):
                rank, distance = 1, 0
            else:
                try:
                    if nonregular_metadata_paths(metadata, temporary):
                        raise ValueError("non-regular metadata path")
                    previous = checkout_metadata(metadata)
                    reason, distance = checkout_changes(repo, previous, commit, force_clean)
                    rank = 2 if reason else 0
                    if before_commit:
                        before_reason, before_distance = checkout_changes(repo, previous, before_commit, force_clean)
                        before_ranked.append((2 if before_reason else 0, before_distance, number))
                except (OSError, ValueError, KeyError, TypeError):
                    rank, distance = 3, 0
            if before_commit and rank in (1, 3):
                before_ranked.append((rank, 0, number))
            if rank == 0 and distance == 0 and not before_commit:
                # No later slot can improve this preference. Resume ranking
                # only if this exact match is busy or unusable.
                yield number, paths, None
            else:
                ranked.append((rank, distance, number, paths))
        # Minimise the combined rank, then the total distance. Equal totals
        # retain the ordinary candidate-first ranking. Lease both slots without
        # waiting, so the before preference survives the candidate's reset.
        pairs = sorted((
            (rank + before_rank, distance + before_distance,
             (rank, distance, number), (before_rank, before_distance, before_number), paths)
            for rank, distance, number, paths in ranked
            for before_rank, before_distance, before_number in before_ranked
            if number != before_number))
        for _, _, (_, _, number), (_, _, before_number), paths in pairs:
            if number in excluded or before_number in excluded:
                continue
            lock = try_lock(number)
            if lock is None:
                continue
            before_lock = try_lock(before_number)
            if before_lock is None:
                lock.close()
                continue
            before_slot = pool / f"slot-{before_number}"
            before_metadata = before_slot.with_suffix(".json")
            reservation = (before_number, (before_slot, before_metadata,
                                           before_metadata.with_suffix(".json.tmp")), before_lock)
            leases.append(before_lock)
            yield number, paths, lock
            # A failed candidate reset must release its paired before slot
            # before trying another assignment. Success keeps the reservation.
            leases.remove(before_lock)
            before_lock.close()
            reservation = None
            excluded.add(number)
        for _, _, number, paths in sorted(ranked):
            if number not in excluded:
                yield number, paths, None

    def try_lock(number):
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
            return None
        except (OSError, ValueError) as error:
            if lock is not None:
                lock.close()
            excluded.add(number)
            failure = f"pool slot {slot} unusable: {error}"
            diagnostic(failure)
            failures.append(failure)
            return None
        return lock

    # This is only a preference: metadata or availability may change before the
    # non-blocking lease succeeds. Recompute cleaning under that lease.
    for number, (slot, metadata, temporary), lock in candidates():
        lock = lock if lock is not None else try_lock(number)
        if lock is None:
            continue
        if lock not in leases:
            leases.append(lock)
        new_slot = not any(os.path.lexists(path) for path in (slot, metadata, temporary))
        reasons = ["new slot"] if new_slot else []
        try:
            nonregular = nonregular_metadata_paths(metadata, temporary)
            for path in nonregular:
                remove_checkout_tree(path)
            if nonregular:
                reasons.append("non-regular metadata path")
            previous = checkout_metadata(metadata)
        except (OSError, ValueError, KeyError, TypeError):
            previous = {"uses": 0, "commit": "", "commits": []}
            if not new_slot:
                reasons.append("missing or corrupt metadata")
                # The history is unusable, so cleaning is already mandatory.
                # Still report changes from the checkout's actual commit.
                try:
                    previous["commit"] = git(slot, "rev-parse", "--verify", "HEAD^{commit}")
                except (OSError, subprocess.CalledProcessError):
                    pass
        changed_reason, _ = checkout_changes(repo, previous, commit, force_clean, untracked)
        reason = "; ".join(filter(None, [changed_reason, *reasons]))
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
                reason = (reason + "; " if reason else "") + "checkout differs from metadata"
            reset_checkout(slot, common, commit, bool(reason), pass_fds)
        except (OSError, ValueError, subprocess.CalledProcessError) as error:
            if not new_slot or str(error) != "missing checkout or registration":
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
                diagnostic(failure)
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
                               f"deep clean: {reason}" if reason else "warm reset"]), reservation
    git(repo, *CHECKOUT_CONFIG, "worktree", "add", "--detach", str(fresh), commit)
    check_checkout(fresh, commit)
    return fresh, "; ".join([*failures, "pool busy or slots unusable; fresh session worktree"]), None


def main():
    global CLEANUP_FAILURES
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
    parser.add_argument("--before", help="required for verifiers: baseline commit, or none for no before checkout")
    parser.add_argument("--clean-checkout", action="store_true",
                        help="deep-clean verifier checkouts, including ignored build output")
    parser.add_argument("--timeout", type=float)
    parser.add_argument("--writable", action="append", default=[], type=Path, metavar="DIR")
    parser.add_argument("--network", action="store_true")
    local = Path(__file__).resolve().parent
    parser.add_argument("--method", default=local if (local / "roles").is_dir()
                        else Path("~/.local/share/agent-method"), type=Path)
    # Even argument errors belong in the result when --out identifies a destination.
    def argument_error(message):
        raise argparse.ArgumentError(None, message)
    original_error = parser.error
    parser.error = argument_error
    args = argparse.Namespace()
    parse_error = None
    try:
        parser.parse_args(namespace=args)
    except argparse.ArgumentError as error:
        destination_parser = argparse.ArgumentParser(add_help=False)
        destination_parser.add_argument("--out", type=Path)
        destination, _ = destination_parser.parse_known_args()
        if destination.out is None:
            original_error(str(error))
        args.out = destination.out
        parse_error = str(error)
    try:
        result = ResultDestination(args)
    except Exception as error:
        diagnostic(f"Cannot prepare result destination {args.out}: {error}")
        return 2
    def handle_signal(signum, frame):
        if result.signal_code is not None:
            return
        result.signal_code = 128 + signum
        if (result.claimed and not result.finalizing and not result.spawning
                and result.saved is None):
            interrupted(signum, frame)
    previous_handlers = {}
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        previous_handlers[sig] = signal.getsignal(sig)
        if sig != signal.SIGHUP or previous_handlers[sig] != signal.SIG_IGN:
            signal.signal(sig, handle_signal)
    parser.error = argument_error
    CLEANUP_FAILURES = None
    try:
        result.claim(args)
        if result.signal_code is not None:
            raise KeyboardInterrupt(result.signal_code - 128)
        if parse_error is not None:
            code = result.refused(parse_error)
        else:
            code = launch(parser, args, result)
    except Exception as error:
        result.finalizing = True
        if result.saved is not None:
            if CLEANUP_FAILURES is None:
                CLEANUP_FAILURES = []
            cleanup_failed("launcher", error)
            code = result.code or 3
        elif not result.claimed:
            diagnostic(f"Cannot prepare result destination {args.out}: {error}")
            code = 2
        else:
            code = result.refused(str(error) or type(error).__name__)
    except KeyboardInterrupt as error:
        result.finalizing = True
        code = 128 + (error.args[0] if error.args else signal.SIGINT)
        if result.saved is None and result.claimed:
            result.write_terminal((result.header + f"Result: interrupted\nEnd UTC: {utc()}\n"
                                   f"Exit code: {code}\n").encode("utf-8", errors="backslashreplace"), code)
    finally:
        result.finalizing = True
        if CLEANUP_FAILURES is None:
            CLEANUP_FAILURES = []
        # A signal can escape an exception handler before it starts finalizing.
        if result.claimed and result.saved is None:
            if result.signal_code is not None:
                result.write_terminal((result.header + f"Result: interrupted\nEnd UTC: {utc()}\n"
                                       f"Exit code: {result.signal_code}\n").encode(
                                           "utf-8", errors="backslashreplace"), result.signal_code)
            else:
                result.refused("launcher ended without a final result")
        if result.lock is not None:
            # Keep ownership through close hooks and the last status update.
            try:
                held_lock = os.dup(result.lock.fileno())
            except (OSError, ValueError):
                held_lock = None
            try:
                cleanup_step("result lock close", result.lock.close)
                result.finish()
            finally:
                # The file is final. Signals after this point cannot change its status.
                for sig in previous_handlers:
                    signal.signal(sig, signal.SIG_IGN)
                if held_lock is not None:
                    cleanup_step("result lock release", os.close, held_lock)
        else:
            for sig in previous_handlers:
                signal.signal(sig, signal.SIG_IGN)
    if result.rendered is not None:
        header = result.rendered.split(b"\n\n", 1)[0]
        rendered_signal = re.search(rb"^Signal: (\d+)$", header, re.MULTILINE)
        if rendered_signal is not None:
            return 128 + int(rendered_signal[1])
        code = result.code
    return 3 if code == 0 and CLEANUP_FAILURES else code


def launch(parser, args, result):
    global CLEANUP_FAILURES
    if (args.before is not None or args.clean_checkout) and args.role != "verifier":
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
    if args.role == "verifier" and args.before is None:
        parser.error("verifier requires --before <commit> or --before none")
    brief = args.brief.expanduser().read_text()
    private = Path(args.private).expanduser().resolve()
    repo, out = args.repo.expanduser().resolve(), result.out
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
            repo, "ls-files", "--others", "--exclude-standard", "--full-name", "-z", text=False).split(b"\0") if path and not result.artifact(repo / os.fsdecode(path))]
        if args.before != "none":
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
        return result.refused(f"Cannot find agent executable: {executable}")
    executable = os.path.abspath(found)
    restricted = args.family == "codex" and args.role in ("challenger", "reviewer")
    environment = ("restricted; discovery did not run"
                   if restricted else "user configuration")
    start, snapshot, code = result.start, "", 1
    writes = "unavailable"
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
        return result.refused(f"Cannot create session folder under {root}: {error}")
    try:
        temp = Path(session.name).resolve()
        temporary = temp / "tmp"
        try:
            temporary.mkdir()
        except OSError as error:
            return result.refused(f"Cannot create session temporary folder {temporary}: {error}")
        length = len(os.fsencode(temporary))
        if length > 62:
            diagnostic(f"Warning: TMPDIR is {length} bytes; programs such as Chromium may fail "
                       "to create Unix sockets in it above 62 bytes; starting anyway")
        agent_env = dict(os.environ, TMPDIR=str(temporary), TMP=str(temporary), TEMP=str(temporary))
        work, answer, source = temp / "work", temp / "answer", temp / "evidence"
        process = None
        agent_started = False
        preflight = "none"
        preflight_log = None
        guarded = False
        systemd = shutil.which("systemd-run")
        unit = f"agent-{args.role}-{temp.name}.scope" if systemd else ""
        def update_header():
            result.header = (f"Role: {args.role}\nFamily: {args.family}\nModel: {header_value(model)}\n"
                             f"Executable: {header_value(executable)}\n"
                             + (f"Requested sandbox: {'workspace-write' if args.role in ('builder', 'verifier') else 'read-only'}\n"
                                f"Network: {'on' if args.network else 'not requested (user config applies)'}\n"
                                f"Requested environment: {header_value(environment)}\n" if args.family == "codex" else "")
                             + f"Author family: {args.author_family or ''}\n"
                             f"Same-family reason: {header_value(args.same_family_reason or '')}\n"
                             f"Scope: {header_value(unit or 'none')}\n"
                             + (f"Snapshot commit: {snapshot}\n" if review else "")
                             + (header_value(checkout_note) + "\n" if checkout_note else "")
                             + (header_value(before_note) + "\n" if before_note else "")
                             + (f"Preflight: {preflight}\n" if args.role == "verifier" else "")
                             + (untracked_note + "\n" if untracked_note else "")
                             + (f"Skipped untracked (>5 MB): {', '.join(header_value(path) for path in skipped)}\n" if skipped else "")
                             + f"Write bytes: {writes}\nStart UTC: {start}\n")
        update_header()
        def supervised(command):
            if unit:
                command = [systemd, "--user", "--scope", "--quiet", "--collect",
                           "--slice=agents.slice", f"--unit={unit}", "--", *command]
            else:
                diagnostic("systemd-run unavailable; running without a scope")
            if guarded:
                command = [sys.executable, "-I", "-c", LEASE_GUARD, str(Path(__file__).resolve()),
                           str(repo), str(temp), str(os.getpid()), *command]
            return command
        try:
            cwd = repo
            if args.role == "verifier":
                snapshot = candidate
                update_header()
                work, note, reserved = lease_checkout(repo, cache, candidate, work, leases, args.clean_checkout,
                                                      untracked, before_commit)
                checkout_note = "Checkout: " + note
                update_header()
                prompt += f"\nSnapshot commit: {snapshot}\n{checkout_note}\n{untracked_note}\n"
                if before_commit:
                    before, note, _ = lease_checkout(repo, cache, before_commit, temp / "before", leases,
                                                     args.clean_checkout, untracked, reserved=reserved)
                    before_note = f"Before checkout: {before} ({before_commit}); {note}"
                    update_header()
                    prompt += "\n" + before_note + "\nUse this writable checkout for the before state.\n"
                else:
                    before_note = "Before checkout: none (--before none)"
                    update_header()
                    prompt += "\n" + before_note + "\n"
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
                artifacts = [raw for raw in git(repo, "ls-files", "-z", env=env, text=False).split(b"\0")
                             if raw and result.artifact(repo / os.fsdecode(raw))]
                if artifacts:
                    git(repo, "update-index", "--force-remove", "-z", "--stdin", env=env,
                        input=b"\0".join(artifacts) + b"\0", text=False)
                untracked = subprocess.check_output(
                    ["git", "-C", str(repo), *GIT_CONFIG, "ls-files", "--others", "--exclude-standard", "-z"],
                    env=env)
                paths = []
                for raw in untracked.split(b"\0"):
                    if not raw:
                        continue
                    path = os.fsdecode(raw)
                    if result.artifact(repo / path):
                        continue
                    if (repo / path).lstat().st_size > 5 * 1024 * 1024:
                        skipped.append(path)
                        update_header()
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
                update_header()
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
            CLEANUP_FAILURES = []
            if args.role == "verifier":
                source.mkdir()
                prompt += (f"\nEvidence folder: {source}; only this folder is kept; "
                           "nothing else in the session folder is kept.\n")
            for path in roots:
                path.mkdir(parents=True, exist_ok=True)
            guarded = bool(leases)
            if args.role == "verifier":
                entry = git(work, *CHECKOUT_CONFIG, "ls-tree", candidate, "--", ".agents/preflight")
                if entry:
                    preflight = "pending"
                    update_header()
                    mode = entry.split()[0]
                    if mode not in ("100644", "100755"):
                        raise ValueError(".agents/preflight is not a regular file")
                    if mode != "100755":
                        raise ValueError(".agents/preflight is not executable")
                    timeout = float(os.environ.get("AGENT_PREFLIGHT_TIMEOUT", "900"))
                    if not math.isfinite(timeout) or timeout <= 0:
                        raise ValueError("AGENT_PREFLIGHT_TIMEOUT must be positive and finite")
                    # Fresh fallback checkouts need the same descendant supervision
                    # as leased slots, including when the launcher is killed outright.
                    guarded = True
                    preflight = "running"
                    update_header()
                    preflight_log = open_result(out.with_suffix(".log"),
                                                os.O_RDWR | os.O_CREAT | os.O_EXCL, "w+b")
                    started = time.monotonic()
                    result.spawning = True
                    try:
                        process = subprocess.Popen(supervised([str(work / ".agents/preflight")]),
                                                   cwd=work, env=agent_env, stdin=subprocess.DEVNULL,
                                                   stdout=preflight_log, stderr=preflight_log,
                                                   start_new_session=True,
                                                   pass_fds=tuple(lock.fileno() for lock in leases))
                    finally:
                        result.spawning = False
                    if result.signal_code is not None:
                        raise KeyboardInterrupt(result.signal_code - 128)
                    try:
                        status = process.wait(timeout=timeout)
                        preflight = f"exited {status}"
                    except subprocess.TimeoutExpired:
                        preflight = f"timed out after {timeout:g} s"
                    stop_failures = len(CLEANUP_FAILURES)
                    stop_session(unit, process, guarded=True)
                    if len(CLEANUP_FAILURES) != stop_failures:
                        result.keep_session = True
                        raise ValueError("preflight cleanup failed")
                    process = None
                    update_header()
                    if preflight.startswith("timed out") or status:
                        raise ValueError(f".agents/preflight {preflight}")
                    # Compare both the index and raw checkout bytes to the candidate;
                    # Git status alone can miss assume-unchanged files and conversions.
                    try:
                        paths = git(work, *CHECKOUT_CONFIG, "ls-files", "-z", text=False).split(b"\0")
                        check_checkout_parents(work, [path for path in paths if path])
                        changed = (git(work, *CHECKOUT_CONFIG, "rev-parse", "HEAD") != candidate
                                   or git(work, *CHECKOUT_CONFIG, "rev-parse", "--abbrev-ref", "HEAD") != "HEAD"
                                   or git(work, *CHECKOUT_CONFIG, "diff", "--cached", "--ignore-submodules=untracked",
                                          "--name-only", candidate)
                                   or checkout_mismatches(work))
                        changes = git(work, *CHECKOUT_CONFIG, "diff", "--raw", "--no-renames",
                                      "--ignore-submodules=untracked", "-z", candidate, text=False).split(b"\0")
                        changed = changed or any(
                            any(mode.lstrip(b":") == b"160000" for mode in header.split()[:2])
                            for header in changes[::2])
                    except (OSError, ValueError, subprocess.CalledProcessError):
                        changed = True
                    if changed:
                        raise ValueError("preflight changed tracked files")
                    preflight = f"passed in {time.monotonic() - started:.3f} s"
                    update_header()
            writable = args.role in ("builder", "verifier")
            if writable:
                prompt += f"\nTemporary folder: {temporary}; removed when the session ends"
                if args.role == "verifier" and not before:
                    prompt += ".\n"
                else:
                    versions = "known-good" if before else "before or known-good"
                    extra = "known-good" if before else "before"
                    prompt += (f"; create {versions} checkouts as shared clones inside it "
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
                    with open_result(out.with_suffix(".log"), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, "wb") as log:
                        environment = "restricted with MCP servers not disabled: discovery did not complete"
                        update_header()
                        servers, reason = discover_servers(executable, cwd, agent_env,
                                                           min(30, args.timeout or 30), log, result)
                    environment = "restricted"
                    if reason:
                        environment += f" with MCP servers not disabled: {reason}"
                        diagnostic(f"Warning: MCP servers were not disabled: {reason}")
                    unsupported = []
                    for name in servers:
                        if re.fullmatch(r"[A-Za-z0-9_-]+", name):
                            command += ["-c", f"mcp_servers.{name}.enabled=false"]
                        else:
                            unsupported.append(ascii(name))
                    if unsupported:
                        reason = f"unsupported server names: {', '.join(unsupported)}"
                        environment += f" with MCP servers not all disabled: {reason}"
                        diagnostic(f"Warning: MCP servers were not all disabled: {reason}")
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
            command = supervised(command)
            update_header()
            with (open_result(answer, os.O_WRONLY | os.O_CREAT
                              | (os.O_EXCL if preflight_log is not None else os.O_TRUNC), "wb")
                  if args.family == "claude" else open(os.devnull, "wb")) as stdout:
                out.parent.mkdir(parents=True, exist_ok=True)
                log_flags = os.O_WRONLY | os.O_CREAT | (os.O_APPEND if restricted else os.O_TRUNC)
                with (preflight_log if preflight_log is not None else
                      open_result(out.with_suffix(".log"), log_flags, "ab" if restricted else "wb")) as log:
                    # Defer signals until Popen returns the identity needed to stop
                    # the child, including a signal delivered inside Popen itself.
                    result.spawning = True
                    try:
                        process = subprocess.Popen(command, cwd=cwd, stdin=subprocess.PIPE, stdout=stdout,
                                                   stderr=log, env=agent_env, start_new_session=True,
                                                   pass_fds=tuple(lock.fileno() for lock in leases))
                        agent_started = True
                    finally:
                        result.spawning = False
                if result.signal_code is not None:
                    raise KeyboardInterrupt(result.signal_code - 128)
                try:
                    process.communicate(prompt.encode(), timeout=args.timeout)
                    code = process.returncode
                except subprocess.TimeoutExpired:
                    code = 124
        except KeyboardInterrupt as error:
            result.finalizing = True
            code = 128 + (error.args[0] if error.args else signal.SIGINT)
            if not agent_started:
                result.write_terminal((result.header + f"Result: interrupted\nEnd UTC: {utc()}\n"
                                       f"Exit code: {code}\n").encode("utf-8", errors="backslashreplace"), code)
        except Exception as error:
            result.finalizing = True
            if not agent_started:
                tail = ""
                if preflight_log is not None:
                    try:
                        preflight_log.seek(0)
                        tail = b"".join(deque(preflight_log, maxlen=50)).decode("utf-8", errors="replace")
                    except Exception as log_error:
                        diagnostic(f"Cannot read preflight output: {log_error}")
                code = result.refused(str(error) or type(error).__name__, tail)
            else:
                diagnostic(f"Session failed: {error}")
                code = 127 if isinstance(error, FileNotFoundError) else 1
        finally:
            # Record late signals without interrupting saving or cleanup.
            result.finalizing = True
            if CLEANUP_FAILURES is None:
                CLEANUP_FAILURES = []
            stop_failures = len(CLEANUP_FAILURES)
            if process is not None:
                cleanup_step("stop session", stop_session, unit, process, guarded=guarded)
            stop_failed = len(CLEANUP_FAILURES) != stop_failures
            if stop_failed:
                result.keep_session = True
                cleanup_failed("session retention", f"stopping agent failed; session folder kept at {temp}"
                               + (f"; evidence left in {source}" if args.role == "verifier" else ""))
            try:
                if agent_started:
                    answer_bytes = None
                    try:
                        try:
                            with open_result(answer, os.O_RDONLY, "rb") as stream:
                                answer_bytes = stream.read()
                        except FileNotFoundError:
                            answer_bytes = b""
                        if not answer_bytes.decode("utf-8", errors="replace").strip():
                            diagnostic("Session failed: agent produced no non-empty answer.")
                            code = code or 1
                        end = utc()
                        final_io = write_bytes()
                        writes = (f"{(final_io - initial_io) / 1024**2:.1f} MiB"
                                  if initial_io is not None and final_io is not None else "unavailable")
                        update_header()
                        header = result.header + f"End UTC: {end}\nExit code: {code}\n\n"
                        out.parent.mkdir(parents=True, exist_ok=True)
                        result.save(header, answer_bytes, code)
                        evidence = out.with_name(out.stem + "-evidence")
                        try:
                            if args.role == "verifier" and not stop_failed:
                                if not stat.S_ISDIR(source.lstat().st_mode):
                                    raise OSError(f"non-directory evidence: {source}")
                                if any(source.iterdir()):
                                    shutil.copytree(source, evidence, symlinks=True)
                        except Exception as error:
                            cleanup_failed(f"evidence copy {evidence}", error)
                    except Exception as error:
                        if result.saved is not None:
                            raise
                        code = result.cannot_save(error, answer)
                if code:
                    try:
                        with open_result(out.with_suffix(".log"), os.O_RDONLY, "rb") as log:
                            if os.fstat(log.fileno()).st_nlink != 1:
                                raise OSError(f"hard-linked session log: {out.with_suffix('.log')}")
                            diagnostic(b"".join(deque(log, maxlen=20)), end="")
                    except FileNotFoundError:
                        pass
                    except Exception as error:
                        cleanup_failed("session log tail", error)
            finally:
                if preflight_log is not None:
                    cleanup_step("preflight log close", preflight_log.close)
                if not stop_failed:
                    cleanup_step("session worktrees", cleanup_worktrees, repo, temp)
    finally:
        if CLEANUP_FAILURES is None:
            CLEANUP_FAILURES = []
        for lock in leases:
            cleanup_step("lease close", lock.close)
        if result.keep_session:
            session._finalizer.detach()
        else:
            cleanup_step(f"temporary folder {session.name}", session.cleanup)
        result.finish()
    return 3 if code == 0 and result.saved is not None and CLEANUP_FAILURES else code


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt as error:
        sys.exit(128 + (error.args[0] if error.args else signal.SIGINT))
    except (OSError, subprocess.CalledProcessError) as error:
        diagnostic(f"Launcher failed: {error}")
        sys.exit(1)
