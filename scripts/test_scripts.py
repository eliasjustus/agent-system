import ast
from contextlib import ExitStack
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest


# Fixtures add home/.cache/am/<session>/tmp beneath a temporary directory.
# Keep those paths within the socket budget independently of the caller's TMPDIR.
_test_root = tempfile.gettempdir()
if not _test_root.isascii() or len(os.fsencode(_test_root)) + 39 > 62:
    tempfile.tempdir = "/tmp"

ROOT = Path(__file__).resolve().parents[1]
FAKE = '''#!/usr/bin/env python3
import json, os, pathlib, re, subprocess, sys, time
args = sys.argv[1:]
cwd = pathlib.Path.cwd()
if args == ["mcp", "list", "--json"]:
    pathlib.Path(os.environ["RECORD"] + ".discovery").write_text(json.dumps({
        "executable": sys.argv[0], "cwd": str(cwd), "tmpdir": os.environ.get("TMPDIR"),
        "temporary": {key: os.environ.get(key) for key in ("TMPDIR", "TMP", "TEMP")},
        "pid": os.getpid()}))
    if os.environ.get("MCP_STDERR"):
        print(os.environ["MCP_STDERR"], file=sys.stderr, flush=True)
    if os.environ.get("MCP_SLEEP"):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        pathlib.Path(os.environ["RECORD"] + ".discovery-child").write_text(str(child.pid))
        time.sleep(30)
    print(os.environ.get("MCP_JSON", "[]"))
    sys.exit(int(os.environ.get("MCP_EXIT", "0")))
record = {"argv": args, "cwd": str(cwd), "stdin": sys.stdin.read(),
          "executable": sys.argv[0],
          "git_index_file": os.environ.get("GIT_INDEX_FILE"),
          "tmpdir": os.environ.get("TMPDIR"),
          "changed": ((cwd / "tracked").read_text() if (cwd / "tracked").exists() else
                      subprocess.check_output(["git", "show", "HEAD:tracked"], text=True)),
          "untracked": (cwd / "new").read_text() if (cwd / "new").exists() else None}
if not (cwd / "new").exists():
    result = subprocess.run(["git", "show", "HEAD:new"], text=True, capture_output=True)
    if result.returncode == 0:
        record["untracked"] = result.stdout
pathlib.Path(os.environ["RECORD"]).write_text(json.dumps(record))
match = re.search(r"^Evidence folder: (.+); only this folder is kept;", record["stdin"], re.M)
evidence = pathlib.Path(match.group(1)) if match else None
if os.environ.get("EVIDENCE"):
    assert evidence is not None and evidence.is_dir()
    (evidence / "proof.txt").write_text("proof")
answer = os.environ.get("SESSION_ANSWER", "answer\\n  unchanged\\n")
if not os.environ.get("NO_ANSWER"):
    if "-o" in args:
        pathlib.Path(args[args.index("-o") + 1]).write_text(answer)
    else:
        print(answer, end="", flush=True)
if os.environ.get("SESSION_STDERR"):
    print(os.environ["SESSION_STDERR"], file=sys.stderr, flush=True)
if os.environ.get("SLEEP"):
    subprocess.Popen([sys.executable, "-c", "import pathlib, time; time.sleep(1); "
                      "pathlib.Path(" + repr(os.environ["RECORD"] + ".child") + ").touch()"])
    time.sleep(30)
sys.exit(int(os.environ.get("SESSION_EXIT", "0")))
'''

SCOPE = '''#!/usr/bin/env python3
import json, os, pathlib, sys
args = sys.argv[1:]
if os.environ.get("SCOPE_FAIL"):
    print("scope refused by test bus", file=sys.stderr)
    sys.exit(19)
unit = next(arg.split("=", 1)[1] for arg in args if arg.startswith("--unit="))
pathlib.Path(os.environ["SCOPE_STATE"], unit).write_text(json.dumps(args))
os.environ["FAKE_SCOPE_UNIT"] = unit
command = args[args.index("--") + 1:]
os.execvp(command[0], command)
'''

STOP_SCOPE = '''#!/usr/bin/env python3
import os, pathlib, signal, sys
unit = sys.argv[-1]
pathlib.Path(os.environ["SCOPE_STATE"], unit + ".stop").touch()
found = False
for entry in pathlib.Path("/proc").iterdir():
    if not entry.name.isdigit():
        continue
    try:
        if ("FAKE_SCOPE_UNIT=" + unit).encode() in (entry / "environ").read_bytes().split(b"\\0"):
            os.kill(int(entry.name), signal.SIGKILL)
            found = True
    except (OSError, ProcessLookupError):
        pass
if os.environ.get("STOP_FAIL"):
    print("scope stop refused", file=sys.stderr)
    sys.exit(1)
if not found:
    print("Unit " + unit + " not loaded.", file=sys.stderr)
    sys.exit(5)
'''


class ScriptsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.home = self.base / "home"
        self.home.mkdir()
        self.repo = self.base / "repo"
        self.repo.mkdir()
        self.env = dict(os.environ, HOME=str(self.home), GIT_CONFIG_NOSYSTEM="1",
                        GIT_CONFIG_GLOBAL=os.devnull, GIT_AUTHOR_NAME="Test",
                        GIT_AUTHOR_EMAIL="test@example.org", GIT_COMMITTER_NAME="Test",
                        GIT_COMMITTER_EMAIL="test@example.org")
        self.git("init", "-q")
        (self.repo / "tracked").write_text("original")
        (self.repo / ".gitignore").write_text("ignored\n")
        self.git("add", ".")
        self.git("commit", "-qm", "baseline")
        self.method = self.home / "method"
        shutil.copytree(ROOT / "product", self.method)
        self.private = self.home / "private"
        self.brief = self.base / "brief.md"
        self.brief.write_text("bounded assignment\n")
        self.out = self.base / "results" / "answer.md"
        self.record = self.base / "record.json"
        fake = self.base / "fake"
        fake.write_text(FAKE)
        fake.chmod(0o755)
        self.env.update(AGENT_CLAUDE_CMD=str(fake), AGENT_CODEX_CMD=str(fake),
                        RECORD=str(self.record))
        self.env.pop("AGENT_CLAUDE_MODEL", None)
        self.env.pop("AGENT_CODEX_MODEL", None)
        self.env.pop("AGENT_CODEX_EFFORT", None)
        self.env.pop("XDG_CACHE_HOME", None)
        # Ordinary launcher tests must not depend on this host's current load.
        for name, value in {"MEMORY_SOME": "101", "MEMORY_FULL": "101", "IO_FULL": "101",
                            "MEM_AVAILABLE": "0", "SWAP_FREE": "0", "CGROUP_HEADROOM": "0",
                            "MAX_WAIT": "0"}.items():
            self.env["AGENT_ADMISSION_" + name] = value
        self.configure_scope()

    def configure_scope(self, present=True):
        self.bin = self.base / "bin"
        self.bin.mkdir(exist_ok=True)
        for name, target in (("python3", sys.executable), ("git", shutil.which("git"))):
            path = self.bin / name
            if not path.exists():
                path.symlink_to(target)
        self.scope_state = self.base / "scopes"
        self.scope_state.mkdir(exist_ok=True)
        self.env.update(PATH=str(self.bin), SCOPE_STATE=str(self.scope_state))
        for name, source in (("systemd-run", SCOPE), ("systemctl", STOP_SCOPE)):
            path = self.bin / name
            if present:
                path.write_text(source)
                path.chmod(0o755)
            elif path.exists():
                path.unlink()

    def agent_extra(self, source):
        fake = Path(self.env["AGENT_CODEX_CMD"])
        fake.write_text(FAKE.replace('if os.environ.get("SLEEP"):',
                                     source + '\nif os.environ.get("SLEEP"):'))

    def inject_launcher(self, source):
        site = self.base / "injection"
        site.mkdir(exist_ok=True)
        (site / "sitecustomize.py").write_text(
            f"import sys\nif sys.argv[0] == {str(ROOT / 'scripts/run.py')!r}:\n"
            + textwrap.indent(textwrap.dedent(source), "    "))
        shutil.rmtree(site / "__pycache__", ignore_errors=True)
        self.env["PYTHONPATH"] = str(site)

    def start_launcher(self, *extra, role="verifier", out=None, record=None):
        if role == "verifier":
            self.round_decision(out)
        if role == "verifier" and "--before" not in extra:
            extra = ("--before", "none", *extra)
        env = dict(self.env, RECORD=str(record or self.record))
        process = subprocess.Popen(
            [sys.executable, str(ROOT / "scripts/run.py"), "--role", role,
             "--family", "codex", "--brief", str(self.brief), "--out", str(out or self.out),
             "--repo", str(self.repo), "--method", str(self.method), "--private", str(self.private),
             "--author-family", "claude", *map(str, extra)],
            env=env, cwd=self.repo, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(self.reap_launcher, process)
        return process

    def reap_launcher(self, process):
        if process.poll() is None:
            process.terminate()
        process.communicate(timeout=10)

    def wait_record(self, record=None):
        record = record or self.record
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if record.exists():
                try:
                    return json.loads(record.read_text())
                except json.JSONDecodeError:
                    pass
            time.sleep(0.02)
        self.fail(f"agent did not start: {record}")

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.repo), *args],
                                       env=self.env, text=True).strip()

    def run_script(self, script, *args):
        return subprocess.run([sys.executable, str(ROOT / "scripts" / script),
                               *map(str, args)], env=self.env, cwd=self.repo,
                              text=True, capture_output=True)

    def launch(self, role="builder", family="codex", *extra):
        if role == "verifier" and "--before" not in extra:
            extra = ("--before", "none", *extra)
        return self.run_script("run.py", "--role", role, "--family", family,
                               "--brief", self.brief, "--out", self.out,
                               "--repo", self.repo, "--method", self.method,
                               "--private", self.private, *extra)

    def pool_root(self):
        common = Path(self.git("rev-parse", "--path-format=absolute", "--git-common-dir")).resolve()
        main = self.git("worktree", "list", "--porcelain").splitlines()[0].removeprefix("worktree ")
        cache = Path(self.env.get("XDG_CACHE_HOME", str(self.home / ".cache")))
        return cache / "am-pool" / f"{Path(main).name}-{hashlib.sha256(os.fsencode(common)).hexdigest()[:12]}"

    def busy_pool(self):
        pool = self.pool_root()
        pool.mkdir(parents=True, exist_ok=True)
        for number in range(1, 6):
            lock = (pool / f"slot-{number}.lock").open("a+b")
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.addCleanup(lock.close)

    def non_pool_worktrees(self):
        entries = self.git("worktree", "list", "--porcelain").split("\n\n")
        return "\n\n".join(entry for entry in entries
                             if not Path(entry.splitlines()[0].removeprefix("worktree ")).is_relative_to(
                                 self.home / ".cache/am-pool"))

    def assert_session_cleaned(self, record):
        self.assertFalse(Path(record["tmpdir"]).parent.exists())
        work = Path(record["cwd"])
        if work.is_relative_to(self.pool_root()):
            self.assertTrue(work.is_dir(), "pool checkout must survive session cleanup")
        else:
            self.assertFalse(work.exists())

    def setup_project(self, *extra):
        return self.run_script("setup.py", "--project", self.repo, "--id", "demo",
                               "--private", self.private, "--method", self.method,
                               *extra)

    def verifier(self, family="codex", *extra):
        self.round_decision()
        result = self.launch("verifier", family, "--author-family",
                             "claude" if family == "codex" else "codex", *extra)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(self.record.read_text())

    def round_decision(self, out=None):
        directory = (out or self.out).parent
        directory.mkdir(parents=True, exist_ok=True)
        decision = directory / "round-decision.md"
        decision.write_text("Retry: exercise the next preservation case.\n")
        newest = max(path.stat().st_mtime_ns for path in directory.iterdir()
                     if path.is_file())
        os.utime(decision, ns=(newest + 1, newest + 1))

    def install_preflight(self, body):
        path = self.repo / ".agents/preflight"
        path.parent.mkdir(exist_ok=True)
        if path.is_symlink():
            path.unlink()
        elif path.is_dir():
            shutil.rmtree(path)
        path.write_text(f"#!{sys.executable}\nimport json, os, pathlib, subprocess, sys, time\n" + body + "\n")
        path.chmod(0o755)
        self.git("add", "-A")
        self.git("commit", "-qm", "preflight fixture")
        return path

    def assert_preflight_refused(self, result, reason):
        self.assertEqual(result.returncode, 2, result.stderr)
        answer = self.out.read_text()
        self.assertIn("Result: launch refused\n", answer)
        self.assertIn(f"Reason: {reason}\n", answer)
        self.assertFalse(self.record.exists(), "verifier started after refused preflight")
        for lockpath in self.pool_root().glob("slot-*.lock"):
            with lockpath.open("a+b") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def blocking_preflight(self):
        marker = self.base / "preflight.json"
        self.install_preflight(f'''
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
pathlib.Path({str(marker)!r}).write_text(json.dumps({{
    "pids": [os.getpid(), child.pid], "cwd": str(pathlib.Path.cwd()), "tmpdir": os.environ["TMPDIR"]}}))
time.sleep(60)
''')
        return marker

    def wait_preflight(self, marker):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if marker.exists():
                try:
                    return json.loads(marker.read_text())
                except json.JSONDecodeError:
                    pass
            time.sleep(0.02)
        self.fail("preflight did not start")

    def test_PREFLIGHT_P1_failure_saves_full_log_last_50_lines_and_releases_leases(self):
        self.install_preflight('''
for number in range(80):
    print(f"line {number}", file=sys.stdout if number % 2 else sys.stderr, flush=True)
sys.exit(7)
''')
        result = self.launch("verifier", "codex", "--author-family", "claude", "--before", "HEAD")
        self.assert_preflight_refused(result, ".agents/preflight exited 7")
        lines = [f"line {number}" for number in range(80)]
        self.assertEqual(self.out.with_suffix(".log").read_text().splitlines(), lines)
        self.assertEqual(self.out.read_text().split("\n\n", 1)[1].splitlines(), lines[-50:])
        self.assertEqual(list((self.home / ".cache/am").iterdir()), [])

    def test_PREFLIGHT_P2_pass_preserves_build_output_checkout_and_temporary_folder(self):
        marker = self.base / "preflight.json"
        self.install_preflight(f'''
import fcntl
with pathlib.Path.cwd().with_suffix(".lock").open("a+b") as lock:
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        pass
    else:
        raise AssertionError("preflight ran without a lease")
pathlib.Path("ignored").write_text("built by preflight")
pathlib.Path({str(marker)!r}).write_text(json.dumps({{
    "cwd": str(pathlib.Path.cwd()), "tmpdir": os.environ["TMPDIR"]}}))
print("preflight stdout", flush=True)
print("preflight stderr", file=sys.stderr, flush=True)
''')
        self.agent_extra('record["built"] = (cwd / "ignored").read_text()\n'
                         'pathlib.Path(os.environ["RECORD"]).write_text(json.dumps(record))')
        self.env["SESSION_STDERR"] = "agent stderr"
        for family in ("codex", "claude"):
            with self.subTest(family=family):
                record = self.verifier(family)
                built = json.loads(marker.read_text())
                self.assertEqual(record["built"], "built by preflight")
                self.assertEqual((record["cwd"], record["tmpdir"]), (built["cwd"], built["tmpdir"]))
                self.assertNotEqual(record["cwd"], str(self.repo))
                self.assertRegex(self.out.read_text(), r"(?m)^Preflight: passed in [0-9.]+ s$")
                self.assertEqual(self.out.with_suffix(".log").read_text().splitlines(),
                                 ["preflight stdout", "preflight stderr", "agent stderr"])
                self.assertFalse((self.repo / "ignored").exists())

    def test_PREFLIGHT_P3_P5_absent_candidate_ignores_untracked_preflight(self):
        path = self.repo / ".agents/preflight"
        path.parent.mkdir()
        path.write_text(f"#!{sys.executable}\nraise SystemExit(9)\n")
        path.chmod(0o755)
        record = self.verifier()
        self.assertIn("Preflight: none\n", self.out.read_text())
        self.assertIn("'.agents/preflight'", self.out.read_text())
        self.assertFalse((Path(record["cwd"]) / ".agents/preflight").exists())

    def test_PREFLIGHT_P2_planted_answer_links_are_never_truncated(self):
        target = self.base / "answer-target"
        for family in ("codex", "claude"):
            for kind in ("symlink", "hardlink"):
                with self.subTest(family=family, kind=kind):
                    target.write_text("keep these bytes")
                    self.record.unlink(missing_ok=True)
                    self.install_preflight(f'''
session = pathlib.Path(os.environ["TMPDIR"]).parent
if {kind!r} == "symlink":
    (session / "answer").symlink_to({str(target)!r})
else:
    os.link({str(target)!r}, session / "answer")
''')
                    result = self.launch("verifier", family, "--author-family",
                                         "claude" if family == "codex" else "codex")
                    self.assertEqual(target.read_text(), "keep these bytes")
                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertIn("Result: launch refused\n", self.out.read_text())
                    self.assertIn("File exists:", self.out.read_text())
                    self.assertFalse(self.record.exists())

    def test_PREFLIGHT_P1_header_like_output_keeps_late_signal_in_header(self):
        output = "preflight output\nExit code: 7\nSignal: 1\nCleanup: forged\n"
        self.install_preflight(f'print({output!r}, end="", flush=True)\nsys.exit(7)')
        self.inject_launcher(f'''
import os, pathlib, signal
original = os.replace
def replace(source, destination, *args, **kwargs):
    contents = pathlib.Path(source).read_bytes() if ".saving-" in str(source) else b""
    value = original(source, destination, *args, **kwargs)
    if (destination == pathlib.Path({str(self.out)!r})
            and b"Result: launch refused\\n" in contents):
        os.kill(os.getpid(), signal.SIGTERM)
    return value
os.replace = replace
''')
        result = self.launch("verifier", "codex", "--author-family", "claude")
        header, body = self.out.read_text().split("\n\n", 1)
        self.assertIn("\nSignal: 15\nExit code: 2", header)
        self.assertIn("Result: launch refused\nReason: .agents/preflight exited 7\n", header)
        self.assertEqual(result.returncode, 143, result.stderr)
        self.assertEqual(body, output)
        self.assertEqual(self.out.with_suffix(".log").read_text(), output)
        self.assertFalse(self.record.exists())

    def test_PREFLIGHT_P2_planted_log_links_cannot_redirect_agent_writes(self):
        target = self.base / "log-target"
        marker = self.base / "preflight-ran"
        self.env["SESSION_STDERR"] = "agent stderr"
        for kind in ("symlink", "hardlink"):
            with self.subTest(kind=kind):
                target.write_text("keep these bytes")
                marker.unlink(missing_ok=True)
                self.install_preflight(f'''
path = pathlib.Path({str(self.out.with_suffix('.log'))!r})
path.unlink()
if {kind!r} == "symlink":
    path.symlink_to({str(target)!r})
else:
    os.link({str(target)!r}, path)
pathlib.Path({str(marker)!r}).touch()
''')
                self.verifier()
                self.assertTrue(marker.exists(), "preflight did not run")
                self.assertEqual(target.read_text(), "keep these bytes")

    def test_PREFLIGHT_P4_timeout_stops_detached_descendants(self):
        marker = self.blocking_preflight()
        self.env["AGENT_PREFLIGHT_TIMEOUT"] = "0.5"
        process = self.start_launcher("--timeout", "10")
        record = self.wait_preflight(marker)
        _, stderr = process.communicate(timeout=10)
        self.assert_preflight_refused(subprocess.CompletedProcess([], process.returncode, stderr=stderr),
                                      ".agents/preflight timed out after 0.5 s")
        for pid in record["pids"]:
            self.assertFalse(Path(f"/proc/{pid}").exists(), "preflight descendant survived timeout")

    def test_PREFLIGHT_P4_agent_timeout_excludes_preflight_time(self):
        self.install_preflight('time.sleep(0.5)\nprint("finished preflight")')
        self.verifier("codex", "--timeout", "0.3")
        self.assertRegex(self.out.read_text(), r"Preflight: passed in [0-9.]+ s")

    def test_PREFLIGHT_P5_committed_script_runs_after_source_changes_during_lease(self):
        marker = self.base / "committed-script"
        path = self.install_preflight(f'pathlib.Path({str(marker)!r}).write_text(str(pathlib.Path.cwd()))')
        self.inject_launcher(f'''
import pathlib, subprocess
original = subprocess.check_output
def change_source(command, *args, **kwargs):
    if "worktree" in command and "add" in command:
        pathlib.Path({str(path)!r}).write_text("#!{sys.executable}\\nraise SystemExit(8)\\n")
    return original(command, *args, **kwargs)
subprocess.check_output = change_source
''')
        record = self.verifier()
        self.assertTrue(marker.exists(), "committed preflight did not run")
        self.assertEqual(marker.read_text(), record["cwd"])
        self.assertIn("raise SystemExit(8)", path.read_text())

    def test_PREFLIGHT_P6_signals_hold_lease_until_descendants_are_gone(self):
        marker = self.blocking_preflight()
        for scoped in (True, False):
            self.configure_scope(present=scoped)
            for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP, signal.SIGKILL):
                with self.subTest(scoped=scoped, signal=sig):
                    marker.unlink(missing_ok=True)
                    process = self.start_launcher()
                    record = self.wait_preflight(marker)
                    slot = Path(record["cwd"])
                    with slot.with_suffix(".lock").open("a+b") as lock:
                        with self.assertRaises(BlockingIOError):
                            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        process.send_signal(sig)
                        deadline = time.monotonic() + 10
                        while True:
                            try:
                                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                                break
                            except BlockingIOError:
                                if time.monotonic() >= deadline:
                                    self.fail("preflight did not release lease after signal")
                                time.sleep(0.02)
                        for pid in record["pids"]:
                            self.assertFalse(Path(f"/proc/{pid}").exists(),
                                             "preflight descendant survived lease release")
                    process.communicate(timeout=10)
                    self.assertFalse(self.record.exists())
                    if sig != signal.SIGKILL:
                        self.assertEqual(process.returncode, 128 + sig)
                        self.assertIn(f"Signal: {sig}\n", self.out.read_text())
                    deadline = time.monotonic() + 5
                    while Path(record["tmpdir"]).parent.exists() and time.monotonic() < deadline:
                        time.sleep(0.02)
                    self.assertFalse(Path(record["tmpdir"]).parent.exists())

    def test_PREFLIGHT_P6_sigkill_fallback_supervises_descendants_too(self):
        marker = self.blocking_preflight()
        self.busy_pool()
        self.configure_scope(present=False)
        process = self.start_launcher()
        record = self.wait_preflight(marker)
        process.kill()
        process.communicate(timeout=10)
        deadline = time.monotonic() + 5
        while Path(record["tmpdir"]).parent.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        for pid in record["pids"]:
            self.assertFalse(Path(f"/proc/{pid}").exists())
        self.assertFalse(Path(record["tmpdir"]).parent.exists())
        self.assertFalse(self.record.exists())

    def test_PREFLIGHT_P7_other_roles_never_run_project_preflight(self):
        marker = self.base / "preflight-ran"
        self.install_preflight(f'pathlib.Path({str(marker)!r}).touch()')
        for role in ("builder", "investigator", "challenger", "reviewer"):
            for family in ("codex", "claude"):
                with self.subTest(role=role, family=family):
                    extra = ("--author-family", "claude" if family == "codex" else "codex")
                    result = self.launch(role, family, *extra)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertFalse(marker.exists())
                    self.assertNotIn("Preflight:", self.out.read_text())
        self.verifier()
        self.assertTrue(marker.exists(), "verifier did not run preflight")

    def test_PREFLIGHT_P8_tracked_or_index_changes_refuse_agent(self):
        for change in (
                'pathlib.Path("tracked").write_text("rewritten")',
                'subprocess.check_call(["git", "update-index", "--assume-unchanged", "tracked"])\n'
                'pathlib.Path("tracked").write_text("rewritten")',
                'pathlib.Path("added").write_text("new tracked file")\n'
                'subprocess.check_call(["git", "add", "added"])',
                'subprocess.check_call(["git", "read-tree", "--empty"])',
                'subprocess.check_call(["git", "checkout", "--detach", "HEAD^"])',
                'pathlib.Path("tracked").chmod(0o755)',
                'pathlib.Path("tracked").rename("ignored")\n'
                'pathlib.Path("tracked").symlink_to("ignored")',
                'os.link("tracked", "ignored")',
                'pathlib.Path(".agents").rename("ignored")\n'
                'pathlib.Path(".agents").symlink_to("ignored", target_is_directory=True)'):
            with self.subTest(change=change):
                self.install_preflight(change)
                result = self.launch("verifier", "codex", "--author-family", "claude", "--clean-checkout")
                self.assert_preflight_refused(result, "preflight changed tracked files")

    def test_PREFLIGHT_P9_existing_refusals_precede_project_commands(self):
        marker = self.base / "preflight-ran"
        self.install_preflight(f'pathlib.Path({str(marker)!r}).touch()')
        (self.repo / "tracked").write_text("uncommitted")
        result = self.launch("verifier", "codex", "--author-family", "claude")
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("commit first", self.out.read_text())
        self.assertFalse(marker.exists())
        (self.repo / "tracked").write_text("original")
        for extra, reason in ((("--timeout", "0"), "--timeout must be positive"),
                              (("--unknown",), "unrecognized arguments")):
            result = self.launch("verifier", "codex", "--author-family", "claude", *extra)
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertIn(reason, self.out.read_text())
            self.assertFalse(marker.exists())
        executable = self.env["AGENT_CODEX_CMD"]
        self.env["AGENT_CODEX_CMD"] = str(self.base / "missing")
        result = self.launch("verifier", "codex", "--author-family", "claude")
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("Cannot find agent executable", self.out.read_text())
        self.assertFalse(marker.exists())
        self.env["AGENT_CODEX_CMD"] = executable
        unborn = self.base / "unborn"
        subprocess.check_call(["git", "init", "-q", str(unborn)], env=self.env)
        shutil.copytree(self.repo / ".agents", unborn / ".agents")
        result = self.launch("verifier", "codex", "--author-family", "claude", "--repo", str(unborn))
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("committed candidate", self.out.read_text())
        self.assertFalse(marker.exists())
        self.verifier()
        self.assertTrue(marker.exists(), "valid candidate's preflight did not run")

    def test_PREFLIGHT_P10_nonexecutable_symlink_and_directory_are_refused(self):
        for kind, reason in (("nonexecutable", "is not executable"),
                             ("symlink", "is not a regular file"),
                             ("directory", "is not a regular file")):
            with self.subTest(kind=kind):
                path = self.install_preflight("raise SystemExit(99)")
                if kind == "nonexecutable":
                    path.chmod(0o644)
                elif kind == "symlink":
                    path.unlink()
                    path.symlink_to(self.env["AGENT_CODEX_CMD"])
                else:
                    path.unlink()
                    path.mkdir()
                    (path / "child").touch()
                self.git("add", "-A")
                self.git("commit", "-qm", kind)
                result = self.launch("verifier", "codex", "--author-family", "claude")
                self.assert_preflight_refused(result, f".agents/preflight {reason}")

    def test_PREFLIGHT_P11_all_lease_routes_run_only_candidate(self):
        marker = self.base / "preflight-runs"
        self.install_preflight(f'''
with pathlib.Path({str(marker)!r}).open("a") as stream:
    stream.write(str(pathlib.Path.cwd()) + "\\n")
pathlib.Path("ignored").write_text("candidate build")
''')
        record = self.verifier("codex", "--before", "HEAD")
        self.assertTrue(marker.exists(), "fresh candidate did not run preflight")
        self.assertEqual(marker.read_text().splitlines(), [record["cwd"]])
        before = Path(next(line.split(": ", 1)[1].split(" (")[0] for line in self.out.read_text().splitlines()
                           if line.startswith("Before checkout:")))
        self.assertFalse((before / "ignored").exists(), "before checkout ran preflight")
        slot = Path(record["cwd"])
        self.hold_other_pool_slots(slot)
        for extra, note in (((), "warm reset"), (("--clean-checkout",), "deep clean")):
            record = self.verifier("codex", *extra)
            self.assertIn(note, self.out.read_text())
            self.assertEqual(record["cwd"], str(slot))
        with slot.with_suffix(".lock").open("a+b") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            record = self.verifier()
            self.assertIn("pool busy or slots unusable", self.out.read_text())
            self.assertFalse(Path(record["cwd"]).is_relative_to(self.pool_root()))
        runs = marker.read_text().splitlines()
        self.assertEqual(runs, [str(slot)] * 3 + [record["cwd"]])

    def seed_pool(self, commits):
        slots = []
        with ExitStack() as held:
            for commit in commits:
                self.git("reset", "--hard", commit)
                slot = Path(self.verifier()["cwd"])
                slots.append(slot)
                lock = held.enter_context(slot.with_suffix(".lock").open("a+b"))
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return slots

    def pool_probe(self):
        self.agent_extra('''
record["head"] = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
record["branch"] = subprocess.check_output(["git", "rev-parse", "--abbrev-ref", "HEAD"], text=True).strip()
record["status"] = subprocess.check_output(["git", "status", "--porcelain", "--ignored=no"], text=True)
record["files"] = sorted(str(p.relative_to(cwd)) for p in cwd.rglob("*") if p.is_file())
pathlib.Path(os.environ["RECORD"]).write_text(json.dumps(record))
''')

    def admission_fixture(self, phases, *, hook="", max_wait="1800"):
        for name in list(self.env):
            if name.startswith("AGENT_ADMISSION_"):
                del self.env[name]
        self.env["AGENT_ADMISSION_MAX_WAIT"] = max_wait
        self.admission_events = self.base / "admission-events.json"
        self.inject_launcher(f'''
import json, pathlib, subprocess, time
phases = {phases!r}
clock = [0]
events = []
events_path = pathlib.Path({str(self.admission_events)!r})
events_path.write_text(json.dumps(events))
original_read = pathlib.Path.read_text
def read(path, *args, **kwargs):
    name = str(path)
    if name.startswith(("/proc/pressure/", "/sys/fs/cgroup/")) or name in ("/proc/meminfo", "/proc/self/cgroup"):
        value = phases[min(len(events), len(phases) - 1)].get(name)
        if value is None:
            raise PermissionError(name)
        return value
    return original_read(path, *args, **kwargs)
pathlib.Path.read_text = read
time.monotonic = lambda: clock[0]
def sleep(seconds):
    events.append(seconds)
    events_path.write_text(json.dumps(events))
    clock[0] += seconds
{textwrap.indent(textwrap.dedent(hook), '    ')}
time.sleep = sleep
''')

    def test_ADMISSION_A1_low_pressure_starts_immediately(self):
        self.admission_fixture([{"/proc/pressure/memory": "some avg10=0\nfull avg10=0\n"}])
        result = self.launch()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Admission: waited 0 s\n", self.out.read_text())
        self.assertEqual(json.loads(self.admission_events.read_text()), [])

    def test_ADMISSION_A2_each_threshold_waits_until_all_are_below(self):
        service = "/user.slice/user-4242.slice/user@4242.service"
        slice_path = "/sys/fs/cgroup" + service + "/agents.slice/"
        readings = [
            {"/proc/pressure/memory": "some avg10=5\n"},
            {"/proc/pressure/memory": "full avg10=2\n"},
            {"/proc/pressure/io": "full avg10=10\n"},
            {"/proc/meminfo": "MemAvailable: 8388607 kB\n"},
            {"/proc/meminfo": "SwapTotal: 100 kB\nSwapFree: 9 kB\n"},
            {"/proc/self/cgroup": "0::" + service + "/agents.slice/agent.scope\n",
             slice_path + "memory.high": str(8 * 1024**3),
             slice_path + "memory.current": str(4 * 1024**3 + 1)},
        ]
        low = {"/proc/pressure/memory": "some avg10=4.99\nfull avg10=1.99\n",
               "/proc/pressure/io": "full avg10=9.99\n",
               "/proc/meminfo": "MemAvailable: 8388608 kB\nSwapTotal: 100 kB\nSwapFree: 10 kB\n",
               "/proc/self/cgroup": "0::" + service + "/agent.scope\n",
               slice_path + "memory.high": str(8 * 1024**3),
               slice_path + "memory.current": str(4 * 1024**3)}
        for reading in readings:
            with self.subTest(reading=reading):
                # Retain the service discovery path in the first sample too.
                first = dict(low, **reading)
                self.admission_fixture([first, first, low])
                result = self.launch()
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(self.admission_events.read_text()), [10, 10])
                self.assertIn("Admission: waited 20 s\n", self.out.read_text())

    def test_ADMISSION_A2_threshold_overrides_and_cgroup_fallback(self):
        slice_path = f"/sys/fs/cgroup/user.slice/user-{os.getuid()}.slice/user@{os.getuid()}.service/agents.slice/"
        cases = [("MEMORY_SOME", {"/proc/pressure/memory": "some avg10=1\n"}, "0.5"),
                 ("MEMORY_FULL", {"/proc/pressure/memory": "full avg10=1\n"}, "0.5"),
                 ("IO_FULL", {"/proc/pressure/io": "full avg10=1\n"}, "0.5"),
                 ("MEM_AVAILABLE", {"/proc/meminfo": "MemAvailable: 1 kB\n"}, "2048"),
                 ("SWAP_FREE", {"/proc/meminfo": "SwapTotal: 100 kB\nSwapFree: 50 kB\n"}, "60"),
                 ("CGROUP_HEADROOM", {slice_path + "memory.high": "4096",
                                       slice_path + "memory.current": "2048"}, "4096")]
        for name, reading, limit in cases:
            with self.subTest(name=name):
                self.admission_fixture([reading], max_wait="10")
                self.env["AGENT_ADMISSION_" + name] = limit
                result = self.launch()
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(self.admission_events.read_text()), [10])
                self.assertIn("Admission: started under pressure after 10 s (" + name + "=", self.out.read_text())

    def test_ADMISSION_A3_deadline_does_not_restart_when_readings_fluctuate(self):
        first = {"/proc/pressure/memory": "some avg10=7\n"}
        second = {"/proc/pressure/io": "full avg10=11\n"}
        self.admission_fixture([first, second, first, second], max_wait="25")
        result = self.launch()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(self.admission_events.read_text()), [10, 10, 5])
        self.assertIn("Admission: started under pressure after 25 s (IO_FULL=11)\n", self.out.read_text())
        self.admission_fixture([first])
        result = self.launch()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(sum(json.loads(self.admission_events.read_text())), 1800)
        self.assertIn("Admission: started under pressure after 1800 s (MEMORY_SOME=7)\n", self.out.read_text())

    def test_ADMISSION_A4_missing_malformed_and_partial_information(self):
        for reading in ({}, {"/proc/pressure/memory": "some avg10=garbage\nfull avg10=nan\n",
                              "/proc/meminfo": "MemAvailable: broken kB\n"}):
            with self.subTest(reading=reading):
                self.admission_fixture([reading])
                result = self.launch()
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("Admission: unavailable\n", self.out.read_text())
                self.assertEqual(json.loads(self.admission_events.read_text()), [])
        self.admission_fixture([{"/proc/pressure/memory": "some avg10=broken\nfull avg10=2\n"},
                                {"/proc/meminfo": "SwapTotal: 0 kB\nSwapFree: 0 kB\nMemAvailable: 8388608 kB\n"}])
        result = self.launch()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(self.admission_events.read_text()), [10])
        self.assertIn("Admission: waited 10 s\n", self.out.read_text())

    def test_ADMISSION_A5_wait_has_no_leases_or_pool_changes(self):
        result = self.launch("verifier", "codex", "--author-family", "claude", "--before", "HEAD")
        self.assertEqual(result.returncode, 0, result.stderr)
        pool = self.pool_root()
        snapshot = {str(path): path.read_bytes() for path in pool.rglob("*") if path.is_file()}
        self.admission_fixture([{"/proc/pressure/io": "full avg10=10\n"},
                                {"/proc/pressure/io": "full avg10=0\n"}], hook=f'''
import fcntl
assert not pathlib.Path({str(self.record)!r}).exists(), "agent started while waiting"
pool = pathlib.Path({str(pool)!r})
assert {{str(path): path.read_bytes() for path in pool.rglob("*") if path.is_file()}} == {snapshot!r}
for path in pool.glob("*.lock"):
    with path.open("rb") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
''')
        self.record.unlink()
        result = self.launch("verifier", "codex", "--author-family", "claude", "--before", "HEAD")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(self.admission_events.read_text()), [10])
        self.assertIn("Admission: waited 10 s\n", self.out.read_text())
        self.assertIn("Before checkout:", self.out.read_text())

    def test_ADMISSION_A6_signals_while_waiting_record_interruption(self):
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            for role in ("builder", "verifier"):
                with self.subTest(sig=sig, role=role):
                    if role == "verifier":
                        self.round_decision()
                    self.admission_fixture([{"/proc/pressure/io": "full avg10=10\n"}],
                                           hook=f"import os, signal\nos.kill(os.getpid(), {int(sig)})")
                    result = self.launch(role, "codex", *(["--author-family", "claude", "--before", "HEAD"]
                                                         if role == "verifier" else []))
                    self.assertEqual(result.returncode, 128 + sig, result.stderr)
                    output = self.out.read_text()
                    self.assertIn("Result: interrupted\n", output)
                    self.assertIn(f"Signal: {sig}\n", output)
                    self.assertFalse(self.record.exists())
                    self.assertFalse(self.pool_root().exists())

    def test_ADMISSION_A7_only_builders_and_verifiers_wait(self):
        for role in ("builder", "verifier", "investigator", "challenger", "reviewer"):
            with self.subTest(role=role):
                self.admission_fixture([{"/proc/pressure/io": "full avg10=10\n"},
                                        {"/proc/pressure/io": "full avg10=0\n"}])
                result = self.launch(role, "codex", *(["--author-family", "claude"]
                                                     if role in ("verifier", "challenger", "reviewer") else []))
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(self.admission_events.read_text()),
                                 [10] if role in ("builder", "verifier") else [])

    def test_ADMISSION_A8_wait_does_not_spend_agent_timeout(self):
        self.admission_fixture([{"/proc/pressure/io": "full avg10=10\n"},
                                {"/proc/pressure/io": "full avg10=0\n"}])
        result = self.launch("builder", "codex", "--timeout", "5")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Admission: waited 10 s\n", self.out.read_text())
        self.assertIn("answer\n  unchanged\n", self.out.read_text())

    def test_ADMISSION_A9_standalone_gate_reports_and_exits_zero(self):
        cases = [([{}], "1800", "Admission: unavailable\n"),
                 ([{"/proc/pressure/io": "full avg10=0\n"}], "1800", "Admission: waited 0 s\n"),
                 ([{"/proc/pressure/io": "full avg10=10\n"}, {"/proc/pressure/io": "full avg10=0\n"}],
                  "1800", "Admission: waited 10 s\n"),
                 ([{"/proc/pressure/io": "full avg10=10\n"}], "10",
                  "Admission: started under pressure after 10 s (IO_FULL=10)\n")]
        for phases, maximum, expected in cases:
            with self.subTest(expected=expected):
                self.admission_fixture(phases, max_wait=maximum)
                result = self.run_script("run.py", "--wait-for-room")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stderr, expected)
                self.assertEqual(result.stdout, "")
                self.assertFalse(self.record.exists())
                self.assertFalse(self.pool_root().exists())

    def test_POOL_S1_reset_removes_previous_files_and_keeps_ignored_output(self):
        for name in ("gone", "ignored-later"):
            (self.repo / name).write_text("old tracked file")
        self.git("add", ".")
        self.git("commit", "-qm", "old files")
        self.pool_probe()
        first = self.verifier()
        slot = Path(first["cwd"])
        self.assertTrue(slot.is_relative_to(self.pool_root()))
        (slot / "ignored").mkdir()
        (slot / "ignored/build").write_text("warm build")
        (slot / "tracked").write_text("local edits")
        (slot / "staged").write_text("staged addition")
        subprocess.check_call(["git", "-C", str(slot), "add", "staged"], env=self.env)
        (slot / "new").write_text("forgotten untracked")
        subprocess.check_call(["git", "init", "-q", str(slot / "nested")], env=self.env)
        (slot / "nested/leftover").touch()
        for name in ("gone", "ignored-later"):
            (self.repo / name).unlink()
        (self.repo / ".gitignore").write_text("ignored\nignored-later\n")
        self.git("add", "-A")
        self.git("commit", "-qm", "remove old files")
        second = self.verifier()
        self.assertEqual(second["cwd"], first["cwd"])
        self.assertEqual(second["head"], self.git("rev-parse", "HEAD"))
        self.assertEqual(second["branch"], "HEAD")
        self.assertEqual(second["status"], "")
        self.assertEqual(second["changed"], "original")
        for name in ("gone", "ignored-later", "new", "staged", "nested"):
            self.assertFalse((slot / name).exists(), name)
        self.assertEqual((slot / "ignored/build").read_text(), "warm build")
        self.assertIn("warm reset", self.out.read_text())
        self.assert_session_cleaned(second)

    def test_POOL_R5_populated_submodules_are_emptied_without_shared_state_changes(self):
        # git-submodule and Git's submodule status need system utilities too.
        self.env["PATH"] += os.pathsep + os.environ["PATH"]
        module = self.base / "module"
        subprocess.check_call(["git", "init", "-q", str(module)], env=self.env)
        (module / "content").write_text("submodule content")
        (module / ".gitignore").write_text("ignored\noutside-link\n")
        for args in (("add", "."), ("commit", "-qm", "submodule baseline")):
            subprocess.check_call(["git", "-C", str(module), *args], env=self.env)
        path = "vendor/sub module"
        subprocess.check_call([
            "git", "-C", str(self.repo), "-c", "protocol.file.allow=always",
            "submodule", "add", "-q", str(module), path], env=self.env)
        self.git("commit", "-qam", "add submodule")
        self.agent_extra(f'''
record["submodule_entries"] = sorted(p.name for p in (cwd / {path!r}).iterdir())
record["status"] = subprocess.check_output(["git", "status", "--porcelain"], text=True)
pathlib.Path(os.environ["RECORD"]).write_text(json.dumps(record))
''')
        first = self.verifier()
        slot = Path(first["cwd"])
        self.hold_other_pool_slots(slot)
        self.assertEqual(first["submodule_entries"], [])
        fresh = self.base / "fresh"
        self.git("worktree", "add", "--quiet", "--detach", str(fresh), "HEAD")
        self.assertEqual(list((fresh / path).iterdir()), [])

        def shared_state():
            modules = self.repo / ".git/modules"
            return ((self.repo / ".git/config").read_bytes(),
                    {str(p.relative_to(modules)): p.read_bytes()
                     for p in modules.rglob("*") if p.is_file()})

        for clean in (False, True):
            with self.subTest(deep_clean=clean):
                subprocess.check_call([
                    "git", "-C", str(slot), "-c", "protocol.file.allow=always",
                    "submodule", "update", "--init", "--quiet", "--", path], env=self.env)
                self.assertEqual((slot / path / "content").read_text(), "submodule content")
                outside = self.base / "outside-submodule"
                outside.mkdir(exist_ok=True)
                (outside / "keep").write_text("untouched")
                (slot / path / "outside-link").unlink(missing_ok=True)
                (slot / path / "outside-link").symlink_to(outside, target_is_directory=True)
                (slot / path / "ignored").write_text("ignored content")
                before = shared_state()
                record = self.verifier("codex", *(["--clean-checkout"] if clean else []))
                self.assertEqual(record["cwd"], str(slot))
                self.assertEqual(record["submodule_entries"], [])
                self.assertEqual(record["status"], "")
                self.assertEqual((outside / "keep").read_text(), "untouched")
                self.assertEqual(shared_state(), before)
                self.assertIn("--clean-checkout" if clean else "warm reset", self.out.read_text())

        for kind in ("symlink", "embedded-git-directory", "missing"):
            with self.subTest(gitlink=kind):
                shutil.rmtree(slot / path)
                if kind == "symlink":
                    (slot / path).symlink_to(outside, target_is_directory=True)
                elif kind == "embedded-git-directory":
                    (slot / path / ".git").mkdir(parents=True)
                    (slot / path / ".git/leftover").write_text("private module state")
                before = shared_state()
                record = self.verifier()
                self.assertEqual(record["cwd"], str(slot))
                self.assertFalse((slot / path).is_symlink())
                self.assertEqual(record["submodule_entries"], [])
                self.assertEqual(record["status"], "")
                self.assertEqual((outside / "keep").read_text(), "untouched")
                self.assertEqual(shared_state(), before)
                self.assertIn("warm reset", self.out.read_text())

    def poisoned_flag_checkout(self, flag):
        slot = Path(self.verifier()["cwd"])
        self.hold_other_pool_slots(slot)
        for clean in (False, True):
            with self.subTest(flag=flag, deep_clean=clean):
                subprocess.check_call(["git", "-C", str(slot), "update-index", flag, "tracked"],
                                      env=self.env)
                (slot / "tracked").write_text("poisoned content")
                (slot / "ignored").write_text("warm output")
                record = self.verifier("codex", *(["--clean-checkout"] if clean else []))
                self.assertEqual(record["cwd"], str(slot))
                self.assertEqual((slot / "tracked").read_text(), "original")
                self.assertEqual(subprocess.check_output(
                    ["git", "-C", str(slot), "ls-files", "-v", "tracked"],
                    env=self.env, text=True), "H tracked\n")
                self.assertEqual((slot / "ignored").exists(), not clean)

    def test_POOL_S1_skip_worktree_is_rebuilt_on_warm_and_deep_leases(self):
        self.poisoned_flag_checkout("--skip-worktree")

    def test_POOL_S1_assume_unchanged_is_rebuilt_on_warm_and_deep_leases(self):
        self.poisoned_flag_checkout("--assume-unchanged")

    def test_POOL_R1_slot_excludes_and_hidden_untracked_config_are_discarded(self):
        self.git("config", "extensions.worktreeConfig", "true")
        slot = Path(self.verifier()["cwd"])
        excludes = self.base / "excludes"
        excludes.write_text("leftover\n")
        for key, value in (("core.excludesFile", str(excludes)), ("status.showUntrackedFiles", "no")):
            subprocess.check_call(["git", "-C", str(slot), "config", "--worktree", key, value], env=self.env)
        (slot / "leftover").touch()
        (slot / "ignored").write_text("warm output")
        common_config = (self.repo / ".git/config").read_bytes()
        self.assertEqual(self.verifier()["cwd"], str(slot))
        self.assertFalse((slot / "leftover").exists())
        self.assertEqual((slot / "ignored").read_text(), "warm output")
        self.assertEqual((self.repo / ".git/config").read_bytes(), common_config)
        self.assertIn("warm reset", self.out.read_text())

    def test_POOL_R1_slot_smudge_filter_cannot_change_candidate_bytes(self):
        (self.repo / ".gitattributes").write_text("tracked filter=slot\n")
        self.git("add", ".gitattributes")
        self.git("commit", "-qm", "filter attribute without a filter")
        self.git("config", "extensions.worktreeConfig", "true")
        slot = Path(self.verifier()["cwd"])
        for kind, content in (("smudge", "poisoned"), ("clean", "original")):
            command = shlex.join([sys.executable, "-c", f"import sys; sys.stdin.read(); sys.stdout.write({content!r})"])
            subprocess.check_call(["git", "-C", str(slot), "config", "--worktree",
                                   f"filter.slot.{kind}", command], env=self.env)
        (slot / "tracked").unlink()
        subprocess.check_call(["git", "-C", str(slot), "checkout", "--", "tracked"], env=self.env)
        self.assertEqual((slot / "tracked").read_text(), "poisoned")
        self.assertEqual(subprocess.check_output(["git", "-C", str(slot), "status", "--porcelain"],
                                                env=self.env), b"")
        record = self.verifier()
        self.assertEqual(record["cwd"], str(slot))
        self.assertEqual(record["changed"], "original")
        self.assertIn("warm reset", self.out.read_text())

    def test_POOL_R1_slot_hooks_and_fsmonitor_do_not_run_during_reset(self):
        self.git("config", "extensions.worktreeConfig", "true")
        slot = Path(self.verifier()["cwd"])
        hooks = self.base / "slot-hooks"
        hooks.mkdir()
        marker = self.base / "slot-command-ran"
        script = f'#!{sys.executable}\nfrom pathlib import Path\nPath({str(marker)!r}).touch()\n'
        for name in ("post-checkout", "fsmonitor"):
            (hooks / name).write_text(script)
            (hooks / name).chmod(0o755)
        for key, value in (("core.hooksPath", str(hooks)), ("core.fsmonitor", str(hooks / "fsmonitor"))):
            subprocess.check_call(["git", "-C", str(slot), "config", "--worktree", key, value], env=self.env)
        record = self.verifier()
        self.assertEqual(record["cwd"], str(slot))
        self.assertFalse(marker.exists(), "a command left by the previous slot user ran")

    def assert_slot_bytes_match_fresh(self, slot):
        fresh = self.base / "fresh-checkout"
        subprocess.check_call(["git", "-C", str(self.repo), "worktree", "add", "--detach", str(fresh), "HEAD"], env=self.env)
        try:
            paths = subprocess.check_output(["git", "-C", str(fresh), "ls-files", "-z"], env=self.env)
            for raw in paths.split(b"\0"):
                if raw:
                    name = os.fsdecode(raw)
                    self.assertEqual((slot / name).read_bytes(), (fresh / name).read_bytes(), name)
        finally:
            self.git("worktree", "remove", "--force", str(fresh))

    def test_POOL_R8_nontracked_attributes_cannot_hide_changed_file_bytes(self):
        (self.repo / "tracked").write_bytes(b"original\n")
        (self.repo / "sub").mkdir()
        (self.repo / "sub/data").write_bytes(b"nested\n")
        (self.repo / "ignored").mkdir()
        (self.repo / "ignored/data").write_bytes(b"ignored directory\n")
        (self.repo / ".gitignore").write_text("ignored\nwarm-output\n")
        self.git("add", "-f", "ignored/data")
        self.git("add", ".")
        self.git("commit", "-qm", "text fixtures")
        slot = Path(self.verifier()["cwd"])
        for name, target, ignored in ((".gitattributes", "tracked", False),
                                      ("sub/.gitattributes", "sub/data", True),
                                      ("ignored/.gitattributes", "ignored/data", True)):
            with self.subTest(name=name, ignored=ignored):
                if ignored:
                    (self.repo / ".gitignore").write_text("ignored\nwarm-output\n**/.gitattributes\n")
                    self.git("add", ".gitignore")
                    if self.git("diff", "--cached", "--name-only"):
                        self.git("commit", "-qm", "ignore attributes")
                (slot / name).write_text("* text\n")
                (slot / target).write_bytes((slot / target).read_bytes().replace(b"\n", b"\r\n"))
                # Avoid Git's same-second racy-stat fallback hiding the bug.
                os.utime(slot / target, (1, 1))
                (slot / "warm-output").write_text("warm build")
                self.assertEqual(self.verifier()["cwd"], str(slot))
                self.assertFalse((slot / name).exists())
                self.assert_slot_bytes_match_fresh(slot)
                self.assertEqual((slot / "warm-output").read_text(), "warm build")

    def test_POOL_R8_committed_attributes_changes_rewrite_unchanged_blobs(self):
        (self.repo / "tracked").write_bytes(b"original\n")
        (self.repo / ".gitattributes").write_text("tracked text eol=lf\n")
        self.git("add", ".")
        self.git("commit", "-qm", "LF attributes")
        blob = self.git("rev-parse", "HEAD:tracked")
        slot = Path(self.verifier()["cwd"])
        for eol in ("crlf", "lf"):
            with self.subTest(eol=eol):
                (self.repo / ".gitattributes").write_text(f"tracked text eol={eol}\n")
                self.git("add", ".gitattributes")
                self.git("commit", "-qm", "change attributes only")
                self.assertEqual(self.git("rev-parse", "HEAD:tracked"), blob)
                os.utime(slot / "tracked", (1, 1))
                (slot / "ignored").write_text("warm build")
                self.assertEqual(self.verifier()["cwd"], str(slot))
                self.assert_slot_bytes_match_fresh(slot)
                self.assertEqual((slot / "ignored").read_text(), "warm build")

    def test_POOL_R8_local_tracked_attributes_cannot_hide_changed_file_bytes(self):
        (self.repo / "tracked").write_bytes(b"original\n")
        (self.repo / ".gitattributes").write_text("tracked text eol=lf\n")
        self.git("add", ".")
        self.git("commit", "-qm", "LF attributes")
        slot = Path(self.verifier()["cwd"])
        (slot / ".gitattributes").write_text("tracked text\n")
        (slot / "tracked").write_bytes(b"original\r\n")
        self.assertEqual(self.verifier()["cwd"], str(slot))
        self.assert_slot_bytes_match_fresh(slot)

    def test_POOL_R8_slot_private_attributes_are_unused_and_shared_config_is_preserved(self):
        (self.repo / "tracked").write_bytes(b"original\n")
        self.git("add", ".")
        self.git("commit", "-qm", "text fixture")
        slot = Path(self.verifier()["cwd"])
        admin = Path(subprocess.check_output(["git", "-C", str(slot), "rev-parse", "--absolute-git-dir"],
                                            text=True, env=self.env).strip())
        for kind in ("private", "configured"):
            with self.subTest(kind=kind):
                if kind == "private":
                    attributes = admin / "info/attributes"
                    attributes.parent.mkdir(exist_ok=True)
                else:
                    attributes = self.base / "global-attributes"
                    self.git("config", "core.attributesFile", str(attributes))
                attributes.write_text("tracked text eol=crlf\n")
                (slot / "tracked").write_bytes(b"original\r\n")
                config = (self.repo / ".git/config").read_bytes()
                self.assertEqual(self.verifier()["cwd"], str(slot))
                if kind == "private":
                    self.assertFalse(attributes.exists(), "slot-private admin state must be cleared")
                self.assert_slot_bytes_match_fresh(slot)
                self.assertEqual((self.repo / ".git/config").read_bytes(), config)

    def test_POOL_R9_raw_eol_bytes_are_checked_on_warm_and_deep_reset(self):
        (self.repo / "tracked").write_bytes(b"original\n")
        (self.repo / ".gitattributes").write_text("tracked text eol=lf\n")
        self.git("add", ".")
        self.git("commit", "-qm", "LF fixture")
        slot = Path(self.verifier()["cwd"])
        self.hold_other_pool_slots(slot)
        for clean in (False, True):
            with self.subTest(deep_clean=clean):
                (slot / "tracked").write_bytes(b"original\r\n")
                os.utime(slot / "tracked", (1, 1))
                self.verifier("codex", *(["--clean-checkout"] if clean else []))
                self.assertEqual((slot / "tracked").read_bytes(), b"original\n")

    def test_POOL_R9_shared_attributes_apply_between_leases(self):
        (self.repo / "text.txt").write_bytes(b"text\n")
        self.git("add", ".")
        self.git("commit", "-qm", "text fixture")
        slot = Path(self.verifier()["cwd"])
        (self.repo / ".git/info/attributes").write_text("*.txt text eol=crlf\n")
        self.verifier()
        self.assertEqual((slot / "text.txt").read_bytes(), b"text\r\n")
        self.assert_slot_bytes_match_fresh(slot)

    def test_POOL_R9_global_attributes_match_fresh_checkout_and_stay_warm(self):
        (self.repo / "text.txt").write_bytes(b"text\n")
        self.git("add", ".")
        self.git("commit", "-qm", "text fixture")
        attributes = self.base / "global-attributes"
        attributes.write_text("*.txt text eol=crlf\n")
        global_config = self.base / "global-config"
        global_config.write_text("[core]\nattributesFile = " + str(attributes) + "\n")
        self.env["GIT_CONFIG_GLOBAL"] = str(global_config)
        slot = Path(self.verifier()["cwd"])
        self.assertEqual((slot / "text.txt").read_bytes(), b"text\r\n")
        before = (slot / "text.txt").stat()
        self.verifier()
        after = (slot / "text.txt").stat()
        self.assertEqual((before.st_ino, before.st_mtime_ns), (after.st_ino, after.st_mtime_ns))
        self.assert_slot_bytes_match_fresh(slot)

    def test_POOL_R9_ident_expansion_is_verified_and_kept_warm(self):
        (self.repo / "tracked").write_bytes(b"$Id$\n")
        (self.repo / ".gitattributes").write_text("tracked ident\n")
        self.git("add", ".")
        self.git("commit", "-qm", "ident fixture")
        slot = Path(self.verifier()["cwd"])
        expected = (slot / "tracked").read_bytes()
        self.assertIn(self.git("rev-parse", "HEAD:tracked").encode(), expected)
        before = (slot / "tracked").stat()
        self.verifier()
        after = (slot / "tracked").stat()
        self.assertEqual((before.st_ino, before.st_mtime_ns), (after.st_ino, after.st_mtime_ns))
        (slot / "tracked").write_bytes(b"$Id: wrong expansion $\n")
        os.utime(slot / "tracked", (1, 1))
        self.verifier()
        self.assertEqual((slot / "tracked").read_bytes(), expected)

    def test_POOL_R9_untracked_ignore_rules_cannot_protect_stale_files(self):
        slot = Path(self.verifier()["cwd"])
        (slot / "src").mkdir()
        (slot / "src/.gitignore").write_text("*\n")
        (slot / "src/stale").write_text("forgotten source")
        (slot / "ignored").write_text("warm build")
        self.verifier()
        self.assertFalse((slot / "src/stale").exists())
        self.assertFalse((slot / "src/.gitignore").exists())
        self.assertEqual((slot / "ignored").read_text(), "warm build")

    def test_POOL_R9_recreation_removes_readonly_directories(self):
        slot = Path(self.verifier()["cwd"])
        for mode in (0o500, 0o000):
            with self.subTest(mode=oct(mode)):
                (slot / "ignored").mkdir()
                (slot / "ignored/leftover").write_text("old build")
                (slot / "ignored").chmod(mode)
                try:
                    (slot / ".git").write_text("broken")
                    record = self.verifier()
                    self.assertEqual(record["cwd"], str(slot))
                    self.assertFalse((slot / "ignored").exists())
                    self.assertIn("deep clean: recreated", self.out.read_text())
                finally:
                    if (slot / "ignored").exists():
                        (slot / "ignored").chmod(0o700)
                        shutil.rmtree(slot / "ignored")

    def test_POOL_R9_unremovable_slot_releases_lock_and_tries_next(self):
        slot = Path(self.verifier()["cwd"])
        (slot / ".git").write_text("broken")
        self.inject_launcher('''
import shutil
original = shutil.rmtree
def cannot_remove(path, *args, **kwargs):
    if str(path).endswith("/slot-1"):
        raise PermissionError("simulated inaccessible slot")
    return original(path, *args, **kwargs)
shutil.rmtree = cannot_remove
''')
        result = self.launch("verifier", "codex", "--author-family", "claude")
        self.assertEqual(result.returncode, 0, result.stderr)
        record = json.loads(self.record.read_text())
        self.assertEqual(record["cwd"], str(slot.with_name("slot-2")))
        for message in (str(slot), "simulated inaccessible slot"):
            self.assertIn(message, result.stderr)
            self.assertIn(message, self.out.read_text())
        with slot.with_suffix(".lock").open("a+b") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_POOL_R9_one_file_change_preserves_unchanged_files_and_build_output(self):
        slot = Path(self.verifier()["cwd"])
        before = (slot / ".gitignore").stat()
        (slot / "ignored").write_text("warm build")
        (self.repo / "tracked").write_text("next candidate")
        self.git("add", ".")
        self.git("commit", "-qm", "one file change")
        record = self.verifier()
        after = (slot / ".gitignore").stat()
        self.assertEqual(record["cwd"], str(slot))
        self.assertEqual((before.st_ino, before.st_mtime_ns), (after.st_ino, after.st_mtime_ns))
        self.assertEqual((slot / "tracked").read_text(), "next candidate")
        self.assertEqual((slot / "ignored").read_text(), "warm build")

    def test_POOL_R9_smudge_filter_bytes_are_checked_and_kept_warm(self):
        (self.repo / "tracked").write_bytes(b"original\n")
        (self.repo / ".gitattributes").write_text("tracked filter=fixture\n")
        clean = shlex.join([sys.executable, "-c",
                            "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read().replace(b'SMUDGED:', b''))"])
        smudge = shlex.join([sys.executable, "-c",
                             "import sys; sys.stdout.buffer.write(b'SMUDGED:' + sys.stdin.buffer.read())"])
        self.git("config", "filter.fixture.clean", clean)
        self.git("config", "filter.fixture.smudge", smudge)
        self.git("add", ".")
        self.git("commit", "-qm", "smudge fixture")
        slot = Path(self.verifier()["cwd"])
        expected = b"SMUDGED:original\n"
        self.assertEqual((slot / "tracked").read_bytes(), expected)
        before = (slot / "tracked").stat()
        self.verifier()
        after = (slot / "tracked").stat()
        self.assertEqual((before.st_ino, before.st_mtime_ns), (after.st_ino, after.st_mtime_ns))
        (slot / "tracked").write_bytes(b"original\n")
        os.utime(slot / "tracked", (1, 1))
        self.verifier()
        self.assertEqual((slot / "tracked").read_bytes(), expected)
        self.assert_slot_bytes_match_fresh(slot)

    def test_POOL_R9_raw_check_handles_odd_paths_symlinks_and_modes(self):
        names = ['tab\tname', 'line\nname', 'quote"name', 'slash\\name', 'café']
        for name in names:
            (self.repo / name).write_bytes(b"line\n")
        (self.repo / "link").symlink_to("tracked")
        (self.repo / "executable").write_text("exit 0\n")
        (self.repo / "executable").chmod(0o755)
        (self.repo / ".gitattributes").write_text("* text eol=crlf\n")
        self.git("add", ".")
        self.git("commit", "-qm", "odd paths and Git modes")
        slot = Path(self.verifier()["cwd"])
        self.git("config", "core.filemode", "false")
        (slot / "executable").chmod(0o644)
        (slot / "link").unlink()
        (slot / "link").write_text("tracked")
        self.verifier()
        self.assertTrue((slot / "executable").stat().st_mode & 0o111)
        self.assertTrue((slot / "link").is_symlink())
        self.assertEqual(os.readlink(slot / "link"), "tracked")
        for name in names:
            self.assertEqual((slot / name).read_bytes(), b"line\r\n")
        self.git("config", "core.symlinks", "false")
        self.verifier()
        self.assertFalse((slot / "link").is_symlink())
        self.assertEqual((slot / "link").read_bytes(), b"tracked")

    def test_POOL_R9_core_eol_and_legacy_crlf_attributes_match_fresh(self):
        (self.repo / "tracked").write_bytes(b"line\n")
        (self.repo / ".gitattributes").write_text("tracked crlf\n")
        self.git("add", ".")
        self.git("commit", "-qm", "legacy attributes")
        slot = Path(self.verifier()["cwd"])
        self.git("config", "core.eol", "CRLF")
        self.verifier()
        self.assertEqual((slot / "tracked").read_bytes(), b"line\r\n")
        self.assert_slot_bytes_match_fresh(slot)

    def test_POOL_R9_reset_has_no_head_message_on_stderr(self):
        self.verifier()
        result = self.launch("verifier", "codex", "--author-family", "claude")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("HEAD is now at", result.stderr)

    def test_POOL_R9_killed_guard_without_systemd_stops_agent_group(self):
        self.configure_scope(present=False)
        self.agent_extra('''
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
pathlib.Path(os.environ["RECORD"] + ".pids").write_text(str(os.getpid()) + " " + str(child.pid))
pathlib.Path(os.environ["RECORD"] + ".guard").write_text(str(os.getppid()))
pathlib.Path(os.environ["RECORD"] + ".ready").touch()
while True:
    time.sleep(0.02)
''')
        self.addCleanup(self.kill_recorded_agents)
        process = self.start_launcher()
        record = self.wait_agent_ready(self.record)
        guard = int(Path(str(self.record) + ".guard").read_text())
        os.kill(guard, signal.SIGKILL)
        process.communicate(timeout=10)
        deadline = time.monotonic() + 2
        def alive(pid):
            try:
                return Path(f"/proc/{pid}/stat").read_text().split()[2] != "Z"
            except FileNotFoundError:
                return False
        pids = list(map(int, Path(str(self.record) + ".pids").read_text().split()))
        while any(alive(pid) for pid in pids) and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertFalse(any(alive(pid) for pid in pids), "agent survived killed guard and lease release")
        with Path(record["cwd"]).with_suffix(".lock").open("a+b") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_POOL_R8_shared_fsmonitor_and_split_index_do_not_affect_launcher(self):
        slot = Path(self.verifier()["cwd"])
        monitor = self.base / "fsmonitor"
        marker = self.base / "monitor-ran"
        monitor.write_text(f'#!{sys.executable}\nfrom pathlib import Path\nPath({str(marker)!r}).touch()\n')
        monitor.chmod(0o755)
        self.git("config", "core.fsmonitor", str(monitor))
        self.git("config", "core.splitIndex", "true")
        common = self.repo / ".git"
        before = {path: path.read_bytes() for path in common.rglob("sharedindex.*")}
        config = (common / "config").read_bytes()
        # Read effective settings without allowing the fake agent's own status
        # commands to run a monitor or write split indexes.
        self.agent_extra('''
record["git_config"] = subprocess.check_output(["git", "config", "--list"], text=True)
pathlib.Path(os.environ["RECORD"]).write_text(json.dumps(record))
''')
        self.assertEqual(self.verifier()["cwd"], str(slot))
        self.assertFalse(marker.exists())
        self.assertEqual({path: path.read_bytes() for path in common.rglob("sharedindex.*")}, before)
        self.assertEqual((common / "config").read_bytes(), config)
        # Snapshot creation also uses a copied index.
        result = self.launch("reviewer", "codex", "--author-family", "claude")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(marker.exists())
        self.assertEqual({path: path.read_bytes() for path in common.rglob("sharedindex.*")}, before)

    def test_POOL_R8_global_excludes_do_not_protect_slot_leftovers(self):
        slot = Path(self.verifier()["cwd"])
        excludes = self.base / "global-excludes"
        excludes.write_text("leftover\n")
        self.git("config", "core.excludesFile", str(excludes))
        (slot / "leftover").write_text("must be removed")
        (slot / "ignored").write_text("warm build")
        self.assertEqual(self.verifier()["cwd"], str(slot))
        self.assertFalse((slot / "leftover").exists())
        self.assertEqual((slot / "ignored").read_text(), "warm build")

    def test_POOL_R8_split_index_copies_do_not_write_shared_indexes(self):
        slot = Path(self.verifier()["cwd"])
        self.git("config", "core.splitIndex", "true")
        self.git("update-index", "--split-index", "--assume-unchanged", "tracked")
        common = self.repo / ".git"
        before = {path: path.read_bytes() for path in common.rglob("sharedindex.*")}
        self.assertTrue(before, "fixture must exercise an existing split index")
        index = (common / "index").read_bytes()
        self.assertEqual(self.verifier()["cwd"], str(slot))
        self.assertEqual((common / "index").read_bytes(), index)
        self.assertEqual({path: path.read_bytes() for path in common.rglob("sharedindex.*")}, before)
        result = self.launch("reviewer", "codex", "--author-family", "claude")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((common / "index").read_bytes(), index)
        self.assertEqual({path: path.read_bytes() for path in common.rglob("sharedindex.*")}, before)

    def test_POOL_R2_operation_state_is_cleared_on_every_reset(self):
        slot = Path(self.verifier()["cwd"])
        self.hold_other_pool_slots(slot)
        admin = Path(subprocess.check_output(["git", "-C", str(slot), "rev-parse", "--absolute-git-dir"],
                                            text=True, env=self.env).strip())
        states = ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "BISECT_LOG", "BISECT_START",
                  "BISECT_TERMS", "rebase-merge", "rebase-apply", "sequencer")
        for clean in (False, True):
            with self.subTest(deep_clean=clean):
                for state in states:
                    path = admin / state
                    if state in ("rebase-merge", "rebase-apply", "sequencer"):
                        path.mkdir(exist_ok=True)
                        (path / "state").write_text("unfinished operation")
                    else:
                        path.write_text(self.git("rev-parse", "HEAD") + "\n")
                (slot / "ignored").write_text("warm output")
                self.assertEqual(self.verifier("codex", *(["--clean-checkout"] if clean else []))["cwd"], str(slot))
                for state in states:
                    self.assertFalse((admin / state).exists(), state)
                self.assertEqual((slot / "ignored").exists(), not clean)

    def test_POOL_R6_shared_sparse_config_uses_pool_without_recreation(self):
        self.git("config", "core.sparseCheckout", "true")
        self.git("config", "core.sparseCheckoutCone", "true")
        (self.repo / ".git/info/sparse-checkout").write_text("/missing/\n")
        config = (self.repo / ".git/config").read_bytes()
        first = self.verifier()
        slot = Path(first["cwd"])
        self.assertTrue(slot.is_relative_to(self.pool_root()))
        self.assertEqual(first["changed"], "original")
        (slot / "ignored").write_text("warm output")
        second = self.verifier()
        self.assertEqual(second["cwd"], str(slot))
        self.assertEqual(second["changed"], "original")
        self.assertEqual((slot / "ignored").read_text(), "warm output")
        self.assertIn("warm reset", self.out.read_text())
        self.assertEqual((self.repo / ".git/config").read_bytes(), config)

    def test_POOL_S1_sparse_checkout_is_disabled_on_warm_and_deep_leases(self):
        slot = Path(self.verifier()["cwd"])
        self.hold_other_pool_slots(slot)
        for clean in (False, True):
            with self.subTest(deep_clean=clean):
                subprocess.check_call(["git", "-C", str(slot), "sparse-checkout", "set",
                                       "--no-cone", "/missing/"], env=self.env)
                self.assertFalse((slot / "tracked").exists())
                common_config = (self.repo / ".git/config").read_bytes()
                record = self.verifier("codex", *(["--clean-checkout"] if clean else []))
                self.assertEqual(record["cwd"], str(slot))
                self.assertTrue((slot / "tracked").exists())
                self.assertEqual((slot / "tracked").read_text(), "original")
                self.assertEqual(subprocess.check_output(
                    ["git", "-C", str(slot), "config", "--type=bool", "--default=false", "--get",
                     "core.sparseCheckout"], env=self.env, text=True).strip(), "false")
                self.assertEqual(subprocess.check_output(
                    ["git", "-C", str(slot), "ls-files", "-v", "tracked"],
                    env=self.env, text=True), "H tracked\n")
                self.assertEqual((self.repo / ".git/config").read_bytes(), common_config)

    def test_POOL_S1_cached_removal_of_now_ignored_file_is_removed_on_both_paths(self):
        baseline = self.git("rev-parse", "HEAD")
        (self.repo / "ignored-later").write_text("old tracked content")
        self.git("add", ".")
        self.git("commit", "-qm", "tracked before ignored")
        old = self.git("rev-parse", "HEAD")
        for clean in (False, True):
            with self.subTest(deep_clean=clean):
                self.git("reset", "--hard", old)
                slot = Path(self.verifier()["cwd"])
                self.hold_other_pool_slots(slot)
                subprocess.check_call(["git", "-C", str(slot), "rm", "--cached", "--quiet",
                                       "ignored-later"], env=self.env)
                (self.repo / "ignored-later").unlink()
                (self.repo / ".gitignore").write_text("ignored\nignored-later\n")
                self.git("add", "-A")
                self.git("commit", "-qm", "delete and ignore")
                self.verifier("codex", *(["--clean-checkout"] if clean else []))
                self.assertFalse((slot / "ignored-later").exists())
        self.git("reset", "--hard", baseline)

    def test_POOL_S2_repository_identity_for_linked_symlink_subdirectory_and_same_name(self):
        main = self.repo
        first = self.verifier()
        linked = self.base / "other-lane"
        self.git("worktree", "add", "--detach", str(linked), "HEAD")
        alias = self.base / "alias"
        alias.symlink_to(main, target_is_directory=True)
        sub = main / "sub"
        sub.mkdir()
        for repo in (linked, alias, sub):
            with self.subTest(repo=repo):
                result = self.verifier("codex", "--repo", repo)
                self.assertEqual(result["cwd"], first["cwd"])
        other = self.base / "other/repo"
        other.mkdir(parents=True)
        self.repo = other
        self.git("init", "-q")
        (other / "tracked").write_text("other repository")
        self.git("add", ".")
        self.git("commit", "-qm", "other")
        result = self.verifier()
        self.assertNotEqual(Path(result["cwd"]).parent, Path(first["cwd"]).parent)
        self.assertEqual(result["changed"], "other repository")
        self.repo = main

    def test_POOL_S3_refusal_cases_preserve_source_and_never_lease(self):
        baseline = self.git("rev-parse", "HEAD")
        blob = self.git("rev-parse", "HEAD:tracked")
        for case in ("unstaged", "staged", "addition", "deletion", "staged-deletion", "rename",
                     "mode", "staged-mode", "conflict", "index-only", "unborn"):
            with self.subTest(case=case):
                self.git("reset", "--hard", baseline)
                self.git("clean", "-ffd")
                tracked = self.repo / "tracked"
                if case in ("unstaged", "staged", "index-only"):
                    tracked.write_text("changed")
                    if case != "unstaged":
                        self.git("add", "tracked")
                    if case == "index-only":
                        tracked.write_text("original")
                elif case == "addition":
                    (self.repo / "added").touch()
                    self.git("add", "added")
                elif case in ("deletion", "staged-deletion"):
                    tracked.unlink()
                    if case == "staged-deletion":
                        self.git("add", "-u")
                elif case == "rename":
                    self.git("mv", "tracked", "renamed")
                elif case in ("mode", "staged-mode"):
                    tracked.chmod(0o755)
                    if case == "staged-mode":
                        self.git("add", "tracked")
                elif case == "conflict":
                    subprocess.run(["git", "-C", str(self.repo), "update-index", "--index-info"],
                                   env=self.env, input=f"0 {'0' * 40}\ttracked\n100644 {blob} 1\ttracked\n"
                                   f"100644 {blob} 2\ttracked\n100644 {blob} 3\ttracked\n",
                                   text=True, check=True)
                else:
                    shutil.rmtree(self.repo / ".git")
                    self.git("init", "-q")
                before = {p: p.read_bytes() for p in self.repo.rglob("*") if p.is_file()}
                result = self.launch("verifier", "codex", "--author-family", "claude")
                self.assertNotEqual(result.returncode, 0, result.stderr)
                self.assertIn("commit first", result.stderr)
                self.assertFalse(self.record.exists())
                self.assertFalse((self.home / ".cache/am-pool").exists())
                self.assertFalse((self.home / ".cache/am").exists())
                self.assertEqual({p: p.read_bytes() for p in self.repo.rglob("*") if p.is_file()}, before)

    def test_POOL_S3_subdirectory_refuses_changes_outside_and_same_content_touch_is_clean(self):
        sub = self.repo / "sub"
        sub.mkdir()
        (self.repo / "tracked").write_text("dirty outside subdirectory")
        result = self.launch("verifier", "codex", "--author-family", "claude", "--repo", sub)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("commit first", result.stderr)
        (self.repo / "tracked").write_text("original")
        self.verifier("codex", "--repo", sub)

    def test_POOL_R3_flagged_source_edits_refuse_without_changing_source(self):
        for flag in ("--assume-unchanged", "--skip-worktree"):
            with self.subTest(flag=flag):
                self.git("update-index", flag, "tracked")
                (self.repo / "tracked").write_text("hidden edit")
                before = {p: (p.read_bytes(), p.stat().st_mtime_ns)
                          for p in self.repo.rglob("*") if p.is_file()}
                result = self.launch("verifier", "codex", "--author-family", "claude")
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("commit first", result.stderr)
                self.assertFalse(self.record.exists())
                self.assertFalse((self.home / ".cache/am-pool").exists())
                self.assertFalse((self.home / ".cache/am").exists())
                self.assertEqual({p: (p.read_bytes(), p.stat().st_mtime_ns)
                                  for p in self.repo.rglob("*") if p.is_file()}, before)
                self.git("update-index", "--no-assume-unchanged", "tracked")
                self.git("update-index", "--no-skip-worktree", "tracked")
                (self.repo / "tracked").write_text("original")

    def test_POOL_R3_absent_sparse_source_file_is_allowed_without_changing_index(self):
        self.git("update-index", "--skip-worktree", "tracked")
        (self.repo / "tracked").unlink()
        index = self.repo / ".git/index"
        before = index.read_bytes(), index.stat().st_mtime_ns
        record = self.verifier()
        self.assertEqual(record["changed"], "original")
        self.assertEqual((index.read_bytes(), index.stat().st_mtime_ns), before)
        self.assertFalse((self.repo / "tracked").exists())

    def test_POOL_S3_stale_index_timestamps_never_write_source_index(self):
        index = self.repo / ".git/index"
        tracked = self.repo / "tracked"
        for dirty in (False, True):
            with self.subTest(dirty=dirty):
                # Touch an unchanged tracked file after committing so diff must
                # refresh its stat entry. The index file itself also has stale times.
                tracked.write_text("original")
                self.git("add", "tracked")
                os.utime(tracked, ns=(1_000_000_000, 1_000_000_000))
                os.utime(index, ns=(2_000_000_000, 2_000_000_000))
                if dirty:
                    (self.repo / "dirty").write_text("committed")
                    self.git("add", "dirty")
                    self.git("commit", "-qm", "second tracked file")
                    (self.repo / "dirty").write_text("uncommitted")
                    os.utime(tracked, ns=(3_000_000_000, 3_000_000_000))
                    os.utime(index, ns=(2_000_000_000, 2_000_000_000))
                before, mtime = index.read_bytes(), index.stat().st_mtime_ns
                result = self.launch("verifier", "codex", "--author-family", "claude")
                self.assertEqual(index.read_bytes(), before)
                self.assertEqual(index.stat().st_mtime_ns, mtime)
                if dirty:
                    self.assertNotEqual(result.returncode, 0, result.stderr)
                    self.assertIn("commit first", result.stderr)
                else:
                    self.assertEqual(result.returncode, 0, result.stderr)

    def test_POOL_S3_candidate_is_pinned_when_builder_commits_again(self):
        self.pool_probe()
        self.env["SLEEP"] = "1"
        candidate = self.git("rev-parse", "HEAD")
        process = self.start_launcher()
        record = self.wait_record()
        (self.repo / "tracked").write_text("later commit")
        self.git("add", ".")
        self.git("commit", "-qm", "builder continues")
        self.assertEqual(subprocess.check_output(["git", "-C", record["cwd"], "rev-parse", "HEAD"],
                                                text=True).strip(), candidate)
        process.terminate()
        _, stderr = process.communicate(timeout=10)
        self.assertEqual(process.returncode, 143, stderr)
        self.assertIn(f"Snapshot commit: {candidate}\n", self.out.read_text())

    def test_POOL_S4_untracked_files_are_named_bounded_untouched_and_forgotten_file_fails(self):
        names = [f"file-{i:02}" for i in range(23)] + ["new"]
        for name in names:
            (self.repo / name).write_text("forgotten")
        with (self.repo / "file-00").open("wb") as file:
            file.truncate(6 * 1024 * 1024)
        before = {name: (self.repo / name).read_bytes() for name in names}
        self.agent_extra('''
assert not (cwd / "new").exists()
check = subprocess.run([sys.executable, "-c", "open('new').read()"], capture_output=True)
assert check.returncode != 0 and b"FileNotFoundError" in check.stderr
(evidence / "forgotten-file.txt").write_bytes(check.stderr)
sys.exit(9)
''')
        result = self.launch("verifier", "codex", "--author-family", "claude")
        self.assertEqual(result.returncode, 9, result.stderr)
        line = next(line for line in self.out.read_text().splitlines() if line.startswith("Untracked files left out:"))
        for name in names[:20]:
            self.assertIn(repr(name), line)
        self.assertNotIn(repr(names[20]), line)
        self.assertIn("4 more", line)
        self.assertIn(line + "\n", json.loads(self.record.read_text())["stdin"])
        self.assertNotIn("Skipped untracked (>5 MB)", self.out.read_text())
        self.assertEqual({name: (self.repo / name).read_bytes() for name in names}, before)
        self.assertIn("FileNotFoundError", (self.out.parent / "answer-evidence/forgotten-file.txt").read_text())

    def test_POOL_S5_second_checkout_is_writable_reset_and_granted_for_both_families(self):
        baseline = self.git("rev-parse", "HEAD")
        (self.repo / "tracked").write_text("candidate")
        self.git("add", ".")
        self.git("commit", "-qm", "candidate")
        self.agent_extra('''
match = re.search(r"^Before checkout: (.+?) \\(", record["stdin"], re.M)
before = pathlib.Path(match.group(1))
assert before != cwd
assert (before / "tracked").read_text() == "original"
assert subprocess.check_output(["git", "-C", str(before), "status", "--porcelain"]) == b""
assert str(before) in [args[i + 1] for i, arg in enumerate(args) if arg == "--add-dir"]
(before / "tracked").write_text("writable")
(before / "ignored").write_text("warm before")
record["before"] = str(before)
pathlib.Path(os.environ["RECORD"]).write_text(json.dumps(record))
''')
        for family in ("codex", "claude"):
            with self.subTest(family=family):
                record = self.verifier(family, "--before", baseline)
                self.assertTrue(Path(record["before"]).is_relative_to(self.pool_root()))
                self.assertIn("Before checkout:", self.out.read_text())
                self.assertEqual((self.repo / "tracked").read_text(), "candidate")
                self.assertTrue(Path(record["before"]).is_dir())

    def test_BEFORE_S1_missing_choice_is_refused_for_both_families(self):
        for family in ("codex", "claude"):
            with self.subTest(family=family):
                result = self.run_script("run.py", "--role", "verifier", "--family", family,
                                         "--author-family", "claude" if family == "codex" else "codex",
                                         "--brief", self.brief, "--out", self.out,
                                         "--repo", self.repo, "--method", self.method,
                                         "--private", self.private)
                self.assert_launch_refused(result, "verifier requires --before <commit> or --before none")
                self.assertFalse(self.pool_root().exists())

    def test_BEFORE_S2_none_leases_only_candidate_for_both_families(self):
        for family in ("codex", "claude"):
            with self.subTest(family=family):
                record = self.verifier(family, "--before", "none")
                self.assertIn("Before checkout: none (--before none)", self.out.read_text())
                self.assertIn("Before checkout: none (--before none)", record["stdin"])
                self.assertIn("there is no before checkout", record["stdin"])
                self.assertEqual(record["stdin"].count(
                    'git clone --quiet --shared --no-checkout . "$TMPDIR/before"'), 1)
                metadata = list(self.pool_root().glob("*.json"))
                self.assertEqual(len(metadata), 1, "none must not lease a baseline slot")
                self.assertEqual(json.loads(metadata[0].read_text())["uses"],
                                 1 if family == "codex" else 2)

    @unittest.skipUnless(shutil.which("cargo"), "cargo is not installed; real warm-build check unavailable")
    def test_BEFORE_S3_two_sessions_keep_both_cargo_trees_fresh(self):
        self.env.update(PATH=self.env["PATH"] + os.pathsep + os.environ["PATH"],
                        CARGO_HOME=os.environ.get("CARGO_HOME", str(Path.home() / ".cargo")),
                        RUSTUP_HOME=os.environ.get("RUSTUP_HOME", str(Path.home() / ".rustup")))
        self.env.pop("CARGO_TARGET_DIR", None)
        self.env.pop("CARGO_BUILD_TARGET_DIR", None)
        (self.repo / ".gitignore").write_text("ignored\ntarget/\n")
        (self.repo / "Cargo.toml").write_text(
            '[package]\nname = "warm_app"\nversion = "0.1.0"\nedition = "2021"\n'
            '[dependencies]\nwarm_dep = { path = "dep" }\n')
        (self.repo / "src").mkdir()
        (self.repo / "src/lib.rs").write_text("pub fn value() -> u32 { warm_dep::value() }\n")
        (self.repo / "dep/src").mkdir(parents=True)
        (self.repo / "dep/Cargo.toml").write_text(
            '[package]\nname = "warm_dep"\nversion = "0.1.0"\nedition = "2021"\n')
        (self.repo / "dep/src/lib.rs").write_text("pub fn value() -> u32 { 7 }\n")
        lock = subprocess.run(["cargo", "generate-lockfile", "--offline"], cwd=self.repo,
                              env=self.env, capture_output=True, text=True)
        self.assertEqual(lock.returncode, 0, lock.stderr)
        self.git("add", ".")
        self.git("commit", "-qm", "local Cargo crate and dependency")
        baseline = self.git("rev-parse", "HEAD")
        (self.repo / "src/lib.rs").write_text("pub fn value() -> u32 { warm_dep::value() + 1 }\n")
        self.git("add", ".")
        self.git("commit", "-qm", "candidate source")
        self.agent_extra('''
import hashlib
before = pathlib.Path(re.search(r"^Before checkout: (.+?) \\(", record["stdin"], re.M).group(1))
record["builds"] = {}
for name, tree in (("candidate", cwd), ("before", before)):
    build = subprocess.run(["cargo", "build", "--offline", "--locked", "-v"], cwd=tree,
                           capture_output=True, text=True)
    assert build.returncode == 0, build.stderr
    fingerprints = {str(p.relative_to(tree)): [p.stat().st_mtime_ns, hashlib.sha256(p.read_bytes()).hexdigest()]
                    for p in (tree / "target/debug/.fingerprint").rglob("*") if p.is_file()}
    assert fingerprints, "build must leave real Cargo fingerprints in its own tree"
    record["builds"][name] = {"tree": str(tree), "output": build.stderr, "fingerprints": fingerprints}
pathlib.Path(os.environ["RECORD"]).write_text(json.dumps(record))
''')
        first = self.verifier("codex", "--before", baseline)["builds"]
        second = self.verifier("codex", "--before", baseline)["builds"]
        self.assertEqual(self.out.read_text().count("warm reset"), 2)
        for name in ("candidate", "before"):
            with self.subTest(tree=name):
                self.assertIn("Compiling warm_dep", first[name]["output"])
                self.assertIn("Compiling warm_app", first[name]["output"])
                self.assertIn("Fresh warm_dep", second[name]["output"])
                self.assertIn("Fresh warm_app", second[name]["output"])
                self.assertNotIn("Compiling", second[name]["output"])
                self.assertEqual(first[name]["tree"], second[name]["tree"])
                self.assertEqual(first[name]["fingerprints"], second[name]["fingerprints"])
        self.assertNotEqual(second["candidate"]["tree"], second["before"]["tree"])

    def test_BEFORE_S4_joint_choice_preserves_closest_baseline_on_candidate_tie(self):
        baseline = self.git("rev-parse", "HEAD")
        (self.repo / "tracked").write_text("other warm version")
        self.git("add", ".")
        self.git("commit", "-qm", "other warm version")
        other = self.git("rev-parse", "HEAD")
        slots = self.seed_pool([baseline, other])
        (slots[0] / "ignored").write_text("baseline output")
        (self.repo / "tracked").write_text("candidate")
        self.git("add", ".")
        self.git("commit", "-qm", "candidate")
        self.agent_extra('''
record["before"] = re.search(r"^Before checkout: (.+?) \\(", record["stdin"], re.M).group(1)
pathlib.Path(os.environ["RECORD"]).write_text(json.dumps(record))
''')
        record = self.verifier("codex", "--before", baseline)
        self.assertEqual(record["cwd"], str(slots[1]))
        self.assertEqual(record["before"], str(slots[0]))
        self.assertEqual((slots[0] / "ignored").read_text(), "baseline output")
        self.assertEqual(self.out.read_text().count("warm reset"), 2)

    def test_BEFORE_S4_only_warm_baseline_survives_candidate_equally_cold_elsewhere(self):
        baseline = self.git("rev-parse", "HEAD")
        slot = Path(self.verifier()["cwd"])
        (slot / "ignored").write_text("baseline output")
        (self.repo / "tracked").write_text("candidate")
        self.git("add", ".")
        self.git("commit", "-qm", "candidate")
        record = self.verifier("codex", "--before", baseline)
        self.assertNotEqual(record["cwd"], str(slot))
        self.assertIn(f"Before checkout: {slot} ({baseline}); pool {slot}; warm reset", self.out.read_text())
        self.assertEqual((slot / "ignored").read_text(), "baseline output")

    def test_BEFORE_S4_candidate_and_baseline_keep_their_own_warm_slots(self):
        baseline = self.git("rev-parse", "HEAD")
        (self.repo / "tracked").write_text("candidate")
        self.git("commit", "-qam", "candidate")
        candidate = self.git("rev-parse", "HEAD")
        slots = self.seed_pool([baseline, candidate])
        for slot in slots:
            (slot / "ignored").write_text(slot.name + " output")
        record = self.verifier("codex", "--before", baseline)
        self.assertEqual(record["cwd"], str(slots[1]))
        self.assertIn(f"Before checkout: {slots[0]} ({baseline}); pool {slots[0]}; warm reset",
                      self.out.read_text())
        self.assertEqual(self.out.read_text().count("warm reset"), 2)
        for slot in slots:
            self.assertEqual((slot / "ignored").read_text(), slot.name + " output")

    def test_BEFORE_S4_only_warm_baseline_survives_candidate_build_input_change(self):
        baseline = self.git("rev-parse", "HEAD")
        slot = Path(self.verifier()["cwd"])
        (slot / "ignored").write_text("baseline output")
        (self.repo / "Cargo.toml").write_text("changed manifest")
        self.git("add", ".")
        self.git("commit", "-qm", "candidate build inputs")
        record = self.verifier("codex", "--before", baseline)
        self.assertNotEqual(record["cwd"], str(slot))
        self.assertIn(f"Before checkout: {slot} ({baseline}); pool {slot}; warm reset", self.out.read_text())
        self.assertEqual((slot / "ignored").read_text(), "baseline output")

    def test_BEFORE_S4_total_distance_beats_candidates_individual_preference(self):
        baseline = self.git("rev-parse", "HEAD")
        (self.repo / "tracked").write_text("candidate")
        (self.repo / "other-1").write_text("other")
        (self.repo / "other-2").write_text("other")
        self.git("add", ".")
        self.git("commit", "-qm", "other warm version")
        other = self.git("rev-parse", "HEAD")
        slots = self.seed_pool([baseline, other])
        self.git("reset", "--hard", baseline)
        (self.repo / "tracked").write_text("candidate")
        self.git("commit", "-qam", "candidate")
        self.agent_extra('''
record["before"] = re.search(r"^Before checkout: (.+?) \\(", record["stdin"], re.M).group(1)
pathlib.Path(os.environ["RECORD"]).write_text(json.dumps(record))
''')
        record = self.verifier("codex", "--before", baseline)
        # Candidate-first costs 1 + 3; assigning it the other slot costs 2 + 0.
        self.assertEqual(record["cwd"], str(slots[1]))
        self.assertEqual(record["before"], str(slots[0]))
        self.assertEqual(self.out.read_text().count("warm reset"), 2)

    def test_BEFORE_S4_joint_choice_uses_closest_free_baseline_when_best_is_busy(self):
        (self.repo / "other").write_text("original")
        self.git("add", ".")
        self.git("commit", "-qm", "baseline")
        baseline = self.git("rev-parse", "HEAD")
        (self.repo / "tracked").write_text("candidate")
        self.git("commit", "-qam", "first warm version")
        first = self.git("rev-parse", "HEAD")
        self.git("reset", "--hard", baseline)
        (self.repo / "other").write_text("candidate")
        self.git("commit", "-qam", "second warm version")
        second = self.git("rev-parse", "HEAD")
        slots = self.seed_pool([baseline, first, second])
        (self.repo / "tracked").write_text("candidate")
        self.git("commit", "-qam", "candidate")
        self.agent_extra('''
record["before"] = re.search(r"^Before checkout: (.+?) \\(", record["stdin"], re.M).group(1)
pathlib.Path(os.environ["RECORD"]).write_text(json.dumps(record))
''')
        with slots[0].with_suffix(".lock").open("a+b") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            record = self.verifier("codex", "--before", baseline)
        # Both free assignments cost 1 + 1, so keep candidate-first slot order.
        self.assertEqual(record["cwd"], str(slots[1]))
        self.assertEqual(record["before"], str(slots[2]))

    def test_BEFORE_S4_failed_candidate_releases_before_and_retries_other_pairs(self):
        baseline = self.git("rev-parse", "HEAD")
        (self.repo / "tracked").write_text("candidate")
        self.git("commit", "-qam", "candidate")
        candidate = self.git("rev-parse", "HEAD")
        slots = self.seed_pool([baseline, candidate])
        (slots[0] / "ignored").write_text("baseline output")
        self.env["UNUSABLE_SLOT"] = str(slots[1])
        self.inject_launcher('''
import os, subprocess
original = subprocess.check_output
def fail_slot(command, *args, **kwargs):
    if os.environ["UNUSABLE_SLOT"] in command:
        raise subprocess.CalledProcessError(71, command)
    return original(command, *args, **kwargs)
subprocess.check_output = fail_slot
''')
        record = self.verifier("codex", "--before", baseline)
        self.assertEqual(record["cwd"], str(slots[0].with_name("slot-3")))
        self.assertIn(f"Before checkout: {slots[0]} ({baseline}); pool {slots[0]}; warm reset",
                      self.out.read_text())
        self.assertEqual((slots[0] / "ignored").read_text(), "baseline output")
        self.assertIn(f"pool slot {slots[1]} unusable:", self.out.read_text())

    def test_BEFORE_S4_failed_locks_are_not_retried_in_pairs_or_fallback(self):
        pool = self.pool_root()
        pool.mkdir(parents=True, exist_ok=True)
        for error in ("OSError", "ValueError"):
            self.inject_launcher(f'''
import fcntl, os
original = fcntl.flock
def fail_lock(stream, operation):
    descriptor = stream if isinstance(stream, int) else stream.fileno()
    if os.readlink(f"/proc/self/fd/{{descriptor}}") == {str(pool / 'slot-1.lock')!r}:
        raise {error}("injected unusable lock")
    return original(stream, operation)
fcntl.flock = fail_lock
''')
            for fallback in (False, True):
                with self.subTest(error=error, fallback=fallback), ExitStack() as held:
                    if fallback:
                        for number in range(2, 6):
                            lock = held.enter_context((pool / f"slot-{number}.lock").open("a+b"))
                            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    self.round_decision()
                    result = self.launch("verifier", "codex", "--author-family", "claude",
                                         "--before", "HEAD", "--clean-checkout")
                    self.assertEqual(result.returncode, 0, result.stderr)
                    reason = f"pool slot {pool / 'slot-1'} unusable: injected unusable lock"
                    # Each checkout may try the bad lock once. Pair search and
                    # fallback within that lease must never try it again.
                    self.assertEqual(result.stderr.count(reason), 2 if fallback else 1)
                    self.assertEqual(self.out.read_text().count(reason), 2 if fallback else 1)
                    record = json.loads(self.record.read_text())
                    self.assertEqual(Path(record["cwd"]).is_relative_to(pool), not fallback)

    def test_BEFORE_S5_verifier_build_outputs_are_separate_in_prompts_and_docs(self):
        for family in ("codex", "claude"):
            for before in ("HEAD", "none"):
                with self.subTest(family=family, before=before):
                    prompt = self.verifier(family, "--before", before)["stdin"]
                    self.assertIn("Build each source tree only into its own build folder", prompt)
                    self.assertIn("Never point one tree's build output", prompt)
                    for example in ("CARGO_TARGET_DIR", "Gradle build directories", "node_modules"):
                        self.assertIn(example, prompt)
                    self.assertEqual(prompt.count("Build each source tree only into its own build folder"), 2)
        for document in ("product/guides/verification.md", "product/roles/verifier.md"):
            with self.subTest(document=document):
                text = (ROOT / document).read_text()
                self.assertIn("Build each source tree only into its own build folder", text)
                self.assertIn("Never point one tree's build output", text)
                for example in ("CARGO_TARGET_DIR", "Gradle build directories", "node_modules"):
                    self.assertIn(example, text)
        for family in ("codex", "claude"):
            result = self.launch("builder", family)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn("Never point one tree's build output", json.loads(self.record.read_text())["stdin"])

    def test_BEFORE_S6_same_commit_has_distinct_writable_trees_and_resolved_header(self):
        self.agent_extra('''
before = pathlib.Path(re.search(r"^Before checkout: (.+?) \\(", record["stdin"], re.M).group(1))
assert before != cwd
(cwd / "tracked").write_text("candidate edits")
assert (before / "tracked").read_text() == "original"
(before / "tracked").write_text("baseline edits")
assert (cwd / "tracked").read_text() == "candidate edits"
record["before"] = str(before)
pathlib.Path(os.environ["RECORD"]).write_text(json.dumps(record))
''')
        for family in ("codex", "claude"):
            with self.subTest(family=family):
                record = self.verifier(family, "--before", "HEAD")
                line = f"Before checkout: {record['before']} ({self.git('rev-parse', 'HEAD')});"
                self.assertIn(line, self.out.read_text())
                self.assertIn(line, record["stdin"])
                self.assertEqual((self.repo / "tracked").read_text(), "original")

    def test_BEFORE_P1_invalid_before_and_nonverifier_choices_are_refused(self):
        for role in ("builder", "investigator", "challenger", "reviewer"):
            for before in ("HEAD", "none"):
                with self.subTest(role=role, before=before):
                    self.assert_launch_refused(self.launch(role, "codex", "--before", before),
                                               "allowed only for verifiers")
        for before in ("missing-commit", "NONE", ""):
            with self.subTest(before=before):
                self.assert_launch_refused(self.launch("verifier", "codex", "--author-family", "claude",
                                                       "--before", before), "returned non-zero")

    def test_BEFORE_P2_busy_pool_reports_two_fresh_checkouts(self):
        self.busy_pool()
        self.agent_extra('''
before = pathlib.Path(re.search(r"^Before checkout: (.+?) \\(", record["stdin"], re.M).group(1))
assert before != cwd
(before / "tracked").write_text("writable fresh baseline")
record["before"] = str(before)
pathlib.Path(os.environ["RECORD"]).write_text(json.dumps(record))
''')
        record = self.verifier("codex", "--before", "HEAD")
        self.assertEqual(self.out.read_text().count("pool busy or slots unusable; fresh session worktree"), 2)
        self.assertFalse(Path(record["before"]).exists())
        self.assert_session_cleaned(record)

    def test_BEFORE_P2_cleaning_applies_to_each_side(self):
        self.agent_extra('''
record["before"] = re.search(r"^Before checkout: (.+?) \\(", record["stdin"], re.M).group(1)
pathlib.Path(os.environ["RECORD"]).write_text(json.dumps(record))
''')
        baseline = self.git("rev-parse", "HEAD")
        record = self.verifier("codex", "--before", baseline)
        slots = [Path(record["cwd"]), Path(record["before"])]
        self.hold_other_pool_slots(*slots)
        for reason in ("--clean-checkout", "20 uses", "lockfile or toolchain changed",
                       "untracked source file was previously tracked"):
            with self.subTest(reason=reason):
                extra = []
                for slot in slots:
                    (slot / "ignored").write_text("stale output")
                    if reason == "20 uses":
                        metadata = slot.with_suffix(".json")
                        state = json.loads(metadata.read_text())
                        state["uses"] = 20
                        metadata.write_text(json.dumps(state))
                if reason == "--clean-checkout":
                    extra = ["--clean-checkout"]
                elif reason == "lockfile or toolchain changed":
                    (self.repo / "Cargo.lock").write_text("changed lockfile")
                    self.git("add", ".")
                    self.git("commit", "-qm", "build input change")
                    baseline = self.git("rev-parse", "HEAD")
                elif reason == "untracked source file was previously tracked":
                    (self.repo / "forgotten").write_text("needed source")
                    self.git("add", ".")
                    self.git("commit", "-qm", "tracked dependency")
                    self.verifier("codex", "--before", "HEAD")
                    self.git("rm", "--cached", "forgotten")
                    self.git("commit", "-qm", "forgotten dependency")
                    baseline = self.git("rev-parse", "HEAD")
                record = self.verifier("codex", "--before", baseline, *extra)
                self.assertEqual({record["cwd"], record["before"]}, set(map(str, slots)))
                self.assertEqual(self.out.read_text().count("deep clean: " + reason), 2)
                for slot in slots:
                    self.assertFalse((slot / "ignored").exists())

    def test_POOL_R2_five_slots_then_fallback_and_before_never_waits(self):
        self.env["SLEEP"] = "1"
        records = [self.base / f"pool-{i}.json" for i in range(6)]
        processes = []
        data = []
        for i, record in enumerate(records):
            process = self.start_launcher(record=record, out=self.base / f"pool-{i}.md")
            processes.append(process)
            data.append(self.wait_record(record))
        self.assertEqual(len({record["cwd"] for record in data}), 6)
        for record in data[:5]:
            self.assertTrue(Path(record["cwd"]).is_relative_to(self.pool_root()))
        self.assertFalse(Path(data[5]["cwd"]).is_relative_to(self.pool_root()))
        for process in processes:
            process.terminate()
            process.communicate(timeout=10)
            self.assertEqual(process.returncode, 143)
        self.assertIn("pool busy", (self.base / "pool-5.md").read_text())
        for record in data:
            self.assert_session_cleaned(record)
        # Candidate and before leases compete for the same five slots.
        first = self.start_launcher("--before", "HEAD", record=self.base / "two-slots.json",
                                    out=self.base / "two-slots.md")
        self.wait_record(self.base / "two-slots.json")
        second = self.start_launcher("--before", "HEAD", record=self.base / "two-more.json",
                                     out=self.base / "two-more.md")
        self.wait_record(self.base / "two-more.json")
        third = self.start_launcher("--before", "HEAD", record=self.base / "one-slot.json",
                                    out=self.base / "one-slot.md")
        self.wait_record(self.base / "one-slot.json")
        for process in (first, second, third):
            process.terminate()
            process.communicate(timeout=10)
        self.assertIn("Before checkout:", (self.base / "one-slot.md").read_text())
        self.assertIn("pool busy", (self.base / "one-slot.md").read_text())

    def test_POOL_S7_persistent_use_limit_failed_runs_and_explicit_clean(self):
        record = self.verifier()
        slot = Path(record["cwd"])
        self.hold_other_pool_slots(slot, slot.with_name("slot-2"))
        with slot.with_suffix(".lock").open("a+b") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.verifier()
        other = slot.with_name("slot-2").with_suffix(".json")
        state = json.loads(other.read_text())
        state["uses"] = 20
        other.write_text(json.dumps(state))
        metadata = slot.with_suffix(".json")
        state = json.loads(metadata.read_text())
        state["uses"] = 19
        metadata.write_text(json.dumps(state))
        (slot / "ignored").write_text("keep until limit")
        self.env["SESSION_EXIT"] = "7"
        self.round_decision()
        result = self.launch("verifier", "codex", "--author-family", "claude")
        self.assertEqual(result.returncode, 7)
        self.assertEqual(json.loads(metadata.read_text())["uses"], 20)
        self.assertTrue((slot / "ignored").exists())
        self.env.pop("SESSION_EXIT")
        self.verifier()
        self.assertFalse((slot / "ignored").exists())
        self.assertIn("deep clean: 20 uses", self.out.read_text())
        self.assertEqual(json.loads(metadata.read_text())["uses"], 1)
        (slot / "ignored").write_text("remove explicitly")
        self.verifier("codex", "--clean-checkout", "--before", "HEAD")
        self.assertFalse((slot / "ignored").exists())
        self.assertEqual(self.out.read_text().count("deep clean: --clean-checkout"), 2)

    def test_POOL_S7_lock_and_toolchain_changes_in_both_directions(self):
        baseline = self.git("rev-parse", "HEAD")
        record = self.verifier()
        slot = Path(record["cwd"])
        self.hold_other_pool_slots(slot)
        for name in ("package-lock.json", "yarn.lock", "pnpm-lock.yaml", "Cargo.lock", "go.sum",
                     "uv.lock", "poetry.lock", "requirements-dev.txt", "rust-toolchain.toml",
                     ".nvmrc", ".node-version", ".tool-versions", ".python-version",
                     "gradle/wrapper/gradle-wrapper.properties"):
            with self.subTest(name=name):
                target = self.repo / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("changed build input")
                self.git("add", ".")
                self.git("commit", "-qm", "build input")
                for reverse in (False, True):
                    (slot / "ignored").write_text("stale output")
                    if reverse:
                        self.git("reset", "--hard", baseline)
                    self.verifier()
                    self.assertFalse((slot / "ignored").exists())
                    self.assertIn("deep clean: lockfile or toolchain changed", self.out.read_text())

    def test_POOL_S7_additional_lockfiles_clean_in_both_directions(self):
        baseline = self.git("rev-parse", "HEAD")
        slot = Path(self.verifier()["cwd"])
        self.hold_other_pool_slots(slot)
        for name in ("Gemfile.lock", "composer.lock", "flake.lock", "bun.lock", "bun.lockb"):
            with self.subTest(name=name):
                target = self.repo / "nested" / name
                target.parent.mkdir(exist_ok=True)
                target.write_text("changed build input")
                self.git("add", ".")
                self.git("commit", "-qm", "build input")
                for reverse in (False, True):
                    (slot / "ignored").write_text("stale output")
                    if reverse:
                        self.git("reset", "--hard", baseline)
                    self.verifier()
                    self.assertFalse((slot / "ignored").exists())
                    self.assertIn("deep clean: lockfile or toolchain changed", self.out.read_text())

    def test_POOL_R2_gradle_inputs_stay_warm_in_both_directions(self):
        baseline = self.git("rev-parse", "HEAD")
        slot = Path(self.verifier()["cwd"])
        for name in ("build.gradle", "build.gradle.kts", "settings.gradle", "settings.gradle.kts",
                     "gradle.properties", "libs.versions.toml", "gradle.lockfile", "custom.lockfile",
                     ".java-version"):
            with self.subTest(name=name):
                target = self.repo / "nested" / name
                target.parent.mkdir(exist_ok=True)
                target.write_text("changed Gradle input")
                self.git("add", ".")
                self.git("commit", "-qm", "Gradle input")
                for reverse in (False, True):
                    (slot / "ignored").write_text("warm output")
                    if reverse:
                        self.git("reset", "--hard", baseline)
                    self.assertEqual(self.verifier()["cwd"], str(slot))
                    self.assertTrue((slot / "ignored").exists(), "Gradle input changes must keep output warm")
                    self.assertEqual((slot / "ignored").read_text(), "warm output")
                    self.assertIn("warm reset", self.out.read_text())

    def hold_other_pool_slots(self, *slots):
        # Force a repair/cleaning scenario to exercise its existing slots even
        # when never-created slots would normally be preferred.
        held = getattr(self, "held_pool_locks", set())
        self.held_pool_locks = held
        available = {slot.with_suffix(".lock") for slot in slots}
        for number in range(1, 6):
            path = slots[0].parent / f"slot-{number}.lock"
            if path not in available and path not in held:
                lock = path.open("a+b")
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.addCleanup(lock.close)
                held.add(path)

    def test_POOL_R3_new_slots_precede_wiping_different_lockfiles(self):
        slots = []
        for number in range(1, 6):
            (self.repo / "package-lock.json").write_text(str(number))
            self.git("add", ".")
            self.git("commit", "-qm", f"lockfile {number}")
            slot = Path(self.verifier()["cwd"])
            self.assertEqual(slot.name, f"slot-{number}")
            for old in slots:
                self.assertTrue((old / "ignored").exists(), "warm slots must survive new candidates")
            (slot / "ignored").write_text("warm output")
            slots.append(slot)
        # Once every slot exists, a changed lockfile must clean one of them.
        (self.repo / "package-lock.json").write_text("sixth candidate")
        self.git("add", ".")
        self.git("commit", "-qm", "sixth lockfile")
        self.assertEqual(self.verifier()["cwd"], str(slots[0]))
        self.assertFalse((slots[0] / "ignored").exists())
        self.assertIn("lockfile or toolchain changed", self.out.read_text())

    def test_POOL_R3_explicit_clean_on_new_slot_has_both_reasons(self):
        self.verifier("codex", "--clean-checkout")
        header = self.out.read_text()
        self.assertIn("deep clean: --clean-checkout; new slot", header)
        self.assertNotIn("recreated (missing checkout or registration)", header)
        self.assertNotIn("missing or corrupt metadata", header)

    def test_POOL_R3_corrupt_metadata_keeps_change_reason(self):
        slot = Path(self.verifier()["cwd"])
        self.hold_other_pool_slots(slot)
        slot.with_suffix(".json").write_text("corrupt")
        (self.repo / "package-lock.json").write_text("changed lockfile")
        self.git("add", ".")
        self.git("commit", "-qm", "change lockfile")
        self.verifier()
        self.assertIn("lockfile or toolchain changed; missing or corrupt metadata", self.out.read_text())

    def test_POOL_R3_all_clean_reasons_survive_combination(self):
        slot = Path(self.verifier()["cwd"])
        self.hold_other_pool_slots(slot)
        commit = self.git("rev-parse", "HEAD")
        slot.with_suffix(".json").write_text(json.dumps({
            "uses": 20, "commit": commit, "commits": ["1" * 40, commit]}))
        slot.with_suffix(".json.tmp").mkdir()
        for name in ("package-lock.json", "Cargo.toml"):
            (self.repo / name).write_text("changed input")
        self.git("add", ".")
        self.git("commit", "-qm", "change inputs")
        (slot / "ignored").write_text("must disappear")
        self.verifier("codex", "--clean-checkout")
        self.assertIn("deep clean: --clean-checkout; 20 uses; lockfile or toolchain changed; "
                      "Cargo manifest changed; missing previous commit; non-regular metadata path",
                      self.out.read_text())
        self.assertFalse((slot / "ignored").exists())

    def test_POOL_R3_exact_match_skips_other_ranking_and_defers_history_to_lock(self):
        baseline = self.git("rev-parse", "HEAD")
        (self.repo / "tracked").write_text("other candidate")
        self.git("add", ".")
        self.git("commit", "-qm", "other candidate")
        other = self.git("rev-parse", "HEAD")
        slots = self.seed_pool([baseline, other, other, other, other])
        self.git("reset", "--hard", baseline)
        self.inject_launcher('''
import fcntl, os, subprocess
original_flock = fcntl.flock
original_output = subprocess.check_output
acquired = False
def locked(fd, operation):
    global acquired
    descriptor = fd if isinstance(fd, int) else fd.fileno()
    if not os.readlink(f"/proc/self/fd/{descriptor}").split("/")[-1].startswith("slot-"):
        return original_flock(fd, operation)
    result = original_flock(fd, operation)
    acquired = True
    return result
def output(command, *args, **kwargs):
    if not acquired:
        assert "cat-file" not in command, "history belongs under the lease"
        assert "--name-only" not in command or "--cached" in command, "exact match needs no ranking diff"
        assert "--name-status" not in command, "exact match needs no ranking diff"
    return original_output(command, *args, **kwargs)
fcntl.flock = locked
subprocess.check_output = output
''')
        self.assertEqual(self.verifier()["cwd"], str(slots[0]))

    def test_POOL_R3_changed_slots_use_one_diff_each_and_defer_history(self):
        baseline = self.git("rev-parse", "HEAD")
        self.seed_pool([baseline] * 5)
        (self.repo / "tracked").write_text("new candidate")
        self.git("add", ".")
        self.git("commit", "-qm", "new candidate")
        self.inject_launcher('''
import fcntl, os, subprocess
original_flock = fcntl.flock
original_output = subprocess.check_output
acquired = False
ranking_diffs = 0
def locked(fd, operation):
    global acquired
    descriptor = fd if isinstance(fd, int) else fd.fileno()
    if not os.readlink(f"/proc/self/fd/{descriptor}").split("/")[-1].startswith("slot-"):
        return original_flock(fd, operation)
    result = original_flock(fd, operation)
    if not acquired:
        assert ranking_diffs == 5, "one change diff per existing slot"
    acquired = True
    return result
def output(command, *args, **kwargs):
    global ranking_diffs
    if not acquired:
        assert "cat-file" not in command, "history belongs under the lease"
        if "diff" in command and "--no-renames" in command and "--raw" not in command:
            assert "--name-status" in command, "one status diff supplies changes and deletions"
            ranking_diffs += 1
    return original_output(command, *args, **kwargs)
fcntl.flock = locked
subprocess.check_output = output
''')
        self.verifier()

    def test_POOL_R2_exact_higher_slot_wins_for_candidate_and_before_both_families(self):
        baseline = self.git("rev-parse", "HEAD")
        commits = [baseline]
        for content in ("candidate", "before"):
            (self.repo / "tracked").write_text(content)
            self.git("add", ".")
            self.git("commit", "-qm", content)
            commits.append(self.git("rev-parse", "HEAD"))
        slots = self.seed_pool([commits[0], commits[2], commits[1]])
        for slot in slots:
            (slot / "ignored").write_text("existing slot output")
        self.git("reset", "--hard", commits[1])
        self.agent_extra('''
import fcntl
before = pathlib.Path(re.search(r"^Before checkout: (.+?) \\(", record["stdin"], re.M).group(1))
record["before"] = str(before)
record["before_head"] = subprocess.check_output(["git", "-C", str(before), "rev-parse", "HEAD"], text=True).strip()
record["head"] = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
for number in range(1, 6):
    slot = cwd.parent / f"slot-{number}"
    with slot.with_suffix(".lock").open("a+b") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            assert slot in (cwd, before), "only returned checkouts may stay locked"
        else:
            assert slot not in (cwd, before), "returned checkouts must stay locked"
pathlib.Path(os.environ["RECORD"]).write_text(json.dumps(record))
''')
        for family in ("codex", "claude"):
            with self.subTest(family=family):
                record = self.verifier(family, "--before", commits[2])
                self.assertEqual(record["cwd"], str(slots[2]))
                self.assertEqual(record["head"], commits[1])
                self.assertEqual(record["before"], str(slots[1]))
                self.assertEqual(record["before_head"], commits[2])
                self.assertEqual(self.out.read_text().count("warm reset"), 2)
                for slot in slots:
                    self.assertEqual((slot / "ignored").read_text(), "existing slot output")

    def test_POOL_R2_fewest_changed_paths_then_lowest_number(self):
        baseline = self.git("rev-parse", "HEAD")
        (self.repo / "odd\npath").touch()
        self.git("add", ".")
        self.git("commit", "-qm", "one changed path")
        closer = self.git("rev-parse", "HEAD")
        (self.repo / "tracked").write_text("candidate")
        self.git("add", ".")
        self.git("commit", "-qm", "candidate")
        candidate = self.git("rev-parse", "HEAD")
        slots = self.seed_pool([baseline, closer, closer])
        self.hold_other_pool_slots(*slots)
        self.git("reset", "--hard", candidate)
        self.assertEqual(self.verifier()["cwd"], str(slots[1]))
        self.assertIn("warm reset", self.out.read_text())
        self.assertEqual(self.verifier("codex", "--clean-checkout")["cwd"], str(slots[1]))
        self.assertIn("deep clean: --clean-checkout", self.out.read_text())

    def test_POOL_R2_warm_slot_precedes_exact_match_needing_deep_clean(self):
        baseline = self.git("rev-parse", "HEAD")
        (self.repo / "tracked").write_text("candidate")
        self.git("add", ".")
        self.git("commit", "-qm", "candidate")
        candidate = self.git("rev-parse", "HEAD")
        slots = self.seed_pool([candidate, baseline])
        metadata = slots[0].with_suffix(".json")
        state = json.loads(metadata.read_text())
        state["uses"] = 20
        metadata.write_text(json.dumps(state))
        self.git("reset", "--hard", candidate)
        self.assertEqual(self.verifier()["cwd"], str(slots[1]))
        self.assertIn("warm reset", self.out.read_text())

    def test_POOL_R2_unusable_metadata_ranks_after_valid_deep_clean_slot(self):
        commit = self.git("rev-parse", "HEAD")
        slots = self.seed_pool([commit, commit])
        self.hold_other_pool_slots(*slots)
        slots[0].with_suffix(".json").write_text("broken metadata")
        metadata = slots[1].with_suffix(".json")
        state = json.loads(metadata.read_text())
        state["uses"] = 20
        metadata.write_text(json.dumps(state))
        self.assertEqual(self.verifier()["cwd"], str(slots[1]))
        self.assertIn("deep clean: 20 uses", self.out.read_text())

    def test_POOL_R2_preferred_busy_or_unusable_slot_falls_through(self):
        baseline = self.git("rev-parse", "HEAD")
        (self.repo / "tracked").write_text("candidate")
        self.git("add", ".")
        self.git("commit", "-qm", "candidate")
        candidate = self.git("rev-parse", "HEAD")
        slots = self.seed_pool([baseline, candidate])
        self.assertEqual(self.verifier()["cwd"], str(slots[1]))
        with slots[1].with_suffix(".lock").open("a+b") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(self.verifier()["cwd"], str(slots[0]))
        # Restore the lower slot to a less suitable version for the next case.
        with slots[1].with_suffix(".lock").open("a+b") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.git("reset", "--hard", baseline)
            self.verifier()
        self.git("reset", "--hard", candidate)
        (slots[1] / ".git").write_text("broken")
        self.inject_launcher('''
import shutil
original = shutil.rmtree
def cannot_remove(path, *args, **kwargs):
    if str(path).endswith("/slot-2"):
        raise PermissionError("preferred slot unavailable")
    return original(path, *args, **kwargs)
shutil.rmtree = cannot_remove
''')
        self.assertEqual(self.verifier()["cwd"], str(slots[0]))
        self.assertIn("preferred slot unavailable", self.out.read_text())
        with slots[1].with_suffix(".lock").open("a+b") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_POOL_R2_ranking_is_unlocked_and_cleaning_is_recomputed_under_lock(self):
        baseline = self.git("rev-parse", "HEAD")
        (self.repo / "tracked").write_text("candidate")
        self.git("add", ".")
        self.git("commit", "-qm", "candidate")
        candidate = self.git("rev-parse", "HEAD")
        slots = self.seed_pool([baseline, candidate])
        (slots[1] / "ignored").write_text("must be cleaned after the race")
        self.inject_launcher('''
import fcntl, json, os, pathlib, subprocess
original_flock = fcntl.flock
original_output = subprocess.check_output
acquired = False
def race(fd, operation):
    global acquired
    descriptor = fd if isinstance(fd, int) else fd.fileno()
    if not os.readlink(f"/proc/self/fd/{descriptor}").split("/")[-1].startswith("slot-"):
        return original_flock(fd, operation)
    if not acquired:
        path = pathlib.Path(os.readlink(f"/proc/self/fd/{fd.fileno()}"))
        assert path.name == "slot-2.lock", "rank the exact match before trying locks"
        metadata = path.with_suffix(".json")
        state = json.loads(metadata.read_text())
        state["uses"] = 20
        metadata.write_text(json.dumps(state))
    result = original_flock(fd, operation)
    acquired = True
    return result
def unlocked_ranking(command, *args, **kwargs):
    if "diff" in command and not acquired:
        pools = (pathlib.Path.home() / ".cache/am-pool").iterdir()
        for pool in pools:
            for path in pool.glob("*.lock"):
                with path.open("a+b") as lock:
                    original_flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return original_output(command, *args, **kwargs)
fcntl.flock = race
subprocess.check_output = unlocked_ranking
''')
        self.assertEqual(self.verifier()["cwd"], str(slots[1]))
        self.assertFalse((slots[1] / "ignored").exists())
        self.assertIn("deep clean: 20 uses", self.out.read_text())

    def test_POOL_R2_new_slot_label_requires_all_creation_paths_absent(self):
        slot = Path(self.verifier()["cwd"])
        self.hold_other_pool_slots(slot)
        self.assertIn("deep clean: new slot", self.out.read_text())
        self.assertNotIn("recreated (missing checkout or registration)", self.out.read_text())
        metadata = slot.with_suffix(".json")
        for leftover in ("checkout", "metadata", "temporary"):
            with self.subTest(leftover=leftover):
                if leftover != "metadata":
                    metadata.unlink()
                if leftover != "checkout":
                    shutil.rmtree(slot)
                if leftover == "temporary":
                    slot.with_suffix(".json.tmp").write_text("interrupted creation")
                self.assertEqual(self.verifier()["cwd"], str(slot))
                self.assertNotIn("new slot", self.out.read_text())
                if leftover != "metadata":
                    self.assertIn("missing or corrupt metadata", self.out.read_text())

    def test_POOL_R2_missing_older_history_commit_forces_deep_clean(self):
        slot = Path(self.verifier()["cwd"])
        metadata = slot.with_suffix(".json")
        commit = self.git("rev-parse", "HEAD")
        for history in (["1" * 40, commit], [commit, "1" * 40, commit]):
            for untracked in (False, True):
                with self.subTest(history=history, untracked=untracked):
                    metadata.write_text(json.dumps({"uses": len(history), "commit": commit,
                                                    "commits": history}))
                    if untracked:
                        (self.repo / "forgotten").write_text("untracked source")
                    (slot / "ignored").write_text("output with lost history")
                    self.assertEqual(self.verifier()["cwd"], str(slot))
                    self.assertFalse((slot / "ignored").exists())
                    self.assertIn("deep clean: missing previous commit", self.out.read_text())
                    self.assertEqual(json.loads(metadata.read_text())["commits"], [commit])
                    (self.repo / "forgotten").unlink(missing_ok=True)

    def test_POOL_R8_python_build_definitions_clean_in_both_directions(self):
        baseline = self.git("rev-parse", "HEAD")
        slot = Path(self.verifier()["cwd"])
        self.hold_other_pool_slots(slot)
        for name in ("pyproject.toml", "setup.py", "setup.cfg"):
            with self.subTest(name=name):
                target = self.repo / "nested" / name
                target.parent.mkdir(exist_ok=True)
                target.write_text("changed build definition")
                self.git("add", ".")
                self.git("commit", "-qm", "Python build definition")
                for reverse in (False, True):
                    (slot / "ignored").write_text("stale output")
                    if reverse:
                        self.git("reset", "--hard", baseline)
                    self.verifier()
                    self.assertFalse((slot / "ignored").exists())
                    self.assertIn("deep clean: lockfile or toolchain changed", self.out.read_text())

    def test_POOL_S12_cargo_manifest_changes_clean_in_both_directions(self):
        manifest = self.repo / "workspace/crate/Cargo.toml"
        manifest.parent.mkdir(parents=True)
        manifest.write_text("old manifest")
        self.git("add", ".")
        self.git("commit", "-qm", "manifest")
        baseline = self.git("rev-parse", "HEAD")
        slot = Path(self.verifier()["cwd"])
        self.hold_other_pool_slots(slot)
        manifest.write_text("changed manifest")
        self.git("add", ".")
        self.git("commit", "-qm", "change manifest")
        for reverse in (False, True):
            with self.subTest(reverse=reverse):
                (slot / "ignored").write_text("stale binary")
                if reverse:
                    self.git("reset", "--hard", baseline)
                self.verifier()
                self.assertFalse((slot / "ignored").exists())
                self.assertIn("deep clean: Cargo manifest changed", self.out.read_text())

    def test_POOL_S12_cargo_source_edits_and_unrelated_deletion_stay_warm(self):
        paths = ("src/main.rs", "src/bin/tool.rs", "crate/not-src/bin/data.txt")
        for name in paths:
            target = self.repo / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("initial source")
        self.git("add", ".")
        self.git("commit", "-qm", "ordinary source files")
        slot = Path(self.verifier()["cwd"])
        for name in paths[:2]:
            (self.repo / name).write_text("edited source")
        (self.repo / paths[2]).unlink()
        self.git("add", "-A")
        self.git("commit", "-qm", "ordinary source edits")
        (slot / "ignored").write_text("warm output")
        self.verifier()
        self.assertTrue((slot / "ignored").exists())
        self.assertIn("warm reset", self.out.read_text())

    def test_POOL_S12_cargo_executable_removal_and_rename_clean(self):
        baseline = self.git("rev-parse", "HEAD")
        for path in ("main.rs", "crate/src/main.rs", "src/bin/tool.rs", "crate/src/bin/tool/main.rs",
                     "crate/src/bin/tool/helper.rs"):
            for rename in (False, True):
                with self.subTest(path=path, rename=rename):
                    self.git("reset", "--hard", baseline)
                    target = self.repo / path
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text("fn main() {}")
                    self.git("add", ".")
                    self.git("commit", "-qm", "executable source")
                    slot = Path(self.verifier()["cwd"])
                    self.hold_other_pool_slots(slot)
                    (slot / "ignored").write_text("stale binary")
                    if rename:
                        self.git("mv", path, "renamed.rs")
                    else:
                        target.unlink()
                    self.git("add", "-A")
                    self.git("commit", "-qm", "remove executable source")
                    self.verifier()
                    self.assertFalse((slot / "ignored").exists())
                    self.assertIn("deep clean: Cargo executable source removed", self.out.read_text())

    def test_POOL_R10_01_gitdir_points_to_main_preserves_source(self):
        slot = Path(self.verifier()["cwd"])
        (slot / ".git").write_text(f"gitdir: {self.repo / '.git'}\n")
        protected = [path for path in self.repo.rglob("*") if path.is_file()
                     and not path.is_relative_to(self.repo / ".git/worktrees")]
        before = {path: path.read_bytes() for path in protected}
        record = self.verifier()
        self.assertEqual(record["cwd"], str(slot))
        self.assertEqual({path: path.read_bytes() for path in protected}, before)
        self.assertIn("recreated", self.out.read_text())

    def test_POOL_R10_02_relative_hooks_disabled_on_reset_and_content_check(self):
        self.git("config", "core.hooksPath", ".githooks")
        slot = Path(self.verifier()["cwd"])
        hooks = slot / ".githooks"
        hooks.mkdir()
        marker = self.base / "hook-ran"
        for name in ("post-checkout", "post-index-change"):
            hook = hooks / name
            hook.write_text(f'#!{sys.executable}\nfrom pathlib import Path\nPath({str(marker)!r}).touch()\n')
            hook.chmod(0o755)
        self.assertEqual(self.verifier()["cwd"], str(slot))
        self.assertFalse(marker.exists())
        self.assertFalse(hooks.exists())

    def test_POOL_R10_01_registration_must_point_back_to_slot(self):
        slot = Path(self.verifier()["cwd"])
        admin = Path(subprocess.check_output(["git", "-C", str(slot), "rev-parse", "--absolute-git-dir"],
                                            text=True, env=self.env).strip())
        # A slot can point at a valid sibling's admin directory. Both halves of
        # the registration must identify this slot, not just the common repo.
        sibling = self.base / "sibling"
        self.git("worktree", "add", "--detach", str(sibling), "HEAD")
        sibling_git = (sibling / ".git").read_bytes()
        (slot / ".git").write_bytes(sibling_git)
        sibling_admin = Path(subprocess.check_output(
            ["git", "-C", str(sibling), "rev-parse", "--absolute-git-dir"], text=True, env=self.env).strip())
        protected = {path: path.read_bytes() for root in (sibling, sibling_admin)
                     for path in root.rglob("*") if path.is_file()}
        self.assertEqual(self.verifier()["cwd"], str(slot))
        self.assertEqual({path: path.read_bytes() for path in protected}, protected)
        self.assertEqual((admin / "gitdir").read_text().strip(), str(slot / ".git"))
        self.assertIn("recreated", self.out.read_text())

    def test_POOL_R10_03_committed_crlf_under_lf_attributes_stays_warm(self):
        (self.repo / "tracked").write_bytes(b"committed\r\n")
        self.git("add", "tracked")
        self.git("commit", "-qm", "CRLF blob")
        (self.repo / ".gitattributes").write_text("* text eol=lf\n")
        self.git("add", ".gitattributes")
        self.git("commit", "-qm", "LF attributes without renormalizing blobs")
        slot = Path(self.verifier()["cwd"])
        (slot / "ignored").write_text("warm build")
        before = (slot / "ignored").stat()
        self.assertEqual(self.verifier()["cwd"], str(slot))
        after = (slot / "ignored").stat()
        self.assertEqual((before.st_ino, before.st_mtime_ns), (after.st_ino, after.st_mtime_ns))
        self.assertEqual((slot / "tracked").read_bytes(), b"committed\r\n")
        self.assertEqual((slot / "ignored").read_text(), "warm build")
        self.assertIn("warm reset", self.out.read_text())
        self.assert_slot_bytes_match_fresh(slot)

    def test_POOL_R10_03_checkout_failure_is_plain_and_cleans_session(self):
        self.busy_pool()
        self.inject_launcher('''
import subprocess
original = subprocess.check_output
def broken_status(command, *args, **kwargs):
    result = original(command, *args, **kwargs)
    if "status" in command:
        return b"? unexpected\\0" if kwargs.get("text") is False else "?? unexpected"
    return result
subprocess.check_output = broken_status
''')
        result = self.launch("verifier", "codex", "--author-family", "claude")
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("checkout is not clean", result.stderr)
        self.assertIn("Result: launch refused\n", self.out.read_text())
        self.assertNotIn("Traceback", result.stderr)
        self.assertFalse(self.record.exists())
        self.assertEqual(list((self.home / ".cache/am").iterdir()), [])

    def current_umask(self):
        umask = os.umask(0)
        os.umask(umask)
        return umask

    def test_POOL_R10_04_permissions_and_hardlinks_match_fresh_checkout(self):
        (self.repo / "executable").write_text("exit 0\n")
        (self.repo / "executable").chmod(0o755)
        self.git("add", ".")
        self.git("commit", "-qm", "executable fixture")
        slot = Path(self.verifier()["cwd"])
        for mode in (0o444, 0o600):
            for name in ("tracked", "executable"):
                with self.subTest(mode=oct(mode), name=name):
                    (slot / name).chmod(mode)
                    self.verifier()
                    self.assertEqual((slot / name).stat().st_mode & 0o777,
                                     (0o777 if name == "executable" else 0o666) & ~self.current_umask())
        outside = self.base / "hardlink"
        outside.hardlink_to(slot / "tracked")
        before = outside.read_bytes(), outside.stat().st_ino
        self.verifier()
        self.assertEqual((slot / "tracked").stat().st_nlink, 1)
        self.assertNotEqual((slot / "tracked").stat().st_ino, outside.stat().st_ino)
        self.assertEqual((outside.read_bytes(), outside.stat().st_ino), before)
        previous_umask = os.umask(0o027)
        try:
            self.verifier()
            self.assertEqual((slot / "tracked").stat().st_mode & 0o777, 0o640)
            self.assertEqual((slot / "executable").stat().st_mode & 0o777, 0o750)
        finally:
            os.umask(previous_umask)

    def test_POOL_R10_06_admin_state_is_removed(self):
        self.git("config", "extensions.worktreeConfig", "true")
        slot = Path(self.verifier()["cwd"])
        (slot / "ignored").write_text("warm output")
        admin = Path(subprocess.check_output(["git", "-C", str(slot), "rev-parse", "--absolute-git-dir"],
                                            text=True, env=self.env).strip())
        for name in ("ORIG_HEAD", "FETCH_HEAD", "logs/old", "refs/worktree/private",
                     "refs/bisect/private", "refs/rewritten/private", "info/attributes", "config.worktree"):
            path = admin / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("[core]\nignoreStat = true\n" if name == "config.worktree"
                            else self.git("rev-parse", "HEAD") + "\n")
        self.assertEqual(self.verifier()["cwd"], str(slot))
        self.assertEqual((slot / "ignored").read_text(), "warm output")
        for name in ("ORIG_HEAD", "FETCH_HEAD", "index.lock", "logs/old", "info", "config.worktree"):
            self.assertFalse((admin / name).exists(), name)
        self.assertTrue((admin / "refs").is_dir())
        self.assertEqual(list((admin / "refs").iterdir()), [])

    def test_POOL_R10_06_stale_index_lock_is_removed_without_recreation(self):
        slot = Path(self.verifier()["cwd"])
        admin = Path(subprocess.check_output(["git", "-C", str(slot), "rev-parse", "--absolute-git-dir"],
                                            text=True, env=self.env).strip())
        (admin / "index.lock").touch()
        (slot / "ignored").write_text("warm output")
        self.assertEqual(self.verifier()["cwd"], str(slot))
        self.assertFalse((admin / "index.lock").exists())
        self.assertTrue((slot / "ignored").exists(), "stale lock must not require recreation")
        self.assertEqual((slot / "ignored").read_text(), "warm output")

    def test_POOL_R10_07_symlinked_parent_never_writes_outside_slot(self):
        (self.repo / "dir").mkdir()
        (self.repo / "dir/file").write_text("candidate")
        self.git("add", ".")
        self.git("commit", "-qm", "nested fixture")
        slot = Path(self.verifier()["cwd"])
        external = self.base / "external"
        external.mkdir()
        (external / "file").write_text("outside")
        before = (external / "file").read_bytes(), (external / "file").stat().st_ino
        shutil.rmtree(slot / "dir")
        (slot / "dir").symlink_to(external, target_is_directory=True)
        self.assertEqual(self.verifier()["cwd"], str(slot))
        self.assertEqual(((external / "file").read_bytes(), (external / "file").stat().st_ino), before)
        self.assertFalse((slot / "dir").is_symlink())
        self.assertEqual((slot / "dir/file").read_text(), "candidate")
        self.assertIn("recreated", self.out.read_text())

    def test_POOL_R10_08_untracked_source_previously_tracked_cleans_stale_output(self):
        (self.repo / "forgotten").write_text("needed source")
        self.git("add", "forgotten")
        self.git("commit", "-qm", "other lane tracks the source")
        slot = Path(self.verifier()["cwd"])
        (slot / "ignored").write_text("old output that could hide forgotten source")
        self.git("rm", "--cached", "forgotten")
        self.git("commit", "-qm", "candidate forgot to track source")
        self.verifier()
        self.assertFalse((slot / "ignored").exists())
        self.assertFalse((slot / "forgotten").exists())
        self.assertEqual((self.repo / "forgotten").read_text(), "needed source")
        self.assertIn("untracked source file was previously tracked", self.out.read_text())

    def test_POOL_R11_forgotten_source_from_earlier_lease_cleans_stale_output(self):
        (self.repo / "forgotten.c").write_text("needed source")
        self.git("add", "forgotten.c")
        self.git("commit", "-qm", "A tracks source")
        slot = Path(self.verifier()["cwd"])
        (slot / "ignored").write_text("A's stale output")
        self.git("rm", "forgotten.c")
        self.git("commit", "-qm", "X has no source")
        self.assertEqual(self.verifier()["cwd"], str(slot))
        self.assertEqual((slot / "ignored").read_text(), "A's stale output")
        (self.repo / "tracked").write_text("unrelated candidate B")
        self.git("add", "tracked")
        self.git("commit", "-qm", "B forgets source")
        (self.repo / "forgotten.c").write_text("needed source")
        self.assertEqual(self.verifier()["cwd"], str(slot))
        self.assertFalse((slot / "ignored").exists())
        self.assertFalse((slot / "forgotten.c").exists())
        self.assertEqual((self.repo / "forgotten.c").read_text(), "needed source")
        self.assertIn("untracked source file was previously tracked", self.out.read_text())
        state = json.loads(slot.with_suffix(".json").read_text())
        self.assertEqual(state["commits"], [self.git("rev-parse", "HEAD")])
        self.assertEqual(state["uses"], 1)

    def test_POOL_R11_history_is_persistent_bounded_and_cleared_by_deep_clean(self):
        slot = Path(self.verifier()["cwd"])
        self.hold_other_pool_slots(slot)
        commit = self.git("rev-parse", "HEAD")
        metadata = slot.with_suffix(".json")
        for uses in range(2, 21):
            self.assertEqual(self.verifier()["cwd"], str(slot))
            state = json.loads(metadata.read_text())
            self.assertEqual(state["uses"], uses)
            self.assertEqual(state["commits"], [commit] * uses)
        self.verifier()
        self.assertEqual(json.loads(metadata.read_text())["commits"], [commit])
        self.assertIn("deep clean: 20 uses", self.out.read_text())
        self.verifier()
        self.verifier("codex", "--clean-checkout")
        self.assertEqual(json.loads(metadata.read_text())["commits"], [commit])

    def test_POOL_R11_history_batch_handles_newlines_and_missing_paths(self):
        name = "forgotten\nsource.c"
        (self.repo / name).write_text("source")
        self.git("add", name)
        self.git("commit", "-qm", "source with newline in name")
        slot = Path(self.verifier()["cwd"])
        (slot / "ignored").write_text("warm output")
        # A missing path's response must not look like a successful object.
        (self.repo / ("never-tracked\n" + "a" * 40 + "\nfile")).touch()
        self.verifier()
        self.assertEqual((slot / "ignored").read_text(), "warm output")
        self.assertIn("warm reset", self.out.read_text())
        self.git("rm", name)
        self.git("commit", "-qm", "unrelated candidate")
        self.verifier()
        self.assertTrue((slot / "ignored").exists())
        (self.repo / name).write_text("forgotten source")
        self.verifier()
        self.assertFalse((slot / "ignored").exists())
        self.assertFalse((slot / name).exists())

    def test_POOL_R11_missing_or_corrupt_commit_history_forces_deep_clean(self):
        slot = Path(self.verifier()["cwd"])
        self.hold_other_pool_slots(slot)
        metadata = slot.with_suffix(".json")
        for history in (None, [], "not a list", [None], ["bad commit"], ["1" * 40]):
            with self.subTest(history=history):
                state = json.loads(metadata.read_text())
                if history is None:
                    del state["commits"]
                else:
                    state["commits"] = history
                metadata.write_text(json.dumps(state))
                (slot / "ignored").write_text("old build")
                self.assertEqual(self.verifier()["cwd"], str(slot))
                self.assertFalse((slot / "ignored").exists())
                self.assertIn("deep clean: missing or corrupt metadata", self.out.read_text())
                self.assertEqual(json.loads(metadata.read_text())["commits"], [self.git("rev-parse", "HEAD")])

    def test_POOL_R11_tracked_directory_modes_match_fresh_checkout(self):
        (self.repo / "dir/nested").mkdir(parents=True)
        (self.repo / "dir/nested/file").write_text("unchanged")
        self.git("add", ".")
        self.git("commit", "-qm", "nested tracked file")
        slot = Path(self.verifier()["cwd"])
        for mask in dict.fromkeys((self.current_umask(), 0o027)):
            previous_umask = os.umask(mask)
            try:
                fresh = self.base / f"fresh-{mask}"
                self.git("worktree", "add", "--detach", str(fresh), "HEAD")
                # Changing umask also repairs file modes, which can recreate a
                # read-only tree on the baseline. Set up warmth after that reset.
                self.verifier()
                (slot / "ignored").write_text("warm build")
                for mode in (0o555, 0o700, 0o777, 0o2775):
                    with self.subTest(umask=oct(mask), mode=oct(mode)):
                        for name in ("dir/nested", "dir", "."):
                            (slot / name).chmod(mode)
                        self.assertEqual(self.verifier()["cwd"], str(slot))
                        for name in (".", "dir", "dir/nested"):
                            self.assertEqual((slot / name).stat().st_mode & 0o7777,
                                             (fresh / name).stat().st_mode & 0o7777)
                        self.assertEqual((slot / "ignored").read_text(), "warm build")
                        self.assertIn("warm reset", self.out.read_text())
            finally:
                os.umask(previous_umask)

    def test_POOL_R11_reset_and_check_disable_untracked_cache(self):
        self.git("config", "core.untrackedCache", "true")
        slot = Path(self.verifier()["cwd"])
        subprocess.check_call(["git", "-C", str(slot), "update-index", "--untracked-cache"], env=self.env)
        subprocess.check_call(["git", "-C", str(slot), "status", "--porcelain"], env=self.env,
                              stdout=subprocess.DEVNULL)
        (slot / "leftover").write_text("must not be hidden")
        self.inject_launcher('''
import subprocess
original = subprocess.check_output
def check_cache_config(command, *args, **kwargs):
    if any("/slot-" in str(arg) for arg in command) and any(
            action in command for action in ("read-tree", "checkout", "clean", "status")):
        assert "core.untrackedCache=false" in command, command
    return original(command, *args, **kwargs)
subprocess.check_output = check_cache_config
''')
        self.assertEqual(self.verifier()["cwd"], str(slot))
        self.assertFalse((slot / "leftover").exists())
        self.assertIn("warm reset", self.out.read_text())

    def test_POOL_R11_fresh_fallback_checks_status_without_content_recheck(self):
        self.busy_pool()
        self.inject_launcher('''
import subprocess
original = subprocess.check_output
def no_content_recheck(command, *args, **kwargs):
    assert not any(action in command for action in ("hash-object", "check-attr", "cat-file")), command
    return original(command, *args, **kwargs)
subprocess.check_output = no_content_recheck
''')
        record = self.verifier()
        self.assertFalse(Path(record["cwd"]).is_relative_to(self.pool_root()))
        self.assertIn("pool busy", self.out.read_text())

    def test_POOL_R11_refusal_hashes_only_changed_source_paths(self):
        (self.repo / "unchanged").write_text("must not be hashed")
        self.git("add", "unchanged")
        self.git("commit", "-qm", "unchanged source")
        (self.repo / "tracked").write_text("dirty source")
        self.inject_launcher('''
import os, pathlib, subprocess
original = subprocess.check_output
def changed_paths_only(command, *args, **kwargs):
    if "hash-object" in command and "--stdin-paths" in command:
        paths = kwargs["input"]
        assert paths == b'"tracked"\\n', paths
        pathlib.Path(os.environ["RECORD"] + ".hashed").write_bytes(paths)
    return original(command, *args, **kwargs)
subprocess.check_output = changed_paths_only
''')
        result = self.launch("verifier", "codex", "--author-family", "claude")
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("commit first", result.stderr)
        self.assertEqual(Path(str(self.record) + ".hashed").read_bytes(), b'"tracked"\n')
        self.assertFalse(self.pool_root().exists())

    def test_POOL_R12_ignored_symlinks_are_removed_without_touching_targets(self):
        (self.repo / ".gitignore").write_text("ignored\nnode_modules\ntarget\n.venv\n")
        self.git("add", ".gitignore")
        self.git("commit", "-qm", "ignored dependency trees")
        slot = Path(self.verifier()["cwd"])
        self.hold_other_pool_slots(slot)
        outside = self.base / "outside"
        outside.mkdir()
        (outside / "keep").write_text("untouched")
        for clean in (False, True):
            with self.subTest(deep_clean=clean):
                (slot / "ignored").mkdir(exist_ok=True)
                (slot / "ignored/warm").write_text("build output")
                for name in ("node_modules", "target", ".venv", "ignored/nested-link"):
                    (slot / name).unlink(missing_ok=True)
                    (slot / name).symlink_to(outside, target_is_directory=True)
                record = self.verifier("codex", *(["--clean-checkout"] if clean else []))
                self.assertEqual(record["cwd"], str(slot))
                for name in ("node_modules", "target", ".venv", "ignored/nested-link"):
                    self.assertFalse(os.path.lexists(slot / name), name)
                self.assertEqual(list(outside.iterdir()), [outside / "keep"])
                self.assertEqual((outside / "keep").read_text(), "untouched")
                self.assertFalse((slot / "ignored").exists())
                self.assertIn("--clean-checkout" if clean else "warm reset", self.out.read_text())

    def test_POOL_R13_internal_dependency_symlinks_survive_warm_reset(self):
        (self.repo / ".gitignore").write_text("node_modules\n.venv\n")
        self.git("add", ".gitignore")
        self.git("commit", "-qm", "dependency trees")
        slot = Path(self.verifier()["cwd"])
        package = slot / "node_modules/.pnpm/tool@1/node_modules/tool"
        package.mkdir(parents=True)
        script = package / "cli"
        script.write_text("#!/bin/sh\nprintf 'works\\n'\n")
        script.chmod(0o755)
        (slot / "node_modules/tool").symlink_to(".pnpm/tool@1/node_modules/tool")
        (slot / "node_modules/.bin").mkdir()
        (slot / "node_modules/.bin/tool").symlink_to("../tool/cli")
        (slot / "node_modules/.bin/future").symlink_to("../future/cli")
        (slot / ".venv/bin").mkdir(parents=True)
        (slot / ".venv/python").write_text("internal interpreter")
        (slot / ".venv/bin/python").symlink_to(slot / ".venv/python")
        record = self.verifier()
        self.assertEqual(record["cwd"], str(slot))
        self.assertIn("warm reset", self.out.read_text())
        self.assertEqual(subprocess.check_output([str(slot / "node_modules/.bin/tool")], text=True), "works\n")
        self.assertEqual((slot / "node_modules/tool").resolve(), package)
        self.assertTrue((slot / "node_modules/.bin/future").is_symlink())
        self.assertEqual((slot / ".venv/bin/python").read_text(), "internal interpreter")

    def test_POOL_R13_external_dependency_link_removes_whole_ignored_tree(self):
        (self.repo / ".gitignore").write_text("node_modules\n")
        self.git("add", ".gitignore")
        self.git("commit", "-qm", "dependency tree")
        slot = Path(self.verifier()["cwd"])
        outside = self.base / "external-package"
        outside.mkdir()
        (outside / "keep").write_text("untouched")
        (slot / "node_modules/package").mkdir(parents=True)
        (slot / "node_modules/installed").write_text("otherwise looks installed")
        (slot / "node_modules/package/link").symlink_to(os.path.relpath(outside, slot / "node_modules/package"))
        self.verifier()
        self.assertFalse((slot / "node_modules").exists())
        self.assertEqual((outside / "keep").read_text(), "untouched")
        self.assertEqual(list(outside.iterdir()), [outside / "keep"])

    def test_POOL_R13_ignored_rule_files_stay_warm(self):
        folders = (".venv", ".pytest_cache", ".mypy_cache", ".ruff_cache")
        (self.repo / ".gitignore").write_text("\n".join(folders) + "\n")
        self.git("add", ".gitignore")
        self.git("commit", "-qm", "ignored tool caches")
        slot = Path(self.verifier()["cwd"])
        for folder in folders:
            (slot / folder).mkdir()
            for name in (".gitignore", ".gitattributes", "warm"):
                (slot / folder / name).write_text("*\n")
        (slot / "src").mkdir()
        (slot / "src/.gitignore").write_text("*\n")
        (slot / "src/.gitattributes").write_text("* -text\n")
        (slot / "src/stale").touch()
        self.verifier()
        for folder in folders:
            for name in (".gitignore", ".gitattributes", "warm"):
                self.assertEqual((slot / folder / name).read_text(), "*\n")
        self.assertFalse((slot / "src").exists())

    def test_POOL_R13_nondirectory_slot_is_recreated(self):
        slot = Path(self.verifier()["cwd"])
        for kind in ("file", "fifo"):
            with self.subTest(kind=kind):
                if slot.is_dir():
                    shutil.rmtree(slot)
                else:
                    slot.unlink()
                if kind == "file":
                    slot.write_text("broken slot")
                else:
                    os.mkfifo(slot)
                record = self.verifier()
                self.assertEqual(record["cwd"], str(slot))
                self.assertTrue(slot.is_dir())
                self.assertEqual(self.git("worktree", "list", "--porcelain").count("worktree "), 2)
                self.assertIn("recreated", self.out.read_text())

    def test_POOL_R13_rulefiles_cannot_outlive_candidate_ignore_rules(self):
        for case in ("local-ignore-edit", "committed-ignore-removal"):
            with self.subTest(case=case):
                if case == "committed-ignore-removal":
                    (self.repo / ".gitignore").write_text("ignored\nstale\n")
                    self.git("commit", "-qam", "ignore stale")
                slot = Path(self.verifier()["cwd"])
                if case == "local-ignore-edit":
                    (slot / ".gitignore").write_text("ignored\nstale\n")
                else:
                    (self.repo / ".gitignore").write_text("ignored\n")
                    self.git("commit", "-qam", "stop ignoring stale")
                (slot / "stale").mkdir(exist_ok=True)
                (slot / "stale/.gitignore").write_text("*\n")
                (slot / "stale/.gitattributes").write_text("* -text\n")
                (slot / "stale/forgotten").touch()
                (slot / "ignored").write_text("warm build")
                self.verifier()
                self.assertFalse((slot / "stale").exists())
                self.assertEqual((slot / "ignored").read_text(), "warm build")

    def test_POOL_R13_relative_worktree_paths_stay_warm(self):
        probe = subprocess.run(["git", "worktree", "add", "-h"], env=self.env,
                               text=True, capture_output=True)
        if "relative-paths" not in probe.stdout + probe.stderr:
            self.skipTest("Git does not support relative worktree paths")
        self.git("config", "worktree.useRelativePaths", "true")
        slot = Path(self.verifier()["cwd"])
        self.assertTrue(slot.is_relative_to(self.pool_root()), "relative path config must permit a pool lease")
        admin = Path(subprocess.check_output(["git", "-C", str(slot), "rev-parse", "--absolute-git-dir"],
                                            env=self.env, text=True).strip())
        self.assertTrue(Path((admin / "gitdir").read_text().strip()).is_absolute())
        # Accept registrations created by an older launcher or Git command too.
        (admin / "gitdir").write_text(os.path.relpath(slot / ".git", admin) + "\n")
        (slot / "ignored").write_text("warm build")
        self.verifier()
        self.assertEqual((slot / "ignored").read_text(), "warm build")
        self.assertIn("warm reset", self.out.read_text())

    def test_POOL_R13_relative_registration_cleanup_fallback(self):
        probe = subprocess.run(["git", "worktree", "add", "-h"], env=self.env,
                               text=True, capture_output=True)
        if "relative-paths" not in probe.stdout + probe.stderr:
            self.skipTest("Git does not support relative worktree paths")
        self.git("config", "worktree.useRelativePaths", "true")
        self.agent_extra('''
import shutil
temporary = pathlib.Path(os.environ["TMPDIR"]) / "extra"
subprocess.check_call(["git", "worktree", "add", "--detach", str(temporary), "HEAD"])
shutil.rmtree(temporary)
''')
        self.inject_launcher('''
import subprocess
original = subprocess.check_output
def fail_remove(command, *args, **kwargs):
    if "worktree" in command and "remove" in command:
        raise subprocess.CalledProcessError(31, command)
    return original(command, *args, **kwargs)
subprocess.check_output = fail_remove
''')
        result = self.launch()
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertEqual(self.git("worktree", "list", "--porcelain").count("worktree "), 1)

    def test_POOL_R13_submodule_untracked_allowed_tracked_edits_refused(self):
        self.env["PATH"] += os.pathsep + os.environ["PATH"]
        module = self.base / "module"
        subprocess.check_call(["git", "init", "-q", str(module)], env=self.env)
        (module / "file").write_text("committed")
        for args in (("add", "."), ("commit", "-qm", "module")):
            subprocess.check_call(["git", "-C", str(module), *args], env=self.env)
        self.git("-c", "protocol.file.allow=always", "submodule", "add", "-q", str(module), "vendor/module")
        self.git("commit", "-qam", "submodule")
        sub = self.repo / "vendor/module"
        (sub / "forgotten").write_text("untracked")
        index = (self.repo / ".git/index").read_bytes()
        slot = Path(self.verifier()["cwd"])
        self.assertFalse((slot / "vendor/module/forgotten").exists())
        self.assertEqual((sub / "forgotten").read_text(), "untracked")
        (sub / "file").write_text("edited")
        result = self.launch("verifier", "codex", "--author-family", "claude")
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("commit first", result.stderr)
        self.assertIn("vendor/module", result.stderr)
        self.assertEqual((self.repo / ".git/index").read_bytes(), index)

    def test_POOL_R14_reset_does_not_recurse_into_leftover_submodule(self):
        self.env["PATH"] += os.pathsep + os.environ["PATH"]
        module = self.base / "module"
        subprocess.check_call(["git", "init", "-q", str(module)], env=self.env)
        (module / "file").write_text("committed")
        for args in (("add", "."), ("commit", "-qm", "module")):
            subprocess.check_call(["git", "-C", str(module), *args], env=self.env)
        self.git("-c", "protocol.file.allow=always", "submodule", "add", "-q", str(module), "vendor/module")
        self.git("commit", "-qam", "submodule")
        slot = Path(self.verifier()["cwd"])
        (slot / "ignored").write_text("warm build")
        (slot / "vendor/module/.git").write_text("broken leftover submodule")
        self.git("config", "submodule.recurse", "true")
        self.assertEqual(self.verifier()["cwd"], str(slot))
        self.assertIn("warm reset", self.out.read_text())
        self.assertEqual((slot / "ignored").read_text(), "warm build")
        self.assertEqual(list((slot / "vendor/module").iterdir()), [])
        self.assertEqual(self.git("config", "submodule.recurse"), "true")

    def test_POOL_R13_removal_retry_preserves_external_hardlink_mode(self):
        slot = Path(self.verifier()["cwd"])
        external = self.base / "external-file"
        external.write_text("untouched")
        external.chmod(0o640)
        (slot / "ignored").mkdir()
        (slot / "ignored/hardlink").hardlink_to(external)
        (slot / "ignored").chmod(0o500)
        (slot / ".git").write_text("broken")
        try:
            self.verifier()
            self.assertEqual(stat.S_IMODE(external.stat().st_mode), 0o640)
            self.assertEqual(external.read_text(), "untouched")
        finally:
            if (slot / "ignored").exists():
                (slot / "ignored").chmod(0o700)

    def test_POOL_R14_guard_never_imports_candidate_or_ignored_modules_at_start(self):
        marker = self.base / "slot-module-imported"
        payload = (f"open({str(marker)!r}, 'w').write('unsafe import')\n"
                   "raise RuntimeError('lease guard imported slot module')\n")
        for committed in (True, False):
            with self.subTest(committed=committed):
                marker.unlink(missing_ok=True)
                (self.repo / ".gitignore").write_text("ignored\nsignal.py\n")
                module = self.repo / "signal.py"
                if committed:
                    module.write_text(payload)
                    self.git("add", "-f", "signal.py")
                else:
                    self.git("rm", "-q", "signal.py")
                self.git("add", ".gitignore")
                self.git("commit", "-qm", "guard import fixture")
                if not committed:
                    # Ignored leftover files survive a warm reset.
                    slot = Path(self.verifier()["cwd"])
                    (slot / "signal.py").write_text(payload)
                self.round_decision()
                result = self.launch("verifier", "codex", "--author-family", "claude")
                self.assertFalse(marker.exists(), "guard imported signal.py from the slot")
                self.assertEqual(result.returncode, 0, result.stderr)
                if not committed:
                    self.assertTrue((slot / "signal.py").exists(), "fixture must survive the warm reset")
                    self.assertIn("warm reset", self.out.read_text())

    def test_POOL_R14_orphan_guard_never_imports_candidate_or_agent_modules(self):
        self.configure_scope(present=False)
        self.addCleanup(self.kill_recorded_agents)
        for committed in (True, False):
            with self.subTest(committed=committed):
                marker = self.base / f"orphan-import-{committed}"
                payload = (f"open({str(marker)!r}, 'w').write('unsafe import')\n"
                           "raise RuntimeError('orphan guard imported slot module')\n")
                if committed:
                    (self.repo / "shutil.py").write_text(payload)
                    # The orphan must load the trusted launcher, never this file.
                    (self.repo / "run.py").write_text(payload)
                    self.git("add", ".")
                else:
                    self.git("rm", "-q", "shutil.py", "run.py")
                self.git("commit", "-qm", "orphan import fixture")
                self.env["SLOT_MODULE_PAYLOAD"] = payload
                self.env["WRITE_SLOT_MODULES"] = "" if committed else "1"
                self.agent_extra('''
if os.environ["WRITE_SLOT_MODULES"]:
    for name in ("shutil.py", "signal.py", "run.py"):
        (cwd / name).write_text(os.environ["SLOT_MODULE_PAYLOAD"])
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True)
pathlib.Path(os.environ["RECORD"] + ".pids").write_text(str(os.getpid()) + " " + str(child.pid))
pathlib.Path(os.environ["RECORD"] + ".ready").touch()
while True:
    time.sleep(0.02)
''')
                record_path = self.base / f"orphan-{committed}.json"
                process = self.start_launcher(record=record_path)
                record = self.wait_agent_ready(record_path)
                process.kill()
                process.communicate(timeout=10)
                with Path(record["cwd"]).with_suffix(".lock").open("a+b") as lock:
                    deadline = time.monotonic() + 5
                    while True:
                        try:
                            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                            break
                        except BlockingIOError:
                            if time.monotonic() >= deadline:
                                self.fail("orphan guard did not release the lease")
                            time.sleep(0.02)
                self.assertFalse(marker.exists(), "orphan guard imported code from the slot")
                self.assertFalse(Path(record["tmpdir"]).parent.exists(), "orphan session was not cleaned")
                pids = map(int, Path(str(record_path) + ".pids").read_text().split())
                self.assertTrue(all(not Path(f"/proc/{pid}").exists() for pid in pids))

    def test_POOL_R14_verifier_prompt_matches_candidate_checkout_and_untracked_header(self):
        candidate = self.git("rev-parse", "HEAD")
        for family in ("codex", "claude"):
            for untracked in (False, True):
                with self.subTest(family=family, untracked=untracked):
                    forgotten = self.repo / "forgotten file"
                    if untracked:
                        forgotten.write_text("left out")
                    else:
                        forgotten.unlink(missing_ok=True)
                    record = self.verifier(family)
                    header = self.out.read_text().split("\n\n", 1)[0]
                    expected = [f"Snapshot commit: {candidate}",
                                "Untracked files left out: " + ("'forgotten file'" if untracked else "none")]
                    checkout = next(line for line in header.splitlines() if line.startswith("Checkout:"))
                    self.assertIn("Checkout: pool ", checkout)
                    for line in [*expected, checkout]:
                        self.assertIn(line + "\n", record["stdin"])
                        self.assertIn(line + "\n", header + "\n")

    def test_POOL_R13_guard_exits_cleanly_before_starting_agent(self):
        tree = ast.parse((ROOT / "scripts/run.py").read_text())
        guard = next(ast.literal_eval(node.value) for node in tree.body
                     if isinstance(node, ast.Assign) and any(
                         isinstance(target, ast.Name) and target.id == "LEASE_GUARD" for target in node.targets))
        for early_exit in ("parent", "signal", "repeated-signal", "missing-proc"):
            with self.subTest(early_exit=early_exit):
                session = self.base / "guard-session"
                session.mkdir(exist_ok=True)
                prefix = ""
                if early_exit in ("signal", "repeated-signal"):
                    prefix = ("import os, signal, subprocess\n"
                              "def interrupted_popen(*args, **kwargs):\n"
                              "    os.kill(os.getpid(), signal.SIGTERM)\n"
                              "subprocess.Popen = interrupted_popen\n")
                if early_exit == "repeated-signal":
                    # Hit the window before all cleanup handlers become SIG_IGN.
                    prefix += ("original_signal = signal.signal\n"
                               "def second_signal(sig, handler):\n"
                               "    if sig == signal.SIGTERM and handler == signal.SIG_IGN:\n"
                               "        os.kill(os.getpid(), signal.SIGINT)\n"
                               "    return original_signal(sig, handler)\n"
                               "signal.signal = second_signal\n")
                if early_exit == "missing-proc":
                    prefix = ("import builtins\n"
                              "original_open = builtins.open\n"
                              "def missing_children(path, *args, **kwargs):\n"
                              "    if str(path).startswith('/proc/') and str(path).endswith('/children'):\n"
                              "        raise FileNotFoundError('test: proc children unavailable')\n"
                              "    return original_open(path, *args, **kwargs)\n"
                              "builtins.open = missing_children\n")
                result = subprocess.run(
                    [sys.executable, "-I", "-c", prefix + guard, str(ROOT / "scripts/run.py"),
                     str(self.repo), str(session), str(os.getpid() if "signal" in early_exit else -1),
                     sys.executable, "-c", "raise RuntimeError('agent must not start')"],
                    env=self.env, text=True, capture_output=True)
                self.assertEqual(result.returncode, 143, result.stderr)
                self.assertNotIn("Traceback", result.stderr)
                if early_exit == "missing-proc":
                    self.assertIn("Warning: cannot list lease guard children", result.stderr)
                    self.assertIn("test: proc children unavailable", result.stderr)

    def test_POOL_R12_special_files_are_removed_on_warm_and_deep_resets(self):
        slot = Path(self.verifier()["cwd"])
        self.hold_other_pool_slots(slot)
        for clean in (False, True):
            with self.subTest(deep_clean=clean):
                for directory in (slot, slot / "sub", slot / "ignored"):
                    directory.mkdir(exist_ok=True)
                    (directory / "pipe").unlink(missing_ok=True)
                    os.mkfifo(directory / "pipe")
                    # A socket inode exercises cleanup without requiring bind
                    # permission in the launcher's restricted test environment.
                    (directory / "socket").unlink(missing_ok=True)
                    os.mknod(directory / "socket", stat.S_IFSOCK | 0o600)
                record = self.verifier("codex", *(["--clean-checkout"] if clean else []))
                self.assertEqual(record["cwd"], str(slot))
                for directory in (slot, slot / "sub", slot / "ignored"):
                    for name in ("pipe", "socket"):
                        self.assertFalse(os.path.lexists(directory / name), str(directory / name))
                self.assertIn("--clean-checkout" if clean else "warm reset", self.out.read_text())

    def test_POOL_R12_reftable_uses_one_fresh_worktree_without_pool(self):
        probe = subprocess.run(["git", "init", "-q", "--ref-format=reftable", str(self.base / "probe")],
                               env=self.env, capture_output=True, text=True)
        if probe.returncode:
            self.skipTest("installed Git does not support reftable: " + probe.stderr)
        shutil.rmtree(self.repo / ".git")
        self.git("init", "-q", "--ref-format=reftable")
        self.git("add", ".")
        self.git("commit", "-qm", "reftable candidate")
        self.inject_launcher('''
import os, pathlib, subprocess
original = subprocess.check_output
def count_adds(command, *args, **kwargs):
    if "worktree" in command and "add" in command:
        with pathlib.Path(os.environ["RECORD"] + ".adds").open("a") as log:
            log.write("add\\n")
    return original(command, *args, **kwargs)
subprocess.check_output = count_adds
''')
        self.pool_probe()
        for before in ("none", "HEAD"):
            for _ in range(2):
                record = self.verifier("codex", "--before", before)
                self.assertEqual(record["head"], self.git("rev-parse", "HEAD"))
                self.assertEqual(record["status"], "")
                self.assertFalse(self.pool_root().exists())
                self.assertEqual(self.out.read_text().count("reftable; fresh session worktree"),
                                 1 if before == "none" else 2)
                if before != "none":
                    line = next(line for line in self.out.read_text().splitlines()
                                if line.startswith("Before checkout:"))
                    path = Path(line.removeprefix("Before checkout: ").split(" (", 1)[0])
                    self.assertNotEqual(str(path), record["cwd"])
                    self.assertFalse(path.exists())
                self.assert_session_cleaned(record)
        self.assertEqual(Path(str(self.record) + ".adds").read_text(), "add\n" * 6)

    def test_POOL_R12_source_permissions_and_hardlinks_do_not_refuse(self):
        # This committed CRLF blob looks modified to diff under LF attributes,
        # so the source content check must accept the actual checkout bytes.
        (self.repo / "tracked").write_bytes(b"original\r\n")
        self.git("add", "tracked")
        self.git("commit", "-qm", "CRLF blob")
        (self.repo / ".gitattributes").write_text("* text eol=lf\n")
        self.git("add", ".gitattributes")
        self.git("commit", "-qm", "LF attributes")
        (self.repo / "tracked").chmod(0o644)
        self.assertIn("tracked", self.git("diff", "--name-only"))
        mask = self.current_umask()
        try:
            for test_mask, hardlinked in ((0o002, False), (0o022, True)):
                with self.subTest(umask=oct(test_mask), hardlinked=hardlinked):
                    os.umask(test_mask)
                    if hardlinked:
                        (self.base / "hardlink").hardlink_to(self.repo / "tracked")
                    paths = (self.repo / "tracked", self.repo / ".git/index", self.repo / ".git/config")
                    before = [(p.read_bytes(), p.stat()) for p in paths]
                    self.verifier()
                    self.assertEqual([(p.read_bytes(), p.stat()) for p in paths], before)
        finally:
            os.umask(mask)

    def test_POOL_R12_source_directory_replaced_by_file_says_commit_first(self):
        (self.repo / "dir").mkdir()
        (self.repo / "dir/file").write_text("committed")
        self.git("add", ".")
        self.git("commit", "-qm", "tracked directory")
        shutil.rmtree(self.repo / "dir")
        (self.repo / "dir").write_text("replacement")
        index = (self.repo / ".git/index").read_bytes()
        result = self.launch("verifier", "codex", "--author-family", "claude")
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("commit first", result.stderr)
        self.assertIn("dir/file", result.stderr)
        self.assertFalse(self.record.exists())
        self.assertFalse(self.pool_root().exists())
        self.assertEqual((self.repo / ".git/index").read_bytes(), index)
        self.assertEqual((self.repo / "dir").read_text(), "replacement")

    def test_POOL_R12_moved_source_gitlink_refuses_and_names_path(self):
        self.env["PATH"] += os.pathsep + os.environ["PATH"]
        module = self.base / "module"
        subprocess.check_call(["git", "init", "-q", str(module)], env=self.env)
        (module / "file").write_text("one")
        for args in (("add", "."), ("commit", "-qm", "one")):
            subprocess.check_call(["git", "-C", str(module), *args], env=self.env)
        subprocess.check_call(["git", "-C", str(self.repo), "-c", "protocol.file.allow=always",
                               "submodule", "add", "-q", str(module), "vendor/module"], env=self.env)
        self.git("commit", "-qam", "submodule")
        sub = self.repo / "vendor/module"
        (sub / "file").write_text("two")
        subprocess.check_call(["git", "-C", str(sub), "commit", "-qam", "two"], env=self.env)
        index = (self.repo / ".git/index").read_bytes()
        result = self.launch("verifier", "codex", "--author-family", "claude")
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("commit first", result.stderr)
        self.assertIn("vendor/module", result.stderr)
        self.assertFalse(self.record.exists())
        self.assertFalse(self.pool_root().exists())
        self.assertEqual((self.repo / ".git/index").read_bytes(), index)

    def test_POOL_R10_09_unchanged_index_stat_data_survives_reset(self):
        self.verifier()
        self.inject_launcher('''
import subprocess
original = subprocess.check_output
def check_index(command, *args, **kwargs):
    if "read-tree" in command:
        probe = command[:command.index("read-tree")] + ["ls-files", "--debug"]
        index = original(probe, text=True)
        assert index and "ctime: 0:0" not in index and "mtime: 0:0" not in index, index
    return original(command, *args, **kwargs)
subprocess.check_output = check_index
''')
        self.verifier()

    def test_POOL_R10_10_before_prompt_uses_supplied_checkout(self):
        record = self.verifier("codex", "--before", "HEAD")
        self.assertIn("Use this writable checkout for the before state", record["stdin"])
        self.assertNotIn("create before or known-good checkouts", record["stdin"])
        self.assertIn("create known-good checkouts", record["stdin"])
        record = self.verifier()
        self.assertIn("Put known-good and other temporary checkouts there too", record["stdin"])

    def test_POOL_R10_10_unusable_slots_are_named_in_fallback(self):
        self.inject_launcher('''
import subprocess
original = subprocess.check_output
def broken_slot(command, *args, **kwargs):
    if "checkout" in command and any("/slot-" in str(arg) for arg in command):
        raise subprocess.CalledProcessError(1, command)
    return original(command, *args, **kwargs)
subprocess.check_output = broken_slot
''')
        record = self.verifier()
        self.assertFalse(Path(record["cwd"]).is_relative_to(self.pool_root()))
        self.assertIn("pool busy or slots unusable", self.out.read_text())

    def test_POOL_R10_11_reset_git_child_keeps_lock_after_launcher_sigkill(self):
        (self.repo / ".gitattributes").write_text("tracked filter=blocked\n")
        self.git("add", ".gitattributes")
        self.git("commit", "-qm", "smudge filter fixture")
        slot = Path(self.verifier()["cwd"])
        ready = self.base / "reset-child-ready"
        release = self.base / "reset-child-release"
        smudge = self.base / "blocked-smudge.py"
        smudge.write_text(
            "import os, pathlib, sys, time\n"
            f"pathlib.Path({str(ready)!r}).write_text(str(os.getpid()))\n"
            f"while not pathlib.Path({str(release)!r}).exists(): time.sleep(0.02)\n"
            "sys.stdout.buffer.write(sys.stdin.buffer.read())\n")
        self.git("config", "filter.blocked.smudge", shlex.join([sys.executable, str(smudge)]))
        (slot / "tracked").write_text("force Git checkout to run the smudge filter")
        process = self.start_launcher()
        # Unblock the real Git checkout even if an assertion fails. The
        # launcher pipes stay open until Git and the filter have exited.
        self.addCleanup(release.touch)
        deadline = time.monotonic() + 10
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue(ready.exists())
        process.kill()
        process.wait(timeout=5)
        with slot.with_suffix(".lock").open("a+b") as lock:
            with self.assertRaises(BlockingIOError):
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            release.touch()
            process.communicate(timeout=5)
            deadline = time.monotonic() + 5
            while True:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        self.fail("reset Git child did not release the lease")
                    time.sleep(0.02)
        self.assertFalse(slot.with_suffix(".json").exists())

    def test_POOL_S8_broken_slots_recover_without_consuming_capacity(self):
        record = self.verifier()
        slot = Path(record["cwd"])
        self.hold_other_pool_slots(slot)
        for case in ("deleted", "registration", "missing-metadata", "corrupt-metadata", "wrong-metadata",
                     "interrupted-reset", "corrupt-git", "symlink-to-source"):
            with self.subTest(case=case):
                metadata = slot.with_suffix(".json")
                if case == "deleted":
                    shutil.rmtree(slot)
                elif case == "registration":
                    admin = Path(subprocess.check_output(["git", "-C", str(slot), "rev-parse", "--absolute-git-dir"],
                                                        text=True).strip())
                    shutil.rmtree(admin)
                elif case in ("missing-metadata", "interrupted-reset"):
                    metadata.unlink()
                elif case == "corrupt-metadata":
                    metadata.write_text("not JSON")
                elif case == "wrong-metadata":
                    metadata.write_text(json.dumps({"uses": -1, "commit": []}))
                elif case == "corrupt-git":
                    (slot / ".git").write_text("broken")
                else:
                    shutil.rmtree(slot)
                    slot.symlink_to(self.repo, target_is_directory=True)
                if slot.is_dir() and not slot.is_symlink():
                    (slot / "ignored").write_text("stale output")
                    (slot / "new").write_text("untracked")
                self.pool_probe()
                next_record = self.verifier()
                self.assertEqual(next_record["cwd"], str(slot))
                self.assertEqual(next_record["status"], "")
                self.assertEqual(next_record["head"], self.git("rev-parse", "HEAD"))
                self.assertFalse((slot / "ignored").exists())
                self.assertEqual(json.loads(metadata.read_text())["uses"], 1)

    def test_POOL_S8_nonregular_metadata_paths_deep_clean_without_following_links(self):
        slot = Path(self.verifier()["cwd"])
        self.hold_other_pool_slots(slot)
        self.pool_probe()
        target = self.base / "metadata-target"
        target.write_text("must stay untouched")
        for suffix in (".json", ".json.tmp"):
            for kind in ("directory", "symlink", "fifo"):
                with self.subTest(path=suffix, kind=kind):
                    self.verifier()
                    # Valid metadata behind the link must still force a deep
                    # clean; merely reading it would silently preserve output.
                    original = slot.with_suffix(".json").read_text()
                    target.write_text(original)
                    path = slot.with_suffix(suffix)
                    path.unlink(missing_ok=True)
                    if kind == "directory":
                        path.mkdir()
                        (path / "nested").mkdir()
                        (path / "nested/link").symlink_to(target)
                    elif kind == "symlink":
                        path.symlink_to(target)
                    else:
                        os.mkfifo(path)
                    (slot / "ignored").write_text("stale build output")
                    process = self.start_launcher()
                    try:
                        try:
                            _, stderr = process.communicate(timeout=5)
                        except subprocess.TimeoutExpired:
                            process.terminate()
                            process.communicate(timeout=10)
                            self.fail("launcher blocked on non-regular metadata path")
                        self.assertEqual(process.returncode, 0, stderr)
                        record = json.loads(self.record.read_text())
                        self.assertEqual(record["cwd"], str(slot))
                        self.assertEqual(record["status"], "")
                        self.assertEqual(target.read_text(), original)
                        self.assertFalse((slot / "ignored").exists())
                        self.assertIn("deep clean:", self.out.read_text())
                        self.assertEqual(json.loads(slot.with_suffix(".json").read_text())["uses"], 1)
                        self.assertFalse(slot.with_suffix(".json.tmp").exists())
                    finally:
                        if path.is_symlink() or (path.exists() and not path.is_file()):
                            if path.is_dir() and not path.is_symlink():
                                shutil.rmtree(path)
                            else:
                                path.unlink()

    def test_POOL_S8_nonregular_lock_paths_skip_without_following_links(self):
        slot = Path(self.verifier()["cwd"])
        path = slot.with_suffix(".lock")
        target = self.base / "lock-target"
        target.write_text("must stay untouched")
        for kind in ("directory", "symlink", "fifo"):
            with self.subTest(kind=kind):
                path.unlink(missing_ok=True)
                if kind == "directory":
                    path.mkdir()
                elif kind == "symlink":
                    path.symlink_to(target)
                else:
                    os.mkfifo(path)
                try:
                    # A subsequent lease must keep skipping an unsafe lock too.
                    for _ in range(2):
                        process = self.start_launcher()
                        try:
                            _, stderr = process.communicate(timeout=5)
                        except subprocess.TimeoutExpired:
                            process.terminate()
                            process.communicate(timeout=10)
                            self.fail("launcher blocked on non-regular lock path")
                        self.assertEqual(process.returncode, 0, stderr)
                        record = json.loads(self.record.read_text())
                        self.assertEqual(record["cwd"], str(slot.with_name("slot-2")))
                        reason = f"pool slot {slot} unusable: non-regular lock path"
                        self.assertIn(reason, stderr)
                        self.assertIn(reason, self.out.read_text())
                        self.assertEqual(target.read_text(), "must stay untouched")
                    with ExitStack() as held:
                        for number in range(2, 6):
                            lock = held.enter_context((slot.parent / f"slot-{number}.lock").open("a+b"))
                            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        record = self.verifier()
                        self.assertFalse(Path(record["cwd"]).is_relative_to(self.pool_root()))
                        self.assertIn("pool busy or slots unusable", self.out.read_text())
                        self.assertIn(reason, self.out.read_text())
                        self.assert_session_cleaned(record)
                finally:
                    if path.is_dir() and not path.is_symlink():
                        path.rmdir()
                    else:
                        path.unlink(missing_ok=True)

    def test_POOL_S8_reset_restores_empty_admin_refs(self):
        slot = Path(self.verifier()["cwd"])
        admin = Path(subprocess.check_output(
            ["git", "-C", str(slot), "rev-parse", "--absolute-git-dir"], text=True).strip())
        refs = admin / "refs"
        refs.mkdir(exist_ok=True)
        (refs / "leftover").write_text("discard local refs")
        self.verifier()
        self.assertTrue(refs.is_dir())
        self.assertEqual(list(refs.iterdir()), [])

    def test_POOL_S8_checkout_hooks_are_disabled_and_interrupted_reset_deep_cleans(self):
        slot = Path(self.verifier()["cwd"])
        self.hold_other_pool_slots(slot)
        hook = self.repo / ".git/hooks/post-checkout"
        marker = self.base / "failed-hook"
        hook.write_text(f'#!{sys.executable}\nfrom pathlib import Path\np = Path({str(marker)!r})\n'
                        'if not p.exists():\n    p.touch()\n    raise SystemExit(1)\n')
        hook.chmod(0o755)
        self.verifier()
        self.assertFalse(marker.exists())
        self.assertIn("warm reset", self.out.read_text())
        self.inject_launcher('''
import os, signal, subprocess
original = subprocess.check_output
def interrupt(command, *args, **kwargs):
    if "checkout" in command and "--force" in command:
        os.kill(os.getpid(), signal.SIGTERM)
    return original(command, *args, **kwargs)
subprocess.check_output = interrupt
''')
        self.round_decision()
        result = self.launch("verifier", "codex", "--author-family", "claude")
        self.assertEqual(result.returncode, 143, result.stderr)
        self.assertFalse(slot.with_suffix(".json").exists())
        (slot / "ignored").touch()
        self.env.pop("PYTHONPATH")
        self.verifier()
        self.assertFalse((slot / "ignored").exists())
        self.assertIn("missing or corrupt metadata", self.out.read_text())

    def test_POOL_R4_S9_sigkilled_launcher_stops_hung_agent_and_detached_children(self):
        for scope in (True, False):
            with self.subTest(scope=scope):
                self.check_killed_launcher(scope)

    def test_POOL_R4_S9_real_systemd_scope_stops_agent_after_launcher_is_killed(self):
        systemctl = shutil.which("systemctl")
        if not systemctl or not shutil.which("systemd-run"):
            self.skipTest("real systemd unavailable")
        probe = subprocess.run([systemctl, "--user", "is-system-running"], capture_output=True, text=True)
        if probe.stdout.strip() not in ("running", "degraded"):
            self.skipTest("real user systemd bus unavailable: " + probe.stderr.strip())
        self.check_killed_launcher("real")

    def check_killed_launcher(self, scope):
        self.configure_scope(present=bool(scope))
        if scope == "real":
            for name in ("systemd-run", "systemctl"):
                (self.bin / name).unlink()
                (self.bin / name).symlink_to(shutil.which(name))
        self.agent_extra('''
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True)
pathlib.Path(os.environ["RECORD"] + ".pids").write_text(str(os.getpid()) + " " + str(child.pid))
pathlib.Path(os.environ["RECORD"] + ".ready").touch()
while True:
    time.sleep(0.02)
''')
        self.addCleanup(self.kill_recorded_agents)
        original_record = self.base / f"killed-{scope}.json"
        process = self.start_launcher(record=original_record, out=self.base / f"killed-{scope}.md")
        record = self.wait_agent_ready(original_record)
        slot = Path(record["cwd"])
        session = Path(record["tmpdir"]).parent
        process.kill()
        process.wait(timeout=10)
        pids = list(map(int, Path(str(original_record) + ".pids").read_text().split()))
        deadline = time.monotonic() + 5
        released = False
        with slot.with_suffix(".lock").open("a+b") as lock:
            while time.monotonic() < deadline:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    pass
                else:
                    self.assertTrue(all(not Path(f"/proc/{pid}").exists() for pid in pids),
                                    "lease released before agent and descendants were reaped")
                    fcntl.flock(lock, fcntl.LOCK_UN)
                    if not session.exists():
                        released = True
                        break
                time.sleep(0.02)
        process.communicate(timeout=10)
        self.assertFalse(session.exists(), "orphan supervisor must clean the session")
        self.assertTrue(released, "orphan supervisor must release the slot")
        for pid in pids:
            self.assertFalse(Path(f"/proc/{pid}").exists(), "descendant survived lease release")
        with slot.with_suffix(".lock").open("a+b") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.agent_extra("")
        result = self.verifier()
        self.assertEqual(result["cwd"], record["cwd"])


    def test_POOL_S10_codex_git_only_instructions_snapshot_unborn_and_full_override(self):
        (self.repo / "AGENTS.md").write_text("root instructions")
        (self.repo / "CLAUDE.md").write_text("root Claude instructions")
        (self.repo / "nested").mkdir()
        (self.repo / "nested/AGENTS.md").write_text("nested instructions")
        self.git("add", ".")
        self.git("commit", "-qm", "instructions")
        (self.repo / "tracked").write_text("uncommitted")
        (self.repo / "new").write_text("forgotten snapshot")
        self.pool_probe()
        for role in ("challenger", "reviewer"):
            for unborn in (False, True):
                with self.subTest(role=role, unborn=unborn):
                    if unborn:
                        shutil.rmtree(self.repo / ".git")
                        self.git("init", "-q")
                    result = self.launch(role, "codex", "--author-family", "claude", "--full-checkout")
                    self.assertEqual(result.returncode, 0, result.stderr)
                    record = json.loads(self.record.read_text())
                    self.assertEqual(record["files"], [".git", "AGENTS.md", "CLAUDE.md"])
                    self.assertEqual(record["status"], "")
                    snapshot = record["head"]
                    self.assertEqual(self.git("show", snapshot + ":tracked"), "uncommitted")
                    self.assertEqual(self.git("show", snapshot + ":new"), "forgotten snapshot")
                    for command in (f"git show {snapshot}:<path>", f"git grep <pattern> {snapshot}", "git diff"):
                        self.assertIn(command, record["stdin"])
                    self.assertIn("Checkout: Git-only", self.out.read_text())
                    self.assert_session_cleaned(record)

    def test_POOL_P2_P3_warm_checkout_preserves_source_and_records_small_write_count(self):
        (self.repo / "large").write_bytes(b"large fixture" * (1024 * 1024))
        self.git("add", ".")
        self.git("commit", "-qm", "large checkout fixture")
        (self.repo / "new").write_text("untracked source")
        protected = [self.repo / name for name in ("tracked", "large", "new", ".git/index", ".git/config", ".git/HEAD")]
        before = {p: p.read_bytes() for p in protected}
        refs = self.git("show-ref")
        first = self.verifier()
        first_header = self.out.read_text().split("\n\n", 1)[0]
        large = Path(first["cwd"]) / "large"
        mtime = large.stat().st_mtime_ns
        second = self.verifier()
        self.assertEqual(second["cwd"], first["cwd"])
        self.assertEqual(large.stat().st_mtime_ns, mtime)
        header = self.out.read_text().split("\n\n", 1)[0]
        for line in first_header.splitlines():
            if not line.startswith(("Write bytes:", "Start UTC:", "End UTC:", "Checkout:", "Scope:")):
                self.assertIn(line, header)
        amount = next(line.removeprefix("Write bytes: ").removesuffix(" MiB") for line in header.splitlines()
                      if line.startswith("Write bytes:"))
        self.assertLess(float(amount), 3, header)
        self.assertEqual({p: p.read_bytes() for p in protected}, before)
        self.assertEqual(self.git("show-ref"), refs)
        self.assert_session_cleaned(first)
        self.assert_session_cleaned(second)

    def deletion_build(self, files, deleted, command, build_env, error_text, marker):
        for name, content in files.items():
            target = self.repo / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
        self.git("add", ".")
        self.git("commit", "-qm", "build deletion fixture")
        self.env["BUILD_COMMAND"] = json.dumps(command)
        self.env["BUILD_ENV"] = json.dumps(build_env)
        self.agent_extra('''
environment = dict(os.environ, **json.loads(os.environ["BUILD_ENV"]))
result = subprocess.run(json.loads(os.environ["BUILD_COMMAND"]), env=environment, capture_output=True)
(evidence / "build.txt").write_bytes(result.stdout + result.stderr)
sys.exit(result.returncode)
''')
        first = self.launch("verifier", "codex", "--author-family", "claude")
        output = (self.out.parent / "answer-evidence/build.txt").read_text()
        if "Could not determine a usable wildcard IP" in output:
            self.skipTest("Gradle cannot start in this sandbox: no usable wildcard IP")
        self.assertEqual(first.returncode, 0, output + first.stderr)
        slot = Path(json.loads(self.record.read_text())["cwd"])
        sentinel = slot / marker
        sentinel.parent.mkdir(parents=True, exist_ok=True)
        sentinel.touch()
        (self.repo / deleted).unlink()
        self.git("add", "-u")
        self.git("commit", "-qm", "delete still referenced source")
        second = self.launch("verifier", "codex", "--author-family", "claude")
        output = (self.out.parent / "answer-evidence/build.txt").read_text()
        self.assertNotEqual(second.returncode, 0, output)
        self.assertIn(error_text, output)
        self.assertEqual(json.loads(self.record.read_text())["cwd"], str(slot))
        self.assertTrue(sentinel.exists(), "the check must exercise warm output")
        self.assertIn("warm reset", self.out.read_text())
        self.assertFalse((slot / deleted).exists())

    def gradle_executable(self):
        gradle = shutil.which("gradle")
        if not gradle:
            installed = sorted((Path.home() / ".gradle/wrapper/dists").glob("gradle-*-bin/*/gradle-*/bin/gradle"))
            gradle = str(installed[-1]) if installed else ""
        if not gradle:
            self.skipTest("Gradle unavailable")
        return gradle

    def gradle_configuration_change(self, changed, content, error_text):
        gradle = self.gradle_executable()
        files = {
            ".gitignore": ".gradle/\nbuild/\n",
            "settings.gradle": "rootProject.name = 'configuration-check'\ninclude 'library', 'app'\n",
            "library/build.gradle": "plugins { id 'java-library' }\n",
            "library/src/main/java/fixture/Removed.java": "package fixture; public class Removed {}\n",
            "app/build.gradle": "plugins { id 'java' }\ndependencies { implementation project(':library') }\n",
            "app/src/main/java/Main.java": "import fixture.Removed; class Main { Removed value; }\n",
        }
        for name, source in files.items():
            target = self.repo / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(source)
        self.git("add", ".")
        self.git("commit", "-qm", "Gradle configuration fixture")
        command = [gradle, "--offline", "--no-daemon", "--max-workers=1",
                   "--gradle-user-home", str(self.base / "gradle-home"), ":app:compileJava"]
        self.env["BUILD_COMMAND"] = json.dumps(command)
        self.env["BUILD_ENV"] = json.dumps({"PATH": os.environ["PATH"]})
        self.agent_extra('''
result = subprocess.run(json.loads(os.environ["BUILD_COMMAND"]),
                        env=dict(os.environ, **json.loads(os.environ["BUILD_ENV"])), capture_output=True)
(evidence / "build.txt").write_bytes(result.stdout + result.stderr)
sys.exit(result.returncode)
''')
        first = self.launch("verifier", "codex", "--author-family", "claude")
        proof = self.out.parent / "answer-evidence/build.txt"
        output = proof.read_text()
        if "Could not determine a usable wildcard IP" in output:
            self.skipTest("Gradle cannot start in this sandbox: no usable wildcard IP")
        self.assertEqual(first.returncode, 0, output + first.stderr)
        slot = Path(json.loads(self.record.read_text())["cwd"])
        marker = slot / "app/build/warm-marker"
        marker.touch()
        (self.repo / changed).write_text(content)
        self.git("add", ".")
        self.git("commit", "-qm", "remove still referenced Gradle input")
        warm = self.launch("verifier", "codex", "--author-family", "claude")
        output = proof.read_text()
        self.assertNotEqual(warm.returncode, 0, output + warm.stderr)
        self.assertIn(error_text, output)
        self.assertEqual(json.loads(self.record.read_text())["cwd"], str(slot))
        self.assertTrue(marker.exists(), "the check must exercise warm output")
        self.assertIn("warm reset", self.out.read_text())
        # A shared clone gives the same committed candidate without any output.
        fresh = self.base / "fresh"
        subprocess.run(["git", "clone", "--quiet", "--shared", "--no-checkout", str(self.repo), str(fresh)],
                       env=self.env, check=True)
        subprocess.run(["git", "-C", str(fresh), "checkout", "--quiet", "--detach", self.git("rev-parse", "HEAD")],
                       env=self.env, check=True)
        result = subprocess.run(command, cwd=fresh, env=dict(self.env, PATH=os.environ["PATH"]),
                                text=True, capture_output=True)
        self.assertEqual(result.returncode, warm.returncode, result.stdout + result.stderr)
        self.assertIn(error_text, result.stdout + result.stderr)

    def test_POOL_R3_gradle_removed_task_output_can_pass_warm_but_fails_clean(self):
        gradle = self.gradle_executable()
        consumer = """tasks.register('consume') {
    doLast {
        assert file('build/generated/value').text == 'generated'
    }
}
"""
        producer = """tasks.register('generate') {
    outputs.file('build/generated/value')
    doLast {
        file('build/generated').mkdirs()
        file('build/generated/value').text = 'generated'
    }
}
tasks.named('consume') { dependsOn 'generate' }
"""
        (self.repo / ".gitignore").write_text(".gradle/\nbuild/\n")
        (self.repo / "settings.gradle").write_text("rootProject.name = 'removed-task-check'\n")
        (self.repo / "build.gradle").write_text(consumer + producer)
        self.git("add", ".")
        self.git("commit", "-qm", "Gradle output producer")
        self.env["BUILD_COMMAND"] = json.dumps([
            gradle, "--offline", "--no-daemon", "--max-workers=1",
            "--gradle-user-home", str(self.base / "gradle-home"), "consume"])
        self.env["BUILD_ENV"] = json.dumps({"PATH": os.environ["PATH"]})
        self.agent_extra('''
result = subprocess.run(json.loads(os.environ["BUILD_COMMAND"]),
                        env=dict(os.environ, **json.loads(os.environ["BUILD_ENV"])), capture_output=True)
(evidence / "build.txt").write_bytes(result.stdout + result.stderr)
sys.exit(result.returncode)
''')
        first = self.launch("verifier", "codex", "--author-family", "claude")
        proof = self.out.parent / "answer-evidence/build.txt"
        output = proof.read_text()
        if "Could not determine a usable wildcard IP" in output:
            self.skipTest("Gradle cannot start in this sandbox: no usable wildcard IP")
        self.assertEqual(first.returncode, 0, output + first.stderr)
        slot = Path(json.loads(self.record.read_text())["cwd"])
        self.hold_other_pool_slots(slot)
        (self.repo / "build.gradle").write_text(consumer)
        self.git("add", ".")
        self.git("commit", "-qm", "remove output producer")
        warm = self.launch("verifier", "codex", "--author-family", "claude")
        self.assertEqual(warm.returncode, 0, proof.read_text() + warm.stderr)
        self.assertIn("warm reset", self.out.read_text())
        self.assertTrue((slot / "build/generated/value").exists())
        self.round_decision()
        clean = self.launch("verifier", "codex", "--author-family", "claude", "--clean-checkout")
        self.assertNotEqual(clean.returncode, 0, proof.read_text() + clean.stderr)
        self.assertIn("build/generated/value", proof.read_text())
        self.assertIn("--clean-checkout", self.out.read_text())
        self.assertFalse((slot / "build/generated/value").exists())

    def test_POOL_R2_gradle_removed_dependency_fails_warm_and_fresh(self):
        self.gradle_configuration_change("app/build.gradle", "plugins { id 'java' }\n", "cannot find symbol")

    def test_POOL_R2_gradle_removed_settings_project_fails_warm_and_fresh(self):
        self.gradle_configuration_change("settings.gradle",
                                         "rootProject.name = 'configuration-check'\ninclude 'app'\n",
                                         "Project with path ':library' could not be found")

    def test_POOL_S12_cargo_deleted_module_cannot_pass_using_warm_output(self):
        cargo = shutil.which("cargo")
        rustc = shutil.which("rustc")
        if not cargo or not rustc:
            self.skipTest("Cargo/Rust unavailable")
        self.deletion_build({
            ".gitignore": "target/\n",
            "Cargo.toml": '[package]\nname = "deletion_check"\nversion = "0.1.0"\nedition = "2021"\n',
            "Cargo.lock": 'version = 4\n[[package]]\nname = "deletion_check"\nversion = "0.1.0"\n',
            "src/main.rs": "mod removed; fn main() { removed::value(); }\n",
            "src/removed.rs": "pub fn value() {}\n",
        }, "src/removed.rs", [cargo, "check", "--offline"], {
            "PATH": os.environ["PATH"], "RUSTC": rustc,
            "CARGO_HOME": str(self.base / "cargo-home"),
            "RUSTUP_HOME": os.environ.get("RUSTUP_HOME", str(Path.home() / ".rustup")),
        }, "file not found for module `removed`", "target/warm-marker")

    def test_POOL_S12_cargo_deleted_binary_cannot_run_from_stale_target(self):
        cargo, rustc = shutil.which("cargo"), shutil.which("rustc")
        if not cargo or not rustc:
            self.skipTest("Cargo/Rust unavailable")
        files = {
            ".gitignore": "target/\n",
            "Cargo.toml": '[package]\nname = "binary_check"\nversion = "0.1.0"\nedition = "2021"\n',
            "Cargo.lock": 'version = 4\n[[package]]\nname = "binary_check"\nversion = "0.1.0"\n',
            "src/main.rs": "fn main() {}\n",
            "src/bin/tool.rs": 'fn main() { println!("old executable runs"); }\n',
        }
        for name, content in files.items():
            target = self.repo / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
        self.git("add", ".")
        self.git("commit", "-qm", "Cargo binary fixture")
        self.env["BUILD_COMMAND"] = json.dumps([cargo, "build", "--bins", "--offline"])
        self.env["BUILD_ENV"] = json.dumps({
            "PATH": os.environ["PATH"], "RUSTC": rustc,
            "CARGO_HOME": str(self.base / "cargo-home"),
            "RUSTUP_HOME": os.environ.get("RUSTUP_HOME", str(Path.home() / ".rustup")),
        })
        self.agent_extra('''
result = subprocess.run(json.loads(os.environ["BUILD_COMMAND"]),
                        env=dict(os.environ, **json.loads(os.environ["BUILD_ENV"])), capture_output=True)
output = result.stdout + result.stderr
if result.returncode == 0:
    try:
        result = subprocess.run([str(cwd / "target/debug/tool")], capture_output=True)
        output += result.stdout + result.stderr
        code = result.returncode
    except FileNotFoundError:
        output += b"removed executable absent"
        code = 9
else:
    code = result.returncode
(evidence / "build.txt").write_bytes(output)
sys.exit(code)
''')
        first = self.launch("verifier", "codex", "--author-family", "claude")
        proof = self.out.parent / "answer-evidence/build.txt"
        self.assertEqual(first.returncode, 0, proof.read_text() + first.stderr)
        self.assertIn("old executable runs", proof.read_text())
        slot = Path(json.loads(self.record.read_text())["cwd"])
        self.hold_other_pool_slots(slot)
        self.assertTrue((slot / "target/debug/tool").is_file())
        (self.repo / "src/bin/tool.rs").unlink()
        self.git("add", "-u")
        self.git("commit", "-qm", "delete executable source")
        second = self.launch("verifier", "codex", "--author-family", "claude")
        self.assertEqual(second.returncode, 9, proof.read_text() + second.stderr)
        self.assertIn("removed executable absent", proof.read_text())
        self.assertFalse((slot / "target/debug/tool").exists())
        self.assertEqual(json.loads(self.record.read_text())["cwd"], str(slot))
        self.assertIn("deep clean: Cargo executable source removed", self.out.read_text())

    def test_POOL_S12_gradle_deleted_class_cannot_pass_using_warm_output(self):
        gradle = self.gradle_executable()
        self.deletion_build({
            ".gitignore": ".gradle/\nbuild/\n",
            "settings.gradle": "rootProject.name = 'deletion-check'\n",
            "build.gradle": "plugins { id 'java' }\n",
            "src/main/java/Main.java": "class Main { Removed value; }\n",
            "src/main/java/Removed.java": "class Removed {}\n",
        }, "src/main/java/Removed.java", [gradle, "--offline", "--no-daemon", "--max-workers=1",
                                           "--gradle-user-home", str(self.base / "gradle-home"), "compileJava"],
            {"PATH": os.environ["PATH"]}, "cannot find symbol", "build/warm-marker")

    def test_write_bytes_header_for_both_families(self):
        for family in ("codex", "claude"):
            for role in ("builder", "investigator", "verifier", "challenger", "reviewer"):
                with self.subTest(family=family, role=role):
                    result = self.launch(role, family, "--author-family",
                                         "claude" if family == "codex" else "codex")
                    self.assertEqual(result.returncode, 0, result.stderr)
                    lines = self.out.read_text().split("\n\n", 1)[0].splitlines()
                    start = next(i for i, line in enumerate(lines) if line.startswith("Start UTC:"))
                    self.assertRegex(lines[start - 1],
                                     r"^Write bytes: (\d+\.\d MiB|unavailable)$")
                    self.assertEqual(sum(line.startswith("Write bytes:") for line in lines), 1)

    def test_write_bytes_reports_counter_differences_in_mib(self):
        self.inject_launcher('''
            from pathlib import Path
            original = Path.read_text
            samples = iter(["write_bytes: 10485760\\n",
                            "write_bytes: 13107200\\n"])
            def read_text(path, *args, **kwargs):
                if path == Path("/proc/self/io"):
                    return next(samples)
                return original(path, *args, **kwargs)
            Path.read_text = read_text
        ''')
        for family in ("codex", "claude"):
            with self.subTest(family=family):
                result = self.launch(family=family)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("Write bytes: 2.5 MiB\nStart UTC:",
                              self.out.read_text())

    def test_write_bytes_unavailable_on_read_or_parse_failure_at_either_sample(self):
        self.env["PYTHONDONTWRITEBYTECODE"] = "1"
        good = "write_bytes: 1048576\n"
        for bad in ("OSError('unreadable')", "UnicodeError('invalid encoding')",
                    repr("write_bytes: invalid\n"),
                    repr("read_bytes: 1\n"), repr("malformed")):
            for sample in (0, 1):
                for family in ("codex", "claude"):
                    with self.subTest(bad=bad, sample=sample, family=family):
                        values = [repr(good), repr(good)]
                        values[sample] = bad
                        self.inject_launcher(f'''
                            from pathlib import Path
                            original = Path.read_text
                            samples = iter([{', '.join(values)}])
                            def read_text(path, *args, **kwargs):
                                if path == Path("/proc/self/io"):
                                    value = next(samples)
                                    if isinstance(value, Exception):
                                        raise value
                                    return value
                                return original(path, *args, **kwargs)
                            Path.read_text = read_text
                        ''')
                        result = self.launch(family=family)
                        self.assertEqual(result.returncode, 0, result.stderr)
                        self.assertIn("Write bytes: unavailable\nStart UTC:", self.out.read_text())

    def test_write_bytes_includes_reaped_child_fsynced_writes(self):
        if sys.platform != "linux" or not shutil.which("stat"):
            self.skipTest("disk write accounting needs Linux /proc and stat")
        filesystem = subprocess.check_output(
            ["stat", "-f", "-c", "%T", str(self.home)], text=True).strip()
        if filesystem in ("tmpfs", "ramfs"):
            self.skipTest(f"session cache is on {filesystem}; writes do not reach a real disk")
        self.agent_extra('''
subprocess.run([sys.executable, "-c",
                "import os; "
                "f = open('child-writes.bin', 'wb'); "
                "f.write(b'x' * (4 * 1024 * 1024)); f.flush(); os.fsync(f.fileno()); f.close()"],
               check=True)
''')
        for family in ("codex", "claude"):
            with self.subTest(family=family):
                result = self.launch(family=family)
                self.assertEqual(result.returncode, 0, result.stderr)
                lines = self.out.read_text().split("\n\n", 1)[0].splitlines()
                writes = [line for line in lines if line.startswith("Write bytes:")]
                self.assertEqual(len(writes), 1)
                if writes[0] == "Write bytes: unavailable":
                    self.skipTest("/proc/self/io write accounting is unavailable")
                self.assertRegex(writes[0], r"^Write bytes: \d+\.\d MiB$")
                self.assertGreaterEqual(float(writes[0].split()[2]), 4.0)

    def test_CE_S1_restricted_roles_disable_extensions_and_enabled_servers(self):
        names = ['plain', 'node_repl', 'dash-name', 'A0', '123']
        self.env["MCP_JSON"] = json.dumps(
            [{"name": name, "enabled": True} for name in names]
            + [{"name": "already-off", "enabled": False}])
        (self.repo / "AGENTS.md").write_text("Project instructions\n")
        for role in ("challenger", "reviewer"):
            with self.subTest(role=role):
                result = self.launch(role, "codex", "--author-family", "claude")
                self.assertEqual(result.returncode, 0, result.stderr)
                record = json.loads(self.record.read_text())
                args = record["argv"]
                self.assertEqual([args[i + 1] for i, a in enumerate(args) if a == "--disable"],
                                 ["plugins", "apps"])
                config = [args[i + 1] for i, a in enumerate(args) if a == "-c"]
                self.assertIn("skills.include_instructions=false", config)
                overrides = [c for c in config if c.startswith("mcp_servers.")]
                self.assertEqual(overrides, [f"mcp_servers.{name}.enabled=false" for name in names])
                self.assertTrue(record["stdin"].startswith(
                    (self.method / "roles" / f"{role}.md").read_text() + "\n---\n"))
                self.assertEqual(self.git("show", self.snapshot() + ":AGENTS.md"),
                                 "Project instructions")
                discovery = json.loads(Path(str(self.record) + ".discovery").read_text())
                self.assertEqual(discovery["cwd"], record["cwd"])
                self.assertEqual(discovery["tmpdir"], record["tmpdir"])
                self.assertIn("Requested environment: restricted\n", self.out.read_text())

    def snapshot(self):
        return next(line.split(": ", 1)[1] for line in self.out.read_text().splitlines()
                    if line.startswith("Snapshot commit:"))

    def test_CE_S2_discovery_is_role_scoped_and_native_commands_are_preserved(self):
        for family in ("codex", "claude"):
            for role in ("builder", "verifier", "investigator", "challenger", "reviewer"):
                if family == "codex" and role in ("challenger", "reviewer"):
                    continue
                with self.subTest(family=family, role=role):
                    result = self.launch(role, family, "--author-family",
                                         "claude" if family == "codex" else "codex")
                    self.assertEqual(result.returncode, 0, result.stderr)
                    record = json.loads(self.record.read_text())
                    writable = role in ("builder", "verifier")
                    evidence = str(Path(record["tmpdir"]).parent / "evidence")
                    if family == "codex":
                        expected = ["exec", "-m", "gpt-6.1-sol", "-c", "model_reasoning_effort=high",
                                    "-s", "workspace-write" if writable else "read-only",
                                    "-C", record["cwd"], "-o",
                                    str(Path(record["tmpdir"]).parent / "answer")]
                    else:
                        settings = {"permissions": {"deny": [] if role == "investigator" else
                                    [f"{tool}(/{self.private}/**)" for tool in ("Read", "Edit")]}}
                        tools = "Read,Grep,Glob,Bash,Write,Edit" if writable else "Read,Grep,Glob"
                        expected = ["-p", "--model", "claude-opus-5-5", "--output-format", "text",
                                    "--settings", json.dumps(settings), "--allowedTools", tools,
                                    "--tools", tools, "--strict-mcp-config"]
                    if role == "verifier":
                        expected += ["--add-dir", evidence]
                    if writable:
                        expected += ["--add-dir", record["tmpdir"]]
                    if family == "codex":
                        expected += ["-"]
                    self.assertEqual(record["argv"], expected)
                    self.assertFalse(Path(str(self.record) + ".discovery").exists())
        for role in ("challenger", "reviewer"):
            result = self.launch(role, "codex", "--author-family", "claude")
            self.assertEqual(result.returncode, 0, result.stderr)
            discovery = Path(str(self.record) + ".discovery")
            self.assertTrue(discovery.exists(), f"no discovery for {role}")
            discovery.unlink()

    def test_CE_S2_network_is_explicit_and_preserves_writable_roots(self):
        for role in ("builder", "verifier"):
            with self.subTest(role=role):
                cache = self.home / ".gradle"
                result = self.launch(role, "codex", "--author-family", "claude",
                                     "--network", "--writable", cache)
                self.assertEqual(result.returncode, 0, result.stderr)
                args = json.loads(self.record.read_text())["argv"]
                self.assertIn("sandbox_workspace_write.network_access=true", args)
                self.assertIn(str(cache), args)
                self.assertIn("Network: on\n", self.out.read_text())
                self.assertIn("Requested sandbox: workspace-write\n", self.out.read_text())
                self.assertIn("Requested environment: user configuration\n", self.out.read_text())
                self.assertFalse(Path(str(self.record) + ".discovery").exists())

    def test_CE_S4_network_refused_before_start_for_other_roles_and_family(self):
        for family in ("codex", "claude"):
            for role in ("builder", "verifier", "investigator", "challenger", "reviewer"):
                if family == "codex" and role in ("builder", "verifier"):
                    continue
                with self.subTest(family=family, role=role):
                    result = self.launch(role, family, "--network", "--author-family",
                                         "claude" if family == "codex" else "codex")
                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertIn("--network is", result.stderr)
                    self.assertFalse(self.record.exists())
                    self.assertFalse(Path(str(self.record) + ".discovery").exists())
                    self.assertFalse((self.home / ".cache/am").exists())

    def test_CE_S5_S7_executable_identity_for_path_wrapper_relative_and_symlink(self):
        original = self.base / "fake"
        link = self.bin / "codex-link"
        link.symlink_to(original)
        wrapper = self.base / "absolute-wrapper"
        wrapper.write_text("#!/bin/sh\nexec " + shlex.quote(sys.executable)
                           + " \"$WRAPPED_CLI\" \"$@\"\n")
        wrapper.chmod(0o755)
        self.env["WRAPPED_CLI"] = str(original)
        for family in ("codex", "claude"):
            for invoked, absolute in (("codex-link", link), (str(wrapper), wrapper),
                                      ("../fake", original), (str(link), link)):
                with self.subTest(family=family, invoked=invoked):
                    self.env[f"AGENT_{family.upper()}_CMD"] = invoked
                    result = self.launch("reviewer", family, "--author-family",
                                         "claude" if family == "codex" else "codex")
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn(f"Executable: {absolute}\n", self.out.read_text())
                    scope = next(p for p in self.scope_state.iterdir() if p.suffix != ".stop")
                    command = json.loads(scope.read_text())
                    self.assertEqual(command[command.index("--") + 1], str(absolute))
                    for path in self.scope_state.iterdir():
                        path.unlink()
                    if family == "codex":
                        discovery = json.loads(Path(str(self.record) + ".discovery").read_text())
                        if absolute != wrapper:
                            self.assertEqual(discovery["executable"], str(absolute))

    def test_CE_S6_discovery_failure_keeps_restrictions_and_explains_fallback(self):
        for output, exit_code, reason in (("[]", "9", "exited 9"),
                                         ("not JSON", "0", "invalid JSON"),
                                         ('{}', "0", "invalid server list"),
                                         ('[{}]', "0", "invalid server list"),
                                         ('[{"name":"a","enabled":1}]', "0", "invalid server list"),
                                         ('[{"name":3,"enabled":true}]', "0", "invalid server list")):
            with self.subTest(output=output, exit_code=exit_code):
                self.env.update(MCP_JSON=output, MCP_EXIT=exit_code,
                                MCP_STDERR="discovery diagnostic", SESSION_STDERR="agent diagnostic")
                result = self.launch("reviewer", "codex", "--author-family", "claude")
                self.assertEqual(result.returncode, 0, result.stderr)
                args = json.loads(self.record.read_text())["argv"]
                self.assertIn("skills.include_instructions=false", args)
                self.assertIn("plugins", args)
                self.assertIn("apps", args)
                self.assertFalse(any(a.startswith("mcp_servers.") for a in args))
                self.assertIn("MCP servers were not disabled", result.stderr)
                self.assertIn(reason, result.stderr)
                self.assertIn("Requested environment: restricted with MCP servers not disabled: discovery ",
                              self.out.read_text())
                self.assertIn(reason, self.out.read_text())
                self.assertEqual(self.out.with_suffix(".log").read_text(),
                                 "discovery diagnostic\nagent diagnostic\n")

    def test_CE_S6_empty_and_disabled_server_lists_are_successful(self):
        for output in ('[]', '[{"name":"off","enabled":false}]'):
            with self.subTest(output=output):
                self.env["MCP_JSON"] = output
                result = self.launch("challenger", "codex", "--author-family", "claude")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertNotIn("MCP servers were not disabled", result.stderr)
                self.assertIn("Requested environment: restricted\n", self.out.read_text())
                args = json.loads(self.record.read_text())["argv"]
                self.assertFalse(any(a.startswith("mcp_servers.") for a in args))

    def test_CE_S6_unsupported_server_names_are_skipped_and_reported(self):
        for name in ('dot.name', 'space name', 'quote"slash\\line\n\x00\x7f', 'café 😀', '', '\ud800'):
            with self.subTest(name=name):
                self.env["MCP_JSON"] = json.dumps([
                    {"name": "node_repl", "enabled": True},
                    {"name": name, "enabled": True}])
                result = self.launch("reviewer", "codex", "--author-family", "claude")
                self.assertEqual(result.returncode, 0, result.stderr)
                args = json.loads(self.record.read_text())["argv"]
                self.assertEqual([a for a in args if a.startswith("mcp_servers.")],
                                 ["mcp_servers.node_repl.enabled=false"])
                self.assertIn("skills.include_instructions=false", args)
                self.assertIn("plugins", args)
                self.assertIn("apps", args)
                note = f"MCP servers not all disabled: unsupported server names: {ascii(name)}"
                self.assertIn(f"Requested environment: restricted with {note}\n", self.out.read_text())
                self.assertIn(f"MCP servers were not all disabled: unsupported server names: {ascii(name)}",
                              result.stderr)

    def test_CE_S6_S9_discovery_timeout_kills_group_then_runs_agent(self):
        self.env.update(MCP_SLEEP="1", MCP_STDERR="discovery timeout diagnostic",
                        SESSION_STDERR="agent diagnostic")
        self.inject_launcher('''
import subprocess
original = subprocess.Popen.communicate
def communicate(self, *args, **kwargs):
    if self.args[1:] == ["mcp", "list", "--json"]:
        assert kwargs["timeout"] == 30
        kwargs["timeout"] = 0.3
    return original(self, *args, **kwargs)
subprocess.Popen.communicate = communicate
''')
        result = self.launch("reviewer", "codex", "--author-family", "claude")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("discovery timed out after 30 seconds", result.stderr)
        self.assertEqual(self.out.with_suffix(".log").read_text(),
                         "discovery timeout diagnostic\nagent diagnostic\n")
        self.assertIn("MCP servers not disabled", self.out.read_text())
        self.assert_discovery_cleaned()

    def test_CE_S6_discovery_is_bounded_by_short_session_timeout(self):
        self.env["MCP_SLEEP"] = "1"
        self.inject_launcher('''
import subprocess
original = subprocess.Popen.communicate
def communicate(self, *args, **kwargs):
    if self.args[1:] == ["mcp", "list", "--json"]:
        assert kwargs["timeout"] == 0.3, kwargs["timeout"]
    return original(self, *args, **kwargs)
subprocess.Popen.communicate = communicate
''')
        result = self.launch("reviewer", "codex", "--author-family", "claude", "--timeout", "0.3")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("discovery timed out after 0.3 seconds", result.stderr)
        self.assertIn("MCP servers not disabled", self.out.read_text())
        args = json.loads(self.record.read_text())["argv"]
        self.assertIn("skills.include_instructions=false", args)
        self.assertIn("plugins", args)
        self.assertIn("apps", args)
        self.assertFalse(any(a.startswith("mcp_servers.") for a in args))
        self.assert_discovery_cleaned()

    def assert_discovery_cleaned(self):
        discovery = json.loads(Path(str(self.record) + ".discovery").read_text())
        for pid in (discovery["pid"], int(Path(str(self.record) + ".discovery-child").read_text())):
            stat = Path(f"/proc/{pid}/stat")
            self.assertTrue(not stat.exists() or stat.read_text().split()[2] == "Z",
                            f"discovery process {pid} survived")
        self.assertFalse(Path(discovery["tmpdir"]).parent.exists())
        self.assertEqual(self.non_pool_worktrees().count("worktree "), 1)

    def test_CE_S9_failure_before_discovery_reports_it_did_not_run(self):
        for operation in (["write-tree"], ["worktree", "add"]):
            with self.subTest(operation=operation):
                self.inject_launcher(f"""
import subprocess
original = subprocess.check_output
def check_output(args, *positional, **kwargs):
    command = args[4:]
    while command[:1] == ["-c"]:
        command = command[2:]
    if args[0] == "git" and command[:{len(operation)}] == {operation!r}:
        raise subprocess.CalledProcessError(31, args)
    return original(args, *positional, **kwargs)
subprocess.check_output = check_output
""")
                result = self.launch("reviewer", "codex", "--author-family", "claude")
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("Requested environment: restricted; discovery did not run\n",
                              self.out.read_text())
                self.assertFalse(Path(str(self.record) + ".discovery").exists())
                self.assertFalse(self.record.exists())
                self.assertEqual(self.non_pool_worktrees().count("worktree "), 1)

    def test_CE_S9_interrupt_during_discovery_saves_header_and_cleans(self):
        self.env["MCP_SLEEP"] = "1"
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            with self.subTest(signal=sig):
                self.record = self.base / f"signal-{sig}.json"
                process = self.start_launcher(role="reviewer")
                self.wait_record(Path(str(self.record) + ".discovery"))
                deadline = time.monotonic() + 5
                while not Path(str(self.record) + ".discovery-child").exists():
                    self.assertLess(time.monotonic(), deadline)
                    time.sleep(0.01)
                process.send_signal(sig)
                _, stderr = process.communicate(timeout=10)
                self.assertEqual(process.returncode, 128 + sig, stderr)
                self.assertIn(f"Exit code: {128 + sig}\n", self.out.read_text())
                self.assertIn("Executable:", self.out.read_text())
                self.assertIn("discovery did not complete", self.out.read_text())
                self.assertFalse(self.record.exists())
                self.assert_discovery_cleaned()

    def test_CE_S9_environment_header_survives_every_agent_outcome(self):
        for outcome, code in (("success", 0), ("failure", 7), ("empty", 1), ("timeout", 124)):
            with self.subTest(outcome=outcome):
                self.env.update(SESSION_EXIT="7" if outcome == "failure" else "0",
                                NO_ANSWER="1" if outcome == "empty" else "",
                                SLEEP="1" if outcome == "timeout" else "")
                result = self.launch("reviewer", "codex", "--author-family", "claude",
                                     *(("--timeout", "0.3") if outcome == "timeout" else ()))
                self.assertEqual(result.returncode, code, result.stderr)
                header = self.out.read_text()
                self.assertIn(f"Executable: {self.env['AGENT_CODEX_CMD']}\n", header)
                self.assertIn("Requested sandbox: read-only\n", header)
                self.assertIn("Network: not requested (user config applies)\n", header)
                self.assertIn("Requested environment: restricted\n", header)
                self.assertIn(f"Exit code: {code}\n", header)
                self.assertEqual(self.non_pool_worktrees().count("worktree "), 1)

    def leave_out_project(self):
        body = (self.method / "AGENTS.md").read_text().replace("{{PROJECT}}", "demo").partition("\n")[2]
        (self.repo / "AGENTS.md").write_text(
            "<!-- Agent System entry for project demo. -->\n" + body)
        (self.repo / "models").mkdir()
        (self.repo / "models/weights").write_bytes(b"weights" * 1024)
        (self.repo / "models-old").write_text("keep")
        self.git("add", ".")
        self.git("commit", "-qm", "leave-out fixture")
        path = self.private / "projects/demo/review-leave-out.txt"
        path.parent.mkdir(parents=True)
        path.write_text("# expensive files\nmodels/\n")
        self.agent_extra('''
record["models_present"] = (cwd / "models/weights").exists()
record["status"] = subprocess.check_output(["git", "status", "--porcelain"], text=True)
record["files"] = [str(p.relative_to(cwd)) for p in cwd.rglob("*") if p.is_file()]
pathlib.Path(os.environ["RECORD"]).write_text(json.dumps(record))
''')
        return path

    def review_checkout(self, *extra, role="reviewer", family="claude"):
        result = self.launch(role, family, "--author-family",
                             "claude" if family == "codex" else "codex", *extra)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(self.record.read_text())

    def test_LO_S1_S3_omitted_checkout_prompt_header_and_git_contents(self):
        self.leave_out_project()
        pinned = self.git("rev-parse", "HEAD")
        self.agent_extra('''
assert not (cwd / "models").exists(), "left-out directory was materialized"
assert subprocess.check_output(["git", "show", "HEAD:models/weights"]) == b"weights" * 1024
assert subprocess.check_output(["git", "status", "--porcelain"]) == b""
assert (cwd / "models-old").read_text() == "keep"
''')
        for role in ("reviewer", "challenger"):
            for family in ("claude",):
                with self.subTest(role=role, family=family):
                    record = self.review_checkout(role=role, family=family)
                    header = self.out.read_text().split("\n\n", 1)[0]
                    self.assertIn("left-out paths ['models']", header)
                    snapshot = next(line.split(": ", 1)[1] for line in header.splitlines()
                                    if line.startswith("Snapshot commit:"))
                    self.assertEqual(self.git("rev-parse", snapshot + "^{tree}"),
                                     self.git("rev-parse", pinned + "^{tree}"))
                    prompt = record["stdin"]
                    for phrase in ("left-out paths ['models']", "absent from the file system",
                                   "present in Git",
                                   "If your check needs them", "say so in your answer"):
                        self.assertIn(phrase, prompt)
                    self.assertEqual(f"git show {snapshot}:<path>" in prompt, family == "codex")
                    self.assert_session_cleaned(record)

    def test_LO_review_fix_protects_root_instructions(self):
        path = self.leave_out_project()
        protected = ["AGENTS.md", "CLAUDE.md", ".claude/agents/reviewer.md", ".codex/config.toml"]
        for name in protected[1:] + ["docs/AGENTS.md"]:
            target = self.repo / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("instructions\n")
        self.git("add", ".")
        self.git("commit", "-qm", "instruction fixtures")
        entries = ["AGENTS.md", "./AGENTS.md", "CLAUDE.md", ".claude/", ".codex",
                   ".claude/agents/reviewer.md", "./.codex/config.toml"]
        path.write_text("\n".join([*entries, "models", "docs/AGENTS.md"]) + "\n")
        result = self.launch("reviewer", "claude", "--author-family", "codex")
        self.assertEqual(result.returncode, 0, result.stderr)
        for entry in entries:
            self.assertIn(f"Warning: ignoring invalid leave-out path {entry!r}", result.stderr)
        record = json.loads(self.record.read_text())
        for name in protected:
            self.assertIn(name, record["files"])
        self.assertFalse(record["models_present"])
        self.assertNotIn("docs/AGENTS.md", record["files"])
        self.assertIn("Checkout: left-out paths ['models', 'docs/AGENTS.md']", record["stdin"])

    def test_LO_review_fix_git_show_hint_only_for_codex(self):
        self.leave_out_project()
        for role in ("challenger", "reviewer"):
            for family in ("claude",):
                with self.subTest(role=role, family=family):
                    record = self.review_checkout(role=role, family=family)
                    self.assertFalse(record["models_present"])
                    self.assertIn("If your check needs them, say so in your answer.", record["stdin"])
                    self.assertEqual("git show " in record["stdin"], family == "codex")

    def test_LO_review_fix_old_git_reports_full_checkout(self):
        self.leave_out_project()
        wrapper = self.bin / "git"
        real_git = str(wrapper.resolve())
        marker = self.base / "old-git-checkout"
        wrapper.unlink()
        wrapper.write_text(
            "#!/usr/bin/env python3\nimport os, pathlib, sys\nargs = sys.argv[1:]\n"
            "if 'checkout-index' in args:\n"
            "    args.append('--ignore-skip-worktree-bits')\n"
            f"    pathlib.Path({str(marker)!r}).touch()\n"
            f"os.execv({real_git!r}, [{real_git!r}, *args])\n")
        wrapper.chmod(0o755)
        for family in ("claude",):
            with self.subTest(family=family):
                marker.unlink(missing_ok=True)
                record = self.review_checkout(family=family)
                self.assertTrue(marker.exists())
                self.assertTrue(record["models_present"])
                self.assertEqual(record["status"], "")
                for text in (record["stdin"], self.out.read_text()):
                    self.assertIn("Checkout: full because left-out paths were checked out anyway "
                                  "(Git older than 2.37?)", text)
                    self.assertNotIn("Checkout: left-out paths", text)
                self.assertNotIn("absent from the file system", record["stdin"])
                self.assertNotIn("git show ", record["stdin"])
                self.assertNotIn("If your check needs them", record["stdin"])

    def test_LO_review_fix_full_checkout_never_reads_list(self):
        self.leave_out_project()
        marker = self.base / "list-read"
        self.inject_launcher(f'''
import pathlib
original = pathlib.Path.read_text
def denied(path, *args, **kwargs):
    if path.name == "review-leave-out.txt":
        pathlib.Path({str(marker)!r}).touch()
        raise PermissionError("leave-out list must not be read")
    return original(path, *args, **kwargs)
pathlib.Path.read_text = denied
''')
        for role in ("challenger", "reviewer"):
            with self.subTest(role=role):
                result = self.launch(role, "claude", "--author-family", "codex", "--full-checkout")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertFalse(marker.exists(), "--full-checkout read the leave-out list")
                self.assertNotIn("Warning:", result.stderr)
                record = json.loads(self.record.read_text())
                self.assertTrue(record["models_present"])
                self.assertIn("Checkout: full (--full-checkout)", record["stdin"])
                self.assertNotIn("absent from the file system", record["stdin"])

    def test_LO_S8_subdirectory_repo_uses_root_relative_leave_out_paths(self):
        path = self.leave_out_project()
        (self.repo / "big").mkdir()
        (self.repo / "big/weights").write_text("omit")
        sub = self.repo / "sub"
        sub.mkdir()
        (sub / "kept").write_text("keep")
        shutil.copyfile(self.repo / "AGENTS.md", sub / "AGENTS.md")
        self.git("add", ".")
        self.git("commit", "-qm", "subdirectory leave-out fixture")
        path.write_text("big\n")
        self.agent_extra('''
record["big_present"] = (cwd / "big").exists()
record["sub_content"] = (cwd / "sub/kept").read_text()
pathlib.Path(os.environ["RECORD"]).write_text(json.dumps(record))
''')
        record = self.review_checkout("--repo", sub)
        header = self.out.read_text().split("\n\n", 1)[0]
        self.assertIn("Checkout: left-out paths ['big']", header)
        self.assertIn("Checkout: left-out paths ['big']", record["stdin"])
        self.assertFalse(record["big_present"])
        self.assertEqual(record["sub_content"], "keep")

    def test_LO_S2_snapshot_changes_force_full_checkout(self):
        self.leave_out_project()
        for kind in ("add", "modify", "delete", "rename-in", "rename-out", "type"):
            for staged in (False, True):
                with self.subTest(kind=kind, staged=staged):
                    self.git("reset", "--hard", "HEAD")
                    self.git("clean", "-fd")
                    weights = self.repo / "models/weights"
                    if kind == "add":
                        (self.repo / "models/added").write_text("addition")
                    elif kind == "modify":
                        weights.write_text("changed")
                    elif kind == "delete":
                        weights.unlink()
                    elif kind == "rename-in":
                        (self.repo / "models-old").rename(self.repo / "models/renamed")
                    elif kind == "rename-out":
                        weights.rename(self.repo / "moved")
                    else:
                        weights.unlink()
                        weights.symlink_to("../tracked")
                    if staged:
                        self.git("add", "-A")
                    record = self.review_checkout()
                    for text in (self.out.read_text(), record["stdin"]):
                        self.assertIn("full because snapshot changes touch leave-out paths", text)
                        self.assertIn("models", text)
                    self.assertNotIn("absent from the file system", record["stdin"])
                    self.assertEqual(record["status"], "")
                    if kind not in ("delete", "rename-out"):
                        self.assertTrue(record["models_present"])

    def test_LO_S2_unborn_and_untracked_size_rule(self):
        self.leave_out_project()
        huge = self.repo / "models/huge"
        huge.write_bytes(b"h" * (5 * 1024 * 1024 + 1))
        record = self.review_checkout()
        self.assertFalse(record["models_present"])
        self.assertIn("Skipped untracked (>5 MB): models/huge", self.out.read_text())
        huge.write_bytes(b"h" * (5 * 1024 * 1024))
        record = self.review_checkout()
        self.assertTrue(record["models_present"])
        self.assertIn("full because snapshot changes touch", self.out.read_text())
        huge.unlink()
        shutil.rmtree(self.repo / ".git")
        self.git("init", "-q")
        record = self.review_checkout()
        self.assertTrue(record["models_present"])
        self.assertIn("full because snapshot changes touch", self.out.read_text())

    def test_LO_S4_no_list_or_entry_keeps_full_checkout(self):
        path = self.leave_out_project()
        entry = self.repo / "AGENTS.md"
        original = entry.read_text()
        for case in ("no-header", "no-entry", "no-list", "empty", "comments", "no-match"):
            with self.subTest(case=case):
                entry.write_text(original)
                path.write_text("models\n")
                if case == "no-header":
                    entry.write_text("a foreign entry\n")
                elif case == "no-entry":
                    entry.unlink()
                elif case == "no-list":
                    path.unlink()
                else:
                    path.write_text({"empty": "", "comments": "\n# models\n   \n",
                                     "no-match": "nonexistent\n"}[case])
                record = self.review_checkout()
                self.assertTrue(record["models_present"])
                self.assertNotIn("Checkout:", self.out.read_text())
                self.assertNotIn("absent from the file system", record["stdin"])

    def test_LO_S5_unreadable_or_invalid_utf8_list_warns_and_uses_full_checkout(self):
        path = self.leave_out_project()
        for case in ("utf8", "directory", "permission"):
            with self.subTest(case=case):
                if path.is_dir():
                    path.rmdir()
                path.write_bytes(b"\xff" if case == "utf8" else b"models\n")
                if case == "directory":
                    path.unlink()
                    path.mkdir()
                if case == "permission":
                    self.inject_launcher('''
import pathlib
original = pathlib.Path.read_text
def denied(path, *args, **kwargs):
    if path.name == "review-leave-out.txt":
        raise PermissionError("denied test list")
    return original(path, *args, **kwargs)
pathlib.Path.read_text = denied
''')
                result = self.launch("reviewer", "claude", "--author-family", "codex")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("Warning: cannot read", result.stderr)
                self.assertIn("review-leave-out.txt", result.stderr)
                self.assertIn("using a full checkout", result.stderr)
                self.assertTrue(json.loads(self.record.read_text())["models_present"])

    def test_LO_S6_other_roles_keep_full_checkout(self):
        self.leave_out_project()
        for role in ("builder", "investigator", "verifier"):
            with self.subTest(role=role):
                record = self.review_checkout(role=role)
                self.assertTrue(record["models_present"])
                if role != "verifier":
                    self.assertNotIn("Checkout:", self.out.read_text())
                self.assertNotIn("absent from the file system", record["stdin"])
                if role != "verifier":
                    self.assertEqual(Path(record["cwd"]), self.repo)
                else:
                    self.assertNotEqual(Path(record["cwd"]), self.repo)

    def test_LO_S7_repository_and_existing_sparse_config_preserved(self):
        self.leave_out_project()
        # Exercise a dirty index/worktree, shared sparse config, and worktree config.
        (self.repo / "tracked").write_text("staged")
        self.git("add", "tracked")
        (self.repo / "tracked").write_text("working")
        (self.repo / "new").write_text("untracked")
        for config in ("plain", "shared-sparse", "worktree-config", "linked"):
            with self.subTest(config=config):
                if config == "shared-sparse":
                    self.git("config", "core.sparseCheckout", "true")
                    (self.repo / ".git/info/sparse-checkout").write_text("/*\n")
                if config == "worktree-config":
                    self.git("config", "extensions.worktreeConfig", "true")
                    self.git("config", "--worktree", "core.sparseCheckoutCone", "false")
                if config == "linked":
                    linked = self.base / "linked"
                    self.git("worktree", "add", "--detach", str(linked), "HEAD")
                    self.repo = linked
                common = Path(self.git("rev-parse", "--path-format=absolute", "--git-common-dir"))
                admin = Path(self.git("rev-parse", "--absolute-git-dir"))
                protected = [common / "config", admin / "config.worktree", admin / "index",
                             admin / "HEAD", common / "info/sparse-checkout",
                             *list((common / "refs").rglob("*"))]
                before = {p: p.read_bytes() if p.is_file() else None for p in protected}
                files = {p: p.read_bytes() for p in self.repo.rglob("*")
                         if p.is_file() and ".git" not in p.relative_to(self.repo).parts}
                refs = self.git("show-ref")
                registrations = self.non_pool_worktrees()
                record = self.review_checkout()
                self.assertEqual(record["status"], "")
                self.assertEqual({p: p.read_bytes() if p.is_file() else None for p in protected}, before)
                self.assertEqual({p: p.read_bytes() for p in files}, files)
                self.assertEqual(self.git("show-ref"), refs)
                self.assertEqual(self.non_pool_worktrees(), registrations)

    def test_LO_S7_setup_failure_and_interrupt_remove_worktree(self):
        self.leave_out_project()
        registrations = self.non_pool_worktrees()
        marker = self.base / "setup-failed"
        for operation in ("worktree", "read-tree", "update-index", "checkout-index"):
            for outcome in ("failure", "interrupt"):
                with self.subTest(operation=operation, outcome=outcome):
                    marker.unlink(missing_ok=True)
                    self.inject_launcher(f'''
import os, pathlib, signal, subprocess
original = subprocess.check_output
def fail(command, *args, **kwargs):
    if (({operation!r} == 'worktree' and 'worktree' in command and 'add' in command)
            or ({operation!r} != 'worktree' and {operation!r} in command and '/work' in command[3])):
        if {operation!r} == 'worktree':
            original(command, *args, **kwargs)
        pathlib.Path({str(marker)!r}).touch()
        if {outcome!r} == 'interrupt':
            os.kill(os.getpid(), signal.SIGTERM)
        raise subprocess.CalledProcessError(23, command)
    return original(command, *args, **kwargs)
subprocess.check_output = fail
''')
                    result = self.launch("reviewer", "claude", "--author-family", "codex")
                    # The baseline never uses these setup commands, so this is also a
                    # preservation check of cleanup on whatever path it exercises.
                    expected = (2 if outcome == "failure" else 143) if marker.exists() else 0
                    self.assertEqual(result.returncode, expected, result.stderr)
                    if expected == 2:
                        self.assertIn("Result: launch refused\n", self.out.read_text())
                        self.assertIn("returned non-zero exit status 23", self.out.read_text())
                    if operation == "worktree":
                        self.assertTrue(marker.exists())
                    self.assertEqual(self.non_pool_worktrees(), registrations)
                    self.assertEqual(list((self.home / ".cache/am").iterdir()), [])

    def test_LO_S7_concurrent_checkouts_do_not_interfere(self):
        self.leave_out_project()
        self.env["SLEEP"] = "1"
        registrations = self.non_pool_worktrees()
        records = [self.base / f"review-{i}.json" for i in range(2)]
        processes = [self.start_launcher(role="reviewer", record=record,
                                         out=self.base / f"review-{i}.md")
                     for i, record in enumerate(records)]
        data = [self.wait_record(record) for record in records]
        processes[0].terminate()
        _, stderr = processes[0].communicate(timeout=10)
        self.assertEqual(processes[0].returncode, 143, stderr)
        self.assertFalse(Path(data[0]["cwd"]).exists())
        self.assertIsNone(processes[1].poll())
        self.assertTrue(Path(data[1]["cwd"]).is_dir())
        processes[1].terminate()
        _, stderr = processes[1].communicate(timeout=10)
        self.assertEqual(processes[1].returncode, 143, stderr)
        self.assertEqual(self.non_pool_worktrees(), registrations)

    def test_LO_S8_literal_paths_and_directory_boundaries(self):
        path = self.leave_out_project()
        names = ["space file", "hash#file", "literal[ab]", " leading space ",
                 "models/line\nbreak", "models/byte-\udcff", "models/\tleading"]
        for name in names:
            (self.repo / name).write_text("payload")
        (self.repo / "literala").write_text("keep wildcard lookalike")
        self.git("add", ".")
        self.git("commit", "-qm", "literal path fixtures")
        for directory in ("models", "models/", "./models/"):
            with self.subTest(directory=directory):
                path.write_text("\n".join([directory, names[0] + "/", *names[1:4],
                                            "# comment", "nothing"]) + "\n")
                record = self.review_checkout()
                self.assertFalse(record["models_present"])
                for name in names:
                    self.assertNotIn(name, record["files"])
                self.assertIn("models-old", record["files"])
                self.assertIn("literala", record["files"])
                self.assertEqual(record["status"], "")

    def test_LO_S8_invalid_paths_warn_and_cannot_omit_outside_repository(self):
        path = self.leave_out_project()
        outside = self.base / "outside"
        outside.write_text("keep outside")
        invalid = [str(outside), "/models", ".", "./", "/", "models/../tracked", "../outside"]
        path.write_text("\n".join(invalid) + "\n")
        result = self.launch("reviewer", "claude", "--author-family", "codex")
        self.assertEqual(result.returncode, 0, result.stderr)
        for name in invalid:
            self.assertIn(f"ignoring invalid leave-out path {name!r}", result.stderr)
        self.assertTrue(json.loads(self.record.read_text())["models_present"])
        self.assertEqual(outside.read_text(), "keep outside")

    def test_LO_S9_full_checkout_override_records_reason(self):
        self.leave_out_project()
        for role in ("challenger", "reviewer"):
            with self.subTest(role=role):
                record = self.review_checkout("--full-checkout", role=role)
                self.assertTrue(record["models_present"])
                self.assertIn("Checkout: full (--full-checkout)", self.out.read_text())
                self.assertIn("Checkout: full (--full-checkout)", record["stdin"])

    def test_LO_S10_installed_default_and_explicit_private_select_list(self):
        path = self.leave_out_project()
        result = self.setup_project()
        self.assertEqual(result.returncode, 0, result.stderr)
        other = self.home / "other-private"
        other_list = other / "projects/demo/review-leave-out.txt"
        other_list.parent.mkdir(parents=True)
        other_list.write_text("models-old\n")
        for root in (None, other):
            with self.subTest(private=root):
                result = subprocess.run(
                    [sys.executable, str(self.method / "run.py"), "--role", "reviewer",
                     "--family", "claude", "--author-family", "codex", "--repo", str(self.repo),
                     "--brief", str(self.brief), "--out", str(self.out),
                     *(["--private", str(root)] if root else [])],
                    env=self.env, cwd=self.repo, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                record = json.loads(self.record.read_text())
                self.assertEqual(record["models_present"], root is not None)
                self.assertEqual("models-old" in record["files"], root is None)
        self.assertEqual(path.read_text(), "# expensive files\nmodels/\n")

    def test_LO_S10_id_validation_and_setup_never_touches_list(self):
        path = self.leave_out_project()
        for project_id in (".", "..", "../demo", "/demo", "bad id", "bad/id", "demo./escape"):
            with self.subTest(project_id=project_id):
                (self.repo / "AGENTS.md").write_text(
                    f"<!-- Agent System entry for project {project_id}. -->\n")
                record = self.review_checkout()
                self.assertTrue(record["models_present"])
        for existing in (False, True):
            with self.subTest(existing=existing):
                (self.repo / "AGENTS.md").unlink()
                path.unlink(missing_ok=True)
                if existing:
                    path.write_bytes(b"\xff user content\n")
                before = (path.read_bytes(), path.stat().st_mtime_ns) if existing else None
                result = self.setup_project()
                self.assertEqual(result.returncode, 0, result.stderr)
                after = (path.read_bytes(), path.stat().st_mtime_ns) if path.exists() else None
                self.assertEqual(after, before)

    def test_LO_S11_verification_guide_reuses_extra_checkouts(self):
        guide = (ROOT / "product/guides/verification.md").read_text()
        self.assertIn("Make each extra checkout once and reuse it between cases", guide)
        self.assertIn("resetting it between cases", guide)
        self.assertIn("never\n  create a checkout per case", guide)

    def test_LO_S12_left_out_checkout_skips_post_checkout_hook(self):
        self.leave_out_project()
        hook = self.repo / ".git/hooks/post-checkout"
        hook.write_text("#!/bin/sh\nexit 1\n")
        hook.chmod(0o755)
        record = self.review_checkout()
        self.assertFalse(record["models_present"])
        self.assertEqual(record["status"], "")

    def test_LO_S12_readme_documents_list_override_and_hook(self):
        readme = (ROOT / "README.md").read_text()
        for phrase in ("review-leave-out.txt", "--full-checkout", "skips the Git post-checkout hook"):
            self.assertIn(phrase, readme)

    def test_POOL_R7_readme_documents_orphan_pool_cleanup_and_full_fallback(self):
        readme = (ROOT / "README.md").read_text()
        for phrase in ("full temporary checkout", "du -sh <cache>/am-pool/*",
                       "Moved, renamed or deleted repositories", "never reclaimed automatically",
                       "git -C /path/to/project worktree prune"):
            self.assertIn(phrase, readme)

    def test_S4_old_python_refused_before_writes_or_launch(self):
        for script in ("setup.py", "run.py"):
            path = ROOT / "scripts" / script
            ast.parse(path.read_text(), feature_version=(3, 9))
            args = (["--project", self.repo, "--id", "demo", "--private", self.private,
                     "--method", self.method] if script == "setup.py" else
                    ["--role", "builder", "--family", "codex", "--brief", self.brief,
                     "--out", self.out, "--repo", self.repo, "--method", self.method])
            for minor in range(9, 13):
                with self.subTest(script=script, minor=minor):
                    before = self.files()
                    result = subprocess.run(
                        [sys.executable, "-c",
                         "import sys; sys.version_info = (3, " + str(minor) + ", 0); "
                         "path = sys.argv.pop(1); "
                         "exec(compile(open(path).read(), path, 'exec'), "
                         "{'__name__': '__main__', '__file__': path})",
                         str(path), *map(str, args)], env=self.env, cwd=self.repo,
                        capture_output=True, text=True)
                    self.assertIn("requires Python 3.13", result.stderr)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertEqual(self.files(), before)
                    self.assertFalse(self.record.exists())

    def test_S5_installed_private_default_reaches_all_readers(self):
        self.private = self.home / "custom 'private' folder"
        result = self.setup_project()
        self.assertEqual(result.returncode, 0, result.stderr)

        def installed(family="codex", *extra):
            return subprocess.run(
                [sys.executable, str(self.method / "run.py"), "--role", "builder",
                 "--family", family, "--brief", str(self.brief), "--out", str(self.out),
                 "--repo", str(self.repo), *map(str, extra)],
                env=self.env, cwd=self.repo, text=True, capture_output=True)

        self.brief.write_text("Do not read " + str(self.private))
        result = installed()
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("brief contains the private folder path", result.stderr)
        self.assertFalse(self.record.exists())
        for form in ("~/custom 'private' folder", str(self.private)):
            self.brief.write_text("Do not read " + form)
            result = installed()
            self.assertEqual(result.returncode, 2, result.stderr)
        self.brief.write_text("bounded assignment\n")
        result = installed("codex", "--writable", self.private / "child")
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("unsafe --writable root", result.stderr)

        for root in (self.private, self.home / "explicit private"):
            extra = () if root == self.private else ("--private", root)
            result = installed("claude", *extra)
            self.assertEqual(result.returncode, 0, result.stderr)
            args = json.loads(self.record.read_text())["argv"]
            settings = json.loads(args[args.index("--settings") + 1])
            self.assertEqual(settings["permissions"]["deny"],
                             [f"{tool}(/{root}/**)" for tool in ("Read", "Edit")])

        self.brief.write_text("Do not read " + str(self.private))
        result = installed("codex", "--private", self.home / "explicit private")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.brief.write_text("bounded assignment\n")
        entry = self.repo / "AGENTS.md"
        original = entry.read_text()
        entry.write_text(original + "hand edit\n")
        for root in (self.private, self.home / "explicit private"):
            extra = () if root == self.private else ("--private", root)
            result = installed("codex", *extra)
            self.assertEqual(result.returncode, 0, result.stderr)
            command = shlex.join(["python3", str(ROOT / "scripts/setup.py"), "--project",
                                  str(self.repo), "--id", "demo", "--method", str(self.method),
                                  "--private", str(root), "--dry-run"])
            self.assertIn(command, result.stderr)
        entry.write_text(original)

        first_repo = self.repo
        second_repo = self.base / "second"
        second_repo.mkdir()
        self.repo = second_repo
        self.git("init", "-q")
        (self.repo / "tracked").write_text("original")
        result = self.setup_project("--id", "second")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.repo = first_repo
        result = self.setup_project()
        self.assertEqual(result.returncode, 0, result.stderr)
        for repo in (first_repo, second_repo):
            self.repo = repo
            result = installed("claude")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stderr, "")
            args = json.loads(self.record.read_text())["argv"]
            self.assertIn(str(self.private), args[args.index("--settings") + 1])

        # Omitting --private at setup retains the usual home-relative behavior.
        self.private = self.home / "dev/agent-private"
        result = self.run_script("setup.py", "--project", self.repo, "--id", "second",
                                 "--method", self.method)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.brief.write_text("Do not read ~/dev/agent-private")
        result = installed()
        self.assertEqual(result.returncode, 2, result.stderr)

    def test_S6_setup_builder_model_override_and_default(self):
        for model in ("custom-claude-model", None):
            with self.subTest(model=model):
                if model is None:
                    self.env.pop("AGENT_CLAUDE_MODEL", None)
                else:
                    self.env["AGENT_CLAUDE_MODEL"] = model
                result = self.setup_project()
                self.assertEqual(result.returncode, 0, result.stderr)
                for path in (self.method / "claude-builder.md",
                             self.repo / ".claude/agents/builder.md"):
                    self.assertIn(f"\nmodel: {model or 'claude-opus-5-5'}\n---\n", path.read_text())

    def test_S7_codex_effort_override_default_and_cli_rejection(self):
        self.agent_extra('''
if "model_reasoning_effort=unsupported-test-value" in args:
    print("unsupported reasoning effort", file=sys.stderr)
    sys.exit(23)
''')
        for effort, code in (("low", 0), (None, 0), ("unsupported-test-value", 23)):
            with self.subTest(effort=effort):
                if effort is None:
                    self.env.pop("AGENT_CODEX_EFFORT", None)
                else:
                    self.env["AGENT_CODEX_EFFORT"] = effort
                result = self.launch()
                self.assertEqual(result.returncode, code, result.stderr)
                args = json.loads(self.record.read_text())["argv"]
                self.assertEqual(args[args.index("-c") + 1],
                                 f"model_reasoning_effort={effort or 'high'}")
                if code:
                    self.assertIn("unsupported reasoning effort", result.stderr)

    def test_S8_empty_answers_fail_keep_artifacts_and_agent_exit(self):
        self.env.update(EVIDENCE="1", SESSION_STDERR="agent diagnostic")
        for family in ("codex", "claude"):
            for answer in (None, "", " \t\r\n", "\u2003\u00a0"):
                for exit_code in (0, 7):
                    with self.subTest(family=family, answer=answer, exit_code=exit_code):
                        self.env.update(SESSION_ANSWER=answer or "", SESSION_EXIT=str(exit_code))
                        self.env["NO_ANSWER"] = "1" if answer is None else ""
                        self.out.parent.mkdir(parents=True, exist_ok=True)
                        self.out.write_text("answer left over from an earlier run\n")
                        result = self.launch("verifier", family, "--author-family",
                                             "claude" if family == "codex" else "codex")
                        self.assertEqual(result.returncode, exit_code or 1, result.stderr)
                        self.assertIn("no non-empty answer", result.stderr)
                        output = self.out.read_text()
                        self.assertIn(f"Exit code: {exit_code or 1}\n", output)
                        self.assertIn(f"Family: {family}\n", output)
                        self.assertIn("Start UTC:", output)
                        self.assertIn("Snapshot commit:", output)
                        self.assertNotIn("answer left over", output)
                        self.assertIn(b"answer left over from an earlier run\n",
                                      [p.read_bytes() for p in self.out.parent.glob("answer.attempt-*.md")])
                        self.assertEqual(self.out.with_suffix(".log").read_text(), "agent diagnostic\n")
                        self.assertEqual((self.out.parent / "answer-evidence/proof.txt").read_text(), "proof")
                        self.assertEqual(self.non_pool_worktrees().count("worktree "), 1)
                        self.assertFalse(Path(json.loads(self.record.read_text())["tmpdir"]).parent.exists())

    def test_S15_gitignore_effect_is_limited_to_generated_root_entries(self):
        (self.repo / ".gitignore").write_bytes((ROOT / ".gitignore").read_bytes())
        paths = ["AGENTS.md", ".claude/agents/builder.md", "__pycache__/x.pyc",
                 "nested/__pycache__/x.pyc", ".scratch/file", "nested/AGENTS.md",
                 "nested/.claude/agents/builder.md", "design/inputs/file",
                 "design/research/topic/scratch/file"]
        result = subprocess.run(["git", "-C", str(self.repo), "check-ignore", "--stdin"],
                                input="\n".join(paths) + "\n", env=self.env,
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), paths[:5])

    def test_prompt_and_native_arguments_for_every_role_and_family(self):
        for family in ("claude", "codex"):
            for role in ("builder", "investigator", "verifier", "challenger", "reviewer"):
                with self.subTest(family=family, role=role):
                    result = self.launch(role, family, "--author-family",
                                         "codex" if family == "claude" else "claude")
                    self.assertEqual(result.returncode, 0, result.stderr)
                    record = json.loads(self.record.read_text())
                    expected = (self.method / "roles" / f"{role}.md").read_text()
                    if role == "verifier":
                        expected += "\n" + (self.method / "guides/verification.md").read_text()
                    expected += "\n---\n" + self.brief.read_text()
                    if family == "codex" and role in ("reviewer", "challenger"):
                        snapshot = next(line.split(": ", 1)[1] for line in self.out.read_text().splitlines()
                                        if line.startswith("Snapshot commit:"))
                        expected += ("\nCheckout: Git-only (root AGENTS.md and CLAUDE.md)\n"
                                     f"Read the code through Git: `git show {snapshot}:<path>`, "
                                     f"`git grep <pattern> {snapshot}` and `git diff`.\n")
                    if role == "verifier":
                        header = self.out.read_text().split("\n\n", 1)[0]
                        metadata = [next(line for line in header.splitlines() if line.startswith(prefix))
                                    for prefix in ("Snapshot commit:", "Checkout:", "Untracked files left out:")]
                        expected += "\n" + "\n".join(metadata) + "\n"
                        expected += "\nBefore checkout: none (--before none)\n"
                        evidence = Path(record["tmpdir"]).parent / "evidence"
                        expected += (f"\nEvidence folder: {evidence}; "
                                     "only this folder is kept; nothing else in the session folder is kept.\n")
                    if role == "verifier":
                        expected += f"\nTemporary folder: {record['tmpdir']}; removed when the session ends.\n"
                    elif role == "builder":
                        expected += (f"\nTemporary folder: {record['tmpdir']}; removed when the session ends; "
                                     "create before or known-good checkouts as shared clones inside it "
                                     "(git clone --quiet --shared --no-checkout . \"$TMPDIR/before\").\n")
                    self.assertEqual(record["stdin"], expected)
                    args = record["argv"]
                    writable = role in ("builder", "verifier")
                    if family == "codex":
                        self.assertEqual(args[:5], ["exec", "-m", "gpt-6.1-sol", "-c",
                                                   "model_reasoning_effort=high"])
                        self.assertEqual(args[args.index("-s") + 1],
                                         "workspace-write" if writable else "read-only")
                        self.assertEqual(args[args.index("-C") + 1], record["cwd"])
                        self.assertEqual(args[-1], "-")
                        roots = [args[i + 1] for i, arg in enumerate(args) if arg == "--add-dir"]
                        expected_roots = [str(evidence)] if role == "verifier" else []
                        if writable:
                            expected_roots.append(record["tmpdir"])
                        self.assertEqual(roots, expected_roots)
                    else:
                        self.assertEqual(args[:5], ["-p", "--model", "claude-opus-5-5",
                                                   "--output-format", "text"])
                        settings = json.loads(args[args.index("--settings") + 1])
                        self.assertEqual(settings["permissions"]["deny"],
                                         [] if role == "investigator" else
                                         [f"{tool}(/{self.private}/**)" for tool in ("Read", "Edit")])
                        self.assertEqual(args[args.index("--allowedTools") + 1],
                                         "Read,Grep,Glob,Bash,Write,Edit" if writable else "Read,Grep,Glob")
                        self.assertEqual(args[args.index("--tools") + 1],
                                         args[args.index("--allowedTools") + 1])
                        self.assertIn("--strict-mcp-config", args)
                        self.assertNotIn("--mcp-config", args)
                    self.assertEqual(record["cwd"] == str(self.repo),
                                     role in ("builder", "investigator"))
                    self.assertTrue(self.out.read_bytes().endswith(b"\n\nanswer\n  unchanged\n"))

    def test_review_requires_author_and_records_same_family_reason(self):
        for role in ("verifier", "challenger", "reviewer"):
            for extra in ((), ("--author-family", "codex")):
                result = self.launch(role, "codex", *extra)
                self.assertEqual(result.returncode, 2, result.stderr)
        self.assertFalse(self.record.exists())
        result = self.launch("reviewer", "codex", "--author-family", "codex",
                             "--same-family-reason", "only available family")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Same-family reason: only available family", self.out.read_text())

    def test_snapshot_preserves_repository(self):
        (self.repo / "tracked").write_text("staged")
        self.git("add", "tracked")
        (self.repo / "tracked").write_text("uncommitted")
        (self.repo / "staged-new").write_text("staged addition")
        self.git("add", "staged-new")
        (self.repo / "new").write_text("untracked")
        (self.repo / "ignored").write_text("ignored content")
        head, refs = self.git("rev-parse", "HEAD"), self.git("show-ref")
        index = (self.repo / ".git/index").read_bytes()
        result = self.launch("reviewer", "claude", "--author-family", "codex")
        self.assertEqual(result.returncode, 0, result.stderr)
        record = json.loads(self.record.read_text())
        self.assertEqual((record["changed"], record["untracked"]), ("uncommitted", "untracked"))
        self.assertEqual(record["git_index_file"], self.env.get("GIT_INDEX_FILE"))
        self.assertEqual(self.git("rev-parse", "HEAD"), head)
        self.assertEqual(self.git("show-ref"), refs)
        self.assertEqual((self.repo / ".git/index").read_bytes(), index)
        snapshot = next(line.split(": ", 1)[1] for line in self.out.read_text().splitlines()
                        if line.startswith("Snapshot commit:"))
        self.assertNotIn("ignored", self.git("ls-tree", "--name-only", snapshot).splitlines())
        self.assertEqual(self.git("show", snapshot + ":staged-new"), "staged addition")
        self.assertEqual(self.git("rev-parse", snapshot + "^"), head)
        self.assert_session_cleaned(record)
        self.assertEqual(self.non_pool_worktrees().count("worktree "), 1)

    def test_failed_session_keeps_answer_and_removes_worktree(self):
        self.env["SESSION_EXIT"] = "7"
        self.env["EVIDENCE"] = "1"
        result = self.launch("verifier", "codex", "--author-family", "claude")
        self.assertEqual(result.returncode, 7, result.stderr)
        self.assertIn("Exit code: 7\n\nanswer\n  unchanged\n", self.out.read_text())
        self.assert_session_cleaned(json.loads(self.record.read_text()))
        self.assertEqual((self.out.parent / "answer-evidence/proof.txt").read_text(), "proof")
        self.assertEqual(self.non_pool_worktrees().count("worktree "), 1)

    def test_snapshot_keeps_staged_large_and_ignored_additions(self):
        size = 5 * 1024 * 1024 + 1
        (self.repo / "staged-large").write_bytes(b"s" * size)
        (self.repo / "ignored").write_text("staged ignored content")
        self.git("add", "--force", "staged-large", "ignored")
        index = (self.repo / ".git/index").read_bytes()
        result = self.launch("reviewer", "codex", "--author-family", "claude")
        self.assertEqual(result.returncode, 0, result.stderr)
        output = self.out.read_text()
        snapshot = next(line.split(": ", 1)[1] for line in output.splitlines()
                        if line.startswith("Snapshot commit:"))
        self.assertEqual(self.git("cat-file", "-s", snapshot + ":staged-large"), str(size))
        self.assertEqual(self.git("show", snapshot + ":ignored"), "staged ignored content")
        self.assertNotIn("Skipped untracked", output)
        self.assertEqual((self.repo / ".git/index").read_bytes(), index)

    def test_failed_checkout_hook_removes_registered_worktree(self):
        hook = self.repo / ".git/hooks/post-checkout"
        hook.write_text("#!/bin/sh\nexit 1\n")
        hook.chmod(0o755)
        result = self.launch("reviewer", "claude", "--author-family", "codex")
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("returned non-zero exit status 1", result.stderr)
        self.assertIn("Result: launch refused\n", self.out.read_text())
        self.assertIn("Exit code: 2", self.out.read_text())
        self.assertFalse(self.record.exists())
        self.assertEqual(self.non_pool_worktrees().count("worktree "), 1)
        registrations = self.repo / ".git/worktrees"
        self.assertFalse(registrations.exists() and any(registrations.iterdir()))

    def test_snapshot_skips_only_oversized_untracked_files(self):
        limit = 5 * 1024 * 1024
        (self.repo / "models").mkdir()
        skipped = ["models/large model.bin", "models/other.bin"]
        for path in skipped:
            (self.repo / path).write_bytes(b"x" * (limit + 1))
        large_blob = self.git("hash-object", skipped[0])
        (self.repo / "boundary").write_bytes(b"b" * limit)
        (self.repo / ":(glob)*\nfile").write_text("literal path")
        (self.repo / "tracked-large").write_text("original")
        (self.repo / "deleted").write_text("original")
        self.git("add", "tracked-large", "deleted")
        self.git("commit", "-qm", "tracked files")
        (self.repo / "tracked-large").write_bytes(b"t" * (limit + 1))
        (self.repo / "deleted").unlink()
        result = self.launch("reviewer", "codex", "--author-family", "claude")
        self.assertEqual(result.returncode, 0, result.stderr)
        output = self.out.read_text()
        self.assertIn("Skipped untracked (>5 MB): " + ", ".join(skipped) + "\n", output)
        snapshot = next(line.split(": ", 1)[1] for line in output.splitlines()
                        if line.startswith("Snapshot commit:"))
        self.assertEqual(self.git("cat-file", "-s", snapshot + ":boundary"), str(limit))
        self.assertEqual(self.git("cat-file", "-s", snapshot + ":tracked-large"), str(limit + 1))
        self.assertEqual(self.git("show", snapshot + ":" + ":(glob)*\nfile"), "literal path")
        for path in [*skipped, "deleted"]:
            self.assertEqual(self.git("ls-tree", snapshot, "--", path), "")
        result = subprocess.run(["git", "-C", str(self.repo), "cat-file", "-e", large_blob],
                                env=self.env, capture_output=True)
        self.assertNotEqual(result.returncode, 0, "oversized untracked blob was stored")

    def test_timeout_keeps_partial_answer_and_removes_worktree(self):
        self.env["SLEEP"] = "1"
        for family in ("claude", "codex"):
            with self.subTest(family=family):
                result = self.launch("verifier", family, "--author-family",
                                     "codex" if family == "claude" else "claude",
                                     "--timeout", "0.5")
                self.assertEqual(result.returncode, 124, result.stderr)
                self.assertIn("Exit code: 124\n\nanswer\n  unchanged\n", self.out.read_text())
                self.assert_session_cleaned(json.loads(self.record.read_text()))
                self.assertEqual(self.non_pool_worktrees().count("worktree "), 1)
                time.sleep(0.8)
                self.assertFalse(Path(str(self.record) + ".child").exists(), "session child survived timeout")

    def test_private_path_in_brief_is_refused(self):
        for path in (str(self.private), "~/private"):
            self.brief.write_text("Do not read " + path)
            result = self.launch()
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertIn("private folder", result.stderr.lower())
        self.assertFalse(self.record.exists())
        self.assertIn("Result: launch refused\n", self.out.read_text())
        result = self.launch("investigator")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("~/private", json.loads(self.record.read_text())["stdin"])

    def test_model_overrides_and_installed_method_discovery(self):
        shutil.copy2(ROOT / "scripts/run.py", self.method / "run.py")
        self.env["AGENT_CODEX_MODEL"] = "custom-model"
        result = subprocess.run([sys.executable, str(self.method / "run.py"),
                                 "--role", "builder", "--family", "codex",
                                 "--brief", str(self.brief), "--out", str(self.out),
                                 "--private", str(self.private)], env=self.env,
                                cwd=self.repo, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Model: custom-model", self.out.read_text())
        self.assertIn("custom-model", json.loads(self.record.read_text())["argv"])

    def test_L1_disk_session_root_ignores_tmpdir_and_is_removed(self):
        temporary = self.base / "other-temporary-directory"
        temporary.mkdir()
        self.env["TMPDIR"] = str(temporary)
        for value in (None, "", str(self.base / "cache")):
            with self.subTest(xdg_cache_home=value):
                if value is None:
                    self.env.pop("XDG_CACHE_HOME", None)
                else:
                    self.env["XDG_CACHE_HOME"] = value
                cache = Path(value) if value else self.home / ".cache"
                self.agent_extra('''
record["files"] = sorted(p.name for p in pathlib.Path(os.environ["TMPDIR"]).parent.iterdir())
pathlib.Path(os.environ["RECORD"]).write_text(json.dumps(record))
''')
                self.round_decision()
                result = self.launch("verifier", "codex", "--author-family", "claude")
                self.assertEqual(result.returncode, 0, result.stderr)
                record = json.loads(self.record.read_text())
                session = Path(record["tmpdir"]).parent
                self.assertEqual(session.parent, cache / "am")
                self.assertIn("tmp", record["files"])
                self.assertIn("answer", record["files"])
                self.assertEqual(Path(record["argv"][record["argv"].index("-o") + 1]).parent, session)
                self.assertFalse(session.exists())
                self.assertEqual(list(session.parent.iterdir()), [])
                self.assertEqual(list(temporary.iterdir()), [])

    def test_L1_unwritable_session_root_fails_before_agent(self):
        for cache, blocker in ((self.base / "cache-file", self.base / "cache-file"),
                               (self.home / ".cache", self.home / ".cache/am")):
            with self.subTest(blocker=blocker):
                blocker.parent.mkdir(parents=True, exist_ok=True)
                blocker.write_text("keep this file")
                self.env["XDG_CACHE_HOME"] = str(cache)
                result = self.launch()
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn(f"Cannot create session folder under {cache / 'am'}:", result.stderr)
                self.assertFalse(self.record.exists())
                self.assertEqual(blocker.read_text(), "keep this file")

    def test_L1_relative_xdg_cache_home_uses_home_cache(self):
        self.env["XDG_CACHE_HOME"] = "relative-cache"
        result = self.launch("verifier", "codex", "--author-family", "claude")
        self.assertEqual(result.returncode, 0, result.stderr)
        session = Path(json.loads(self.record.read_text())["tmpdir"]).parent
        self.assertEqual(session.parent, self.home / ".cache/am")
        self.assertFalse((self.repo / "relative-cache").exists())
        self.assertFalse(session.exists())

    def test_SS_S2_short_session_folder_scope_and_discovery_environment(self):
        for role in ("challenger", "reviewer"):
            with self.subTest(role=role):
                result = self.launch(role, "codex", "--author-family", "claude")
                self.assertEqual(result.returncode, 0, result.stderr)
                record = json.loads(self.record.read_text())
                temporary = Path(record["tmpdir"])
                session = temporary.parent
                self.assertEqual(session.parent, self.home / ".cache/am")
                self.assertRegex(session.name, r"^[a-z0-9_]{8}$")
                self.assertIn(f"Scope: agent-{role}-{session.name}.scope\n", self.out.read_text())
                discovery = json.loads(Path(str(self.record) + ".discovery").read_text())
                self.assertEqual(discovery["temporary"],
                                 dict.fromkeys(("TMPDIR", "TMP", "TEMP"), str(temporary)))
                self.assertFalse(session.exists())

    def test_SS_preserves_existing_and_legacy_cache_entries(self):
        cache = self.home / ".cache"
        roots = (cache / "am", cache / "agent-method/sessions")
        for root in roots:
            (root / "unrelated-folder").mkdir(parents=True)
            (root / "unrelated-file").write_text("keep file")
            (root / "unrelated-folder/contents").write_text("keep contents")
            name = "keep1234" if root.name == "am" else "agent-session-existing"
            (root / name / "tmp").mkdir(parents=True)
            (root / name / "tmp/scratch").write_text("keep session contents")
        before = {path: path.read_bytes() if path.is_file() else None
                  for root in roots for path in root.rglob("*")}
        result = self.launch("verifier", "codex", "--author-family", "claude")
        self.assertEqual(result.returncode, 0, result.stderr)
        after = {path: path.read_bytes() if path.is_file() else None
                 for root in roots for path in root.rglob("*")}
        self.assertEqual(after, before)

    def socket_budget_cache(self, length, component="cache"):
        # Use a short fixture so both sides of the Linux Chromium limit are reachable.
        short = tempfile.TemporaryDirectory(dir="/tmp")
        self.addCleanup(short.cleanup)
        stem = Path(short.name).resolve() / component
        # /am/<8 random characters>/tmp adds 16 bytes to the cache path.
        cache = stem / ("x" * (length - 16 - len(os.fsencode(stem)) - 1))
        cache.mkdir(parents=True)
        return cache

    def test_SS_S3_tmpdir_socket_budget_boundary(self):
        for length in (62, 63):
            with self.subTest(length=length):
                self.env["XDG_CACHE_HOME"] = str(self.socket_budget_cache(length))
                result = self.launch()
                self.assertEqual(result.returncode, 0, result.stderr)
                temporary = Path(json.loads(self.record.read_text())["tmpdir"])
                self.assertEqual(len(os.fsencode(temporary)), length)
                if length == 62:
                    self.assertEqual(result.stderr, "")
                else:
                    self.assertEqual(len(result.stderr.splitlines()), 1, result.stderr)
                    self.assertEqual(result.stderr,
                                     "Warning: TMPDIR is 63 bytes; programs such as Chromium may fail "
                                     "to create Unix sockets in it above 62 bytes; starting anyway\n")
                self.assertFalse(temporary.parent.exists())

    def test_SS_S3_non_ascii_tmpdir_counts_bytes(self):
        self.env["XDG_CACHE_HOME"] = str(self.socket_budget_cache(63, "é"))
        result = self.launch()
        self.assertEqual(result.returncode, 0, result.stderr)
        temporary = json.loads(self.record.read_text())["tmpdir"]
        self.assertEqual(len(os.fsencode(temporary)), 63)
        self.assertLessEqual(len(temporary), 62)
        self.assertEqual(len(result.stderr.splitlines()), 1, result.stderr)
        self.assertIn("63 bytes", result.stderr)
        self.assertIn("above 62 bytes; starting anyway", result.stderr)

    def test_SS_S3_symlink_cache_uses_resolved_length(self):
        cache = self.socket_budget_cache(63)
        alias = cache.parents[1] / "alias"
        alias.symlink_to(cache, target_is_directory=True)
        self.assertLess(len(os.fsencode(alias)) + 16, 62)
        self.env["XDG_CACHE_HOME"] = str(alias)
        result = self.launch()
        self.assertEqual(result.returncode, 0, result.stderr)
        temporary = Path(json.loads(self.record.read_text())["tmpdir"])
        self.assertEqual(temporary.parent.parent.parent, cache)
        self.assertEqual(len(os.fsencode(temporary)), 63)
        self.assertEqual(len(result.stderr.splitlines()), 1, result.stderr)
        self.assertIn("63 bytes", result.stderr)
        self.assertIn("above 62 bytes; starting anyway", result.stderr)

    def test_L2_scope_command_and_header(self):
        result = self.launch()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        units = [line[7:] for line in self.out.read_text().splitlines() if line.startswith("Scope: ")]
        self.assertEqual(len(units), 1, self.out.read_text())
        unit = units[0]
        self.assertTrue(unit.startswith("agent-builder-"), unit)
        self.assertTrue(unit.endswith(".scope"), unit)
        args = json.loads((self.scope_state / unit).read_text())
        self.assertEqual(args[:6], ["--user", "--scope", "--quiet", "--collect",
                                    "--slice=agents.slice", "--unit=" + unit])
        self.assertEqual(args[6:8], ["--", self.env["AGENT_CODEX_CMD"]])
        self.assertTrue((self.scope_state / (unit + ".stop")).exists())

    def test_T1_temporary_environment_for_every_role_and_family(self):
        inherited = self.base / "inherited temporary files"
        inherited.mkdir()
        self.env.update(TMPDIR=str(inherited), TMP=str(inherited), TEMP=str(inherited))
        before = dict(self.env)
        self.agent_extra('''
import tempfile
record["temporary"] = {key: os.environ.get(key) for key in ("TMPDIR", "TMP", "TEMP")}
record["temporary_exists"] = pathlib.Path(os.environ["TMPDIR"]).is_dir()
with tempfile.NamedTemporaryFile() as handle:
    record["temporary_file"] = handle.name
record["child_temporary"] = subprocess.check_output(
    [sys.executable, "-c", "import tempfile; print(tempfile.mkdtemp())"], text=True).strip()
pathlib.Path(os.environ["RECORD"]).write_text(json.dumps(record))
''')
        for family in ("codex", "claude"):
            for role in ("builder", "investigator", "verifier", "challenger", "reviewer"):
                with self.subTest(role=role, family=family):
                    result = self.launch(role, family, "--author-family",
                                         "claude" if family == "codex" else "codex")
                    self.assertEqual(result.returncode, 0, result.stderr)
                    record = json.loads(self.record.read_text())
                    temporary = Path(record["temporary"]["TMPDIR"])
                    self.assertEqual(temporary.name, "tmp")
                    self.assertTrue(temporary.is_absolute())
                    self.assertEqual(temporary.parent.parent,
                                     self.home / ".cache/am")
                    self.assertEqual(record["temporary"],
                                     dict.fromkeys(("TMPDIR", "TMP", "TEMP"), str(temporary)))
                    self.assertTrue(record["temporary_exists"])
                    self.assertEqual(Path(record["temporary_file"]).parent, temporary)
                    self.assertEqual(Path(record["child_temporary"]).parent, temporary)
                    self.assertFalse(temporary.parent.exists())
        self.assertEqual(self.env, before)
        self.assertEqual(list(inherited.iterdir()), [])

    def test_T1_temporary_folder_creation_failure_prevents_agent(self):
        self.inject_launcher('''
import pathlib
mkdir = pathlib.Path.mkdir
def fail_temporary(self, *args, **kwargs):
    if self.name == "tmp" and self.parent.parent.name == "am":
        raise OSError("forced temporary folder creation failure")
    return mkdir(self, *args, **kwargs)
pathlib.Path.mkdir = fail_temporary
''')
        result = self.launch()
        self.assertNotEqual(result.returncode, 0, result.stderr)
        self.assertIn("forced temporary folder creation failure", result.stderr)
        self.assertFalse(self.record.exists())
        self.assertEqual(list((self.home / ".cache/am").iterdir()), [])

    def test_T1_temporary_files_are_not_evidence(self):
        inherited = self.base / "inherited"
        inherited.mkdir()
        self.env.update(TMPDIR=str(inherited), EVIDENCE="1")
        self.agent_extra('''
temporary = pathlib.Path(os.environ["TMPDIR"])
(temporary / "scratch-secret").write_text("temporary data")
record["temporary"] = str(temporary)
pathlib.Path(os.environ["RECORD"]).write_text(json.dumps(record))
''')
        result = self.launch("verifier", "codex", "--author-family", "claude")
        self.assertEqual(result.returncode, 0, result.stderr)
        temporary = Path(json.loads(self.record.read_text())["temporary"])
        self.assertFalse(temporary.exists())
        evidence = self.out.parent / "answer-evidence"
        self.assertEqual([path.name for path in evidence.iterdir()], ["proof.txt"])

    def test_T3_temporary_clone_prompt_for_builders_and_verifiers(self):
        for family in ("codex", "claude"):
            for role in ("builder", "verifier"):
                with self.subTest(role=role, family=family):
                    result = self.launch(role, family, "--author-family",
                                         "claude" if family == "codex" else "codex")
                    self.assertEqual(result.returncode, 0, result.stderr)
                    prompt = json.loads(self.record.read_text())["stdin"]
                    line = next((line for line in prompt.splitlines()
                                 if line.startswith("Temporary folder: ")), "")
                    self.assertTrue(line, prompt)
                    folder, instructions = line.removeprefix("Temporary folder: ").split(";", 1)
                    self.assertTrue(Path(folder).is_absolute())
                    self.assertEqual(Path(folder).name, "tmp")
                    self.assertIn("removed when the session ends", instructions)
                    clone_instructions = instructions if role == "builder" else prompt
                    self.assertIn("shared clones", clone_instructions)
                    self.assertIn("before", clone_instructions)
                    self.assertIn("known-good", clone_instructions)
                    self.assertIn("$TMPDIR", (self.method / "roles/builder.md").read_text())
                    for document in ("roles/builder.md", "guides/verification.md"):
                        self.assertIn('git clone --quiet --shared --no-checkout . "$TMPDIR/before"',
                                      (self.method / document).read_text())

    def test_T2_writable_roles_grant_temporary_folder_access(self):
        inherited = self.base / "inherited"
        inherited.mkdir()
        self.env["TMPDIR"] = str(inherited)
        self.agent_extra('''
temporary = pathlib.Path(os.environ["TMPDIR"])
roots = [args[i + 1] for i, arg in enumerate(args) if arg == "--add-dir"]
assert str(temporary) in roots, "temporary folder is outside the agent's writable workspace"
path = temporary / "probe"
path.write_text("temporary write")
record["temporary_write"] = path.read_text()
pathlib.Path(os.environ["RECORD"]).write_text(json.dumps(record))
''')
        for family in ("codex", "claude"):
            for role in ("builder", "verifier"):
                with self.subTest(role=role, family=family):
                    result = self.launch(role, family, "--author-family",
                                         "claude" if family == "codex" else "codex")
                    self.assertEqual(result.returncode, 0, result.stderr)
                    record = json.loads(self.record.read_text())
                    self.assertEqual(record["temporary_write"], "temporary write")
                    self.assertNotEqual(record["tmpdir"], str(inherited))
                    self.assertFalse(Path(record["tmpdir"]).exists())
                    self.assertEqual(self.non_pool_worktrees().count("worktree "), 1)

    def test_T3_documented_clone_from_review_and_linked_checkouts(self):
        baseline = self.git("rev-parse", "HEAD")
        self.env["BASELINE"] = baseline
        self.env["VERIFICATION_GUIDE"] = str(self.method / "guides/verification.md")
        self.env["BUILDER_CONTRACT"] = str(self.method / "roles/builder.md")
        self.agent_extra('''
guide = pathlib.Path(os.environ["VERIFICATION_GUIDE"]).read_text()
command = next(part for part in guide.split("`") if "$TMPDIR/before" in part)
command = command.replace("<baseline>", os.environ["BASELINE"])
common = pathlib.Path(subprocess.check_output(
    ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"], text=True).strip())
before = {str(p): p.read_bytes() for p in common.rglob("*") if p.is_file()}
subprocess.run(command, shell=True, check=True)
path = pathlib.Path(os.environ["TMPDIR"]) / "before"
assert subprocess.check_output(["git", "-C", str(path), "rev-parse", "HEAD"], text=True).strip() == os.environ["BASELINE"]
after = {str(p): p.read_bytes() for p in common.rglob("*") if p.is_file()}
assert before == after, "documented checkout command wrote into the source repository"
assert "--shared --no-checkout" in command
assert command.replace(os.environ["BASELINE"], "<baseline>") in pathlib.Path(os.environ["BUILDER_CONTRACT"]).read_text()
record["baseline_checkout"] = str(path)
pathlib.Path(os.environ["RECORD"]).write_text(json.dumps(record))
''')
        linked = self.base / "linked source with spaces"
        self.git("worktree", "add", "--detach", str(linked), "HEAD")
        original = self.repo
        for repo in (original, linked):
            self.repo = repo
            for role in ("builder", "verifier"):
                with self.subTest(repo=repo, role=role):
                    registrations = self.non_pool_worktrees()
                    result = self.launch(role, "codex", "--author-family", "claude")
                    self.assertEqual(result.returncode, 0, result.stderr)
                    record = json.loads(self.record.read_text())
                    self.assertFalse(Path(record["baseline_checkout"]).exists())
                    self.assertEqual(self.non_pool_worktrees(), registrations)

    def temporary_worktree_agent(self):
        # Exercise clean, modified, locked and already deleted session checkouts.
        self.agent_extra('''
temporary = pathlib.Path(os.environ["TMPDIR"])
temporary.mkdir(exist_ok=True)
paths = [temporary / name for name in ("clean before", "modified known good", "locked", "deleted")]
for path in paths:
    subprocess.check_output(["git", "-C", str(cwd), "worktree", "add", "--detach", str(path), "HEAD"],
                            stderr=subprocess.STDOUT)
(paths[1] / "tracked").write_text("session modification")
(paths[1] / "untracked").write_text("session untracked file")
subprocess.check_output(["git", "worktree", "lock", str(paths[2])], stderr=subprocess.STDOUT)
import shutil
shutil.rmtree(paths[3])
record["temporary"] = str(temporary)
record["worktrees"] = list(map(str, paths))
pathlib.Path(os.environ["RECORD"]).write_text(json.dumps(record))
pathlib.Path(os.environ["RECORD"] + ".ready").touch()
''')

    def wait_agent_ready(self, record):
        deadline = time.monotonic() + 10
        while not Path(str(record) + ".ready").exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue(Path(str(record) + ".ready").exists(), "agent did not create its worktrees")
        return json.loads(record.read_text())

    def test_T4_worktree_cleanup_every_role_and_exit_in_linked_repository(self):
        linked = self.base / "linked repository"
        self.git("worktree", "add", "--detach", str(linked), "HEAD")
        self.repo = linked
        user = self.base / "user worktree"
        self.git("worktree", "add", "--detach", str(user), "HEAD")
        (user / "tracked").write_text("user changes")
        missing_user = self.base / "unmounted user worktree"
        self.git("worktree", "add", "--detach", str(missing_user), "HEAD")
        shutil.rmtree(missing_user)
        registrations = self.non_pool_worktrees()
        self.temporary_worktree_agent()
        for role in ("builder", "investigator", "verifier", "challenger", "reviewer"):
            for outcome in ("normal", "failure", "timeout", signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
                with self.subTest(role=role, outcome=outcome):
                    record = self.base / f"{role}-{outcome}.json"
                    self.env["SESSION_EXIT"] = "7" if outcome == "failure" else "0"
                    self.env["SLEEP"] = "1" if outcome not in ("normal", "failure") else ""
                    extra = ("--timeout", "1") if outcome == "timeout" else ()
                    process = self.start_launcher(*extra, role=role, record=record)
                    data = self.wait_agent_ready(record)
                    if isinstance(outcome, signal.Signals):
                        process.send_signal(outcome)
                    _, stderr = process.communicate(timeout=10)
                    expected = (128 + outcome if isinstance(outcome, signal.Signals) else
                                {"normal": 0, "failure": 7, "timeout": 124}[outcome])
                    self.assertEqual(process.returncode, expected, stderr)
                    self.assertFalse(Path(data["temporary"]).parent.exists())
                    self.assertEqual(self.non_pool_worktrees(), registrations)
                    self.assertEqual((user / "tracked").read_text(), "user changes")
                    self.assertTrue(linked.is_dir())

    def test_P2_missing_user_worktree_registration_survives_every_role(self):
        for family in ("codex", "claude"):
            for role in ("builder", "investigator", "verifier", "challenger", "reviewer"):
                with self.subTest(role=role, family=family):
                    user = self.base / f"unmounted user {family} {role}"
                    self.git("worktree", "add", "--detach", str(user), "HEAD")
                    shutil.rmtree(user)
                    registrations = self.non_pool_worktrees()
                    result = self.launch(role, family, "--author-family",
                                         "claude" if family == "codex" else "codex")
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(self.non_pool_worktrees(), registrations)

    def test_T4_locked_and_deleted_worktrees_are_removed_for_either_family(self):
        self.temporary_worktree_agent()
        registrations = self.non_pool_worktrees()
        for family in ("codex", "claude"):
            for role in ("builder", "investigator", "verifier", "challenger", "reviewer"):
                with self.subTest(role=role, family=family):
                    result = self.launch(role, family, "--author-family",
                                         "claude" if family == "codex" else "codex")
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(self.non_pool_worktrees(), registrations)
                    record = json.loads(self.record.read_text())
                    self.assertFalse(Path(record["temporary"]).parent.exists())

    def test_T4_builder_cleanup_failure_is_reported_before_folder_removal(self):
        real_git = shutil.which("git")
        (self.bin / "git").unlink()
        state = self.base / "cleanup-state"
        self.env.update(CLEANUP_STATE=str(state), SESSION_EXIT="7")
        (self.bin / "git").write_text(
            "#!" + sys.executable + "\nimport os, pathlib, sys\n"
            "args = sys.argv[1:]\n"
            "if 'worktree' in args and 'list' in args:\n"
            "    root = pathlib.Path.home() / '.cache/am'\n"
            "    pathlib.Path(os.environ['CLEANUP_STATE']).write_text(str(any(root.iterdir())))\n"
            "    print('forced listing failure', file=sys.stderr)\n    sys.exit(31)\n"
            f"os.execv({real_git!r}, [{real_git!r}, *args])\n")
        (self.bin / "git").chmod(0o755)
        result = self.launch()
        self.assertEqual(result.returncode, 7, result.stderr)
        self.assertIn("Cleanup failed: session worktrees", result.stderr)
        self.assertIn("forced listing failure", result.stderr)
        self.assertEqual(state.read_text(), "True")

    def test_cleanup_in_plain_folder_is_silent(self):
        self.repo = self.base / "plain folder"
        self.repo.mkdir()
        (self.repo / "tracked").write_text("original")
        for family in ("codex", "claude"):
            for role in ("builder", "investigator"):
                with self.subTest(family=family, role=role):
                    result = self.launch(role, family)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stderr, "")

    def test_cleanup_non_utf8_paths_preserves_session_status_and_user_worktrees(self):
        for name in (b"user-present-\xff", b"user-missing-\xff"):
            user = self.base / os.fsdecode(name)
            self.git("worktree", "add", "--detach", str(user), "HEAD")
            if b"missing" in name:
                shutil.rmtree(user)
        registrations = subprocess.check_output(
            ["git", "-C", str(self.repo), "worktree", "list", "--porcelain", "-z"],
            env=self.env)
        real_git = shutil.which("git")
        (self.bin / "git").unlink()
        (self.bin / "git").write_text(
            "#!" + sys.executable + "\nimport os, pathlib, sys\n"
            "args = sys.argv[1:]\n"
            "if ('worktree' in args and 'remove' in args\n"
            "        and os.environ.get('FORCE_FALLBACK') and not pathlib.Path(args[-1]).exists()):\n"
            "    print('forced missing checkout removal failure', file=sys.stderr)\n"
            "    sys.exit(31)\n"
            f"os.execv({real_git!r}, [{real_git!r}, *args])\n")
        (self.bin / "git").chmod(0o755)
        self.agent_extra(r'''
import shutil
for name in (b"session-present-\xff", b"session-missing-\xff"):
    path = pathlib.Path(os.environ["TMPDIR"]) / os.fsdecode(name)
    subprocess.check_output(["git", "worktree", "add", "--detach", str(path), "HEAD"],
                            stderr=subprocess.STDOUT)
    if b"missing" in name:
        shutil.rmtree(path)
''')
        for fallback in ("", "1"):
            for status in (0, 7):
                with self.subTest(fallback=bool(fallback), status=status):
                    self.env.update(FORCE_FALLBACK=fallback, SESSION_EXIT=str(status))
                    result = self.launch()
                    self.assertEqual(result.returncode, 3 if fallback and status == 0 else status, result.stderr)
                    self.assertNotIn("UnicodeDecodeError", result.stderr)
                    if fallback:
                        self.assertIn("forced missing checkout removal failure", result.stderr)
                    else:
                        self.assertEqual(result.stderr, "")
                    self.assertFalse(Path(json.loads(self.record.read_text())["tmpdir"]).parent.exists())
                    self.assertEqual(subprocess.check_output(
                        ["git", "-C", str(self.repo), "worktree", "list", "--porcelain", "-z"],
                        env=self.env), registrations)
                    self.assertTrue((self.base / os.fsdecode(b"user-present-\xff")).is_dir())

    def test_cleanup_fallback_skips_bad_registration_and_continues(self):
        real_git = shutil.which("git")
        (self.bin / "git").unlink()
        (self.bin / "git").write_text(
            "#!" + sys.executable + "\nimport os, pathlib, sys\n"
            "args = sys.argv[1:]\n"
            "if 'worktree' in args and 'remove' in args:\n"
            "    if os.environ.get('DAMAGED_REGISTRATION'):\n"
            "        pathlib.Path(os.environ['DAMAGED_REGISTRATION']).write_bytes(b'\\xff\\n')\n"
            "    print('forced removal failure', file=sys.stderr)\n    sys.exit(31)\n"
            f"os.execv({real_git!r}, [{real_git!r}, *args])\n")
        (self.bin / "git").chmod(0o755)
        self.inject_launcher('''
            from pathlib import Path
            original_iterdir, original_read = Path.iterdir, Path.read_bytes
            def ordered(self):
                entries = original_iterdir(self)
                return iter(sorted(entries)) if self.name == "worktrees" else entries
            def read(self, *args, **kwargs):
                if self.parent.name == "000-unreadable" and self.name == "gitdir":
                    raise PermissionError("forced unreadable registration")
                return original_read(self, *args, **kwargs)
            Path.iterdir, Path.read_bytes = ordered, read
        ''')
        self.agent_extra('''
import shutil
for name in ("first", "second"):
    path = pathlib.Path(os.environ["TMPDIR"]) / name
    subprocess.check_output(["git", "worktree", "add", "--detach", str(path), "HEAD"],
                            stderr=subprocess.STDOUT)
    shutil.rmtree(path)
''')
        registrations = self.repo / ".git/worktrees"
        for damage in ("unreadable", "missing", "directory", "encoding"):
            with self.subTest(damage=damage):
                bad = registrations / ("000-" + damage)
                bad.mkdir(parents=True)
                gitdir = bad / "gitdir"
                if damage == "unreadable":
                    gitdir.write_text(str(self.base / "unrelated/.git"))
                elif damage == "directory":
                    gitdir.mkdir()
                elif damage == "encoding":
                    # Corrupt it after Git lists paths, before fallback reads it.
                    self.env["DAMAGED_REGISTRATION"] = str(gitdir)
                try:
                    result = self.launch()
                    self.assertEqual(result.returncode, 3, result.stderr)
                    self.assertEqual(sorted(path.name for path in registrations.iterdir()), [bad.name])
                    if damage == "encoding":
                        # Non-UTF-8 bytes are valid filesystem paths, not read failures.
                        self.assertNotIn(str(gitdir), result.stderr)
                    else:
                        self.assertIn(str(gitdir), result.stderr)
                finally:
                    for entry in registrations.iterdir():
                        shutil.rmtree(entry)
                    self.env.pop("DAMAGED_REGISTRATION", None)

    def test_cleanup_preserves_registered_path_escaping_session_with_dotdot(self):
        user = self.base / "user worktree"
        self.git("worktree", "add", "--detach", str(user), "HEAD")
        admin = Path((user / ".git").read_text().strip().removeprefix("gitdir: "))
        self.env.update(USER_WORKTREE=str(user), USER_ADMIN=str(admin))
        self.agent_extra('''
temporary = pathlib.Path(os.environ["TMPDIR"])
user = pathlib.Path(os.environ["USER_WORKTREE"])
alias = temporary / os.path.relpath(user, temporary)
(pathlib.Path(os.environ["USER_ADMIN"]) / "gitdir").write_text(str(alias / ".git") + "\\n")
record["registered_alias"] = str(alias)
pathlib.Path(os.environ["RECORD"]).write_text(json.dumps(record))
''')
        result = self.launch()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(user.is_dir(), "cleanup removed a worktree outside the session")
        self.assertTrue(admin.is_dir(), "cleanup removed an unrelated registration")
        alias = json.loads(self.record.read_text())["registered_alias"]
        self.assertIn("worktree " + alias, self.non_pool_worktrees())
        self.assertEqual(result.stderr, "")

    def test_recovery_and_takeover_guides_use_shared_clones_in_tmpdir(self):
        for name in ("recovery", "takeover"):
            with self.subTest(guide=name):
                guide = (ROOT / "product/guides" / (name + ".md")).read_text()
                self.assertIn("shared clone", guide)
                self.assertIn("$TMPDIR", guide)
                self.assertNotIn("worktree", guide)
                if name == "takeover":
                    self.assertIn("($TMPDIR in sessions the launcher starts)", guide)
                    self.assertLess(guide.index("Create temporary checkouts"),
                                    guide.index("Remove them before stopping"))

    def test_T5_concurrent_temporary_worktrees_are_independent(self):
        self.temporary_worktree_agent()
        self.env["SLEEP"] = "1"
        registrations = self.non_pool_worktrees()
        records = [self.base / "first-temp.json", self.base / "second-temp.json"]
        processes = [self.start_launcher(role="builder", record=record,
                                         out=self.base / f"answer-{index}.md")
                     for index, record in enumerate(records)]
        first, second = [self.wait_agent_ready(record) for record in records]
        self.assertNotEqual(first["temporary"], second["temporary"])
        processes[0].terminate()
        _, stderr = processes[0].communicate(timeout=10)
        self.assertEqual(processes[0].returncode, 143, stderr)
        self.assertFalse(Path(first["temporary"]).exists())
        self.assertIsNone(processes[1].poll())
        self.assertTrue(Path(second["temporary"]).is_dir())
        listed = self.non_pool_worktrees()
        for path in first["worktrees"]:
            self.assertNotIn(path, listed)
        for path in second["worktrees"]:
            self.assertEqual(Path(path).is_dir(), Path(path).name != "deleted")
            self.assertIn(path, listed)
        processes[1].terminate()
        _, stderr = processes[1].communicate(timeout=10)
        self.assertEqual(processes[1].returncode, 143, stderr)
        self.assertFalse(Path(second["temporary"]).exists())
        self.assertEqual(self.non_pool_worktrees(), registrations)

    def test_L3_failed_scope_never_starts_agent(self):
        self.env["SCOPE_FAIL"] = "1"
        result = self.launch()
        self.assertEqual(result.returncode, 19, result.stderr)
        self.assertFalse(self.record.exists())
        self.assertIn("scope refused by test bus", self.out.with_suffix(".log").read_text())
        self.assertIn("scope refused by test bus", result.stderr)

    def test_L3_no_systemd_runs_with_one_warning(self):
        self.configure_scope(present=False)
        result = self.launch()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(result.stderr.splitlines()), 1)
        self.assertIn("without a scope", result.stderr)
        self.assertIn("Scope: none\n", self.out.read_text())

    def detached_agent(self):
        self.agent_extra('''
child = subprocess.Popen([sys.executable, "-c", "import pathlib, time; time.sleep(1.5); "
                          "pathlib.Path(" + repr(os.environ["RECORD"] + ".detached") + ").touch()"],
                         start_new_session=True)
pathlib.Path(os.environ["RECORD"] + ".pids").write_text(str(os.getpid()) + " " + str(child.pid))
''')
        self.addCleanup(self.kill_recorded_agents)

    def kill_recorded_agents(self):
        for path in self.base.glob("*.pids"):
            for pid in map(int, path.read_text().split()):
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def test_L4_scope_stops_detached_children_on_normal_and_timeout(self):
        self.detached_agent()
        for timeout in (False, True):
            with self.subTest(timeout=timeout):
                self.record = self.base / ("timeout.json" if timeout else "normal.json")
                self.env["RECORD"] = str(self.record)
                if timeout:
                    self.env["SLEEP"] = "1"
                result = self.launch("verifier", "codex", "--author-family", "claude",
                                     *( ("--timeout", "0.4") if timeout else () ))
                self.assertEqual(result.returncode, 124 if timeout else 0, result.stderr)
                self.assertTrue(self.out.read_text().endswith("answer\n  unchanged\n"))
                time.sleep(1.6)
                self.assertFalse(Path(str(self.record) + ".detached").exists())
                self.assertFalse(Path(str(self.record) + ".child").exists())

    def test_L4_signals_keep_answer_evidence_and_stop_detached_children(self):
        self.detached_agent()
        self.env.update(SLEEP="1", EVIDENCE="1")
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            with self.subTest(signal=sig):
                record = self.base / f"signal-{sig}.json"
                process = self.start_launcher(record=record)
                self.wait_record(record)
                deadline = time.monotonic() + 5
                while not Path(str(record) + ".pids").exists() and time.monotonic() < deadline:
                    time.sleep(0.02)
                process.send_signal(sig)
                _, stderr = process.communicate(timeout=10)
                self.assertEqual(process.returncode, 128 + sig, stderr)
                self.assertTrue(self.out.read_text().endswith("answer\n  unchanged\n"))
                self.assertEqual((self.out.parent / "answer-evidence/proof.txt").read_text(), "proof")
                time.sleep(1.6)
                self.assertFalse(Path(str(record) + ".detached").exists())
                self.assertFalse(Path(str(record) + ".child").exists())

    def test_L4_no_scope_stops_group_after_normal_exit(self):
        self.configure_scope(present=False)
        self.agent_extra('''
subprocess.Popen([sys.executable, "-c", "import pathlib, time; time.sleep(1); "
                  "pathlib.Path(" + repr(os.environ["RECORD"] + ".child") + ").touch()"])
''')
        result = self.launch()
        self.assertEqual(result.returncode, 0, result.stderr)
        time.sleep(1.2)
        self.assertFalse(Path(str(self.record) + ".child").exists())

    def test_L4_ignored_sighup_stays_ignored(self):
        self.inject_launcher("""
import signal
signal.signal(signal.SIGHUP, signal.SIG_IGN)
""")
        self.agent_extra('''
pathlib.Path(os.environ["RECORD"] + ".ready").touch()
while not pathlib.Path(os.environ["RECORD"] + ".release").exists():
    time.sleep(0.02)
''')
        process = self.start_launcher(role="builder")
        deadline = time.monotonic() + 10
        ready = Path(str(self.record) + ".ready")
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue(ready.exists(), "agent did not become ready")
        process.send_signal(signal.SIGHUP)
        Path(str(self.record) + ".release").touch()
        _, stderr = process.communicate(timeout=10)
        self.assertEqual(process.returncode, 0, stderr)
        self.assertTrue(self.out.read_text().endswith("answer\n  unchanged\n"))

    def test_startup_interrupt_is_not_masked_by_cleanup(self):
        self.inject_launcher("""
import os, pathlib, signal
resolve = pathlib.Path.resolve
def interrupt_session_path(self, *args, **kwargs):
    if self.parent.name == 'am':
        os.kill(os.getpid(), signal.SIGINT)
    return resolve(self, *args, **kwargs)
pathlib.Path.resolve = interrupt_session_path
""")
        result = self.launch()
        self.assertNotIn("UnboundLocalError", result.stderr)
        self.assertIn("Result: interrupted\n", self.out.read_text())
        self.assertEqual(result.returncode, 128 + signal.SIGINT, result.stderr)
        self.assertFalse(self.record.exists())
        self.assertIn("Exit code: 130\n", self.out.read_text())
        self.assertEqual(list((self.home / ".cache/am").iterdir()), [])

    def test_L5_evidence_prompt_precreated_folder_and_timeout_retention(self):
        self.agent_extra('''
assert evidence.is_absolute() and evidence.is_dir()
assert evidence.parent == pathlib.Path(os.environ["TMPDIR"]).parent and not evidence.is_relative_to(cwd)
(evidence / "proof.txt").write_text("partial proof")
(cwd / "discard.txt").write_text("discard")
''')
        self.env["SLEEP"] = "1"
        result = self.launch("verifier", "codex", "--author-family", "claude", "--timeout", "0.4")
        self.assertEqual(result.returncode, 124, result.stderr)
        record = json.loads(self.record.read_text())
        evidence = Path(record["tmpdir"]).parent / "evidence"
        self.assertIn(f"Evidence folder: {evidence}; only this folder is kept; "
                      "nothing else in the session folder is kept.\n", record["stdin"])
        self.assertEqual((self.out.parent / "answer-evidence/proof.txt").read_text(), "partial proof")
        self.assertFalse((self.out.parent / "discard.txt").exists())
        self.assert_session_cleaned(record)

    def test_L5_rerun_archives_previous_evidence_even_without_new_files(self):
        self.env["EVIDENCE"] = "1"
        result = self.launch("verifier", "codex", "--author-family", "claude")
        self.assertEqual(result.returncode, 0, result.stderr)
        evidence = self.out.parent / "answer-evidence"
        self.assertTrue(evidence.exists())
        del self.env["EVIDENCE"]
        self.env["SESSION_EXIT"] = "7"
        result = self.launch("verifier", "codex", "--author-family", "claude")
        self.assertEqual(result.returncode, 7, result.stderr)
        self.assertFalse(evidence.exists())
        self.assertEqual((self.out.parent / "answer.attempt-1-evidence/proof.txt").read_text(), "proof")

    def test_L5_only_verifiers_receive_evidence_folder(self):
        for role in ("reviewer", "challenger"):
            with self.subTest(role=role):
                result = self.launch(role, "codex", "--author-family", "claude")
                self.assertEqual(result.returncode, 0, result.stderr)
                record = json.loads(self.record.read_text())
                self.assertNotIn("Evidence folder:", record["stdin"])
                self.assertNotIn("--add-dir", record["argv"])

    def test_L5_tracked_evidence_is_not_session_evidence(self):
        (self.repo / "evidence").mkdir()
        (self.repo / "evidence/repository.txt").write_text("repository fixture")
        self.git("add", "evidence")
        self.git("commit", "-qm", "tracked evidence fixture")
        self.env["EVIDENCE"] = "1"
        self.agent_extra('''
assert (cwd / "evidence/repository.txt").read_text() == "repository fixture"
''')
        result = self.launch("verifier", "codex", "--author-family", "claude")
        self.assertEqual(result.returncode, 0, result.stderr)
        kept = self.out.parent / "answer-evidence"
        self.assertEqual(sorted(p.name for p in kept.iterdir()), ["proof.txt"])

    def test_L5_evidence_symlinks_are_copied_as_links(self):
        target = self.base / "external"
        target.mkdir()
        (target / "secret").write_text("outside session")
        self.env["LINK_TARGET"] = str(target)
        self.agent_extra('''
target = pathlib.Path(os.environ["LINK_TARGET"])
(evidence / "file-link").symlink_to(target / "secret")
(evidence / "dir-link").symlink_to(target, target_is_directory=True)
''')
        result = self.launch("verifier", "codex", "--author-family", "claude")
        self.assertEqual(result.returncode, 0, result.stderr)
        kept = self.out.parent / "answer-evidence"
        for name, destination in (("file-link", target / "secret"), ("dir-link", target)):
            with self.subTest(link=name):
                self.assertTrue((kept / name).is_symlink())
                self.assertEqual((kept / name).readlink(), destination)
        self.assertEqual((target / "secret").read_text(), "outside session")

    def test_L5_dangling_evidence_symlink_is_kept(self):
        self.agent_extra('''
(evidence / "missing").symlink_to("absent-target")
''')
        result = self.launch("verifier", "codex", "--author-family", "claude")
        self.assertEqual(result.returncode, 0, result.stderr)
        link = self.out.parent / "answer-evidence/missing"
        self.assertTrue(link.is_symlink())
        self.assertEqual(link.readlink(), Path("absent-target"))

    def test_L5_rerun_as_builder_archives_previous_evidence(self):
        self.env["EVIDENCE"] = "1"
        result = self.launch("verifier", "codex", "--author-family", "claude")
        self.assertEqual(result.returncode, 0, result.stderr)
        evidence = self.out.parent / "answer-evidence"
        self.assertTrue(evidence.exists())
        del self.env["EVIDENCE"]
        result = self.launch()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(evidence.exists())
        self.assertEqual((self.out.parent / "answer.attempt-1-evidence/proof.txt").read_text(), "proof")

    def test_L5_rerun_replaces_file_and_symlink_destinations_without_following_links(self):
        self.env.update(EVIDENCE="1", SESSION_EXIT="7")
        kept = self.out.parent / "answer-evidence"
        kept.parent.mkdir()
        target = self.base / "external-evidence"
        for kind in ("file", "directory-link", "file-link", "dangling-link"):
            with self.subTest(kind=kind):
                if kept.is_symlink() or kept.is_file():
                    kept.unlink()
                elif kept.exists():
                    shutil.rmtree(kept)
                if kind == "file":
                    kept.write_text("previous evidence")
                else:
                    destination = target / kind
                    if kind == "directory-link":
                        destination.mkdir(parents=True)
                        (destination / "old.txt").write_text("untouched")
                    elif kind == "file-link":
                        destination.write_text("untouched")
                    kept.symlink_to(destination, target_is_directory=kind == "directory-link")
                self.round_decision()
                result = self.launch("verifier", "codex", "--author-family", "claude")
                self.assertEqual(result.returncode, 7, result.stderr)
                self.assertFalse(kept.is_symlink())
                self.assertEqual((kept / "proof.txt").read_text(), "proof")
                if kind == "directory-link":
                    self.assertEqual(list(destination.iterdir()), [destination / "old.txt"])
                    self.assertEqual((destination / "old.txt").read_text(), "untouched")
                elif kind == "file-link":
                    self.assertEqual(destination.read_text(), "untouched")
                elif kind == "dangling-link":
                    self.assertFalse(destination.exists())

    def test_L5_evidence_oserrors_keep_session_status_and_answer(self):
        kept = self.out.parent / "answer-evidence"
        kept.parent.mkdir()
        self.env["EVIDENCE"] = "1"
        operation = "copytree"
        for code in (0, 7, 124):
            with self.subTest(operation=operation, code=code):
                if kept.is_file():
                    kept.unlink()
                elif kept.exists():
                    shutil.rmtree(kept)
                self.inject_launcher(f"""
import pathlib, shutil
original = shutil.copytree
def fail_evidence(*args, **kwargs):
    if pathlib.Path(args[1]) == pathlib.Path({str(kept)!r}):
        raise OSError('forced evidence {operation} failure')
    return original(*args, **kwargs)
shutil.copytree = fail_evidence
""")
                self.env["SESSION_EXIT"] = str(0 if code == 124 else code)
                if code == 124:
                    self.env["SLEEP"] = "1"
                else:
                    self.env.pop("SLEEP", None)
                self.round_decision()
                result = self.launch("verifier", "codex", "--author-family", "claude",
                                     *(("--timeout", "0.4") if code == 124 else ()))
                self.assertEqual(result.returncode, code or 3, result.stderr)
                self.assertIn(f"Cleanup failed: evidence copy {kept}: forced evidence {operation} failure",
                              result.stderr)
                self.assertNotIn("Launcher failed:", result.stderr)
                self.assertIn(f"Exit code: {code}\n", self.out.read_text())
                self.assertIn("Cleanup: failed: evidence copy", self.out.read_text())
                self.assertTrue(self.out.read_text().endswith("answer\n  unchanged\n"))
                session = Path(json.loads(self.record.read_text())["tmpdir"]).parent
                self.assertFalse(session.exists())
                self.assertEqual(self.non_pool_worktrees().count("worktree "), 1)

    def test_L5_claude_verifier_can_write_to_evidence_folder(self):
        self.env["EVIDENCE"] = "1"
        self.agent_extra('''
assert "--add-dir" in args, "evidence folder is outside Claude's writable workspace"
assert str(evidence) in [args[i + 1] for i, arg in enumerate(args) if arg == "--add-dir"]
''')
        result = self.launch("verifier", "claude", "--author-family", "codex")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.out.parent / "answer-evidence/proof.txt").read_text(), "proof")

    def test_L6_unborn_repository_snapshot_preserves_head_refs_index_and_files(self):
        shutil.rmtree(self.repo / ".git")
        self.git("init", "-q")
        (self.repo / "tracked").write_text("staged")
        (self.repo / "ignored").write_text("staged ignored")
        self.git("add", "--force", "tracked", "ignored")
        (self.repo / "tracked").write_text("working tree")
        (self.repo / "new").write_text("new file")
        (self.repo / "huge").write_bytes(b"x" * (5 * 1024 * 1024 + 1))
        before = {p: p.read_bytes() for p in self.repo.rglob("*") if p.is_file()}
        head = (self.repo / ".git/HEAD").read_bytes()
        index = (self.repo / ".git/index").read_bytes()
        result = self.launch("reviewer", "codex", "--author-family", "claude")
        self.assertEqual(result.returncode, 0, result.stderr)
        record = json.loads(self.record.read_text())
        self.assertEqual((record["changed"], record["untracked"]), ("working tree", "new file"))
        snapshot = next(line.split(": ", 1)[1] for line in self.out.read_text().splitlines()
                        if line.startswith("Snapshot commit: "))
        self.assertEqual(self.git("rev-list", "--parents", "-n", "1", snapshot), snapshot)
        self.assertEqual(self.git("show", snapshot + ":ignored"), "staged ignored")
        self.assertEqual(self.git("ls-tree", snapshot, "--", "huge"), "")
        self.assertEqual((self.repo / ".git/HEAD").read_bytes(), head)
        self.assertEqual((self.repo / ".git/index").read_bytes(), index)
        self.assertEqual(sorted((self.repo / ".git/refs").rglob("*")),
                         [self.repo / ".git/refs/heads", self.repo / ".git/refs/tags"])
        for path, contents in before.items():
            self.assertEqual(path.read_bytes(), contents, str(path))

    def test_L6_unborn_repository_without_index(self):
        shutil.rmtree(self.repo / ".git")
        self.git("init", "-q")
        result = self.launch("reviewer", "codex", "--author-family", "claude")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.repo / ".git/index").exists())
        self.assertEqual(json.loads(self.record.read_text())["changed"], "original")

    def test_L7_writable_roots_arguments_and_role_restrictions(self):
        for role in ("builder", "verifier"):
            for family in ("codex",):
                with self.subTest(role=role, family=family):
                    first = self.home / f"cache-{role}-{family}"
                    second = self.home / "other cache"
                    result = self.launch(role, family, "--author-family",
                                         "claude" if family == "codex" else "codex",
                                         "--writable", "~/" + first.name, "--writable", second)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertTrue(first.is_dir())
                    self.assertTrue(second.is_dir())
                    args = json.loads(self.record.read_text())["argv"]
                    roots = [args[i + 1] for i, arg in enumerate(args) if arg == "--add-dir"]
                    expected = [str(first), str(second)]
                    if role == "verifier":
                        expected.append(str(Path(json.loads(self.record.read_text())["tmpdir"]).parent / "evidence"))
                    expected.append(json.loads(self.record.read_text())["tmpdir"])
                    self.assertEqual(roots, expected)
        self.record.unlink(missing_ok=True)
        for role in ("investigator", "challenger", "reviewer"):
            result = self.launch(role, "codex", "--author-family", "claude",
                                 "--writable", self.home / "refused")
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertIn("only builder and verifier", result.stderr)
        self.assertFalse(self.record.exists())
        self.assertFalse((self.home / "refused").exists())

    def test_L8_scope_cleanup_failure_keeps_status_answer_and_session_evidence(self):
        self.env.update(STOP_FAIL="1", SESSION_EXIT="7", EVIDENCE="1")
        result = self.launch("verifier", "codex", "--author-family", "claude")
        self.assertEqual(result.returncode, 7, result.stderr)
        self.assertIn("Cleanup failed:", result.stderr)
        self.assertIn("agent-verifier-", result.stderr)
        self.assertIn("scope stop refused", result.stderr)
        self.assertIn("Cleanup: failed:", self.out.read_text())
        self.assertIn("Exit code: 7\n", self.out.read_text())
        self.assertTrue(self.out.read_text().endswith("answer\n  unchanged\n"))
        session = Path(json.loads(self.record.read_text())["tmpdir"]).parent
        self.assertEqual((session / "evidence/proof.txt").read_text(), "proof")
        self.assertFalse((self.out.parent / "answer-evidence").exists())
        self.assertIn(f"session folder kept at {session}; evidence left in {session / 'evidence'}",
                      self.out.read_text())

    def assert_writable_refused(self, root):
        result = self.launch("builder", "codex", "--writable", root)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("unsafe --writable root " + str(Path(root).resolve()), result.stderr)
        self.assertFalse(self.record.exists())
        self.assertIn("Result: launch refused\n", self.out.read_text())
        self.assertEqual(list(self.scope_state.iterdir()), [])
        self.assertFalse((self.home / ".cache/am").exists())

    def test_L7_refuses_filesystem_root(self):
        self.assert_writable_refused(Path("/"))

    def test_L7_refuses_home_folder(self):
        self.assert_writable_refused(self.home)

    def test_L7_refuses_private_folder(self):
        self.assert_writable_refused(self.private)

    def test_L7_refuses_ancestor_of_private_folder(self):
        self.private = self.home / "records/private"
        self.assert_writable_refused(self.private.parent)
        self.assertFalse(self.private.parent.exists())

    def test_L7_refuses_descendant_of_private_folder(self):
        self.assert_writable_refused(self.private / "nested")
        self.assertFalse(self.private.exists())

    def test_L7_refuses_symlink_to_resolved_private_folder(self):
        self.private.mkdir()
        alias = self.home / "private-alias"
        alias.symlink_to(self.private, target_is_directory=True)
        self.assert_writable_refused(alias)

    def test_L7_refuses_writable_roots_for_claude(self):
        for role in ("builder", "verifier"):
            with self.subTest(role=role):
                root = self.home / (role + "-cache")
                result = self.launch(role, "claude", "--author-family", "codex",
                                     "--writable", root)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("only for the codex family", result.stderr)
                self.assertFalse(root.exists())
                self.assertFalse(self.record.exists())

    def test_L7_verifier_refuses_roots_overlapping_repository(self):
        nested = self.base / "repositories/repo"
        nested.parent.mkdir()
        shutil.move(self.repo, nested)
        self.repo = nested
        alias = self.home / "repo-alias"
        alias.symlink_to(self.repo, target_is_directory=True)
        for root in (self.repo, self.repo.parent, self.repo / "cache", alias):
            with self.subTest(root=root):
                self.record.unlink(missing_ok=True)
                result = self.launch("verifier", "codex", "--author-family", "claude",
                                     "--writable", root)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("unsafe --writable root " + str(root.resolve()), result.stderr)
                self.assertIn("repository", result.stderr)
                self.assertFalse(self.record.exists())
                self.assertIn("Result: launch refused\n", self.out.read_text())
                self.assertEqual(list(self.scope_state.iterdir()), [])
                self.assertFalse((self.home / ".cache/am").exists())
        self.assertFalse((self.repo / "cache").exists())

    def test_POOL_R5_writable_roots_cannot_overlap_session_or_pool_caches(self):
        for setting in (None, "relative-cache", str(self.base / "custom-cache")):
            if setting is None:
                self.env.pop("XDG_CACHE_HOME", None)
            else:
                self.env["XDG_CACHE_HOME"] = setting
            cache = Path(setting) if setting and Path(setting).is_absolute() else self.home / ".cache"
            cache.mkdir(exist_ok=True)
            alias = self.base / "cache-alias"
            alias.unlink(missing_ok=True)
            alias.symlink_to(cache, target_is_directory=True)
            for name in ("am", "am-pool"):
                for root in (cache, cache / name, cache / name / "nested", alias / name):
                    for role in ("builder", "verifier"):
                        with self.subTest(cache=setting, role=role, root=root):
                            result = self.launch(role, "codex", "--author-family", "claude", "--writable", root)
                            self.assertEqual(result.returncode, 2, result.stderr)
                            self.assertIn("unsafe --writable root " + str(root.resolve()), result.stderr)
                            self.assertIn("launcher sessions or checkout pools", result.stderr)
                            self.assertFalse(self.record.exists())
                            self.assertIn("Result: launch refused\n", self.out.read_text())
                            self.assertFalse((cache / name).exists())

    def test_failed_session_prints_only_last_twenty_log_lines(self):
        self.env["SESSION_EXIT"] = "7"
        self.agent_extra('''
for i in range(35):
    print(f"agent stderr {i}", file=sys.stderr)
''')
        for scoped in (True, False):
            with self.subTest(scoped=scoped):
                self.configure_scope(present=scoped)
                result = self.launch()
                self.assertEqual(result.returncode, 7, result.stderr)
                lines = [f"agent stderr {i}" for i in range(35)]
                warning = [] if scoped else ["systemd-run unavailable; running without a scope"]
                self.assertEqual(result.stderr.splitlines(), warning + lines[-20:])
                self.assertEqual(self.out.with_suffix(".log").read_text().splitlines(), lines)

    def test_missing_agent_executable_fails_before_session_setup(self):
        for family in ("codex", "claude"):
            with self.subTest(family=family):
                missing = self.base / (family + "-not-installed")
                self.env[f"AGENT_{family.upper()}_CMD"] = str(missing)
                result = self.launch("verifier", family, "--author-family",
                                     "claude" if family == "codex" else "codex")
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("Cannot find agent executable: " + str(missing), result.stderr)
                self.assertFalse(self.record.exists())
                self.assertIn("Result: launch refused\n", self.out.read_text())
                self.assertFalse(self.out.with_suffix(".log").exists())
                self.assertFalse((self.home / ".cache/am").exists())
                self.assertEqual(list(self.scope_state.iterdir()), [])
                self.assertEqual(self.non_pool_worktrees().count("worktree "), 1)

    def test_L8_missing_worktree_fallback_removes_only_matching_registration(self):
        self.busy_pool()
        linked = self.base / "linked source with spaces"
        self.git("worktree", "add", "--detach", str(linked), "HEAD")
        self.repo = linked
        user = self.base / "missing user checkout"
        self.git("worktree", "add", "--detach", str(user), "HEAD")
        shutil.rmtree(user)
        registrations = self.non_pool_worktrees()
        real_git = shutil.which("git")
        (self.bin / "git").unlink()
        cleanup_order = self.base / "cleanup-state"
        self.env.update(CLEANUP_STATE=str(cleanup_order), SESSION_EXIT="7", EVIDENCE="1")
        (self.bin / "git").write_text(
            "#!" + sys.executable + "\nimport os, pathlib, shutil, sys\n"
            "args = sys.argv[1:]\n"
            "if 'worktree' in args and 'remove' in args:\n"
            "    root = pathlib.Path.home() / '.cache/am'\n"
            "    pathlib.Path(os.environ['CLEANUP_STATE']).write_text(str(any(root.iterdir())))\n"
            "    shutil.rmtree(args[-1])\n"
            "    print('forced worktree removal failure', file=sys.stderr)\n    sys.exit(31)\n"
            f"os.execv({real_git!r}, [{real_git!r}, *args])\n")
        (self.bin / "git").chmod(0o755)
        result = self.launch("verifier", "codex", "--author-family", "claude")
        self.assertEqual(result.returncode, 7, result.stderr)
        self.assertIn("Cleanup failed: worktree remove", result.stderr)
        record = json.loads(self.record.read_text())
        self.assertIn(record["cwd"], result.stderr)
        self.assertEqual(cleanup_order.read_text(), "True")
        self.assertFalse(Path(record["cwd"]).parent.exists())
        self.assertEqual(self.non_pool_worktrees(), registrations)
        self.assertIn("Cleanup: failed:", self.out.read_text())
        self.assertTrue(self.out.read_text().endswith("answer\n  unchanged\n"))
        self.assertEqual((self.out.parent / "answer-evidence/proof.txt").read_text(), "proof")

    def test_L8_worktree_and_temporary_cleanup_failures_keep_session_status(self):
        self.busy_pool()
        real_git = shutil.which("git")
        (self.bin / "git").unlink()
        (self.bin / "git").write_text(
            "#!" + sys.executable + "\nimport os, sys\n"
            "args = sys.argv[1:]\n"
            "if 'worktree' in args and 'remove' in args:\n"
            "    print('forced worktree cleanup failure', file=sys.stderr)\n    sys.exit(31)\n"
            f"os.execv({real_git!r}, [{real_git!r}, *args])\n")
        (self.bin / "git").chmod(0o755)
        # Inject only the failing filesystem boundary in a separate launcher process.
        site = self.base / "injection"
        site.mkdir()
        (site / "sitecustomize.py").write_text(
            "import tempfile\n"
            "def fail(self):\n    raise OSError('forced temporary cleanup failure')\n"
            "tempfile.TemporaryDirectory.cleanup = fail\n")
        self.env.update(PYTHONPATH=str(site), SESSION_EXIT="7", EVIDENCE="1")
        result = self.launch("verifier", "codex", "--author-family", "claude")
        self.assertEqual(result.returncode, 7, result.stderr)
        self.assertIn("Cleanup failed: worktree remove", result.stderr)
        self.assertIn("Cleanup failed: temporary folder", result.stderr)
        self.assertIn(str(Path(json.loads(self.record.read_text())["cwd"])), result.stderr)
        self.assertIn("forced temporary cleanup failure", result.stderr)
        self.assertIn("Cleanup: failed:", self.out.read_text())
        self.assertTrue(self.out.read_text().endswith("answer\n  unchanged\n"))
        self.assertTrue((self.out.parent / "answer-evidence/proof.txt").exists())

    def assert_launch_refused(self, result, reason, scopes=()):
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn(reason, result.stderr)
        output = self.out.read_text()
        self.assertIn("Result: launch refused\n", output)
        self.assertIn(reason, output)
        self.assertIn("Exit code: 2\n", output)
        self.assertFalse(self.record.exists())
        self.assertEqual(set(self.scope_state.iterdir()), set(scopes))

    def round_results(self, *headers):
        self.out.parent.mkdir(parents=True, exist_ok=True)
        paths = []
        for number, header in enumerate(headers):
            path = self.out.parent / f"review-{number}.attempt-7.txt"
            path.write_text(header)
            paths.append(path)
        return paths

    def test_ROUND_R1_first_two_launches_run_then_third_is_refused(self):
        for _ in range(2):
            result = self.launch("verifier", "codex", "--author-family", "claude")
            self.assertEqual(result.returncode, 0, result.stderr)
        self.record.unlink()
        scopes = set(self.scope_state.iterdir())
        pool = {p.name: p.stat().st_mtime_ns for p in self.pool_root().iterdir()}
        result = self.launch("verifier", "codex", "--author-family", "claude")
        self.assert_launch_refused(result, "round-decision.md", scopes)
        self.assertEqual({p.name: p.stat().st_mtime_ns for p in self.pool_root().iterdir()}, pool)

    def test_ROUND_R2_refusal_precedes_leasing_and_explains_options(self):
        marker = self.base / "preflight-ran"
        self.install_preflight(f'pathlib.Path({str(marker)!r}).touch()')
        self.round_results("Role: verifier\n\nanswer", "Role: verifier\nResult: interrupted\n")
        self.admission_fixture([{"/proc/pressure/io": "full avg10=10\n"}],
                               hook='raise AssertionError("admission waited before round refusal")')
        result = self.launch("verifier", "codex", "--author-family", "claude")
        self.assert_launch_refused(result, "round-decision.md")
        self.assertFalse(self.pool_root().exists())
        self.assertFalse(marker.exists())
        self.assertEqual(json.loads(self.admission_events.read_text()), [])
        for phrase in ("regular file (not a symlink)", "non-empty after stripping whitespace",
                       "modified after the newest counted verifier result", "table of cases",
                       "expected result per case", "what must keep working", "re-scope or redesign",
                       "with its reason, named in the delivery", "retry with its reason",
                       "environment, contention, flaky check"):
            self.assertIn(phrase, self.out.read_text())

    def test_ROUND_R3_fresh_decision_runs_and_records_round(self):
        self.round_results("Role: verifier\n\nanswer", "Role: verifier\n\nanswer")
        self.round_decision()
        decision = self.out.parent / "round-decision.md"
        decision.write_text("The launcher does not judge this text.\n")
        result = self.launch("verifier", "codex", "--author-family", "claude")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Round: 3; decision: round-decision.md\n", self.out.read_text())
        self.assertTrue(self.record.exists())

    def test_ROUND_R2_output_cannot_supply_its_own_decision(self):
        self.round_results("Role: verifier\n\nanswer", "Role: verifier\n\nanswer")
        self.round_decision()
        self.out = self.out.parent / "round-decision.md"
        original = self.out.read_bytes()
        entries = set(self.out.parent.iterdir())
        result = self.launch("verifier", "codex", "--author-family", "claude")
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("--out must not replace round-decision.md", result.stderr)
        self.assertEqual(self.out.read_bytes(), original)
        self.assertEqual(set(self.out.parent.iterdir()), entries)
        self.assertFalse(self.record.exists())
        self.assertFalse(self.pool_root().exists())

    def test_ROUND_R2_reserved_decision_output_is_refused_for_every_role(self):
        self.out.parent.mkdir()
        for role in ("builder", "investigator", "verifier", "reviewer", "challenger"):
            with self.subTest(role=role):
                self.out = self.out.parent / "round-decision.md"
                result = self.launch(role, "codex", "--author-family", "claude")
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("round-decision.md", result.stderr)
                self.assertEqual(list(self.out.parent.iterdir()), [])
                self.assertFalse(self.record.exists())

    def test_ROUND_R4_each_later_launch_needs_a_newer_decision(self):
        self.round_results("Role: verifier\n\nanswer", "Role: verifier\n\nanswer")
        self.round_decision()
        result = self.launch("verifier", "codex", "--author-family", "claude")
        self.assertEqual(result.returncode, 0, result.stderr)
        decision = self.out.parent / "round-decision.md"
        newest = self.out.stat().st_mtime_ns
        os.utime(decision, ns=(newest, newest))
        self.record.unlink()
        scopes = set(self.scope_state.iterdir())
        result = self.launch("verifier", "codex", "--author-family", "claude")
        self.assert_launch_refused(result, "round-decision.md", scopes)
        self.round_decision()
        result = self.launch("verifier", "codex", "--author-family", "claude")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Round: 4; decision: round-decision.md\n", self.out.read_text())

    def test_ROUND_R5_counts_each_result_archive_failure_and_interruption_once(self):
        paths = self.round_results("Role: verifier\n\nanswer", "Role: verifier\nResult: interrupted\n",
                                  "Role: verifier\nRole: verifier\nResult: failed: cannot save answer\n",
                                  "Role: verifier\nResult: running\n",
                                  "Role: verifier\nResult: launch refused\n")
        self.round_decision()
        future = time.time_ns() + 10**12
        os.utime(paths[-1], ns=(future, future))
        result = self.launch("verifier", "codex", "--author-family", "claude")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Round: 5; decision: round-decision.md\n", self.out.read_text())

    def test_ROUND_R5_all_started_result_statuses_count_with_any_stem_or_suffix(self):
        self.out.parent.mkdir()
        statuses = ("done", "blocked", "failed", "signal", "", "running")
        for number, status in enumerate(statuses):
            name = f"arbitrary-{number}" + (".attempt-2.data" if number % 2 else ".md")
            (self.out.parent / name).write_text(f"Role: verifier\nResult: {status}\n\n")
        self.round_decision()
        result = self.launch("verifier", "codex", "--author-family", "claude")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Round: 7; decision: round-decision.md\n", self.out.read_text())

    def test_ROUND_R5_long_refusal_headers_never_count(self):
        self.round_results("Role: verifier\nResult: done\n")
        for _ in range(3):
            result = self.launch("verifier", "codex", "--author-family", "claude",
                                 "--same-family-reason", "x" * 70000, "--timeout", "0")
            self.assert_launch_refused(result, "--timeout must be positive")
        result = self.launch("verifier", "codex", "--author-family", "claude")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("Round:", self.out.read_text())
        for path in self.out.parent.glob("answer.attempt-*.md"):
            header = path.read_bytes().split(b"\n\n", 1)[0]
            self.assertLess(len(header), 65536)
            self.assertIn(b"Result: launch refused\n", header)

    def test_ROUND_R5_abandoned_running_marker_counts(self):
        self.round_results("Role: verifier\nResult: done\n", "Role: verifier\nResult: running\n")
        result = self.launch("verifier", "codex", "--author-family", "claude")
        self.assert_launch_refused(result, "round-decision.md")

    def test_ROUND_R5_legacy_refusal_beyond_header_bound_is_skipped(self):
        self.round_results("Role: verifier\nResult: done\n\n")
        legacy = self.out.parent / "legacy.attempt-1.md"
        # Earlier launchers did not cap header values; write their output directly.
        legacy.write_text("Role: verifier\nSame-family reason: " + "x" * 70000
                          + "\nResult: launch refused\n\n")
        original = legacy.read_bytes()
        result = self.launch("verifier", "codex", "--author-family", "claude")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("Round:", self.out.read_text())
        self.assertIn(str(legacy), result.stderr)
        self.assertIn("header does not end within 65536 bytes", result.stderr)
        self.assertEqual(legacy.read_bytes(), original)

    def test_ROUND_R6_evidence_files_are_never_opened_or_counted(self):
        self.round_results("Role: verifier\nResult: done\n\n",
                           "Role: verifier\nResult: done\n\n")
        evidence = [self.out.parent / name for name in
                    ("other-evidence", "other.attempt-1-evidence")]
        for path in evidence:
            path.write_text("Role: verifier\nResult: running\n\n")
        self.round_decision()
        for path in evidence:
            os.utime(path, ns=(time.time_ns() + 10**12,) * 2)
        self.inject_launcher(f'''
import os
original = os.open
def guarded(path, *args, **kwargs):
    if str(path) in {tuple(map(str, evidence))!r}:
        raise AssertionError("opened an evidence sibling")
    return original(path, *args, **kwargs)
os.open = guarded
''')
        result = self.launch("verifier", "codex", "--author-family", "claude")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Round: 3; decision: round-decision.md\n", self.out.read_text())

    def test_ROUND_R6_launcher_artifacts_and_non_headers_never_count(self):
        self.round_results("Role: verifier\nResult: done\n", "Role: verifier\nResult: done\n")
        ignored = ("other.saving-abcdefgh", "other.log", "other.lock", "other.attempt-1.log",
                   "other.attempt-2.lock", "other.attempt-3.saving-abcdefgh")
        for name in ignored:
            (self.out.parent / name).write_text("Role: verifier\nResult: running\n")
        for name in ("other-evidence", "other.attempt-1-evidence", "subdirectory"):
            directory = self.out.parent / name
            directory.mkdir()
            (directory / "result.md").write_text("Role: verifier\nResult: done\n")
        (self.out.parent / "no-header.md").write_text("Notes\n\nRole: verifier\nResult: done\n")
        (self.out.parent / "another-role.md").write_text("Role: builder\nResult: done\n")
        self.round_decision()
        # Ignored artifacts cannot make an otherwise fresh decision stale.
        for name in ignored:
            os.utime(self.out.parent / name, ns=(time.time_ns() + 10**12,) * 2)
        self.inject_launcher(f'''
import os
original = os.open
def guarded(path, *args, **kwargs):
    if str(path) in {tuple(str(self.out.parent / name) for name in ignored)!r}:
        raise AssertionError("opened a launcher artifact")
    return original(path, *args, **kwargs)
os.open = guarded
''')
        result = self.launch("verifier", "codex", "--author-family", "claude")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Round: 3; decision: round-decision.md\n", self.out.read_text())

    def test_ROUND_R6_decision_with_verifier_header_never_counts_itself(self):
        self.round_results("Role: verifier\nResult: done\n", "Role: verifier\nResult: done\n")
        self.round_decision()
        decision = self.out.parent / "round-decision.md"
        decision.write_text("Role: verifier\nResult: done\n\nRetry: restored environment.\n")
        original = decision.read_bytes()
        result = self.launch("verifier", "codex", "--author-family", "claude")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Round: 3; decision: round-decision.md\n", self.out.read_text())
        self.assertEqual(decision.read_bytes(), original)

    def test_ROUND_R6_other_roles_and_directories_do_not_count_or_gate(self):
        self.round_results(*(f"Role: {role}\n\nanswer" for role in
                             ("builder", "investigator", "reviewer", "challenger")))
        elsewhere = self.base / "another-task"
        elsewhere.mkdir()
        for number in range(3):
            (elsewhere / f"verifier-{number}.md").write_text("Role: verifier\n\nanswer")
        for _ in range(2):
            result = self.launch("verifier", "codex", "--author-family", "claude")
            self.assertEqual(result.returncode, 0, result.stderr)
        for role in ("builder", "investigator", "reviewer", "challenger"):
            result = self.launch(role, "codex", "--author-family", "claude")
            self.assertEqual(result.returncode, 0, result.stderr)
        self.record.unlink()
        scopes = set(self.scope_state.iterdir())
        result = self.launch("verifier", "codex", "--author-family", "claude")
        self.assert_launch_refused(result, "round-decision.md", scopes)

    def test_ROUND_R7_empty_whitespace_symlink_and_special_decisions_are_refused(self):
        self.round_results("Role: verifier\n\nanswer", "Role: verifier\n\nanswer")
        decision = self.out.parent / "round-decision.md"
        target = self.base / "decision-target"
        target.write_text("Retry: environment restored.\n")
        original = target.stat()
        for kind in ("empty", "whitespace", "unicode-whitespace", "symlink", "fifo", "directory"):
            with self.subTest(kind=kind):
                if kind == "symlink":
                    decision.symlink_to(target)
                elif kind == "fifo":
                    os.mkfifo(decision)
                elif kind == "directory":
                    decision.mkdir()
                else:
                    decision.write_text({"empty": "", "whitespace": " \n\t\r",
                                         "unicode-whitespace": "\u2003\u00a0\n"}[kind])
                try:
                    result = self.launch("verifier", "codex", "--author-family", "claude")
                    self.assert_launch_refused(result, "round-decision.md")
                finally:
                    if kind == "directory":
                        decision.rmdir()
                    else:
                        decision.unlink()
        self.assertEqual(target.read_text(), "Retry: environment restored.\n")
        self.assertEqual(target.stat().st_mtime_ns, original.st_mtime_ns)

    def test_ROUND_R8_skips_links_special_files_and_unreadable_results(self):
        self.round_results("Role: verifier\n\nanswer", "Role: verifier\n\nanswer")
        target = self.base / "linked-result"
        target.write_text("Role: verifier\n\nanswer")
        (self.out.parent / "linked.md").symlink_to(target)
        os.link(target, self.out.parent / "hard-linked.md")
        os.mkfifo(self.out.parent / "pipe.md")
        (self.out.parent / "directory.md").mkdir()
        unreadable = self.out.parent / "unreadable.md"
        unreadable.write_text("Role: verifier\n\nanswer")
        self.inject_launcher(f'''
import os
original = os.open
def guarded(path, *args, **kwargs):
    if str(path) == {str(unreadable)!r}:
        raise PermissionError("forced unreadable result")
    if str(path) in {tuple(str(self.out.parent / name) for name in ("linked.md", "hard-linked.md", "pipe.md", "directory.md"))!r}:
        raise AssertionError("opened a planted link or special file")
    return original(path, *args, **kwargs)
os.open = guarded
''')
        original = target.stat()
        self.round_decision()
        result = self.launch("verifier", "codex", "--author-family", "claude")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(str(unreadable), result.stderr)
        self.assertIn("forced unreadable result", result.stderr)
        for name in ("linked.md", "hard-linked.md", "pipe.md"):
            self.assertIn(str(self.out.parent / name), result.stderr)
        self.assertIn("Round: 3; decision: round-decision.md\n", self.out.read_text())
        self.assertEqual(target.stat().st_mtime_ns, original.st_mtime_ns)
        self.assertEqual(target.read_text(), "Role: verifier\n\nanswer")

    def test_ROUND_R8_unreadable_decision_never_grants_launch(self):
        self.round_results("Role: verifier\n\nanswer", "Role: verifier\n\nanswer")
        self.round_decision()
        decision = self.out.parent / "round-decision.md"
        self.inject_launcher(f'''
import os
original = os.open
def guarded(path, *args, **kwargs):
    if str(path) == {str(decision)!r}:
        raise PermissionError("forced unreadable decision")
    return original(path, *args, **kwargs)
os.open = guarded
''')
        result = self.launch("verifier", "codex", "--author-family", "claude")
        self.assert_launch_refused(result, "round-decision.md")
        self.assertIn("forced unreadable decision", result.stderr)

    def test_ROUND_R8_reads_only_bounded_headers(self):
        self.round_results("Role: verifier\n\nanswer", "Role: verifier\n\nanswer")
        body = self.out.parent / "body.txt"
        body.write_text("Role: builder\n\nRole: verifier\n" + "x" * 100000)
        long = self.out.parent / "long.txt"
        long.write_text("x" * 100000 + "\nRole: verifier\n")
        marker = self.base / "read-sizes.json"
        self.inject_launcher(f'''
import os, json
original = os.fdopen
reads = {{}}
class HeaderReader:
    def __init__(self, stream, path):
        self.stream, self.path = stream, path
    def __getattr__(self, name):
        return getattr(self.stream, name)
    def __enter__(self):
        return self
    def __exit__(self, *args):
        return self.stream.__exit__(*args)
    def readline(self, size=-1):
        assert 0 < size <= 65536, size
        line = self.stream.readline(size)
        reads[self.path] = reads.get(self.path, 0) + len(line)
        assert reads[self.path] <= 65536, reads
        with open({str(marker)!r}, "w") as report:
            json.dump(reads, report)
        return line
def fdopen(fd, *args, **kwargs):
    stream = original(fd, *args, **kwargs)
    path = os.readlink(f"/proc/self/fd/{{fd}}")
    return HeaderReader(stream, path) if path in ({str(body)!r}, {str(long)!r}) else stream
os.fdopen = fdopen
''')
        self.round_decision()
        result = self.launch("verifier", "codex", "--author-family", "claude")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(marker.exists(), "result headers were not scanned")
        reads = json.loads(marker.read_text())
        self.assertEqual(reads[str(body)], len("Role: builder\n\n"))
        self.assertEqual(reads[str(long)], 65536)
        self.assertIn("Round: 3; decision: round-decision.md\n", self.out.read_text())

    def test_ROUND_R9_concurrent_different_stems_cannot_race_past_gate(self):
        self.round_results("Role: verifier\nResult: done\n")
        # Overlap the bounded header scans, as in the reported reproducer.
        for number in range(80):
            (self.out.parent / f"noise-{number:03d}").write_bytes(b"x\n" * 32768)
        self.env["EVIDENCE"] = "1"
        self.agent_extra("time.sleep(0.5)")
        release = self.base / "launch-all"
        self.inject_launcher(f'''
import pathlib, time
while not pathlib.Path({str(release)!r}).exists():
    time.sleep(0.01)
''')
        outputs = [self.out.with_name(f"parallel-{i}.md") for i in range(4)]
        records = [self.base / f"parallel-{i}.json" for i in range(4)]
        processes = []
        for out, record in zip(outputs, records):
            process = subprocess.Popen(
                [sys.executable, str(ROOT / "scripts/run.py"), "--role", "verifier",
                 "--family", "codex", "--author-family", "claude", "--before", "none",
                 "--brief", str(self.brief), "--out", str(out), "--repo", str(self.repo),
                 "--method", str(self.method), "--private", str(self.private)],
                env=dict(self.env, RECORD=str(record)), cwd=self.repo,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            self.addCleanup(self.reap_launcher, process)
            processes.append(process)
        release.touch()
        errors = [process.communicate(timeout=20)[1] for process in processes]
        self.assertEqual(sorted(process.returncode for process in processes), [0, 2, 2, 2], errors)
        for process, out, record in zip(processes, outputs, records):
            if process.returncode == 2:
                self.assertIn("Result: launch refused\n", out.read_text())
                self.assertIn("round-decision.md", out.read_text())
                self.assertFalse(record.exists(), "refused launch started an agent")
            else:
                self.assertTrue(record.exists())
                self.assertTrue(out.read_text().endswith("answer\n  unchanged\n"))
                self.assertEqual((out.with_name(out.stem + "-evidence") / "proof.txt").read_text(),
                                 "proof")
        self.assertEqual(len(list(self.scope_state.glob("*.stop"))), 1,
                         "only the admitted launch may lease a checkout and start a scope")
        self.assertEqual(list(self.out.parent.glob("*.attempt-*")), [
            self.out.parent / "review-0.attempt-7.txt"])
        self.assertEqual({path.name for path in self.out.parent.iterdir()},
                         {"review-0.attempt-7.txt"}
                         | {f"noise-{i:03d}" for i in range(80)}
                         | {out.name for out in outputs}
                         | {out.with_suffix(".lock").name for out in outputs}
                         | {out.with_suffix(".log").name for out, process in zip(outputs, processes)
                            if process.returncode == 0}
                         | {out.stem + "-evidence" for out, process in zip(outputs, processes)
                            if process.returncode == 0})

    def test_RESULTS_S1_refusals_record_reason_before_start(self):
        cases = (
            ("builder", "--before", "HEAD", "allowed only for verifiers"),
            ("reviewer", "--network", "only builder and verifier"),
            ("verifier", "--author-family", "codex", "same-family review"),
            ("builder", "--writable", self.private, "unsafe --writable"),
            ("builder", "--timeout", "0", "must be positive"),
            ("verifier", "--author-family", "claude", "--before", "missing", "returned non-zero"),
        )
        for role, *extra, reason in cases:
            with self.subTest(role=role, extra=extra):
                self.assert_launch_refused(self.launch(role, "codex", *extra), reason)
        self.brief.write_text(str(self.private))
        self.assert_launch_refused(self.launch(), "brief contains the private folder path")
        self.brief.unlink()
        self.assert_launch_refused(self.launch(), "No such file")
        self.brief.write_text("bounded assignment")
        self.inject_launcher(f'''
import pathlib
original = pathlib.Path.read_text
def unreadable(self, *args, **kwargs):
    if self == pathlib.Path({str(self.brief)!r}):
        raise PermissionError("brief is unreadable")
    return original(self, *args, **kwargs)
pathlib.Path.read_text = unreadable
''')
        self.assert_launch_refused(self.launch(), "brief is unreadable")
        self.env.pop("PYTHONPATH")
        (self.repo / "tracked").write_text("dirty")
        self.assert_launch_refused(self.launch("verifier", "codex", "--author-family", "claude"),
                                   "tracked files to match HEAD")
        self.repo = self.base / "unborn"
        self.repo.mkdir()
        self.git("init", "-q")
        self.assert_launch_refused(self.launch("verifier", "codex", "--author-family", "claude"),
                                   "requires a committed candidate")

    def test_RESULTS_S1_session_temporary_folder_refusal(self):
        self.inject_launcher('''
import pathlib
original = pathlib.Path.mkdir
def mkdir(self, *args, **kwargs):
    if self.name == "tmp" and self.parent.parent.name == "am":
        raise PermissionError("forced session tmp failure")
    return original(self, *args, **kwargs)
pathlib.Path.mkdir = mkdir
''')
        self.assert_launch_refused(self.launch(), "forced session tmp failure")
        self.assertEqual(list((self.home / ".cache/am").iterdir()), [])

    def test_RESULTS_S2_archives_every_outcome_without_clobbering(self):
        self.out.parent.mkdir()
        (self.out.parent / "answer.attempt-1.log").write_bytes(b"reserved\x00\xff")
        for number, outcome in enumerate(("success", "failure", "empty", "timeout", "refusal", "interrupt"), 2):
            with self.subTest(outcome=outcome):
                self.round_decision()
                self.env.update(SESSION_EXIT="7" if outcome == "failure" else "0",
                                NO_ANSWER="1" if outcome == "empty" else "",
                                SLEEP="1" if outcome in ("timeout", "interrupt") else "",
                                EVIDENCE="1", SESSION_STDERR=outcome)
                if outcome == "interrupt":
                    process = self.start_launcher()
                    self.wait_record()
                    process.terminate()
                    _, stderr = process.communicate(timeout=10)
                    self.assertEqual(process.returncode, 143, stderr)
                else:
                    extra = ("--timeout", "0.3") if outcome == "timeout" else ()
                    if outcome == "refusal":
                        extra = ("--author-family", "codex")
                    result = self.launch("verifier", "codex", "--author-family", "claude", *extra)
                    self.assertEqual(result.returncode,
                                     {"success": 0, "failure": 7, "empty": 1, "timeout": 124, "refusal": 2}[outcome],
                                     result.stderr)
                previous = self.out.read_bytes()
                log = self.out.with_suffix(".log")
                previous_log = log.read_bytes() if log.exists() else None
                evidence = self.out.parent / "answer-evidence"
                if evidence.exists():
                    (evidence / "link").symlink_to("absent")
                self.record.unlink(missing_ok=True)
                scopes = set(self.scope_state.iterdir())
                refused = self.launch("reviewer")
                self.assert_launch_refused(refused, "require --author-family", scopes)
                # The first launch in each later iteration also archives the preceding refusal.
                attempt = 2 + (number - 2) * 2
                archived = self.out.parent / f"answer.attempt-{attempt}.md"
                self.assertEqual(archived.read_bytes(), previous)
                archived_log = archived.with_suffix(".log")
                if previous_log is not None:
                    self.assertEqual(archived_log.read_bytes(), previous_log)
                if evidence.exists():
                    self.fail("refusal left active evidence")
                archived_evidence = archived.with_name(archived.stem + "-evidence")
                if outcome != "refusal":
                    self.assertEqual((archived_evidence / "proof.txt").read_bytes(), b"proof")
                    self.assertEqual((archived_evidence / "link").readlink(), Path("absent"))
        self.assertEqual((self.out.parent / "answer.attempt-1.log").read_bytes(), b"reserved\x00\xff")

    def test_RESULTS_S3_simultaneous_launches_share_no_files(self):
        self.env.update(SLEEP="1", EVIDENCE="1", SESSION_STDERR="winner log")
        records = [self.base / f"concurrent-{i}.json" for i in range(2)]
        processes = [self.start_launcher(record=record) for record in records]
        deadline = time.monotonic() + 10
        while all(p.poll() is None for p in processes) and time.monotonic() < deadline:
            time.sleep(0.02)
        losers = [i for i, p in enumerate(processes) if p.poll() is not None]
        self.assertEqual(len(losers), 1)
        loser = losers[0]
        _, stderr = processes[loser].communicate(timeout=10)
        self.assertEqual(processes[loser].returncode, 2, stderr)
        self.assertIn("Cannot prepare result destination", stderr)
        self.assertFalse(records[loser].exists())
        winner = 1 - loser
        self.wait_record(records[winner])
        self.assertIn("Result: running\n", self.out.read_text())
        processes[winner].terminate()
        _, stderr = processes[winner].communicate(timeout=10)
        self.assertEqual(processes[winner].returncode, 143, stderr)
        self.assertTrue(self.out.read_bytes().endswith(b"answer\n  unchanged\n"))
        self.assertEqual(self.out.with_suffix(".log").read_bytes(), b"winner log\n")
        self.assertEqual((self.out.parent / "answer-evidence/proof.txt").read_bytes(), b"proof")
        self.assertEqual(list(self.out.parent.glob("*.attempt-*")), [])

    def wait_path(self, path):
        deadline = time.monotonic() + 10
        while not path.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue(path.exists(), f"launcher did not reach {path}")

    def test_RESULTS_S4_sigkill_in_checkout_and_agent_keeps_running(self):
        marker = self.base / "preparing"
        self.inject_launcher(f'''
import pathlib, subprocess, time
original = subprocess.check_output
def prepare(command, *args, **kwargs):
    if "worktree" in command and "add" in command:
        pathlib.Path({str(marker)!r}).touch()
        time.sleep(30)
    return original(command, *args, **kwargs)
subprocess.check_output = prepare
''')
        process = self.start_launcher()
        self.wait_path(marker)
        placeholder = self.out.read_bytes()
        process.kill()
        process.communicate(timeout=10)
        self.assertEqual(process.returncode, -signal.SIGKILL)
        self.assertEqual(self.out.read_bytes(), placeholder)
        self.assertIn(b"Result: running\n", placeholder)
        self.assertIn(b"Start UTC:", placeholder)
        self.assertNotIn(b"End UTC:", placeholder)
        self.assertFalse(self.record.exists())
        self.env.pop("PYTHONPATH")
        self.env["SLEEP"] = "1"
        process = self.start_launcher()
        record = self.wait_record()
        placeholder = self.out.read_bytes()
        process.kill()
        process.communicate(timeout=10)
        self.assertEqual(process.returncode, -signal.SIGKILL)
        self.assertEqual(self.out.read_bytes(), placeholder)
        self.assertIn(b"Result: running\n", placeholder)
        # The real lease guard must still release the checkout after its parent dies.
        deadline = time.monotonic() + 10
        while Path(record["tmpdir"]).parent.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertFalse(Path(record["tmpdir"]).parent.exists())

    def test_RESULTS_S5_sigkill_during_result_replace_never_partially_writes(self):
        for after in (False, True):
            with self.subTest(after_replace=after):
                marker = self.base / f"saving-{after}"
                self.inject_launcher(f'''
import os, pathlib, time
original = os.replace
def replace(source, destination, *args, **kwargs):
    final = pathlib.Path(source).read_bytes() if ".saving-" in str(source) else b""
    if destination == pathlib.Path({str(self.out)!r}) and b"Exit code:" in final:
        if {after!r}:
            original(source, destination, *args, **kwargs)
        pathlib.Path({str(marker)!r}).touch()
        time.sleep(30)
    return original(source, destination, *args, **kwargs)
os.replace = replace
''')
                process = self.start_launcher(role="builder")
                self.wait_path(marker)
                contents = self.out.read_bytes()
                process.kill()
                process.communicate(timeout=10)
                self.assertEqual(self.out.read_bytes(), contents)
                if after:
                    self.assertIn(b"Cleanup: pending\n", contents)
                    self.assertTrue(contents.endswith(b"answer\n  unchanged\n"))
                else:
                    self.assertIn(b"Result: running\n", contents)
                    self.assertNotIn(b"Exit code:", contents)
                stale = list(self.out.parent.glob("answer.saving-*"))
                self.assertEqual(bool(stale), not after)
                unrelated = self.out.parent / "other.saving-keep"
                unrelated.write_text("unrelated writer")
                self.env.pop("PYTHONPATH")
                result = self.launch()
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(list(self.out.parent.glob("answer.saving-*")), [])
                self.assertEqual(unrelated.read_text(), "unrelated writer")

    def test_RESULTS_S6_unexpected_cleanup_and_lease_errors_continue(self):
        self.inject_launcher('''
import os, subprocess, tempfile
original_output, original_fdopen = subprocess.check_output, os.fdopen
class Lease:
    def __init__(self, stream):
        self.stream = stream
    def fileno(self):
        return self.stream.fileno()
    def close(self):
        self.stream.close()
        raise RuntimeError("unexpected lease close failure")
def fdopen(fd, *args, **kwargs):
    stream = original_fdopen(fd, *args, **kwargs)
    return Lease(stream) if args == ("a+b",) else stream
def check_output(command, *args, **kwargs):
    if "worktree" in command and "list" in command:
        raise RuntimeError("unexpected worktree cleanup failure")
    return original_output(command, *args, **kwargs)
def cleanup(self):
    raise RuntimeError("unexpected folder cleanup failure")
os.fdopen = fdopen
# Install the worktree failure only after an answer exists, during cleanup.
original_run = subprocess.run
def run(command, *args, **kwargs):
    if "stop" in command:
        subprocess.check_output = check_output
    return original_run(command, *args, **kwargs)
subprocess.run = run
tempfile.TemporaryDirectory.cleanup = cleanup
''')
        self.env["EVIDENCE"] = "1"
        result = self.launch("verifier", "codex", "--author-family", "claude")
        self.assertEqual(result.returncode, 3, result.stderr)
        output = self.out.read_text()
        self.assertIn("Cleanup: failed:", output)
        for reason in ("unexpected worktree cleanup failure", "unexpected lease close failure",
                       "unexpected folder cleanup failure"):
            self.assertIn(reason, output)
            self.assertIn(reason, result.stderr)
        self.assertTrue(output.endswith("answer\n  unchanged\n"))
        self.assertEqual((self.out.parent / "answer-evidence/proof.txt").read_text(), "proof")

    def test_RESULTS_S6_unexpected_worktree_removal_continues(self):
        marker = self.base / "second-removed"
        self.agent_extra('''
for name in ("first", "second"):
    subprocess.check_output(["git", "worktree", "add", "--detach",
                             str(pathlib.Path(os.environ["TMPDIR"]) / name), "HEAD"],
                            stderr=subprocess.STDOUT)
''')
        self.inject_launcher(f'''
import pathlib, subprocess
original = subprocess.check_output
def remove(command, *args, **kwargs):
    if "worktree" in command and "remove" in command:
        if pathlib.Path(command[-1]).name == "first":
            raise RuntimeError("unexpected first removal failure")
        result = original(command, *args, **kwargs)
        pathlib.Path({str(marker)!r}).touch()
        return result
    return original(command, *args, **kwargs)
subprocess.check_output = remove
''')
        result = self.launch()
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertIn("unexpected first removal failure", self.out.read_text())
        self.assertTrue(marker.exists(), "later worktree cleanup did not run")
        self.assertTrue(self.out.read_text().endswith("answer\n  unchanged\n"))
        self.assertFalse(Path(json.loads(self.record.read_text())["tmpdir"]).parent.exists())

    def test_RESULTS_S7_unwritable_destination_preserves_existing_files(self):
        self.out.parent.mkdir()
        self.out.write_bytes(b"previous answer\x00\xff")
        self.out.with_suffix(".log").write_bytes(b"previous log")
        self.out.parent.chmod(0o555)
        self.addCleanup(self.out.parent.chmod, 0o755)
        result = self.launch()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Cannot prepare result destination", result.stderr)
        self.assertEqual(self.out.read_bytes(), b"previous answer\x00\xff")
        self.assertEqual(self.out.with_suffix(".log").read_bytes(), b"previous log")
        self.assertFalse(self.record.exists())

    def test_RESULTS_S7_nonregular_lock_is_refused_without_following_it(self):
        self.out.parent.mkdir()
        lock = self.out.with_suffix(".lock")
        target = self.base / "unrelated-lock"
        target.write_bytes(b"keep lock target")
        for kind in ("symlink", "fifo"):
            with self.subTest(kind=kind):
                if kind == "symlink":
                    lock.symlink_to(target)
                else:
                    os.mkfifo(lock)
                result = self.launch()
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("Cannot prepare result destination", result.stderr)
                self.assertEqual(target.read_bytes(), b"keep lock target")
                self.assertFalse(self.out.exists())
                self.assertFalse(self.record.exists())
                lock.unlink()

    def test_RESULTS_S8_fixture_root_ignores_long_and_non_ascii_inheritance(self):
        for name in ("x" * 70, "é"):
            with self.subTest(name=name):
                inherited = self.base / name
                inherited.mkdir()
                probe = subprocess.run(
                    [sys.executable, "-c",
                     f"import runpy, tempfile; runpy.run_path({str(ROOT / 'scripts/test_scripts.py')!r}); "
                     "fixture = tempfile.TemporaryDirectory(); print(fixture.name); fixture.cleanup()"],
                    env=dict(self.env, TMPDIR=str(inherited)), text=True, capture_output=True)
                self.assertEqual(probe.returncode, 0, probe.stderr)
                fixture = probe.stdout.strip()
                self.assertTrue(fixture.isascii())
                self.assertLessEqual(len(os.fsencode(fixture + "/home/.cache/am/12345678/tmp")), 62)
                self.assertEqual(list(inherited.iterdir()), [])

    def test_RESULTS_S1_unexpected_exceptions_before_agent_are_refused(self):
        for stage in ("brief", "checkout", "spawn"):
            with self.subTest(stage=stage):
                self.inject_launcher(f'''
import pathlib, subprocess
original_read = pathlib.Path.read_text
original_output = subprocess.check_output
original_spawn = subprocess.Popen
def read(self, *args, **kwargs):
    if {stage!r} == "brief" and self == pathlib.Path({str(self.brief)!r}):
        raise RuntimeError("unexpected brief failure")
    return original_read(self, *args, **kwargs)
def output(command, *args, **kwargs):
    if {stage!r} == "checkout" and "worktree" in command and "add" in command:
        raise RuntimeError("unexpected checkout failure")
    return original_output(command, *args, **kwargs)
def spawn(command, *args, **kwargs):
    if {stage!r} == "spawn" and "--scope" in command:
        raise RuntimeError("unexpected spawn failure")
    return original_spawn(command, *args, **kwargs)
pathlib.Path.read_text = read
subprocess.check_output = output
subprocess.Popen = spawn
''')
                self.assert_launch_refused(
                    self.launch("reviewer", "codex", "--author-family", "claude"),
                    f"unexpected {stage} failure")
                self.assertNotIn("Cleanup: ok", self.out.read_text())

    def test_RESULTS_S1_argument_errors_record_refusal(self):
        for extra, reason in (
                (("--unknown-option",), "unrecognized arguments"),
                (("--timeout", "invalid"), "invalid float value"),
                (("--role", "invalid"), "invalid choice")):
            with self.subTest(extra=extra):
                self.assert_launch_refused(self.launch("builder", "codex", *extra), reason)
        self.assert_launch_refused(self.run_script("run.py", "--out", self.out),
                                   "the following arguments are required")

    def pause_result_stage(self, stage, marker, release):
        self.inject_launcher(f'''
import os, pathlib, shutil, subprocess, tempfile, time
out = pathlib.Path({str(self.out)!r})
def pause():
    pathlib.Path({str(marker)!r}).touch()
    while not pathlib.Path({str(release)!r}).exists():
        time.sleep(0.01)
original_replace = os.replace
def replace(source, destination, *args, **kwargs):
    contents = pathlib.Path(source).read_bytes() if ".saving-" in str(source) else b""
    selected = destination == out and (
        ({stage!r} == "claim" and b"Result: running\\n" in contents) or
        ({stage!r} in ("save", "saved") and b"Cleanup: pending\\n" in contents) or
        ({stage!r} in ("finish", "finished") and b"Cleanup: ok\\n" in contents))
    if selected and {stage!r} in ("claim", "saved", "finished"):
        result = original_replace(source, destination, *args, **kwargs)
        pause()
        return result
    if selected:
        pause()
    return original_replace(source, destination, *args, **kwargs)
os.replace = replace
original_copy = shutil.copytree
def copy(source, destination, *args, **kwargs):
    if {stage!r} == "evidence" and pathlib.Path(destination) == out.with_name(out.stem + "-evidence"):
        pause()
    return original_copy(source, destination, *args, **kwargs)
shutil.copytree = copy
original_output = subprocess.check_output
def output(command, *args, **kwargs):
    if ({stage!r} == "worktrees" and "worktree" in command and "list" in command
            and out.exists() and b"Cleanup: pending\\n" in out.read_bytes()):
        pause()
    return original_output(command, *args, **kwargs)
subprocess.check_output = output
original_fdopen = os.fdopen
class Lease:
    def __init__(self, stream, kind):
        self.stream, self.kind = stream, kind
    def fileno(self):
        return self.stream.fileno()
    def close(self):
        if ({stage!r}, self.kind) in (("leases", "lease"), ("unlock", "result")):
            pause()
        self.stream.close()
def fdopen(fd, *args, **kwargs):
    stream = original_fdopen(fd, *args, **kwargs)
    if args in (("a+b",), ("a",)):
        return Lease(stream, "lease" if args == ("a+b",) else "result")
    return stream
os.fdopen = fdopen
original_cleanup = tempfile.TemporaryDirectory.cleanup
def cleanup(self):
    if {stage!r} == "folder" and pathlib.Path(self.name).parent.name == "am":
        pause()
    return original_cleanup(self)
tempfile.TemporaryDirectory.cleanup = cleanup
''')
        self.addCleanup(release.touch)

    def test_RESULTS_early_signals_are_handled_during_claim(self):
        for sig in (signal.SIGINT, signal.SIGTERM):
            with self.subTest(signal=sig):
                marker, release = self.base / f"claim-{sig}", self.base / f"release-claim-{sig}"
                self.pause_result_stage("claim", marker, release)
                process = self.start_launcher(role="builder")
                self.wait_path(marker)
                self.assertIn("Result: running\n", self.out.read_text())
                process.send_signal(sig)
                release.touch()
                _, stderr = process.communicate(timeout=10)
                self.assertEqual(process.returncode, 128 + sig, stderr)
                self.assertIn("Result: interrupted\n", self.out.read_text())
                self.assertFalse(self.record.exists())

    def test_RESULTS_signal_at_final_write_preserves_answer(self):
        self.env.update(SESSION_EXIT="7", EVIDENCE="1")
        for sig in (signal.SIGINT, signal.SIGTERM):
            with self.subTest(signal=sig):
                self.inject_launcher(f'''
import os, pathlib, signal
original = os.replace
writes = 0
def replace(source, destination, *args, **kwargs):
    global writes
    contents = pathlib.Path(source).read_bytes() if ".saving-" in str(source) else b""
    if destination == pathlib.Path({str(self.out)!r}) and b"Exit code:" in contents:
        writes += 1
        if writes == 2:
            assert pathlib.Path({str(self.out.parent / 'answer-evidence/proof.txt')!r}).read_text() == "proof"
            os.kill(os.getpid(), {int(sig)})
    return original(source, destination, *args, **kwargs)
os.replace = replace
''')
                result = self.launch("verifier", "codex", "--author-family", "claude")
                self.assertEqual(result.returncode, 128 + sig, result.stderr)
                output = self.out.read_text()
                self.assertIn("Cleanup: ok\n", output)
                self.assertIn("Exit code: 7\n", output)
                self.assertTrue(output.endswith("answer\n  unchanged\n"))
                self.assertNotIn("Result: interrupted", output)

    def test_RESULTS_signals_during_save_and_cleanup_preserve_answer(self):
        self.env.update(EVIDENCE="1", SESSION_EXIT="7")
        for stage in ("save", "saved", "evidence", "worktrees", "leases", "folder", "finish", "finished", "unlock"):
            for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGKILL):
                with self.subTest(stage=stage, signal=sig):
                    marker = self.base / f"{stage}-{sig}"
                    release = self.base / f"release-{stage}-{sig}"
                    self.pause_result_stage(stage, marker, release)
                    process = self.start_launcher()
                    self.wait_path(marker)
                    contents = self.out.read_bytes()
                    with self.out.with_suffix(".lock").open() as lock:
                        with self.assertRaises(BlockingIOError):
                            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    if stage == "save":
                        self.assertIn(b"Result: running\n", contents)
                    else:
                        cleanup = b"ok" if stage in ("finished", "unlock") else b"pending"
                        self.assertIn(b"Cleanup: " + cleanup + b"\n", contents)
                        self.assertIn(b"Exit code: 7\n", contents)
                        self.assertTrue(contents.endswith(b"answer\n  unchanged\n"))
                    if stage in ("worktrees", "leases", "folder", "finish", "finished", "unlock"):
                        self.assertEqual((self.out.parent / "answer-evidence/proof.txt").read_text(), "proof")
                    process.send_signal(sig)
                    release.touch()
                    _, stderr = process.communicate(timeout=10)
                    if sig == signal.SIGKILL:
                        self.assertEqual(process.returncode, -sig, stderr)
                        self.assertEqual(self.out.read_bytes(), contents)
                    else:
                        self.assertEqual(process.returncode, 128 + sig, stderr)
                        output = self.out.read_bytes()
                        self.assertIn(b"Cleanup: ok\n", output)
                        self.assertIn(b"Exit code: 7\n", output)
                        self.assertTrue(output.endswith(b"answer\n  unchanged\n"))
                        self.assertNotIn(b"Result: interrupted\n", output)
                    with self.out.with_suffix(".lock").open() as lock:
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_RESULTS_archive_suffix_and_no_attempt_locks(self):
        for suffix in (".txt", ""):
            with self.subTest(suffix=suffix):
                self.out = self.base / "results" / ("answer" + suffix)
                self.out.parent.mkdir(exist_ok=True)
                self.out.write_bytes(b"original answer")
                reserved = self.out.with_name("answer.attempt-1" + suffix)
                reserved.write_bytes(b"reserved")
                result = self.launch()
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(reserved.read_bytes(), b"reserved")
                self.assertEqual(self.out.with_name("answer.attempt-2" + suffix).read_bytes(), b"original answer")
                self.assertEqual(list(self.out.parent.glob("*.attempt-*.lock")), [])
                self.assertEqual(list(self.out.parent.glob("*.lock")), [self.out.with_suffix(".lock")])
                for path in self.out.parent.iterdir():
                    if path.is_file():
                        path.unlink()

    def test_RESULTS_log_output_alias_is_refused_before_claim(self):
        self.out = self.base / "answer.log"
        self.out.write_bytes(b"previous log answer")
        result = self.launch()
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("--out must differ from its sibling .log path", result.stderr)
        self.assertEqual(self.out.read_bytes(), b"previous log answer")
        self.assertFalse(self.record.exists())
        self.assertEqual(list(self.base.glob("answer.attempt-*")), [])
        self.assertFalse(self.out.with_suffix(".lock").exists())

    def test_RESULTS_R6_hardlinked_answer_cannot_change_private_inode(self):
        secret = self.base / "secret"
        secret.write_bytes(b"private answer must stay private")
        secret.chmod(0o600)
        self.agent_extra(f'''
answer = pathlib.Path(args[args.index("-o") + 1])
answer.unlink()
os.link({str(secret)!r}, answer)
pathlib.Path(os.environ["RECORD"] + ".links").write_text(str(answer.stat().st_nlink))
''')
        self.inject_launcher('''
import os, pathlib
original = os.open
def open(path, flags, *args, **kwargs):
    path = pathlib.Path(path)
    if path.name == "answer" and path.parent.parent.name == "am":
        raise PermissionError("forced answer read failure")
    return original(path, flags, *args, **kwargs)
os.open = open
''')
        result = self.launch()
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(stat.S_IMODE(secret.stat().st_mode), 0o600)
        self.assertEqual(secret.stat().st_nlink, int(Path(str(self.record) + ".links").read_text()))
        session = Path(json.loads(self.record.read_text())["tmpdir"]).parent
        self.assertTrue(session.is_dir())
        self.assertIn(f"; answer left in {session}\n", self.out.read_text())
        self.assertNotIn(secret.read_text(), result.stderr + self.out.read_text())

    def test_RESULTS_R6_evidence_root_symlink_is_not_copied(self):
        outside = self.base / "outside"
        outside.mkdir()
        (outside / "secret").write_text("outside evidence")
        self.agent_extra(f'''
assert evidence is not None
evidence.rmdir()
evidence.symlink_to({str(outside)!r}, target_is_directory=True)
''')
        result = self.launch("verifier", "codex", "--author-family", "claude")
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertFalse(self.out.with_name("answer-evidence").exists())
        self.assertIn("Cleanup: failed: evidence", self.out.read_text())
        self.assertEqual((outside / "secret").read_text(), "outside evidence")

    def test_RESULTS_R6_signal_inside_main_exception_before_finalizing(self):
        self.inject_launcher(f'''
import os, pathlib, signal, sys
original = pathlib.Path.read_text
class Refusal(RuntimeError):
    pass
def read(self, *args, **kwargs):
    if self == pathlib.Path({str(self.brief)!r}):
        raise Refusal("brief failure")
    return original(self, *args, **kwargs)
pathlib.Path.read_text = read
def trace(frame, event, arg):
    if (frame.f_code.co_name == "main" and event == "line"
            and isinstance(frame.f_locals.get("error"), Refusal)
            and not frame.f_locals["result"].finalizing):
        sys.settrace(None)
        os.kill(os.getpid(), signal.SIGTERM)
    return trace
sys.settrace(trace)
''')
        result = self.launch()
        self.assertEqual(result.returncode, 143, result.stderr)
        self.assertIn("Result: interrupted\n", self.out.read_text())
        self.assertNotIn("Result: running", self.out.read_text())
        self.assertNotIn("Traceback", result.stderr)

    def test_RESULTS_R6_signal_after_final_finish_matches_file(self):
        self.inject_launcher('''
import os, signal, sys
original = os.close
def close(fd):
    value = original(fd)
    frame = sys._getframe(1)
    if frame.f_code.co_name == "cleanup_step":
        frame = frame.f_back
        if frame.f_code.co_name == "main" and fd == frame.f_locals.get("held_lock"):
            os.kill(os.getpid(), signal.SIGTERM)
    return value
os.close = close
''')
        for agent_code in (0, 7):
            with self.subTest(agent_code=agent_code):
                self.env["SESSION_EXIT"] = str(agent_code)
                result = self.launch()
                output = self.out.read_text()
                self.assertEqual(result.returncode, agent_code, result.stderr)
                self.assertIn(f"Exit code: {agent_code}\n", output)
                self.assertNotIn("Signal:", output)

    def test_RESULTS_R6_signal_at_claim_keeps_stem_lock(self):
        lock_state = self.base / "lock-state"
        self.inject_launcher(f'''
import fcntl, os, pathlib, signal, sys
def setattr(self, name, value):
    object.__setattr__(self, name, value)
    if name == "claimed" and value:
        os.kill(os.getpid(), signal.SIGTERM)
def trace(frame, event, arg):
    if frame.f_code.co_name == "claim" and event == "call":
        type(frame.f_locals["self"]).__setattr__ = setattr
        sys.settrace(None)
    return trace
sys.settrace(trace)
original = os.replace
def replace(source, destination, *args, **kwargs):
    if b"Result: interrupted\\n" in pathlib.Path(source).read_bytes():
        with pathlib.Path({str(self.out.with_suffix('.lock'))!r}).open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                state = "held"
            else:
                state = "unlocked"
            pathlib.Path({str(lock_state)!r}).write_text(state)
    return original(source, destination, *args, **kwargs)
os.replace = replace
''')
        result = self.launch()
        self.assertEqual(result.returncode, 143, result.stderr)
        self.assertEqual(lock_state.read_text(), "held")
        self.assertIn("Result: interrupted\n", self.out.read_text())
        self.assertNotIn("Traceback", result.stderr)

    def test_RESULTS_R6_non_utf8_log_tail_is_not_cleanup_failure(self):
        self.env["SESSION_EXIT"] = "7"
        self.agent_extra('''
os.write(2, b"binary stderr\\xff\\n")
''')
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts/run.py"), "--role", "builder",
             "--family", "codex", "--brief", str(self.brief), "--out", str(self.out),
             "--repo", str(self.repo), "--method", str(self.method), "--private", str(self.private)],
            env=self.env, cwd=self.repo, capture_output=True)
        self.assertEqual(result.returncode, 7, result.stderr)
        self.assertIn(b"binary stderr\xff\n", result.stderr)
        self.assertIn("Cleanup: ok\n", self.out.read_text())
        self.assertNotIn("Cleanup failed:", result.stderr.decode(errors="replace"))

    def test_RESULTS_R6_cannot_write_failure_reports_kept_folder_on_stderr(self):
        self.inject_launcher(f'''
import os, pathlib
original_replace, original_open = os.replace, os.open
def replace(source, destination, *args, **kwargs):
    if (destination == pathlib.Path({str(self.out)!r})
            and b"Result: running\\n" not in pathlib.Path(source).read_bytes()):
        raise OSError("forced result write failure")
    return original_replace(source, destination, *args, **kwargs)
def open(path, flags, *args, **kwargs):
    if pathlib.Path(path) == pathlib.Path({str(self.out)!r}):
        raise OSError("forced terminal write failure")
    return original_open(path, flags, *args, **kwargs)
os.replace, os.open = replace, open
''')
        result = self.launch()
        self.assertEqual(result.returncode, 1, result.stderr)
        session = Path(json.loads(self.record.read_text())["tmpdir"]).parent
        self.assertTrue(session.is_dir())
        self.assertEqual((session / "answer").read_bytes(), b"answer\n  unchanged\n")
        self.assertIn("Result: failed: cannot save answer: forced result write failure; "
                      f"answer left in {session}\n", result.stderr)
        self.assertFalse(self.out.exists())

    def assert_unsaved_answer(self, result, reason):
        self.assertEqual(result.returncode, 1, result.stderr)
        output = self.out.read_text()
        self.assertIn(f"Result: failed: cannot save answer: {reason}", output)
        self.assertEqual(output.count("End UTC:"), 1)
        self.assertNotIn("launch refused", output)
        self.assertIn("Exit code: 1\n", output)
        session = Path(json.loads(self.record.read_text())["tmpdir"]).parent
        self.assertTrue(session.is_dir())
        self.assertIn(f"; answer left in {session}\n", output)
        return session

    def test_RESULTS_R3_replace_failure_keeps_binary_answer(self):
        self.env["SESSION_EXIT"] = "7"
        self.agent_extra('''
pathlib.Path(args[args.index("-o") + 1]).write_bytes(b"answer\\x00\\xff\\n")
''')
        self.inject_launcher(f'''
import os, pathlib
original = os.replace
def replace(source, destination, *args, **kwargs):
    if (destination == pathlib.Path({str(self.out)!r})
            and b"Cleanup: pending\\n" in pathlib.Path(source).read_bytes()):
        raise OSError("forced answer replace failure")
    return original(source, destination, *args, **kwargs)
os.replace = replace
''')
        result = self.launch()
        session = self.assert_unsaved_answer(result, "forced answer replace failure")
        self.assertEqual((session / "answer").read_bytes(), b"answer\x00\xff\n")
        self.assertEqual(list(self.out.parent.glob("answer.saving-*")), [])
        # A later claim must keep the only saved copy of this failed answer.
        self.env.pop("PYTHONPATH")
        self.assertEqual(self.launch().returncode, 7)
        self.assertEqual((session / "answer").read_bytes(), b"answer\x00\xff\n")

    def test_RESULTS_R3_unreadable_answer_is_preserved_after_agent(self):
        self.agent_extra('''
pathlib.Path(args[args.index("-o") + 1]).chmod(0)
''')
        self.inject_launcher('''
import os, pathlib
original_open = os.open
def open(path, flags, *args, **kwargs):
    path = pathlib.Path(path)
    if path.name == "answer" and path.parent.parent.name == "am":
        raise PermissionError("forced answer read failure")
    return original_open(path, flags, *args, **kwargs)
os.open = open
''')
        result = self.launch()
        session = self.assert_unsaved_answer(result, "forced answer read failure")
        answer = session / "answer"
        self.assertEqual(stat.S_IMODE(answer.stat().st_mode), 0)
        answer.chmod(0o600)
        self.assertEqual(answer.read_bytes(), b"answer\n  unchanged\n")

    def test_RESULTS_R3_write_failure_keeps_answer(self):
        self.inject_launcher('''
import tempfile
original = tempfile.NamedTemporaryFile
class FailingWrite:
    def __init__(self, stream):
        self.stream = stream
    def __getattr__(self, name):
        return getattr(self.stream, name)
    def __enter__(self):
        return self
    def __exit__(self, *args):
        return self.stream.__exit__(*args)
    def write(self, contents):
        if b"Cleanup: pending\\n" in contents:
            raise OSError("forced answer write failure: disk full")
        return self.stream.write(contents)
def temporary(*args, **kwargs):
    return FailingWrite(original(*args, **kwargs))
tempfile.NamedTemporaryFile = temporary
''')
        result = self.launch()
        session = self.assert_unsaved_answer(result, "forced answer write failure: disk full")
        self.assertEqual((session / "answer").read_bytes(), b"answer\n  unchanged\n")

    def test_RESULTS_R3_readonly_destination_keeps_answer_in_session(self):
        self.agent_extra(f'''
pathlib.Path({str(self.out.parent)!r}).chmod(0o555)
''')
        self.addCleanup(self.out.parent.chmod, 0o755)
        result = self.launch()
        session = self.assert_unsaved_answer(result, "[Errno 13] Permission denied")
        self.assertEqual((session / "answer").read_bytes(), b"answer\n  unchanged\n")
        self.assertIn(f"; answer left in {session}\n", result.stderr)

    def test_RESULTS_R3_signal_inside_popen_stops_spawned_scope(self):
        self.env["SLEEP"] = "1"
        child_pid = self.base / "spawned-pid"
        self.inject_launcher(f'''
import os, pathlib, signal, subprocess, time
original = subprocess.Popen
def spawn(command, *args, **kwargs):
    process = original(command, *args, **kwargs)
    if "--scope" in command:
        pathlib.Path({str(child_pid)!r}).write_text(str(process.pid))
        process.stdin.write(b"test prompt")
        process.stdin.close()
        deadline = time.monotonic() + 5
        answer = pathlib.Path(command[command.index("-o") + 1])
        while (not pathlib.Path({str(self.record)!r}).exists()
               or not answer.exists() or not answer.stat().st_size) and time.monotonic() < deadline:
            time.sleep(0.01)
        os.kill(os.getpid(), signal.SIGTERM)
    return process
subprocess.Popen = spawn
''')
        for role in ("builder", "verifier"):
            with self.subTest(role=role):
                self.record.unlink(missing_ok=True)
                previous_stops = set(self.scope_state.glob("*.stop"))
                result = self.launch(role, "codex", "--author-family", "claude")
                try:
                    self.assertEqual(result.returncode, 143, result.stderr)
                    self.assertEqual(len(set(self.scope_state.glob("*.stop")) - previous_stops), 1, result.stderr)
                    record = json.loads(self.record.read_text())
                    self.assertFalse(Path(record["tmpdir"]).parent.exists())
                    self.assertTrue(self.out.read_bytes().endswith(b"answer\n  unchanged\n"))
                    self.assertEqual(self.out.read_text().count("End UTC:"), 1)
                finally:
                    if child_pid.exists():
                        try:
                            os.killpg(int(child_pid.read_text()), signal.SIGKILL)
                        except ProcessLookupError:
                            pass

    def closed_stderr_outcome(self, outcome):
        hook = self.base / "escaped-exception"
        extra = ()
        expected = {"success": 0, "timeout": 124, "refusal": 2, "cleanup": 3, "save": 1}[outcome]
        if outcome == "success":
            self.configure_scope(present=False)
        elif outcome == "timeout":
            self.env.update(SLEEP="1", SESSION_STDERR="timeout log tail")
            extra = ("--timeout", "0.3")
        elif outcome == "refusal":
            extra = ("--before", "HEAD")
        for closed in (True, False):
            with self.subTest(closed=closed):
                self.inject_launcher(f'''
import os, pathlib, sys, tempfile
sys.excepthook = lambda *args: pathlib.Path({str(hook)!r}).write_text("traceback")
sys.unraisablehook = lambda *args: pathlib.Path({str(hook)!r}).write_text("unraisable")
if {closed!r}:
    os.close(2)
else:
    reader, writer = os.pipe()
    os.close(reader)
    os.dup2(writer, 2)
    os.close(writer)
original_cleanup = tempfile.TemporaryDirectory.cleanup
def cleanup(self):
    original_cleanup(self)
    if {outcome!r} == "cleanup" and pathlib.Path(self.name).parent.name == "am":
        raise OSError("forced cleanup failure")
tempfile.TemporaryDirectory.cleanup = cleanup
original_replace = os.replace
def replace(source, destination, *args, **kwargs):
    if ({outcome!r} == "save" and destination == pathlib.Path({str(self.out)!r})
            and b"Cleanup: pending\\n" in pathlib.Path(source).read_bytes()):
        raise OSError("forced save failure")
    return original_replace(source, destination, *args, **kwargs)
os.replace = replace
''')
                result = self.launch("builder", "codex", *extra)
                self.assertEqual(result.returncode, expected, result.stderr)
                self.assertFalse(hook.exists())
                self.assertNotIn("Traceback", result.stdout + result.stderr)
                output = self.out.read_text()
                self.assertNotIn("Result: running", output)
                self.assertEqual(output.count("End UTC:"), 1)
                self.assertIn(f"Exit code: {expected if outcome != 'cleanup' else 0}\n", output)
                if outcome in ("success", "timeout", "cleanup"):
                    self.assertTrue(output.endswith("answer\n  unchanged\n"))
                    self.assertNotIn("cannot save", output)
                elif outcome == "refusal":
                    self.assertIn("Result: launch refused\n", output)
                    self.assertIn("allowed only for verifiers", output)
                else:
                    self.assertIn("Result: failed: cannot save answer: forced save failure; answer left in ", output)
                if outcome == "cleanup":
                    self.assertIn("Cleanup: failed:", output)

    def test_RESULTS_R5_closed_stderr_success(self):
        self.closed_stderr_outcome("success")

    def test_RESULTS_R5_closed_stderr_timeout(self):
        self.closed_stderr_outcome("timeout")

    def test_RESULTS_R5_closed_stderr_refusal(self):
        self.closed_stderr_outcome("refusal")

    def test_RESULTS_R5_closed_stderr_cleanup_failure(self):
        self.closed_stderr_outcome("cleanup")

    def test_RESULTS_R5_closed_stderr_cannot_save(self):
        self.closed_stderr_outcome("save")

    def test_RESULTS_R5_answer_symlink_does_not_expose_or_chmod_private_file(self):
        secret = self.base / "secret"
        secret.write_bytes(b"private answer must never be exposed")
        secret.chmod(0o600)
        self.agent_extra(f'''
answer = pathlib.Path(args[args.index("-o") + 1])
answer.unlink()
answer.symlink_to({str(secret)!r})
''')
        result = self.launch()
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(stat.S_IMODE(secret.stat().st_mode), 0o600)
        self.assertEqual(secret.stat().st_nlink, 1)
        self.assertNotIn(secret.read_text(), self.out.read_text() + result.stderr)
        self.assertIn("Result: failed: cannot save answer:", self.out.read_text())

    def test_RESULTS_R5_header_values_cannot_forge_exit_or_cleanup(self):
        reason = "reason\nExit code: 0\r\t\x1b\x85"
        name = "oversized\nExit code: 0\r\t\x1b\x85"
        with (self.repo / name).open("wb") as stream:
            stream.truncate(5 * 1024 * 1024 + 1)
        for refused in (False, True):
            with self.subTest(refused=refused):
                extra = ("--before", "HEAD") if refused else ()
                role = "builder" if refused else "reviewer"
                result = self.launch(role, "codex", "--author-family", "claude",
                                     "--same-family-reason", reason, *extra)
                self.assertEqual(result.returncode, 2 if refused else 0, result.stderr)
                output = self.out.read_text()
                self.assertIn("Same-family reason: reason\\nExit code: 0\\r\\t\\x1b\\x85\n", output)
                self.assertEqual(output.count("\nExit code:"), 1)
                if not refused:
                    self.assertIn("Skipped untracked (>5 MB): oversized\\nExit code: 0\\r\\t\\x1b\\x85\n", output)
                    self.assertIn("Cleanup: ok\nExit code: 0\n", output)

    def test_RESULTS_R5_path_header_values_are_single_line(self):
        fake = self.base / "fake\nExit code: 0\t\x1b"
        fake.write_text(FAKE)
        fake.chmod(0o755)
        self.env["AGENT_CODEX_CMD"] = str(fake)
        result = self.launch()
        self.assertEqual(result.returncode, 0, result.stderr)
        output = self.out.read_text()
        self.assertIn("fake\\nExit code: 0\\t\\x1b\n", output)
        self.assertEqual(output.count("\nExit code:"), 1)
        self.assertIn("Cleanup: ok\nExit code: 0\n", output)

    def test_RESULTS_R5_late_signal_is_recorded_for_every_final_result(self):
        for outcome in ("answer", "refusal", "save", "interrupted"):
            for stage in ("write", "unlock"):
                with self.subTest(outcome=outcome, stage=stage):
                    self.inject_launcher(f'''
import os, pathlib, signal
original_replace, original_close, original_read = os.replace, os.fdopen, pathlib.Path.read_text
def read(self, *args, **kwargs):
    if {outcome!r} == "interrupted" and self == pathlib.Path({str(self.brief)!r}):
        raise KeyboardInterrupt(signal.SIGINT)
    return original_read(self, *args, **kwargs)
pathlib.Path.read_text = read
def replace(source, destination, *args, **kwargs):
    contents = pathlib.Path(source).read_bytes() if ".saving-" in str(source) else b""
    if {outcome!r} == "save" and b"Cleanup: pending\\n" in contents:
        raise OSError("forced save failure")
    value = original_replace(source, destination, *args, **kwargs)
    if ({stage!r} == "write" and destination == pathlib.Path({str(self.out)!r})
            and b"Result: running\\n" not in contents):
        os.kill(os.getpid(), signal.SIGTERM)
    return value
os.replace = replace
class Lock:
    def __init__(self, stream):
        self.stream = stream
    def fileno(self):
        return self.stream.fileno()
    def close(self):
        os.kill(os.getpid(), signal.SIGTERM)
        self.stream.close()
def fdopen(fd, *args, **kwargs):
    stream = original_close(fd, *args, **kwargs)
    return Lock(stream) if {stage!r} == "unlock" and args == ("a",) else stream
os.fdopen = fdopen
''')
                    extra = ("--before", "HEAD") if outcome == "refusal" else ()
                    result = self.launch("builder", "codex", *extra)
                    self.assertEqual(result.returncode, 143, result.stderr)
                    output = self.out.read_text()
                    self.assertIn("\nSignal: 15\n", output)
                    self.assertEqual(output.count("\nSignal:"), 1)
                    self.assertEqual(output.count("End UTC:"), 1)
                    if outcome == "answer":
                        self.assertTrue(output.endswith("answer\n  unchanged\n"))
                    else:
                        expected = {"refusal": "launch refused", "save": "failed: cannot save answer:",
                                    "interrupted": "interrupted"}[outcome]
                        self.assertIn("Result: " + expected, output)

    def test_RESULTS_R5_secondary_exception_keeps_original_terminal_result(self):
        self.inject_launcher(f'''
import pathlib, shutil, subprocess
original_spawn, original_rmtree = subprocess.Popen, shutil.rmtree
def spawn(command, *args, **kwargs):
    if "--scope" in command:
        raise RuntimeError("original launch refusal")
    return original_spawn(command, *args, **kwargs)
def rmtree(path, *args, **kwargs):
    if pathlib.Path(path).parent.name == "am":
        raise RuntimeError("secondary cleanup error")
    return original_rmtree(path, *args, **kwargs)
subprocess.Popen, shutil.rmtree = spawn, rmtree
''')
        result = self.launch()
        self.assertEqual(result.returncode, 2, result.stderr)
        output = self.out.read_text()
        self.assertIn("Reason: original launch refusal\n", output)
        self.assertIn("Cleanup: failed:", output)
        self.assertNotIn("Traceback", result.stderr)

    def test_RESULTS_R5_first_signal_during_main_exception_keeps_original_reason(self):
        self.inject_launcher(f'''
import os, pathlib, signal
original = pathlib.Path.read_text
class Refusal(RuntimeError):
    def __str__(self):
        os.kill(os.getpid(), signal.SIGTERM)
        return "original brief refusal"
def read(self, *args, **kwargs):
    if self == pathlib.Path({str(self.brief)!r}):
        raise Refusal()
    return original(self, *args, **kwargs)
pathlib.Path.read_text = read
''')
        result = self.launch()
        self.assertEqual(result.returncode, 143, result.stderr)
        output = self.out.read_text()
        self.assertIn("Result: launch refused\nReason: original brief refusal\n", output)
        self.assertIn("Signal: 15\n", output)
        self.assertNotIn("Result: running", output)
        self.assertNotIn("Traceback", result.stderr)

    def test_RESULTS_R5_terminal_fallback_does_not_follow_result_symlink(self):
        secret = self.base / "secret"
        secret.write_text("keep private bytes")
        secret.chmod(0o600)
        self.inject_launcher(f'''
import os, pathlib
original = os.replace
def replace(source, destination, *args, **kwargs):
    if (destination == pathlib.Path({str(self.out)!r})
            and b"Result: launch refused\\n" in pathlib.Path(source).read_bytes()):
        destination.unlink()
        destination.symlink_to({str(secret)!r})
        raise OSError("forced terminal replacement failure")
    return original(source, destination, *args, **kwargs)
os.replace = replace
''')
        result = self.launch("builder", "codex", "--before", "HEAD")
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(secret.read_text(), "keep private bytes")
        self.assertEqual(stat.S_IMODE(secret.stat().st_mode), 0o600)
        self.assertTrue(self.out.is_symlink())
        self.assertEqual(self.out.readlink(), secret)
        self.assertIn("holds a file the launcher does not own", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_RESULTS_R5_log_tail_does_not_read_agent_symlink(self):
        secret = self.base / "secret"
        secret.write_text("private log contents")
        secret.chmod(0o600)
        self.env["SESSION_EXIT"] = "7"
        self.agent_extra(f'''
log = pathlib.Path({str(self.out.with_suffix('.log'))!r})
log.unlink()
log.symlink_to({str(secret)!r})
''')
        result = self.launch()
        self.assertEqual(result.returncode, 7, result.stderr)
        self.assertNotIn(secret.read_text(), self.out.read_text() + result.stderr)
        self.assertIn("Cleanup: failed: session log tail:", self.out.read_text())
        self.assertTrue(self.out.read_text().endswith("answer\n  unchanged\n"))

    def test_RESULTS_R7_terminal_fallback_checks_links_and_identity_before_truncating(self):
        for replacement in ("hardlink", "extra-link", "single-link"):
            with self.subTest(replacement=replacement):
                secret = self.base / (replacement + "-secret")
                observed = self.base / (replacement + "-observed")
                secret.write_bytes(b"keep private bytes")
                self.inject_launcher(f'''
import atexit, os, pathlib
original = os.replace
def replace(source, destination, *args, **kwargs):
    if (destination == pathlib.Path({str(self.out)!r})
            and b"Result: launch refused\\n" in pathlib.Path(source).read_bytes()):
        secret = pathlib.Path({str(secret)!r})
        if {replacement!r} == "extra-link":
            secret.unlink()
            os.link(destination, secret)
        else:
            destination.unlink()
            if {replacement!r} == "hardlink":
                os.link(secret, destination)
            else:
                secret.rename(destination)
        stream = destination.open("rb")
        def observe():
            pathlib.Path({str(observed)!r}).write_bytes(stream.read())
            stream.close()
        atexit.register(observe)
        raise OSError("forced terminal replacement failure")
    return original(source, destination, *args, **kwargs)
os.replace = replace
''')
                result = self.launch("builder", "codex", "--before", "HEAD")
                self.assertEqual(result.returncode, 2, result.stderr)
                expected = (b"Result: running\n" if replacement == "extra-link"
                            else b"keep private bytes")
                self.assertIn(expected, observed.read_bytes())
                if replacement == "extra-link":
                    self.assertFalse(self.out.exists())
                else:
                    self.assertEqual(self.out.read_bytes(), b"keep private bytes")
                    self.assertIn("holds a file the launcher does not own", result.stderr)
                self.assertIn("result file changed:", result.stderr)

    def test_RESULTS_R7_log_tail_does_not_read_agent_hardlink(self):
        secret = self.base / "secret"
        secret.write_text("private log contents")
        self.env["SESSION_EXIT"] = "7"
        self.agent_extra(f'''
log = pathlib.Path({str(self.out.with_suffix('.log'))!r})
log.unlink()
os.link({str(secret)!r}, log)
''')
        result = self.launch()
        self.assertEqual(result.returncode, 7, result.stderr)
        self.assertNotIn(secret.read_text(), self.out.read_text() + result.stderr)
        self.assertIn("Cleanup: failed: session log tail:", self.out.read_text())
        self.assertEqual(secret.read_text(), "private log contents")

    def test_RESULTS_R7_stop_exception_skips_evidence_copy_and_keeps_session(self):
        self.env["EVIDENCE"] = "1"
        copied = self.base / "copied"
        self.inject_launcher(f'''
import pathlib, shutil, subprocess
original_run, original_copy = subprocess.run, shutil.copytree
def run(command, *args, **kwargs):
    if command[:3] == ["systemctl", "--user", "stop"]:
        raise RuntimeError("forced stop exception")
    return original_run(command, *args, **kwargs)
def copy(source, destination, *args, **kwargs):
    if pathlib.Path(destination) == pathlib.Path({str(self.out.with_name('answer-evidence'))!r}):
        pathlib.Path({str(copied)!r}).touch()
    return original_copy(source, destination, *args, **kwargs)
subprocess.run, shutil.copytree = run, copy
''')
        result = self.launch("verifier", "codex", "--author-family", "claude")
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertFalse(copied.exists())
        session = Path(json.loads(self.record.read_text())["tmpdir"]).parent
        self.assertEqual((session / "evidence/proof.txt").read_text(), "proof")
        output = self.out.read_text()
        self.assertIn("Cleanup: failed:", output)
        self.assertIn("forced stop exception", output)
        self.assertIn(f"session folder kept at {session}; evidence left in {session / 'evidence'}", output)

    def test_RESULTS_R7_stale_temp_names_require_eight_tempfile_characters(self):
        self.out.parent.mkdir()
        stale = self.out.parent / "answer.saving-a0_b1_c2"
        stale.write_bytes(b"stale temporary result")
        others = [self.out.parent / name for name in (
            "answer.saving-notes", "answer.saving-abcdefg", "answer.saving-abcdefghi",
            "answer.saving-ABCDEFGH", "answer.saving-abcd-efg")]
        for path in others:
            path.write_bytes(b"user file")
        result = self.launch()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(stale.exists())
        for path in others:
            self.assertTrue(path.exists(), f"deleted user file: {path}")
            self.assertEqual(path.read_bytes(), b"user file")

    def test_RESULTS_R5_failed_stderr_recovery_preserves_session_answer(self):
        self.agent_extra(f'''
pathlib.Path({str(self.out.parent)!r}).chmod(0o555)
''')
        self.addCleanup(self.out.parent.chmod, 0o755)
        self.inject_launcher('''
import os
reader, writer = os.pipe()
os.close(reader)
os.dup2(writer, 2)
os.close(writer)
''')
        result = self.launch()
        self.assertEqual(result.returncode, 1, result.stderr)
        session = Path(json.loads(self.record.read_text())["tmpdir"]).parent
        self.assertTrue(session.exists())
        self.assertEqual((session / "answer").read_bytes(), b"answer\n  unchanged\n")
        self.assertIn("Result: failed: cannot save answer:", self.out.read_text())

    def test_RESULTS_R4_non_utf8_tracked_filename_refusal_is_recorded(self):
        path = self.repo / os.fsdecode(b"modified-\xff")
        path.write_bytes(b"committed")
        self.git("add", "--", str(path))
        self.git("commit", "-qm", "non-UTF-8 filename")
        path.write_bytes(b"modified")
        result = self.launch("verifier", "codex", "--author-family", "claude")
        self.assert_launch_refused(result, "commit first: verifier requires tracked files to match HEAD")
        output = self.out.read_text()
        self.assertIn("modified-\\udcff", output)
        self.assertNotIn("Result: running", output)
        self.assertNotIn("Traceback", result.stderr)
        self.assertEqual(output.count("End UTC:"), 1)
        self.assertEqual(path.read_bytes(), b"modified")

    def test_RESULTS_R4_non_utf8_initial_header_is_recorded(self):
        result = self.launch("builder", "codex", "--same-family-reason", os.fsdecode(b"reason-\xff"),
                             "--before", "HEAD")
        self.assert_launch_refused(result, "allowed only for verifiers")
        self.assertIn("Same-family reason: reason-\\udcff\n", self.out.read_text())

    def test_RESULTS_R4_non_utf8_skipped_filename_in_terminal_headers(self):
        path = self.repo / os.fsdecode(b"oversized-\xff")
        with path.open("wb") as stream:
            stream.truncate(5 * 1024 * 1024 + 1)
        for interrupted in (False, True):
            with self.subTest(interrupted=interrupted):
                self.inject_launcher(f'''
import os, signal, subprocess
original = subprocess.Popen
def spawn(command, *args, **kwargs):
    if "--scope" in command:
        if {interrupted!r}:
            os.kill(os.getpid(), signal.SIGTERM)
            raise KeyboardInterrupt(signal.SIGTERM)
        raise RuntimeError("forced spawn failure")
    return original(command, *args, **kwargs)
subprocess.Popen = spawn
''')
                result = self.launch("reviewer", "codex", "--author-family", "claude")
                self.assertEqual(result.returncode, 143 if interrupted else 2, result.stderr)
                output = self.out.read_text()
                self.assertIn("Result: interrupted\n" if interrupted else "Result: launch refused\n", output)
                self.assertIn("Skipped untracked (>5 MB): oversized-\\udcff\n", output)
                self.assertEqual(output.count("End UTC:"), 1)
                self.assertNotIn("Result: running", output)
                self.assertNotIn("Traceback", result.stderr)
                self.assertFalse(self.record.exists())

    def test_RESULTS_R4_terminal_write_exceptions_do_not_leave_running(self):
        for fallback_fails in (False, True):
            with self.subTest(fallback_fails=fallback_fails):
                self.inject_launcher(f'''
import os, pathlib
original_replace, original_open = os.replace, pathlib.Path.open
original_os_open = os.open
def replace(source, destination, *args, **kwargs):
    if (destination == pathlib.Path({str(self.out)!r})
            and b"Result: launch refused\\n" in pathlib.Path(source).read_bytes()):
        raise RuntimeError("forced terminal replace failure")
    return original_replace(source, destination, *args, **kwargs)
def open(self, mode="r", *args, **kwargs):
    if {fallback_fails!r} and self == pathlib.Path({str(self.out)!r}) and mode == "wb":
        raise RuntimeError("forced terminal fallback failure")
    return original_open(self, mode, *args, **kwargs)
def os_open(path, flags, *args, **kwargs):
    if {fallback_fails!r} and pathlib.Path(path) == pathlib.Path({str(self.out)!r}) and flags & os.O_WRONLY:
        raise RuntimeError("forced terminal fallback failure")
    return original_os_open(path, flags, *args, **kwargs)
os.replace, pathlib.Path.open, os.open = replace, open, os_open
''')
                result = self.launch("builder", "codex", "--before", "HEAD")
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertNotIn("Traceback", result.stderr)
                if fallback_fails:
                    self.assertFalse(self.out.exists())
                    self.assertIn("Cannot record result", result.stderr)
                    self.assertIn("Result: launch refused\n", result.stderr)
                else:
                    self.assert_launch_refused(result, "allowed only for verifiers")

    def test_RESULTS_R4_evidence_destination_stems_are_refused(self):
        for name in ("answer-evidence", "answer-evidence.md", "answer-evidence.txt"):
            with self.subTest(name=name):
                self.out = self.base / name
                self.out.write_bytes(b"keep existing evidence")
                result = self.launch()
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("--out stem must not end in -evidence", result.stderr)
                self.assertEqual(self.out.read_bytes(), b"keep existing evidence")
                self.assertFalse(self.record.exists())
                self.assertFalse(self.out.with_suffix(".lock").exists())
                self.assertEqual(list(self.base.glob("answer-evidence.attempt-*")), [])

    def test_RESULTS_R3_double_signal_during_refusal_write_finishes_refusal(self):
        self.inject_launcher(f'''
import os, pathlib, signal
original = os.replace
def replace(source, destination, *args, **kwargs):
    if (destination == pathlib.Path({str(self.out)!r})
            and b"Result: launch refused\\n" in pathlib.Path(source).read_bytes()):
        os.kill(os.getpid(), signal.SIGTERM)
        os.kill(os.getpid(), signal.SIGINT)
    return original(source, destination, *args, **kwargs)
os.replace = replace
''')
        for extra, reason in ((("--before", "HEAD"), "allowed only for verifiers"),
                              (("--unknown-option",), "unrecognized arguments")):
            with self.subTest(extra=extra):
                result = self.launch("builder", "codex", *extra)
                self.assertEqual(result.returncode, 143, result.stderr)
                output = self.out.read_text()
                self.assertIn("Result: launch refused\n", output)
                self.assertIn(reason, output)
                self.assertEqual(output.count("End UTC:"), 1)
                self.assertNotIn("interrupted", output)
                self.assertFalse(self.record.exists())

    def test_RESULTS_R3_double_signal_during_failure_write_keeps_failure(self):
        self.inject_launcher(f'''
import os, pathlib, signal
original = os.replace
def replace(source, destination, *args, **kwargs):
    if destination == pathlib.Path({str(self.out)!r}):
        contents = pathlib.Path(source).read_bytes()
        if b"Cleanup: pending\\n" in contents:
            raise OSError("forced answer replace failure")
        if b"Result: failed: cannot save answer:" in contents:
            os.kill(os.getpid(), signal.SIGTERM)
            os.kill(os.getpid(), signal.SIGINT)
    return original(source, destination, *args, **kwargs)
os.replace = replace
''')
        result = self.launch()
        self.assertEqual(result.returncode, 143, result.stderr)
        self.assertIn("Result: failed: cannot save answer:", self.out.read_text())
        self.assertEqual(self.out.read_text().count("End UTC:"), 1)
        session = Path(json.loads(self.record.read_text())["tmpdir"]).parent
        self.assertEqual((session / "answer").read_bytes(), b"answer\n  unchanged\n")

    def test_RESULTS_R3_interrupted_header_and_double_signal_are_complete(self):
        self.inject_launcher(f'''
import os, pathlib, signal
original_read, original_replace = pathlib.Path.read_text, os.replace
def read(self, *args, **kwargs):
    if self == pathlib.Path({str(self.brief)!r}):
        os.kill(os.getpid(), signal.SIGTERM)
    return original_read(self, *args, **kwargs)
def replace(source, destination, *args, **kwargs):
    if (destination == pathlib.Path({str(self.out)!r})
            and b"Result: interrupted\\n" in pathlib.Path(source).read_bytes()):
        os.kill(os.getpid(), signal.SIGINT)
        os.kill(os.getpid(), signal.SIGHUP)
    return original_replace(source, destination, *args, **kwargs)
pathlib.Path.read_text, os.replace = read, replace
''')
        result = self.launch()
        self.assertEqual(result.returncode, 143, result.stderr)
        self.assertIn("Result: interrupted\n", self.out.read_text())
        self.assertEqual(self.out.read_text().count("End UTC:"), 1)
        self.assertFalse(self.record.exists())

    def test_RESULTS_R3_stale_temp_removal_preserves_other_destinations(self):
        self.out.parent.mkdir()
        own = self.out.parent / "answer.saving-abcdefgh"
        own.write_bytes(b"stale temporary result")
        others = [self.out.parent / name for name in (
            "answer.saving-x.md", "answer.saving-abcdefgh.more", "answer.saving-abcdefgh.lock")]
        for other in others:
            other.write_bytes(b"another destination")
        result = self.launch()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(own.exists())
        for other in others:
            self.assertTrue(other.exists(), f"deleted another destination: {other}")
            self.assertEqual(other.read_bytes(), b"another destination")

    def test_RESULTS_R3_reserved_destination_names_are_refused(self):
        for name in ("answer.saving-x.md", "answer.attempt-1.md",
                     "answer.saving-x", "answer.attempt-1"):
            with self.subTest(name=name):
                self.out = self.base / name
                self.out.write_bytes(b"keep existing answer")
                result = self.launch()
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("--out name must not contain", result.stderr)
                self.assertEqual(self.out.read_bytes(), b"keep existing answer")
                self.assertFalse(self.record.exists())
                self.assertFalse(self.out.with_suffix(".lock").exists())

    def test_RESULTS_R3_directory_and_aliases_leave_existing_files_untouched(self):
        for kind in ("directory", "log", "lock"):
            with self.subTest(kind=kind):
                self.record.unlink(missing_ok=True)
                self.out = self.base / ("answer.md" if kind == "directory" else "answer." + kind)
                if kind == "directory":
                    self.out.mkdir()
                    (self.out / "keep").write_bytes(b"keep directory contents")
                else:
                    self.out.write_bytes(b"keep alias contents")
                before = set(self.base.iterdir())
                result = self.launch()
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertEqual(set(self.base.iterdir()), before)
                if kind == "directory":
                    self.assertEqual((self.out / "keep").read_bytes(), b"keep directory contents")
                else:
                    self.assertEqual(self.out.read_bytes(), b"keep alias contents")
                self.assertFalse(self.record.exists())

    def test_RESULTS_R3_refusal_headers_have_one_end_and_parsed_role(self):
        self.inject_launcher('''
import subprocess
original = subprocess.Popen
def spawn(command, *args, **kwargs):
    if "--scope" in command:
        raise OSError("forced spawn failure")
    return original(command, *args, **kwargs)
subprocess.Popen = spawn
''')
        self.assert_launch_refused(self.launch(), "forced spawn failure")
        self.assertEqual(self.out.read_text().count("End UTC:"), 1)

    def test_RESULTS_R3_argument_error_role_is_parsed_or_omitted(self):
        for extra, role in ((("--unknown-option",), "builder"),
                            (("--timeout", "invalid"), "builder"),
                            (("--role", "invalid"), "builder")):
            with self.subTest(extra=extra):
                result = self.launch("builder", "codex", *extra)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn(f"Role: {role}\n", self.out.read_text())
                self.assertEqual(self.out.read_text().count("End UTC:"), 1)
        result = self.run_script("run.py", "--out", self.out)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertFalse(any(line.startswith("Role:") for line in self.out.read_text().splitlines()))
        self.assertEqual(self.out.read_text().count("End UTC:"), 1)

    def test_RESULTS_R3_result_modes_follow_umask_at_every_write(self):
        modes = self.base / "result-modes"
        self.inject_launcher(f'''
import os, pathlib, stat
os.umask(0o027)
original = os.replace
def replace(source, destination, *args, **kwargs):
    if destination == pathlib.Path({str(self.out)!r}):
        with open({str(modes)!r}, "a") as record:
            record.write(oct(stat.S_IMODE(pathlib.Path(source).stat().st_mode)) + "\\n")
    return original(source, destination, *args, **kwargs)
os.replace = replace
''')
        result = self.launch()
        self.assertEqual(result.returncode, 0, result.stderr)
        result = self.launch("builder", "codex", "--before", "HEAD")
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(modes.read_text().splitlines(), ["0o640"] * 5)
        self.assertEqual(stat.S_IMODE(self.out.stat().st_mode), 0o640)

    def test_RESULTS_R3_signal_inside_discovery_popen_stops_discovery_group(self):
        self.env["MCP_SLEEP"] = "1"
        discovery = Path(str(self.record) + ".discovery")
        child_pid = self.base / "discovery-pid"
        self.inject_launcher(f'''
import os, pathlib, signal, subprocess, time
original = subprocess.Popen
def spawn(command, *args, **kwargs):
    process = original(command, *args, **kwargs)
    if command[1:] == ["mcp", "list", "--json"]:
        pathlib.Path({str(child_pid)!r}).write_text(str(process.pid))
        deadline = time.monotonic() + 5
        while not pathlib.Path({str(self.record) + '.discovery-child'!r}).exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        os.kill(os.getpid(), signal.SIGTERM)
    return process
subprocess.Popen = spawn
''')
        result = self.launch("reviewer", "codex", "--author-family", "claude")
        try:
            self.assertEqual(result.returncode, 143, result.stderr)
            self.assertIn("Result: interrupted\n", self.out.read_text())
            self.assertEqual(self.out.read_text().count("End UTC:"), 1)
            self.assertTrue(discovery.exists())
            self.assert_discovery_cleaned()
        finally:
            if child_pid.exists():
                try:
                    os.killpg(int(child_pid.read_text()), signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def test_RESULTS_R3_double_signal_after_save_preserves_answer_and_first_signal(self):
        self.env["SESSION_EXIT"] = "7"
        self.inject_launcher(f'''
import os, pathlib, signal
original = os.replace
def replace(source, destination, *args, **kwargs):
    pending = b"Cleanup: pending\\n" in pathlib.Path(source).read_bytes()
    result = original(source, destination, *args, **kwargs)
    if destination == pathlib.Path({str(self.out)!r}) and pending:
        os.kill(os.getpid(), signal.SIGTERM)
        os.kill(os.getpid(), signal.SIGINT)
    return result
os.replace = replace
''')
        result = self.launch()
        self.assertEqual(result.returncode, 143, result.stderr)
        output = self.out.read_text()
        self.assertIn("Cleanup: ok\n", output)
        self.assertIn("Exit code: 7\n", output)
        self.assertEqual(output.count("End UTC:"), 1)
        self.assertTrue(output.endswith("answer\n  unchanged\n"))
        self.assertNotIn("Result: interrupted", output)

    def test_RESULTS_R3_refusal_write_failure_retains_exit_two(self):
        self.inject_launcher(f'''
import pathlib
original = pathlib.Path.read_text
def read(self, *args, **kwargs):
    if self == pathlib.Path({str(self.brief)!r}):
        pathlib.Path({str(self.out.parent)!r}).chmod(0o555)
        raise PermissionError("forced brief read failure")
    return original(self, *args, **kwargs)
pathlib.Path.read_text = read
''')
        self.addCleanup(self.out.parent.chmod, 0o755)
        self.assert_launch_refused(self.launch(), "forced brief read failure")
        self.assertEqual(self.out.read_text().count("End UTC:"), 1)

    def test_RESULTS_R3_cleanup_status_write_failure_keeps_saved_agent_status(self):
        self.inject_launcher(f'''
import os, pathlib
original = os.replace
def replace(source, destination, *args, **kwargs):
    if (destination == pathlib.Path({str(self.out)!r})
            and b"Cleanup: ok\\n" in pathlib.Path(source).read_bytes()):
        raise OSError("forced cleanup status failure")
    return original(source, destination, *args, **kwargs)
os.replace = replace
''')
        for agent_code in (0, 7):
            with self.subTest(agent_code=agent_code):
                self.env["SESSION_EXIT"] = str(agent_code)
                result = self.launch()
                self.assertEqual(result.returncode, agent_code or 3, result.stderr)
                output = self.out.read_text()
                self.assertIn("Cleanup: failed: result finalization: forced cleanup status failure\n", output)
                self.assertIn(f"Exit code: {agent_code}\n", output)
                self.assertEqual(output.count("End UTC:"), 1)
                self.assertTrue(output.endswith("answer\n  unchanged\n"))
                self.assertNotIn("Result: launch refused", output)

    def test_RESULTS_P3_output_in_repository_is_not_snapshot_input(self):
        self.out = self.repo / "results/answer.md"
        self.out.parent.mkdir()
        (self.out.parent / "answer.attempt-1.md").write_text("prior attempt")
        index = (self.repo / ".git/index").read_bytes()
        for role in ("challenger", "reviewer", "verifier"):
            for family in ("codex", "claude"):
                with self.subTest(role=role, family=family):
                    result = self.launch(role, family, "--author-family",
                                         "claude" if family == "codex" else "codex")
                    self.assertEqual(result.returncode, 0, result.stderr)
                    output = self.out.read_text()
                    if role == "verifier":
                        self.assertIn("Untracked files left out: none\n", output)
                    else:
                        snapshot = next(line.split(": ", 1)[1] for line in output.splitlines()
                                        if line.startswith("Snapshot commit:"))
                        self.assertEqual(self.git("ls-tree", "-r", snapshot, "--", "results"), "")
                    self.assertEqual((self.repo / ".git/index").read_bytes(), index)

    def test_RESULTS_P3_tracked_output_is_excluded_from_review_snapshot(self):
        self.out = self.repo / "answer.md"
        self.out.write_text("old tracked output")
        self.git("add", "answer.md")
        self.git("commit", "-qm", "tracked output")
        index = (self.repo / ".git/index").read_bytes()
        for role in ("challenger", "reviewer"):
            result = self.launch(role, "codex", "--author-family", "claude")
            self.assertEqual(result.returncode, 0, result.stderr)
            snapshot = next(line.split(": ", 1)[1] for line in self.out.read_text().splitlines()
                            if line.startswith("Snapshot commit:"))
            self.assertEqual(self.git("ls-tree", snapshot, "--", "answer.md"), "")
            self.assertEqual((self.repo / ".git/index").read_bytes(), index)

    def test_L9_concurrent_sessions_have_separate_folders_and_scopes(self):
        self.agent_extra('''
pathlib.Path(os.environ["RECORD"] + ".pids").write_text(str(os.getpid()))
''')
        self.addCleanup(self.kill_recorded_agents)
        self.env["SLEEP"] = "1"
        first_record, second_record = self.base / "first.json", self.base / "second.json"
        first_out, second_out = self.base / "first.md", self.base / "second.md"
        first = self.start_launcher(out=first_out, record=first_record)
        second = self.start_launcher(out=second_out, record=second_record)
        first_data, second_data = self.wait_record(first_record), self.wait_record(second_record)
        self.assertNotEqual(first_data["cwd"], second_data["cwd"])
        units = list(self.scope_state.iterdir())
        self.assertEqual(len(units), 2)
        first.terminate()
        first.communicate(timeout=10)
        self.assertEqual(first.returncode, 143)
        self.assertIsNone(second.poll())
        self.assertTrue(Path(second_data["cwd"]).is_dir())
        second.terminate()
        second.communicate(timeout=10)
        self.assertEqual(second.returncode, 143)
        first_unit = next(line for line in first_out.read_text().splitlines() if line.startswith("Scope: "))
        second_unit = next(line for line in second_out.read_text().splitlines() if line.startswith("Scope: "))
        self.assertNotEqual(first_unit, second_unit)
        self.assert_session_cleaned(first_data)
        self.assert_session_cleaned(second_data)

    def test_setup_dry_run_changes_nothing(self):
        before = self.files()
        result = self.setup_project("--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("AGENTS.md", result.stdout)
        self.assertIn("builder.md", result.stdout)
        self.assertEqual(self.files(), before)

    def setup_metadata(self, commit, dirty):
        site = self.base / "setup-injection"
        site.mkdir(exist_ok=True)
        (site / "sitecustomize.py").write_text(
            "import subprocess\noriginal = subprocess.check_output\n"
            "def metadata(command, *args, **kwargs):\n"
            f"    if command[:4] == ['git', '--no-optional-locks', '-C', {str(ROOT)!r}]:\n"
            f"        if command[4:] == ['rev-parse', 'HEAD']: return {commit + chr(10)!r}\n"
            f"        if command[4:] == ['status', '--porcelain']: return {' M fixture' if dirty else ''!r}\n"
            "    return original(command, *args, **kwargs)\n"
            "subprocess.check_output = metadata\n")
        self.env["PYTHONPATH"] = str(site)

    def test_E1_entry_records_commit_dirty_state_and_body_checksum(self):
        for dirty in (False, True):
            with self.subTest(dirty=dirty):
                (self.repo / "AGENTS.md").unlink(missing_ok=True)
                self.setup_metadata("0123456789abcdef" * 2 + "01234567", dirty)
                result = self.setup_project()
                self.assertEqual(result.returncode, 0, result.stderr)
                first, _, body = (self.repo / "AGENTS.md").read_bytes().partition(b"\n")
                self.assertIn(b"Method commit: 0123456789ab" + (b"-dirty" if dirty else b"") + b".", first)
                self.assertIn(b"Checksum: " + hashlib.sha256(body).hexdigest()[:12].encode() + b".", first)
                self.assertTrue(first.endswith(b" -->"))
                self.assertIn("{{PROJECT}}", (self.method / "AGENTS.md").read_text())
                self.assertEqual((self.method / "claude-builder.md").read_bytes(),
                                 (self.repo / ".claude/agents/builder.md").read_bytes())
                self.assertEqual((self.method / "SOURCE").read_text().splitlines()[2], f"Path: {ROOT}")

    def test_E2_commit_only_change_keeps_entry_bytes_and_timestamp(self):
        self.setup_metadata("1" * 40, False)
        self.assertEqual(self.setup_project().returncode, 0)
        entry = self.repo / "AGENTS.md"
        before = entry.read_bytes(), entry.stat().st_mtime_ns
        self.setup_metadata("2" * 40, True)
        result = self.setup_project()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Method commit: " + "1" * 12, entry.read_text().splitlines()[0])
        self.assertIn(f"Kept {entry}", result.stdout)
        self.assertEqual((entry.read_bytes(), entry.stat().st_mtime_ns), before)
        self.assertIn("Commit: " + "2" * 40, (self.method / "SOURCE").read_text())

    def test_E3_launcher_reports_each_stale_or_edited_file_every_run(self):
        self.assertEqual(self.setup_project().returncode, 0)
        entry, builder = self.repo / "AGENTS.md", self.repo / ".claude/agents/builder.md"
        for path in (entry, builder):
            path.write_text(path.read_text() + "hand edit\n")
        before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in (entry, builder)}
        command = (f"python3 {ROOT}/scripts/setup.py --project {self.repo} --id demo"
                   f" --method {self.method} --private {self.private} --dry-run")
        for role, extra in (("builder", ()), ("reviewer", ("--author-family", "claude"))):
            result = self.launch(role, "codex", *extra)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual([line for line in result.stderr.splitlines() if line.startswith("Warning:")], [
                f"Warning: {path} differs from the installed method; see {command}"
                for path in (entry, builder)])
            self.assertTrue(self.out.read_text().endswith("answer\n  unchanged\n"))
            self.assertEqual({path: (path.read_bytes(), path.stat().st_mtime_ns)
                              for path in (entry, builder)}, before)

    def test_E3_launcher_uses_selected_repo_method_and_ignores_entry_header(self):
        self.assertEqual(self.setup_project().returncode, 0)
        entry = self.repo / "AGENTS.md"
        first, body = entry.read_text().split("\n", 1)
        entry.write_text(first.replace(" -->", " Method commit: ffffffffffff. -->") + "\n" + body)
        result = self.launch()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        template = self.method / "AGENTS.md"
        template.write_text(template.read_text() + "method update\n")
        result = self.launch()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(result.stderr.splitlines()), 1)
        self.assertIn(f"Warning: {entry} differs", result.stderr)

    def test_E3_missing_template_warns_once_and_other_entries_are_ignored(self):
        self.assertEqual(self.setup_project().returncode, 0)
        (self.method / "AGENTS.md").unlink()
        result = self.launch()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(result.stderr.splitlines()), 1)
        self.assertIn("cannot check", result.stderr.lower())
        self.assertIn(str(self.method / "AGENTS.md"), result.stderr)
        for content in (None, "foreign instructions\nAgent System entry\n"):
            entry = self.repo / "AGENTS.md"
            if content is None:
                entry.unlink()
            else:
                entry.write_text(content)
            result = self.launch()
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stderr, "")

    def test_E3_entry_check_read_errors_warn_once_and_session_runs(self):
        self.assertEqual(self.setup_project().returncode, 0)
        for path in (self.repo / "AGENTS.md", self.repo / ".claude/agents/builder.md",
                     self.method / "SOURCE", self.method / "AGENTS.md",
                     self.method / "claude-builder.md"):
            for error in ("OSError", "ValueError"):
                with self.subTest(path=path, error=error):
                    self.inject_launcher(f'''
                        from pathlib import Path
                        original = Path.open
                        def read_error(self, *args, **kwargs):
                            if str(self) == {str(path)!r}:
                                raise {error}("forced read failure")
                            return original(self, *args, **kwargs)
                        Path.open = read_error
                    ''')
                    shutil.rmtree(self.base / "injection/__pycache__", ignore_errors=True)
                    self.record.unlink(missing_ok=True)
                    result = self.launch()
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stderr.splitlines(), [
                        f"Warning: cannot check {path}: forced read failure"])
                    self.assertTrue(self.record.is_file())
                    self.assertTrue(self.out.read_text().endswith("answer\n  unchanged\n"))

    def test_E3_binary_source_warns_once_and_session_runs(self):
        self.assertEqual(self.setup_project().returncode, 0)
        source = self.method / "SOURCE"
        source.write_bytes(b"Path: \xff\n")
        result = self.launch()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(result.stderr.splitlines()), 1)
        self.assertTrue(result.stderr.startswith(f"Warning: cannot check {source}: "))
        self.assertIn("utf-8", result.stderr)
        self.assertTrue(self.record.is_file())
        self.assertTrue(self.out.read_text().endswith("answer\n  unchanged\n"))

    def test_E3_warning_command_preserves_supplied_method_and_private(self):
        method_arg, private_arg = "~/custom method", "~/custom private"
        self.method = self.home / "custom method"
        self.private = self.home / "custom private"
        self.assertEqual(self.setup_project().returncode, 0)
        entry = self.repo / "AGENTS.md"
        entry.write_text(entry.read_text(encoding="utf-8") + "hand edit\n", encoding="utf-8")
        result = self.launch("builder", "codex", "--method", method_arg, "--private", private_arg)
        self.assertEqual(result.returncode, 0, result.stderr)
        command = shlex.join(["python3", str(ROOT / "scripts/setup.py"), "--project", str(self.repo),
                              "--id", "demo", "--method", method_arg, "--private", private_arg,
                              "--dry-run"])
        self.assertEqual(result.stderr.splitlines(), [
            f"Warning: {entry} differs from the installed method; see {command}"])

    def test_E3_builder_warning_without_source_and_optional_builder_files(self):
        self.assertEqual(self.setup_project().returncode, 0)
        (self.method / "SOURCE").unlink()
        builder = self.repo / ".claude/agents/builder.md"
        builder.write_text("old definition\n")
        result = self.launch()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(result.stderr.splitlines()), 1)
        self.assertIn(f"Warning: {builder} differs", result.stderr)
        self.assertIn(f"python3 setup.py --project {self.repo} --id demo"
                      f" --method {self.method} --private {self.private} --dry-run", result.stderr)
        (self.method / "claude-builder.md").unlink()
        result = self.launch()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        builder.unlink()
        result = self.launch()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")

    def test_E4_dry_run_diffs_project_and_new_private_files_and_lists_method_changes(self):
        (self.method / "obsolete.md").write_text("remove me\n")
        before = self.files()
        result = self.setup_project("--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.files(), before)
        for path in (self.repo / "AGENTS.md", self.repo / ".claude/agents/builder.md",
                     self.repo / ".git/info/exclude", self.private / "projects/demo/goals.md",
                     self.private / "user/preferences.md"):
            self.assertIn(f"--- {path}\n+++ {path}\n@@ ", result.stdout)
        for change, path in (("A", "SOURCE"), ("A", "claude-builder.md"),
                             ("M", "AGENTS.md"), ("D", "obsolete.md")):
            self.assertIn(f"{change} {self.method / path}\n", result.stdout)

    def test_E4_dry_run_keeps_existing_private_files_without_diff(self):
        self.assertEqual(self.setup_project().returncode, 0)
        paths = [self.private / "projects/demo" / name
                 for name in ("goals.md", "knowledge.md", "checks.md")]
        paths.append(self.private / "user/preferences.md")
        for path in paths:
            path.write_text("personal content\n")
        builder = self.repo / ".claude/agents/builder.md"
        builder.write_text("old builder\n")
        before = self.files()
        result = self.setup_project("--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"--- {builder}\n+++ {builder}\n@@ ", result.stdout)
        self.assertIn("-old builder", result.stdout)
        for path in paths:
            self.assertIn(f"Kept {path}\n", result.stdout)
            self.assertNotIn(f"--- {path}\n", result.stdout)
        self.assertEqual(self.files(), before)

    def test_E5_hand_edit_blocks_all_writes_until_forced(self):
        self.assertEqual(self.setup_project().returncode, 0)
        entry = self.repo / "AGENTS.md"
        entry.write_text(entry.read_text() + "manual edit\n")
        (self.method / "obsolete").write_text("old method\n")
        before = self.files()
        for extra in ((), ("--dry-run",)):
            result = self.setup_project(*extra)
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertIn("edited by hand; use --force to replace it", result.stdout + result.stderr)
            self.assertIn(f"--- {entry}\n+++ {entry}\n@@ ", result.stdout)
            self.assertIn("-manual edit", result.stdout)
            self.assertEqual(self.files(), before)
        result = self.setup_project("--force", "--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.files(), before)
        result = self.setup_project("--force")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("manual edit", entry.read_text())
        first, _, body = entry.read_bytes().partition(b"\n")
        self.assertIn(hashlib.sha256(body).hexdigest()[:12].encode(), first)

    def test_E6_legacy_entry_is_diffed_and_replaced_without_force(self):
        entry = self.repo / "AGENTS.md"
        entry.write_text("<!-- Agent System entry for project demo. -->\nlegacy text\n")
        before = self.files()
        result = self.setup_project("--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"--- {entry}\n+++ {entry}\n@@ ", result.stdout)
        self.assertIn("-legacy text", result.stdout)
        self.assertIn(". Checksum:", result.stdout)
        self.assertEqual(self.files(), before)
        result = self.setup_project()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Checksum:", entry.read_text().splitlines()[0])
        self.assertNotIn("legacy text", entry.read_text())

    def test_E6_current_legacy_entry_gains_metadata_without_force(self):
        self.assertEqual(self.setup_project().returncode, 0)
        entry = self.repo / "AGENTS.md"
        first, body = entry.read_text(encoding="utf-8").split("\n", 1)
        first = first.split(" Method commit:", 1)[0] + " -->"
        entry.write_text(first + "\n" + body, encoding="utf-8")
        before = self.files()
        result = self.setup_project("--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"--- {entry}\n+++ {entry}\n@@ ", result.stdout)
        self.assertIn(". Checksum:", result.stdout)
        self.assertEqual(self.files(), before)
        result = self.setup_project()
        self.assertEqual(result.returncode, 0, result.stderr)
        new_first, new_body = entry.read_text(encoding="utf-8").split("\n", 1)
        self.assertIn("Method commit:", new_first)
        self.assertIn("Checksum: " + hashlib.sha256(body.encode("utf-8")).hexdigest()[:12], new_first)
        self.assertEqual(new_body, body)

    def test_setup_text_files_use_utf8_under_ascii_locale(self):
        self.env.update(LC_ALL="C", PYTHONUTF8="0", PYTHONCOERCECLOCALE="0",
                        PYTHONIOENCODING="utf-8")
        claude = self.repo / "CLAUDE.md"
        claude.write_text("# Café\n@AGENTS.md\n", encoding="utf-8")
        exclude = self.repo / ".git/info/exclude"
        exclude.write_text("# Café\n", encoding="utf-8")
        result = self.setup_project()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("# Café\n", exclude.read_text(encoding="utf-8"))
        before = self.files()
        result = self.setup_project()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.files(), before)

    def test_E3_check_text_files_use_utf8_under_ascii_locale(self):
        self.assertEqual(self.setup_project().returncode, 0)
        source = self.method / "SOURCE"
        source.write_text(source.read_text(encoding="utf-8") + "# Café\n", encoding="utf-8")
        self.env.update(LC_ALL="C", PYTHONUTF8="0", PYTHONCOERCECLOCALE="0",
                        PYTHONIOENCODING="utf-8")
        result = self.launch()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertTrue(self.record.is_file())

    def test_E5_force_repairs_checksum_even_when_body_is_current(self):
        self.assertEqual(self.setup_project().returncode, 0)
        entry = self.repo / "AGENTS.md"
        first, body = entry.read_text().split("\n", 1)
        # Supply a checksum even on the old implementation, so its missing guard is tested.
        if "Checksum:" in first:
            first = first.split(" Checksum:", 1)[0] + " Checksum: 000000000000. -->"
        else:
            first = first.removesuffix(" -->") + " Checksum: 000000000000. -->"
        entry.write_text(first + "\n" + body)
        before = self.files()
        result = self.setup_project()
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(self.files(), before)
        result = self.setup_project("--force")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("Checksum: 000000000000.", entry.read_text())
        self.assertIn("Checksum: " + hashlib.sha256(body.encode()).hexdigest()[:12], entry.read_text())

    def files(self):
        return {str(p.relative_to(self.base)): (p.read_bytes(), p.stat().st_mtime_ns)
                for p in self.base.rglob("*") if p.is_file()}

    def test_setup_installs_and_second_run_is_unchanged(self):
        shutil.rmtree(self.method)
        result = self.setup_project()
        self.assertEqual(result.returncode, 0, result.stderr)
        entry = (self.repo / "AGENTS.md").read_text()
        self.assertIn("project demo", entry)
        self.assertIn("~/private/projects/demo/", entry)
        self.assertIn("~/method/", entry)
        self.assertIn("builder, investigator, verifier, challenger or reviewer", entry)
        self.assertTrue((self.method / "roles/investigator.md").is_file())
        self.assertFalse((self.repo / "CLAUDE.md").exists())
        expected_launcher = (ROOT / "scripts/run.py").read_text().replace(
            'DEFAULT_PRIVATE = "~/dev/agent-private"  # Set by setup.',
            f"DEFAULT_PRIVATE = {str(self.private)!r}  # Set by setup.")
        self.assertEqual((self.method / "run.py").read_text(), expected_launcher)
        for md in self.method.rglob("*.md"):
            self.assertNotIn("{{METHOD}}", md.read_text())
            self.assertNotIn("{{PRIVATE}}", md.read_text())
        project = self.private / "projects/demo"
        for name in ("goals.md", "knowledge.md", "checks.md"):
            self.assertIn("demo", (project / name).read_text())
            self.assertNotIn("=== file:", (project / name).read_text())
        self.assertTrue((project / "tasks").is_dir())
        self.assertTrue((project / "findings").is_dir())
        self.assertTrue((self.private / "user/preferences.md").is_file())
        builder = (self.repo / ".claude/agents/builder.md").read_text()
        self.assertIn("model: claude-opus-5-5\n---\n", builder)
        self.assertTrue(builder.endswith((ROOT / "product/roles/builder.md").read_text()))
        exclude = (self.repo / ".git/info/exclude").read_text()
        self.assertEqual(exclude.splitlines().count("/.claude/agents/builder.md"), 1)
        self.assertIn(subprocess.check_output(["git", "-C", str(ROOT), "rev-parse", "HEAD"],
                                             text=True).strip(), (self.method / "SOURCE").read_text())
        before = self.files()
        result = self.setup_project()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.files(), before)

    def test_setup_refuses_foreign_entry_unless_forced(self):
        (self.repo / "AGENTS.md").write_text("foreign instructions\n")
        before = self.files()
        result = self.setup_project()
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("refused", result.stdout.lower() + result.stderr.lower())
        self.assertEqual(self.files(), before)
        result = self.setup_project("--force")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Agent System entry", (self.repo / "AGENTS.md").read_text())

    def test_setup_warns_if_existing_claude_file_does_not_import_agents(self):
        claude = self.repo / "CLAUDE.md"
        for content, warns in (("existing instructions\n", True),
                               ("Mention @AGENTS.md inline\n", True),
                               ("existing instructions\n@AGENTS.md\n", False)):
            with self.subTest(content=content):
                claude.write_text(content)
                before = (claude.read_bytes(), claude.stat().st_mtime_ns)
                result = self.setup_project()
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual("Claude Code may not load AGENTS.md" in result.stderr, warns)
                if warns:
                    self.assertIn("add a line @AGENTS.md", result.stderr)
                self.assertEqual((claude.read_bytes(), claude.stat().st_mtime_ns), before)

    def test_setup_rejects_method_inside_source_repository(self):
        before = self.files()
        for method in (ROOT, ROOT / "product", ROOT / "nested-method"):
            with self.subTest(method=method):
                result = self.setup_project("--method", method, "--dry-run")
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("overlap source", result.stderr)
                self.assertEqual(self.files(), before)

    def test_setup_keeps_private_files(self):
        knowledge = self.private / "projects/demo/knowledge.md"
        knowledge.parent.mkdir(parents=True)
        knowledge.write_text("existing knowledge")
        result = self.setup_project()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(knowledge.read_text(), "existing knowledge")

    def test_setup_replaces_old_method_and_keeps_tracked_builder_visible(self):
        (self.method / "obsolete").write_text("remove me")
        builder = self.repo / ".claude/agents/builder.md"
        builder.parent.mkdir(parents=True)
        builder.write_text("old builder")
        self.git("add", ".claude/agents/builder.md")
        result = self.setup_project()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.method / "obsolete").exists())
        self.assertNotIn("/.claude/agents/builder.md", (self.repo / ".git/info/exclude").read_text())


if __name__ == "__main__":
    unittest.main()
