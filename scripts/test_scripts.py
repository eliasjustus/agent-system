import ast
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
import tomllib
import unittest


ROOT = Path(__file__).resolve().parents[1]
FAKE = '''#!/usr/bin/env python3
import json, os, pathlib, re, subprocess, sys, time
args = sys.argv[1:]
cwd = pathlib.Path.cwd()
if args == ["mcp", "list", "--json"]:
    pathlib.Path(os.environ["RECORD"] + ".discovery").write_text(json.dumps({
        "executable": sys.argv[0], "cwd": str(cwd), "tmpdir": os.environ.get("TMPDIR"),
        "pid": os.getpid()}))
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
          "changed": (cwd / "tracked").read_text(),
          "untracked": (cwd / "new").read_text() if (cwd / "new").exists() else None}
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

    def setup_project(self, *extra):
        return self.run_script("setup.py", "--project", self.repo, "--id", "demo",
                               "--private", self.private, "--method", self.method,
                               *extra)

    def test_CE_S1_restricted_roles_disable_extensions_and_enabled_servers(self):
        names = ['plain', 'dot.name', 'quote"slash\\line\n\x00\x7f', 'café U0001f600']
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
                overrides = [tomllib.loads(c)["mcp_servers"] for c in config
                             if c.startswith("mcp_servers.")]
                self.assertEqual(overrides, [{name: {"enabled": False}} for name in names])
                self.assertTrue(record["stdin"].startswith(
                    (self.method / "roles" / f"{role}.md").read_text() + "\n---\n"))
                self.assertEqual(self.git("show", self.snapshot() + ":AGENTS.md"),
                                 "Project instructions")
                discovery = json.loads(Path(str(self.record) + ".discovery").read_text())
                self.assertEqual(discovery["cwd"], record["cwd"])
                self.assertEqual(discovery["tmpdir"], record["tmpdir"])
                self.assertIn("Environment: restricted\n", self.out.read_text())

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
                self.assertIn("Sandbox: workspace-write\n", self.out.read_text())
                self.assertIn("Environment: full\n", self.out.read_text())
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
                    self.assertFalse((self.home / ".cache/agent-method/sessions").exists())

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
                self.env.update(MCP_JSON=output, MCP_EXIT=exit_code)
                result = self.launch("reviewer", "codex", "--author-family", "claude")
                self.assertEqual(result.returncode, 0, result.stderr)
                args = json.loads(self.record.read_text())["argv"]
                self.assertIn("skills.include_instructions=false", args)
                self.assertIn("plugins", args)
                self.assertIn("apps", args)
                self.assertFalse(any(a.startswith("mcp_servers.") for a in args))
                self.assertIn("MCP servers were not disabled", result.stderr)
                self.assertIn(reason, result.stderr)
                self.assertIn("Environment: restricted with MCP servers not disabled: discovery ",
                              self.out.read_text())
                self.assertIn(reason, self.out.read_text())

    def test_CE_S6_empty_and_disabled_server_lists_are_successful(self):
        for output in ('[]', '[{"name":"off","enabled":false}]'):
            with self.subTest(output=output):
                self.env["MCP_JSON"] = output
                result = self.launch("challenger", "codex", "--author-family", "claude")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertNotIn("MCP servers were not disabled", result.stderr)
                self.assertIn("Environment: restricted\n", self.out.read_text())
                args = json.loads(self.record.read_text())["argv"]
                self.assertFalse(any(a.startswith("mcp_servers.") for a in args))

    def test_CE_S6_S9_discovery_timeout_kills_group_then_runs_agent(self):
        self.env["MCP_SLEEP"] = "1"
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
        self.assertIn("MCP servers not disabled", self.out.read_text())
        self.assert_discovery_cleaned()

    def assert_discovery_cleaned(self):
        discovery = json.loads(Path(str(self.record) + ".discovery").read_text())
        for pid in (discovery["pid"], int(Path(str(self.record) + ".discovery-child").read_text())):
            stat = Path(f"/proc/{pid}/stat")
            self.assertTrue(not stat.exists() or stat.read_text().split()[2] == "Z",
                            f"discovery process {pid} survived")
        self.assertFalse(Path(discovery["tmpdir"]).parent.exists())
        self.assertEqual(self.git("worktree", "list", "--porcelain").count("worktree "), 1)

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
                self.assertIn("Sandbox: read-only\n", header)
                self.assertIn("Network: not requested (user config applies)\n", header)
                self.assertIn("Environment: restricted\n", header)
                self.assertIn(f"Exit code: {code}\n", header)
                self.assertEqual(self.git("worktree", "list", "--porcelain").count("worktree "), 1)

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
                        self.assertEqual(self.git("worktree", "list", "--porcelain").count("worktree "), 1)
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
                    if role == "verifier":
                        evidence = Path(record["cwd"]).parent / "evidence"
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

    def test_snapshot_preserves_repository_and_copies_evidence(self):
        (self.repo / "tracked").write_text("staged")
        self.git("add", "tracked")
        (self.repo / "tracked").write_text("uncommitted")
        (self.repo / "staged-new").write_text("staged addition")
        self.git("add", "staged-new")
        (self.repo / "new").write_text("untracked")
        (self.repo / "ignored").write_text("ignored content")
        head, refs = self.git("rev-parse", "HEAD"), self.git("show-ref")
        index = (self.repo / ".git/index").read_bytes()
        self.env["EVIDENCE"] = "1"
        result = self.launch("verifier", "claude", "--author-family", "codex")
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
        self.assertEqual((self.out.parent / "answer-evidence/proof.txt").read_text(), "proof")
        self.assertFalse(Path(record["cwd"]).exists())
        self.assertEqual(self.git("worktree", "list", "--porcelain").count("worktree "), 1)

    def test_failed_session_keeps_answer_and_removes_worktree(self):
        self.env["SESSION_EXIT"] = "7"
        self.env["EVIDENCE"] = "1"
        result = self.launch("verifier", "codex", "--author-family", "claude")
        self.assertEqual(result.returncode, 7, result.stderr)
        self.assertIn("Exit code: 7\n\nanswer\n  unchanged\n", self.out.read_text())
        self.assertFalse(Path(json.loads(self.record.read_text())["cwd"]).exists())
        self.assertEqual((self.out.parent / "answer-evidence/proof.txt").read_text(), "proof")
        self.assertEqual(self.git("worktree", "list", "--porcelain").count("worktree "), 1)

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
        result = self.launch("reviewer", "codex", "--author-family", "claude")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("Launcher failed:", result.stderr)
        self.assertIn("Exit code: 1", self.out.read_text())
        self.assertFalse(self.record.exists())
        self.assertEqual(self.git("worktree", "list", "--porcelain").count("worktree "), 1)
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
                self.assertFalse(Path(json.loads(self.record.read_text())["cwd"]).exists())
                self.assertEqual(self.git("worktree", "list", "--porcelain").count("worktree "), 1)
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
        self.env.pop("XDG_CACHE_HOME", None)
        for cache in (self.home / ".cache", self.base / "cache"):
            with self.subTest(cache=cache):
                if cache != self.home / ".cache":
                    self.env["XDG_CACHE_HOME"] = str(cache)
                self.agent_extra('''
record["files"] = sorted(str(p.relative_to(cwd.parent)) for p in cwd.parent.iterdir())
pathlib.Path(os.environ["RECORD"]).write_text(json.dumps(record))
''')
                result = self.launch("verifier", "codex", "--author-family", "claude")
                self.assertEqual(result.returncode, 0, result.stderr)
                record = json.loads(self.record.read_text())
                session = Path(record["cwd"]).parent
                self.assertEqual(session.parent, cache / "agent-method/sessions")
                self.assertIn("index", record["files"])
                self.assertIn("answer", record["files"])
                self.assertEqual(Path(record["argv"][record["argv"].index("-o") + 1]).parent, session)
                self.assertFalse(session.exists())
                self.assertEqual(list(session.parent.iterdir()), [])
                self.assertEqual(list(temporary.iterdir()), [])

    def test_L1_unwritable_session_root_fails_before_agent(self):
        blocker = self.base / "cache-file"
        blocker.write_text("not a directory")
        self.env["XDG_CACHE_HOME"] = str(blocker)
        result = self.launch()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("session folder", result.stderr.lower())
        self.assertIn(str(blocker), result.stderr)
        self.assertFalse(self.record.exists())

    def test_L1_relative_xdg_cache_home_uses_home_cache(self):
        self.env["XDG_CACHE_HOME"] = "relative-cache"
        result = self.launch("verifier", "codex", "--author-family", "claude")
        self.assertEqual(result.returncode, 0, result.stderr)
        session = Path(json.loads(self.record.read_text())["cwd"]).parent
        self.assertEqual(session.parent, self.home / ".cache/agent-method/sessions")
        self.assertFalse((self.repo / "relative-cache").exists())
        self.assertFalse(session.exists())

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
                                     self.home / ".cache/agent-method/sessions")
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
    if self.name == "tmp" and self.parent.name.startswith("agent-session-"):
        raise OSError("forced temporary folder creation failure")
    return mkdir(self, *args, **kwargs)
pathlib.Path.mkdir = fail_temporary
''')
        result = self.launch()
        self.assertNotEqual(result.returncode, 0, result.stderr)
        self.assertIn("forced temporary folder creation failure", result.stderr)
        self.assertFalse(self.record.exists())
        self.assertEqual(list((self.home / ".cache/agent-method/sessions").iterdir()), [])

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
                    self.assertEqual(self.git("worktree", "list", "--porcelain").count("worktree "), 1)

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
                    registrations = self.git("worktree", "list", "--porcelain")
                    result = self.launch(role, "codex", "--author-family", "claude")
                    self.assertEqual(result.returncode, 0, result.stderr)
                    record = json.loads(self.record.read_text())
                    self.assertFalse(Path(record["baseline_checkout"]).exists())
                    self.assertEqual(self.git("worktree", "list", "--porcelain"), registrations)

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
        registrations = self.git("worktree", "list", "--porcelain")
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
                    self.assertEqual(self.git("worktree", "list", "--porcelain"), registrations)
                    self.assertEqual((user / "tracked").read_text(), "user changes")
                    self.assertTrue(linked.is_dir())

    def test_P2_missing_user_worktree_registration_survives_every_role(self):
        for family in ("codex", "claude"):
            for role in ("builder", "investigator", "verifier", "challenger", "reviewer"):
                with self.subTest(role=role, family=family):
                    user = self.base / f"unmounted user {family} {role}"
                    self.git("worktree", "add", "--detach", str(user), "HEAD")
                    shutil.rmtree(user)
                    registrations = self.git("worktree", "list", "--porcelain")
                    result = self.launch(role, family, "--author-family",
                                         "claude" if family == "codex" else "codex")
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(self.git("worktree", "list", "--porcelain"), registrations)

    def test_T4_locked_and_deleted_worktrees_are_removed_for_either_family(self):
        self.temporary_worktree_agent()
        registrations = self.git("worktree", "list", "--porcelain")
        for family in ("codex", "claude"):
            for role in ("builder", "investigator", "verifier", "challenger", "reviewer"):
                with self.subTest(role=role, family=family):
                    result = self.launch(role, family, "--author-family",
                                         "claude" if family == "codex" else "codex")
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(self.git("worktree", "list", "--porcelain"), registrations)
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
            "    root = pathlib.Path.home() / '.cache/agent-method/sessions'\n"
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
        self.assertIn("worktree " + alias, self.git("worktree", "list", "--porcelain"))
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
        registrations = self.git("worktree", "list", "--porcelain")
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
        listed = self.git("worktree", "list", "--porcelain")
        for path in first["worktrees"]:
            self.assertNotIn(path, listed)
        for path in second["worktrees"]:
            self.assertEqual(Path(path).is_dir(), Path(path).name != "deleted")
            self.assertIn(path, listed)
        processes[1].terminate()
        _, stderr = processes[1].communicate(timeout=10)
        self.assertEqual(processes[1].returncode, 143, stderr)
        self.assertFalse(Path(second["temporary"]).exists())
        self.assertEqual(self.git("worktree", "list", "--porcelain"), registrations)

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
    if self.name.startswith('agent-session-'):
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
        self.assertEqual(list((self.home / ".cache/agent-method/sessions").iterdir()), [])

    def test_L5_evidence_prompt_precreated_folder_and_timeout_retention(self):
        self.agent_extra('''
assert evidence.is_absolute() and evidence.is_dir()
assert evidence.parent == cwd.parent and not evidence.is_relative_to(cwd)
(evidence / "proof.txt").write_text("partial proof")
(cwd / "discard.txt").write_text("discard")
''')
        self.env["SLEEP"] = "1"
        result = self.launch("verifier", "codex", "--author-family", "claude", "--timeout", "0.4")
        self.assertEqual(result.returncode, 124, result.stderr)
        record = json.loads(self.record.read_text())
        evidence = Path(record["cwd"]).parent / "evidence"
        self.assertIn(f"Evidence folder: {evidence}; only this folder is kept; "
                      "nothing else in the session folder is kept.\n", record["stdin"])
        self.assertEqual((self.out.parent / "answer-evidence/proof.txt").read_text(), "partial proof")
        self.assertFalse((self.out.parent / "discard.txt").exists())
        self.assertFalse(Path(record["cwd"]).exists())

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
                    session = Path(json.loads(self.record.read_text())["cwd"]).parent
                    self.assertFalse(session.exists())
                    self.assertEqual(self.git("worktree", "list", "--porcelain").count("worktree "), 1)

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
                        expected.append(str(Path(json.loads(self.record.read_text())["cwd"]).parent / "evidence"))
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
        self.assertFalse((self.home / ".cache/agent-method/sessions").exists())

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
                self.assertFalse((self.home / ".cache/agent-method/sessions").exists())
        self.assertFalse((self.repo / "cache").exists())

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
                self.assertFalse((self.home / ".cache/agent-method/sessions").exists())
                self.assertEqual(list(self.scope_state.iterdir()), [])
                self.assertEqual(self.git("worktree", "list", "--porcelain").count("worktree "), 1)

    def test_L8_missing_worktree_fallback_removes_only_matching_registration(self):
        linked = self.base / "linked source with spaces"
        self.git("worktree", "add", "--detach", str(linked), "HEAD")
        self.repo = linked
        user = self.base / "missing user checkout"
        self.git("worktree", "add", "--detach", str(user), "HEAD")
        shutil.rmtree(user)
        registrations = self.git("worktree", "list", "--porcelain")
        real_git = shutil.which("git")
        (self.bin / "git").unlink()
        cleanup_order = self.base / "cleanup-state"
        self.env.update(CLEANUP_STATE=str(cleanup_order), SESSION_EXIT="7", EVIDENCE="1")
        (self.bin / "git").write_text(
            "#!" + sys.executable + "\nimport os, pathlib, shutil, sys\n"
            "args = sys.argv[1:]\n"
            "if 'worktree' in args and 'remove' in args:\n"
            "    root = pathlib.Path.home() / '.cache/agent-method/sessions'\n"
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
        self.assertEqual(self.git("worktree", "list", "--porcelain"), registrations)
        self.assertTrue(self.out.read_text().endswith("answer\n  unchanged\n"))
        self.assertEqual((self.out.parent / "answer-evidence/proof.txt").read_text(), "proof")

    def test_L8_worktree_and_temporary_cleanup_failures_keep_session_status(self):
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
        self.assertFalse(Path(first_data["cwd"]).exists())
        self.assertFalse(Path(second_data["cwd"]).exists())

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
