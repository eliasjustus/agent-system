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

`--timeout` applies to the agent session. Server discovery adds at most 30 seconds before it.

For Codex builders and verifiers, add `--network` when checks need local servers, child processes
from Node or network access. Without it, the user's network configuration applies. Add
`--writable DIR` for a directory outside the usual writable roots, for example
`--writable ~/.gradle` for a build cache. These permissions are independent: `--network` does not
open extra directories. There is no full-access option because these cover the known needs.

Challengers and reviewers run on a snapshot of committed, staged and uncommitted files, including
untracked files up to 5 MB. Ignored files and larger untracked files are omitted; oversized files appear in
the answer's header. Check that list before trusting a review. The answer and its header are saved
at `--out`, stderr in a sibling `.log`, and verifier evidence in a sibling `<output-stem>-evidence`
folder. Empty answers fail. Temporary session folders and their worktrees are removed after the run;
verifier pool slots remain for reuse.
The header records the executable's absolute path and, for Codex, `Requested sandbox:`, `Network:`
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

Verifier checkouts are leased without waiting from `<cache>/am-pool/<name>-<repository hash>/`,
where `<cache>` follows `XDG_CACHE_HOME` below. The name comes from the main repository and the hash
from its resolved Git common directory. Main trees, linked worktrees, symlinks and subdirectory
`--repo` paths share one pool; different repositories with the same name do not. No Agent System
entry is required. Each repository has **3 slots**, using up to three built trees of disk space.
Before each use, a slot is reset to the detached candidate and cleaned of tracked edits and
untracked files, including nested repositories. Ignored build output remains warm. Source working
files, index, branches and config stay untouched.

Use verifier-only `--before <commit>` to lease a second writable slot at a baseline commit; its
path is in the prompt and the `Before checkout:` header. Without it, use one shared clone inside
$TMPDIR for each needed extra version and reuse it between cases. Candidate and before leases
use the same pool. When all slots are busy, the launcher creates a temporary session worktree
and says `pool busy` in the header. Session cleanup never removes a pool checkout.

Slots are deep-cleaned, including ignored files, after 20 uses since the last deep clean (failed
runs count), when a lockfile or toolchain/build definition changes in either direction, and with
verifier-only `--clean-checkout`. The fixed list covers npm/yarn/pnpm, Cargo, Gradle (including
wrapper properties and lockfiles), Go, Python requirements/uv/poetry, Gemfile.lock, composer.lock,
flake.lock, bun.lock/bun.lockb and toolchain pins (including .java-version). Changes to any
Cargo.toml also trigger a deep clean, as do deleted or renamed-away main.rs files and files under
any src/bin/ directory: Cargo can otherwise leave an obsolete executable in target/. Counts
survive restarts. Missing/corrupt metadata or interrupted resets force cleaning; broken checkouts
are recreated, with a fresh session worktree as a fallback. The header says whether a reset was
warm or deep and why. Pool reset writes count in `Write bytes:`.

Gradle and Cargo incremental builds must notice deleted source files; the tests exercise this with
a still-referenced deleted Java class and Rust module, plus a removed Cargo executable. The Cargo
manifest and executable-source deep-clean rules prevent obsolete binaries from passing a check.
Custom output paths, other build systems and custom build scripts may still reuse stale ignored
output incorrectly. Retry with `--clean-checkout` after an environment-looking failure and report
both results; a warm pass alone cannot establish that a custom build is correct. No shared Rust
compile cache (sccache) is configured.

A Linux subreaper supervisor holds the pool locks outside systemd-run, stops and waits for the
agent's descendants, and releases locks only after they are gone. If the launcher is killed, the
supervisor keeps its leases, reaps descendants and removes the orphaned session folder. This does
not rely on systemd-run forwarding file descriptors. Pool supervision requires Linux.

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

Shielding prevents accidental leaks of the private root into briefs and through Claude's own file
tools. It is not a security boundary: Codex sessions can read the private folder, and a writable
Claude session has shell access. Give checking sessions only the brief, role contract and evidence
they need.

For a committed candidate, the manual route is a detached worktree at its exact commit:

```sh
git worktree add --detach /path/to/checkout COMMIT
```

Start a fresh session of the other model family in that checkout. Give it the appropriate installed
role contract and brief, plus `guides/verification.md` for a verifier. Configure the session's
permissions for the role, save its full answer unedited outside the repository, and retain verifier
evidence. Remove the checkout with `git worktree remove /path/to/checkout` after checking.
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
To inspect a pool, read its `slot-*.json` files (commit and use count), run
`git -C /path/to/project worktree list`, and inspect its size with
`du -sh <cache>/am-pool/<name>-<hash>`. To remove a repository's pool safely, first stop its launcher
sessions and wait for their agents and supervisors to finish, including orphaned sessions. Then
remove each registered slot with
`git -C /path/to/project worktree remove --force --force <pool>/slot-N` before deleting that
repository's pool folder. For a deleted slot folder, the same remove command usually clears its
registration; if it fails, remove only the matching entry under the common Git directory's
`worktrees/` whose `gitdir` names that slot. Do not prune unrelated worktrees. Never delete or
replace a live slot's `.lock` file. Check that the pool registrations are gone before deleting
its lock and metadata files.

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
  (briefs may not name the private folder). Ask a Claude session to read the symlink and confirm the
  read is refused. This checks accidental shielding, not isolation from shell access.
- Record the OS/platform and Python, Git, Claude Code, Codex and, when used, systemd versions in the
  release notes. State any skipped family or platform checks.

## Layout

| Path | What it holds |
| --- | --- |
| [product/](product/) | Entry, guides, role contracts and templates installed for agents |
| [scripts/](scripts/) | Installer, optional launcher and their tests |
| [LICENSE](LICENSE) | MIT license |
