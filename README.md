# Agent System

Agent System gives the owner's projects one consistent way for AI coding agents to work, and one
way to set it up. Agents clarify the user's intent, agree what “done” means, weigh the cost of
their work, and have a separate session check it, from another model family when one is available.
The aim is complete, verified changes for as little of the user's attention as possible.

The method is plain instructions that Claude Code and Codex load in a project. The launcher for
starting checking sessions is optional. Each release names the tools and platforms it was tested
with.

Maintained for the owner's own use. Bug reports are welcome; no response is promised.

## Install

You need Git, Python 3.13 or newer, and Claude Code and/or Codex installed. Check Python first:

```sh
python3 --version
git --version
```

Clone this repository; setup needs a Git clone, not a ZIP download. The target project must also
be a Git repository (run `git init` there first if needed).

```sh
git clone https://github.com/eliasjustus/agent-system.git
cd agent-system
python3 scripts/setup.py --project /path/to/project --id my-project
```

Replace `/path/to/project` with your project's path. Choose a unique lowercase project ID for
each project: a reused ID shares private records, and IDs differing only in case can collide on
macOS.

Setup copies the method to `~/.local/share/agent-method`, writes the project's `AGENTS.md` and
`.claude/agents/builder.md`, and excludes the generated builder from Git unless already tracked.
It creates private project records and user preferences under the default `~/dev/agent-private`,
preserving existing private files. Use `--dry-run` to preview changes. A foreign or hand-edited
`AGENTS.md` requires `--force` to replace; inspect the diff before doing so.

You can choose paths with `--private /path/to/private` and `--method /path/to/method`. Use one
private root per method install: reinstalling with another `--private` changes the installed
launcher's default for every project using that install. An explicit launcher `--private` overrides
that default.

## First session

1. Fill the generated `goals.md`, `knowledge.md` and `checks.md` in the private root's
   `projects/my-project/` folder, and `user/preferences.md`. Include how to run your project and
   its checks, and agree a commit policy in `knowledge.md`.
2. Log in to Claude Code and/or Codex, then start `claude` or `codex` in the target project.
3. Check that the entry loads, for example with `/context` in a new Claude Code session. If it does not
   appear (older Claude Code versions do not load `AGENTS.md` themselves), create a `CLAUDE.md` in the
   project containing the line `@AGENTS.md`.

If the project or any parent folder has a `CLAUDE.md`, `CLAUDE.local.md` or `.claude/CLAUDE.md`,
Claude Code may read those instead of `AGENTS.md`. Put a line `@AGENTS.md` in the project's own
`CLAUDE.md`, creating it if only a parent has one. An import in a parent points at the parent's
file. Setup warns only about the project's own `CLAUDE.md`; check the loaded context yourself.

## Optional launcher

Write a bounded brief outside the repository, then run from the target project, for example:

```sh
python3 ~/.local/share/agent-method/run.py --role reviewer --family codex --author-family claude --brief /path/to/brief.md --out /path/to/results/reviewer.md
```

Roles are `builder`, `investigator`, `verifier`, `challenger` and `reviewer`; families are `claude`
and `codex`. Builders work in the project. Investigators work there read-only and may read private
records. Review roles require `--author-family`; a same-family check requires a recorded reason
through `--same-family-reason`.

Codex challengers and reviewers start with plugins, apps, skill instructions and discovered MCP
servers disabled. If server discovery fails or times out, the session still starts; stderr and the
answer's header explain that MCP servers were not disabled. Role contracts and the project's
`AGENTS.md` still load. Servers also stay enabled when their names cannot be written as bare keys
(ASCII letters, digits, `_` and `-`); stderr and the header list those names. Other Codex roles
keep the user's configured environment. Claude sessions use a fixed tool list and no MCP servers.

Builders and verifiers wait for memory and IO room before leasing checkouts or starting agents,
polling every 10 seconds. Investigators, challengers and reviewers start without this wait.
Admission waits at most 1800 seconds (`AGENT_ADMISSION_MAX_WAIT`), then starts under pressure;
fluctuating readings do not reset the deadline. Missing or malformed readings are ignored, and
without any readings the session starts immediately. The header records `Admission: waited <s> s`,
`Admission: unavailable`, or `Admission: started under pressure after <s> s (<reading>)`.
Project build gates can run `python3 <method>/run.py --wait-for-room` with no other arguments:
it uses the same wait, prints one status line to stderr, and exits 0 even after the maximum wait
or when readings are unavailable. `--timeout` applies only to the agent session; admission and
server discovery (at most 30 seconds) happen before it.

The provisional thresholds below each have an `AGENT_ADMISSION_<NAME>` override. Waiting ends
when all available readings are below the pressure thresholds or at least the free-space limits.
Invalid overrides use the defaults; overrides must be finite nonnegative numbers.

| Name | Wait while |
| --- | --- |
| `MEMORY_SOME` | `/proc/pressure/memory` some avg10 >= 5 |
| `MEMORY_FULL` | `/proc/pressure/memory` full avg10 >= 2 |
| `IO_FULL` | `/proc/pressure/io` full avg10 >= 10 |
| `MEM_AVAILABLE` | `/proc/meminfo` MemAvailable < 8589934592 bytes (8 GiB) |
| `SWAP_FREE` | SwapFree < 10 percent of SwapTotal, when SwapTotal > 0 |
| `CGROUP_HEADROOM` | agents.slice memory.high minus memory.current < 4294967296 bytes (4 GiB), when memory.high is numeric |

The agents slice is found through `/proc/self/cgroup`'s user service, falling back to
`/sys/fs/cgroup/user.slice/user-<uid>.slice/user@<uid>.service/agents.slice/`.

For Codex builders and verifiers, add `--network` when checks need local servers, child processes
from Node or network access. Without it, the user's network configuration applies. Add
`--writable DIR` for a directory outside the usual writable roots, for example
`--writable ~/.gradle` for a build cache. These permissions are independent: `--network` does not
open extra directories. There is no full-access option because these cover the known needs.

Challengers and reviewers run on a snapshot of committed, staged and uncommitted files, including
untracked files up to 5 MB. Ignored files and larger untracked files are omitted; oversized files appear in
the answer's header. Check that list before trusting a review. The answer and its header are saved
at `--out`, agent stderr, preflight output and MCP discovery stderr in a sibling `.log`, and verifier evidence in a sibling `<output-stem>-evidence`
folder. Empty answers fail. Temporary session folders and their worktrees are removed after the run;
verifier pool slots remain for reuse.
Use one suffix per result stem: `r.md` and `r.txt` share a lock, log and evidence.
While a launcher runs, `--out` and its siblings (`.log`, `-evidence`, `.lock`, `.attempt-N`) belong to it.
Earlier results, logs and evidence are kept as `.attempt-N` siblings, retaining the output suffix
(for example, `answer.attempt-1.txt` for `--out answer.txt`). The result file decides; exit 2 means
refused (and then the file says so) or the destination cannot be claimed (an earlier result may
already have moved to `.attempt-N`; stderr says why). Exit 1 with
`Result: failed: cannot save answer: <error>; answer left in <session folder>` means the agent ran
but saving its answer failed. The session folder is kept with the answer in place; remove it manually
after recovering the answer. If the result cannot be written, the same failure and folder are reported
on stderr (best effort). Exit 3 means a saved answer with exit 0 and failed cleanup. The header's
`Exit code:` keeps the agent's code, including when cleanup fails.
`Cleanup: pending` means the answer is saved and cleanup is still running; its final status is
`Cleanup: ok` or `Cleanup: failed: <what>; …`. If stopping the agent fails, evidence stays in the
session folder, the session folder is kept, and the cleanup status gives its location.
A non-zero agent code survives cleanup failures.
A handled signal is recorded as `Signal: <number>` and exits with 128 plus that number, including
after the answer was saved. The header's `Exit code:` retains the original outcome.
The result file decides what completed: a late signal never replaces a saved answer. Once the final
status is written, further signals are ignored and the exit status matches the recorded outcome.
`Result: running` means the launcher is running or was killed. Check liveness with
`flock -n <stem>.lock true` (fails while the launcher lives). An output ending in `.log` or `.lock`,
an existing directory, a stem ending in `-evidence`, or a name containing `.saving-` or `.attempt-`
is refused before claiming or archiving anything.
The header records the executable's absolute path, `Shield:`, and, for Codex, `Requested sandbox:`, `Network:`
and `Requested environment:`. The requested environment is `restricted` (with any MCP fallback
reason) or `user configuration`. If snapshot or worktree creation fails before discovery, it is
`restricted; discovery did not run`.

The header's `Write bytes:` line reports, in MiB, the bytes sent toward disk-backed storage from
just before the session folder is created until the agent has been stopped and waited for, by the
launcher and the children it waited for (and theirs, in turn). It includes data deleted before it
reached the disk, and is `unavailable` when `/proc/self/io` cannot be read or parsed. Writes inside a
tmpfs folder add nothing; writes elsewhere, such as Git objects in the repository, still count. It
misses daemons, descendants that no parent waited for, and saving the answer and cleanup.

Verifiers test the commit that HEAD names when the launcher starts. Commit before verifying:
staged or unstaged tracked changes, including conflicts, refuse the session before a checkout is
leased. Untracked files of any size stay in the source tree and are omitted from the verifier's
checkout; the header names at most 20, then reports the remaining count. A forgotten file must fail
verification. `Snapshot commit:` records the committed candidate, even if the builder commits again.

The first two verifier launches into one results folder (the `--out` directory) need no round
decision. Before a third or later launch leases any checkout, `round-decision.md` in that folder must
be a regular file, not a symlink, non-empty after stripping whitespace, and modified after the newest
counted result. Otherwise the launcher writes `Result: launch refused` with a reason and exits 2.
The decision records one of: a table of cases for the failure class, with the expected result per
case and what must keep working; a re-scope or redesign, with its reason, named in the delivery;
or a retry with its reason (environment, contention, flaky check). The launcher does
not judge its contents. An accepted decision adds `Round: <n>; decision: round-decision.md` to the
header. Every later launch needs a decision newer than the newest counted result.
Concurrent launches in that folder serialize counting and writing their running markers, even
when they use different output stems.

Each regular result file whose header says `Role: verifier` counts once, whatever its stem or
suffix, including `.attempt-N` archives and interrupted or failed runs. A `Result: running` marker
counts even if its launcher was killed: the agent may have run. `Result: launch refused`, other
roles and results in other directories do not count; other roles are never gated. The decision,
`.saving-*` temporaries, `.log` and `.lock` files (including archived siblings), entries ending in
`-evidence` (files or directories, including archived siblings), subdirectories and their contents
never count. The scan reads at most 64 KiB of each file's header; headers that do not end within
that bound are skipped and named on stderr. Launcher header values are capped at 4 KiB so
classification fits within that bound. Symlinks, hard links, special
files and unreadable files are skipped and named on stderr. `--out` cannot name `round-decision.md`:
that destination is refused on stderr before anything is written or archived.
See [Recovery](product/guides/recovery.md) for repair briefs and decisions.

If the candidate contains `.agents/preflight`, the launcher runs it in the candidate checkout
after admission and checkout leasing, before starting the verifier, with the session's temporary folder.
The round gate refuses before admission or any lease. Use this executable regular file
for the project's cheap checks. A symlink, directory or nonexecutable file refuses the launch.
Exit 0 records `Preflight: passed in <s> s`; the verifier receives the same checkout and build
output. A nonzero exit, timeout or changed tracked files refuses the launch with exit 2, without
starting an agent. Combined stdout/stderr stays in the sibling `.log`; refusals include its last
50 lines. The default timeout is 900 seconds (`AGENT_PREFLIGHT_TIMEOUT`), separate from `--timeout`.
Preflight descendants are stopped before verification or lease release, including after launcher
signals or death. The before checkout and other roles never run it. Without a committed preflight,
launching is unchanged and the verifier header says `Preflight: none`.

Verifier checkouts are leased without waiting from `<cache>/am-pool/<name>-<repository hash>/`,
where `<cache>` follows `XDG_CACHE_HOME` below. The name comes from the main repository and the hash
from its resolved Git common directory. Main trees, linked worktrees, symlinks and subdirectory
`--repo` paths share one pool; different repositories with the same name do not. No Agent System
entry is required. Verifiers require Linux (the lease guard uses `prctl`). Each repository has
**5 slots**, using up to five built trees of disk space. Existing slots 1–3 and their metadata
and locks remain reusable after an upgrade; slots 4–5 are created only when needed.
Repositories using `extensions.refStorage=reftable` bypass the pool and create one fresh session
worktree per requested checkout, removed at session end; they do not retain warm build output.
Before each use, a slot is reset to the detached candidate and cleaned of tracked edits and
untracked files, including nested repositories. Slot resets and content checks run with Git hooks
disabled, including relative `core.hooksPath` hooks. Ignored build output remains warm. Source working
files, index, branches and config stay untouched.
Every reset removes FIFO/socket files and ignored symlinks whose resolved target is outside the
slot, together with the top-level ignored folder containing such a link (a top-level link is
removed itself). Targets are never removed. Internal dependency links, including dangling ones,
stay warm. Python virtual environments whose interpreter symlink points outside the slot are
removed by this rule (including `.venv`) and must be rebuilt on every lease.
Files ignored only through the user's global `core.excludesFile` are removed every time;
projects should ignore build folders in their own `.gitignore` to keep them warm. A default ACL
on a tracked directory is not reset; this is a known limit of checkout reuse.

Verifiers require `--before <commit>` to lease a second writable slot at a baseline commit, or
`--before none` to start without a before checkout. Its path and resolved commit (or explicit
absence) are in the prompt and the `Before checkout:` header. If a before state is needed after
all with `--before none`, make an ad-hoc shared clone inside $TMPDIR and reuse it between cases.
Build each source tree only into its own build folder. Never point one tree's build output
(`CARGO_TARGET_DIR`, Gradle build directories, `node_modules` links) at another tree. Candidate and before leases
use the same pool. Slots are ranked without locking: those needing no deep clean first, then
new slots before existing slots needing a deep clean. Within each group, the fewest changed
paths from the last commit to the requested commit wins, then the lowest slot number. When
candidate slots rank equally, leave the best baseline slot for the before lease if possible. Slots
without usable metadata come last. Each lease tries that order without waiting,
skipping busy or unusable slots, and recomputes cleaning after taking its lock.
When all slots are busy, the launcher creates a full temporary checkout in a
session worktree and says `pool busy or slots unusable` in the header. Session cleanup never removes
a pool checkout.

Slots are deep-cleaned, including ignored files, after 20 uses since the last deep clean (failed
runs count), when a lockfile or toolchain/build definition changes in either direction, and with
verifier-only `--clean-checkout`. The fixed list covers npm/yarn/pnpm, Cargo, Gradle wrapper
properties, Go, Python requirements/uv/poetry and pyproject.toml/setup.py/setup.cfg,
Gemfile.lock, composer.lock,
flake.lock, bun.lock/bun.lockb and toolchain pins (except .java-version). Gradle build/settings
files, gradle.properties, version catalogs, *.lockfile files and .java-version do not trigger
pool cleaning. Gradle can retain stale output when a removed task no longer produces generated
sources still referenced by a `srcDir`, or copy/package output that another task reads. When a
candidate removes Gradle tasks or generated source folders, verify with `--clean-checkout`.
Changes to any Cargo.toml also trigger a deep clean, as do deleted or renamed-away main.rs files
and files under any src/bin/ directory: Cargo can otherwise leave an obsolete executable in target/. A source file
left untracked that was tracked in any commit held by the slot since its last deep clean also
forces a deep clean, so stale output from another lane cannot hide that forgotten file. Counts
and commit history survive restarts; metadata without that history forces a deep clean on upgrade.
If any recorded commit is missing from the repository, the slot is deep-cleaned because its
forgotten-file history can no longer be checked completely.
Missing/corrupt metadata or interrupted resets force cleaning; broken checkouts
are recreated, with a fresh session worktree as a fallback. The header says whether a reset was
warm or deep and why; `new slot` means the checkout, metadata and temporary metadata were all absent.
Pool reset writes count in `Write bytes:`.

Gradle and Cargo incremental builds must notice deleted source files; the tests exercise this with
a still-referenced deleted Java class and Rust module, plus a removed Cargo executable. Gradle
must also reject a removed dependency still used by Java code and a removed settings project
still referenced by another project, on both warm output and a fresh checkout. The Cargo
manifest and executable-source deep-clean rules prevent obsolete binaries from passing a check.
Custom output paths, other build systems and custom build scripts may still reuse stale ignored
output incorrectly. Changes to package.json alone do not trigger a deep clean; `npm ci` reinstalls
node_modules anyway. Retry with `--clean-checkout` after an environment-looking failure and report
both results; a warm pass alone cannot establish that a custom build is correct. No shared Rust
compile cache (sccache) is configured.

A Linux subreaper supervisor holds the pool locks outside systemd-run, stops and waits for the
agent's descendants, and releases locks only after they are gone. If the launcher is killed, the
supervisor stops the agent and its descendants, keeps its leases until they are reaped, and removes
the orphaned session folder. A launcher killed before its lease guard starts can leave its session
folder under `<cache>/am`; reset Git children inherit its locks and hold them until they exit.
Sessions without a lease guard (builders, investigators, challengers, reviewers, and verifiers
using only fresh fallback checkouts without preflight) killed outright can also leave their folder and worktree
registrations. After confirming their processes have stopped, delete the specific session folder,
then run `git -C /path/to/project worktree prune` in its repository. Check for unrelated stale
registrations before pruning.
This does not rely on systemd-run forwarding file descriptors. Pool supervision requires Linux.

Codex challengers and reviewers read the snapshot through `git show <snapshot>:<path>`,
`git grep <pattern> <snapshot>` and `git diff`. Only root `AGENTS.md` and `CLAUDE.md`, if present,
are materialized; the index is clean and the post-checkout hook is skipped. `--full-checkout` does
not change this Codex behavior. Builders and investigators keep their existing checkout behavior.

For **Claude** challengers and reviewers, keep an optional
`<private>/projects/<id>/review-leave-out.txt` beside the project's private records. The launcher
gets the ID from the first line of `AGENTS.md` in `--repo` and uses its default private root or
`--private`. A subdirectory `--repo` without its own `AGENTS.md` gets a full checkout. Setup never
creates or changes this list. Put one literal repository-relative file or directory path per line;
trailing slashes are allowed. Spaces are part of the path and lines are not trimmed. Lines starting
with `#` are comments. Absolute paths, the repository root and paths containing a `..` component
warn and are ignored. Entries targeting the repository root's `AGENTS.md`, `CLAUDE.md`, `.claude`
or `.codex`, including their contents, also warn and are ignored. Paths matching nothing are
ignored. A missing or empty list gives a full checkout; an unreadable or invalid UTF-8 list warns
and gives a full checkout.

Listed paths remain in Git but are absent from the checkout. The prompt and answer header name
them; agents should say if their checks need those paths. If the snapshot changes a listed path,
the launcher uses a full checkout and explains why. A review of committed changes already on HEAD
is not detected by this fallback: pass `--full-checkout` when the reviewed range touches listed
paths. Also use `--full-checkout` whenever a Claude check needs all files; it skips reading the list.
A checkout with left-out paths skips the Git post-checkout hook. If Git checks out the listed
paths anyway, the launcher reports a full checkout.

Every role except investigator shields the private folder, including shell access: Codex uses a
permission profile denying its paths; Claude uses bubblewrap to hide it behind read-only empty
mounts and keeps its Read/Edit deny settings. The header says `Shield: private folder denied (codex
permission profile)`, `Shield: private folder hidden (bwrap)` or `Shield: none (investigator)`.
Missing bubblewrap, a failed namespace probe, or `sandbox_mode` in the Codex user config
(`$CODEX_HOME/config.toml`, otherwise `~/.codex/config.toml`) refuses a shielded launch with exit 2
and `Result: launch refused`. Preflight runs outside the shield. Outside brokers (the systemd user
manager and external MCP/file servers in Codex user configuration), pre-existing hard links and
bind mounts are not covered. Give checking sessions only the brief, role contract and evidence they need.

For a committed candidate, the manual route is a detached worktree at its exact commit:

```sh
git worktree add --detach /path/to/checkout COMMIT
```

Before a manual verifier session, run `.agents/preflight` if present in that checkout; require an
executable regular file, exit 0 and tracked files still matching the candidate. Keep its build output
for verification. Builders also run the project's preflight before handing back.
Start a fresh session of the other model family in that checkout. Give it the appropriate installed
role contract and brief, plus `guides/verification.md` for a verifier. Configure the session's
permissions for the role, save its full answer unedited outside the repository, and retain verifier
evidence. Remove the checkout with `git worktree remove /path/to/checkout` after checking.
Manually started third and later verifications need the same fresh `round-decision.md` in the
task's results folder before starting.
Commit the candidate before verification. Uncommitted work may go through `run.py` for a
challenger or reviewer snapshot.

## Environment

| Variable | Effect |
| --- | --- |
| `AGENT_CLAUDE_MODEL` | Claude launcher model and the builder definitions generated at setup; default `claude-opus-5-5` |
| `AGENT_CODEX_MODEL` | Codex launcher model; default `gpt-6.1-sol` |
| `AGENT_CODEX_EFFORT` | Codex reasoning effort; default `high`. Values pass through to the CLI, which rejects unsupported values. |
| `AGENT_CLAUDE_CMD` | Claude executable name or path; default `claude` |
| `AGENT_CODEX_CMD` | Codex executable name or path; default `codex` |
| `AGENT_PREFLIGHT_TIMEOUT` | Verifier preflight timeout in seconds; positive finite number, default `900`. Does not count against `--timeout`. |
| `XDG_CACHE_HOME` | Session and pool cache root; defaults to `~/.cache` when unset, empty or relative. Sessions use `am/<8 random characters>/` beneath it. |

Set the model variable when running setup to change generated Claude builder definitions; rerun
setup for each project to update its copy. Command variables take a single executable, without
extra arguments.

Each session sets `TMPDIR`, `TMP` and `TEMP` to its disk-backed `tmp/` folder and removes the
session folder after the run. With a home folder such as `/home/alice`, the default TMPDIR is 34 bytes. If its
resolved path exceeds 62 bytes, the launcher warns on stderr and starts anyway. This budget is
for Chromium's typical socket name on Linux; the warning does not make sockets work. Use a
shorter absolute `XDG_CACHE_HOME` if needed. After upgrading, `<cache>/agent-method/sessions/`
can be deleted once no session started by an older version is running.

## Upgrade, rollback and removal

To upgrade, run `git pull` in this clone. For each project, preview and then apply setup with the
same ID and paths used originally:

```sh
python3 scripts/setup.py --project /path/to/project --id my-project --dry-run
python3 scripts/setup.py --project /path/to/project --id my-project
```

Include your custom `--private` and `--method` on both commands if you used them. Private records
are kept. Upgrading creates pool slots lazily; existing session caches need no migration.
To inspect a pool, read its `slot-*.json` files for slots 1–5 (commit, use count and commit history), run
`git -C /path/to/project worktree list`, and inspect its size with
`du -sh <cache>/am-pool/<name>-<hash>`. To remove a repository's pool safely, first stop its launcher
sessions and wait for their agents and supervisors to finish, including orphaned sessions. Then
remove each registered slot while holding its lock, checking it is free:
`flock -n <pool>/slot-N.lock git -C /path/to/project worktree remove --force --force <pool>/slot-N`
for each existing slot, with N from 1 to 5.
If the lock is busy, wait for its session to finish. Before deleting that repository's pool folder,
ensure no launcher can start. For a deleted slot folder, the same remove command usually clears its
registration; if it fails, remove only the matching entry under the common Git directory's
`worktrees/` whose `gitdir` names that slot. Do not prune unrelated worktrees. Never delete or
replace `.lock` files while launchers may start. Check that the pool registrations are gone before deleting
its lock and metadata files.

Moved, renamed or deleted repositories leave pools that are never reclaimed automatically.
Inspect all pool sizes with `du -sh <cache>/am-pool/*`. After stopping their sessions as above,
delete pools whose repository is gone. If the repository still exists at a new path, remove its
old pool and run `git -C /path/to/project worktree prune` there to clear the missing slots'
registrations; check for unrelated stale registrations before pruning.
Delete the old pool first: Git retains registrations while their checkout folders still exist.

To roll back, stop current sessions, remove their pool with the procedure above, check out the previous release tag in this clone and rerun setup for each
project with those same arguments.

To remove the method from a project, remove its generated `AGENTS.md` and
`.claude/agents/builder.md`, the `/.claude/agents/builder.md` line from Git's `info/exclude`, and any
`@AGENTS.md` line you added. Once no project uses the shared method folder and no session is
running, remove the method folder and delete only the session folder `<cache>/am/` from the
cache. Remove each repository pool using the procedure above too. Private records stay unless
you delete them.

## Platforms

Setup is tested on Linux. macOS and WSL2 should work but are untested; WSL1 is not supported.
Verifiers require Linux because their lease guard uses `prctl`.
Without `systemd-run`, launcher sessions run without a scope and use process-group cleanup.

## Before a release

Run the automated checks:

```sh
python3 -m unittest discover -s scripts -p 'test_*.py'
```

Then smoke-test with real sessions in a throwaway Git repository and a throwaway private root,
for each installed family (Claude Code and Codex):

- Run a writable builder and confirm it makes the requested change.
- Run a read-only reviewer on a snapshot and a verifier that saves evidence. Confirm the source
  checkout is unchanged by these checks and the evidence survives cleanup.
- Check every answer is non-empty, stderr logs are saved, and session folders and worktrees are
  removed after completion.
- Put a canary file in the throwaway private folder and a symlink to it in the throwaway repository
  (briefs may not name the private folder). Confirm shielded sessions cannot read it through file
  tools, the symlink or the shell, and cannot write into the private folder.
- Record the OS/platform and Python, Git, Claude Code, Codex and, when used, systemd versions in the
  release notes. State any skipped family or platform checks.

## Layout

| Path | What it holds |
| --- | --- |
| [product/](product/) | Entry, guides, role contracts and templates installed for agents |
| [scripts/](scripts/) | Installer, optional launcher and their tests |
| [LICENSE](LICENSE) | MIT license |
