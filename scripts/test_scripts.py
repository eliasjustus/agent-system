import ast
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest


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
        self.env["PYTHONPATH"] = str(site)

    def start_launcher(self, *extra, role="verifier", out=None, record=None):
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
        for number in range(1, 4):
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
        result = self.launch("verifier", family, "--author-family",
                             "claude" if family == "codex" else "codex", *extra)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(self.record.read_text())

    def pool_probe(self):
        self.agent_extra('''
record["head"] = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
record["branch"] = subprocess.check_output(["git", "rev-parse", "--abbrev-ref", "HEAD"], text=True).strip()
record["status"] = subprocess.check_output(["git", "status", "--porcelain", "--ignored=no"], text=True)
record["files"] = sorted(str(p.relative_to(cwd)) for p in cwd.rglob("*") if p.is_file())
pathlib.Path(os.environ["RECORD"]).write_text(json.dumps(record))
''')

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
        record = self.verifier()
        self.assertEqual(record["cwd"], str(slot.with_name("slot-2")))
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

    def test_POOL_S6_three_slots_then_fallback_and_before_never_waits(self):
        self.env["SLEEP"] = "1"
        records = [self.base / f"pool-{i}.json" for i in range(4)]
        processes = []
        data = []
        for i, record in enumerate(records):
            process = self.start_launcher(record=record, out=self.base / f"pool-{i}.md")
            processes.append(process)
            data.append(self.wait_record(record))
        self.assertEqual(len({record["cwd"] for record in data}), 4)
        for record in data[:3]:
            self.assertTrue(Path(record["cwd"]).is_relative_to(self.pool_root()))
        self.assertFalse(Path(data[3]["cwd"]).is_relative_to(self.pool_root()))
        for process in processes:
            process.terminate()
            process.communicate(timeout=10)
            self.assertEqual(process.returncode, 143)
        self.assertIn("pool busy", (self.base / "pool-3.md").read_text())
        for record in data:
            self.assert_session_cleaned(record)
        # A candidate and before lease compete for the same three slots.
        first = self.start_launcher("--before", "HEAD", record=self.base / "two-slots.json",
                                    out=self.base / "two-slots.md")
        self.wait_record(self.base / "two-slots.json")
        second = self.start_launcher("--before", "HEAD", record=self.base / "one-slot.json",
                                     out=self.base / "one-slot.md")
        self.wait_record(self.base / "one-slot.json")
        for process in (first, second):
            process.terminate()
            process.communicate(timeout=10)
        self.assertIn("Before checkout:", (self.base / "one-slot.md").read_text())
        self.assertIn("pool busy", (self.base / "one-slot.md").read_text())

    def test_POOL_S7_persistent_use_limit_failed_runs_and_explicit_clean(self):
        record = self.verifier()
        slot = Path(record["cwd"])
        metadata = slot.with_suffix(".json")
        state = json.loads(metadata.read_text())
        state["uses"] = 19
        metadata.write_text(json.dumps(state))
        (slot / "ignored").write_text("keep until limit")
        self.env["SESSION_EXIT"] = "7"
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
        for name in ("package-lock.json", "yarn.lock", "pnpm-lock.yaml", "Cargo.lock", "go.sum",
                     "uv.lock", "poetry.lock", "requirements-dev.txt", "rust-toolchain.toml",
                     ".nvmrc", ".node-version", ".tool-versions", ".python-version",
                     "gradle/wrapper/gradle-wrapper.properties", "module/gradle.lockfile"):
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

    def test_POOL_S7_additional_lockfiles_and_java_pin_clean_in_both_directions(self):
        baseline = self.git("rev-parse", "HEAD")
        slot = Path(self.verifier()["cwd"])
        for name in ("Gemfile.lock", "composer.lock", "flake.lock", "bun.lock", "bun.lockb", ".java-version"):
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

    def test_POOL_R8_python_build_definitions_clean_in_both_directions(self):
        baseline = self.git("rev-parse", "HEAD")
        slot = Path(self.verifier()["cwd"])
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
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("Session failed: checkout is not clean", result.stderr)
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
        for name in ("ORIG_HEAD", "FETCH_HEAD", "index.lock", "logs/old", "refs", "info", "config.worktree"):
            self.assertFalse((admin / name).exists(), name)

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
        self.assertIn("create before or known-good checkouts", record["stdin"])

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

    def test_POOL_S8_checkout_hooks_are_disabled_and_interrupted_reset_deep_cleans(self):
        slot = Path(self.verifier()["cwd"])
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
        gradle = shutil.which("gradle")
        if not gradle:
            installed = sorted((Path.home() / ".gradle/wrapper/dists").glob("gradle-*-bin/*/gradle-*/bin/gradle"))
            gradle = str(installed[-1]) if installed else ""
        if not gradle:
            self.skipTest("Gradle unavailable")
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
                self.assertEqual(result.returncode, 1, result.stderr)
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
                    expected = (1 if outcome == "failure" else 143) if marker.exists() else 0
                    self.assertEqual(result.returncode, expected, result.stderr)
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
                        evidence = Path(record["tmpdir"]).parent / "evidence"
                        expected += (f"\nEvidence folder: {evidence}; "
                                     "only this folder is kept; nothing else in the session folder is kept.\n")
                    if role in ("builder", "verifier"):
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
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("Launcher failed:", result.stderr)
        self.assertIn("Exit code: 1", self.out.read_text())
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
        self.assertFalse(self.out.exists())
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
                self.assertEqual(result.returncode, 1, result.stderr)
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
                    self.assertIn("shared clones", instructions)
                    self.assertIn("before", instructions)
                    self.assertIn("known-good", instructions)
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
                    self.assertEqual(result.returncode, status, result.stderr)
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
                    self.assertEqual(result.returncode, 0, result.stderr)
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
        self.assertIn("KeyboardInterrupt", result.stderr)
        self.assertEqual(result.returncode, -signal.SIGINT, result.stderr)
        self.assertFalse(self.record.exists())
        self.assertFalse(self.out.exists())
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

    def test_L5_rerun_clears_previous_evidence_even_without_new_files(self):
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

    def test_L5_rerun_as_builder_clears_previous_evidence(self):
        self.env["EVIDENCE"] = "1"
        result = self.launch("verifier", "codex", "--author-family", "claude")
        self.assertEqual(result.returncode, 0, result.stderr)
        evidence = self.out.parent / "answer-evidence"
        self.assertTrue(evidence.exists())
        del self.env["EVIDENCE"]
        result = self.launch()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(evidence.exists())

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
        for operation in ("rmtree", "unlink", "copytree"):
            for code in (0, 7, 124):
                with self.subTest(operation=operation, code=code):
                    if kept.is_file():
                        kept.unlink()
                    elif kept.exists():
                        shutil.rmtree(kept)
                    if operation == "rmtree":
                        kept.mkdir()
                    elif operation == "unlink":
                        kept.write_text("old evidence")
                    module = "pathlib.Path" if operation == "unlink" else "shutil"
                    argument = "args[1]" if operation == "copytree" else "args[0]"
                    self.inject_launcher(f"""
import pathlib, shutil
original = {module}.{operation}
def fail_evidence(*args, **kwargs):
    if pathlib.Path({argument}) == pathlib.Path({str(kept)!r}):
        raise OSError('forced evidence {operation} failure')
    return original(*args, **kwargs)
{module}.{operation} = fail_evidence
""")
                    self.env["SESSION_EXIT"] = str(0 if code == 124 else code)
                    if code == 124:
                        self.env["SLEEP"] = "1"
                    else:
                        self.env.pop("SLEEP", None)
                    result = self.launch("verifier", "codex", "--author-family", "claude",
                                         *(("--timeout", "0.4") if code == 124 else ()))
                    self.assertEqual(result.returncode, code, result.stderr)
                    self.assertIn(f"Evidence failed: {kept}: forced evidence {operation} failure",
                                  result.stderr)
                    self.assertNotIn("Launcher failed:", result.stderr)
                    self.assertIn(f"Exit code: {code}\n", self.out.read_text())
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

    def test_L8_scope_cleanup_failure_keeps_status_answer_and_evidence(self):
        self.env.update(STOP_FAIL="1", SESSION_EXIT="7", EVIDENCE="1")
        result = self.launch("verifier", "codex", "--author-family", "claude")
        self.assertEqual(result.returncode, 7, result.stderr)
        self.assertIn("Cleanup failed:", result.stderr)
        self.assertIn("agent-verifier-", result.stderr)
        self.assertIn("scope stop refused", result.stderr)
        self.assertTrue(self.out.read_text().endswith("answer\n  unchanged\n"))
        self.assertTrue((self.out.parent / "answer-evidence/proof.txt").exists())

    def assert_writable_refused(self, root):
        result = self.launch("builder", "codex", "--writable", root)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("unsafe --writable root " + str(Path(root).resolve()), result.stderr)
        self.assertFalse(self.record.exists())
        self.assertFalse(self.out.exists())
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
                self.assertFalse(self.out.exists())
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
                            self.assertFalse(self.out.exists())
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
                self.assertEqual(result.returncode, 127, result.stderr)
                self.assertIn("Cannot find agent executable: " + str(missing), result.stderr)
                self.assertFalse(self.record.exists())
                self.assertFalse(self.out.exists())
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
        self.assertTrue(self.out.read_text().endswith("answer\n  unchanged\n"))
        self.assertTrue((self.out.parent / "answer-evidence/proof.txt").exists())

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
