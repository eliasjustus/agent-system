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

Review roles run on a snapshot of committed, staged and uncommitted files, including untracked
files up to 5 MB. Ignored files and larger untracked files are omitted; oversized files appear in
the answer's header. Check that list before trusting a review. The answer and its header are saved
at `--out`, stderr in a sibling `.log`, and verifier evidence in a sibling `<output-stem>-evidence`
folder. Empty answers fail. Temporary session folders and worktrees are removed after the run.
The header records the executable's absolute path and, for Codex, `Requested sandbox:`, `Network:`
and `Requested environment:`. The requested environment is `restricted` (with any MCP fallback
reason) or `user configuration`. If snapshot or worktree creation fails before discovery, it is
`restricted; discovery did not run`.

To reduce checkout writes for challengers and reviewers, keep an optional
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
paths. Also use `--full-checkout` whenever a check needs all files; it skips reading the list.
A checkout with left-out paths skips the Git post-checkout hook. If Git checks out the listed
paths anyway, the launcher reports a full checkout. Builder, investigator and verifier sessions
always keep their usual checkout behavior.

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
Uncommitted or untracked candidates go through `run.py` to capture the exact snapshot.

## Environment

| Variable | Effect |
| --- | --- |
| `AGENT_CLAUDE_MODEL` | Claude launcher model and the builder definitions generated at setup; default `claude-opus-5-5` |
| `AGENT_CODEX_MODEL` | Codex launcher model; default `gpt-6.1-sol` |
| `AGENT_CODEX_EFFORT` | Codex reasoning effort; default `high`. Values pass through to the CLI, which rejects unsupported values. |
| `AGENT_CLAUDE_CMD` | Claude executable name or path; default `claude` |
| `AGENT_CODEX_CMD` | Codex executable name or path; default `codex` |
| `XDG_CACHE_HOME` | Session cache root; defaults to `~/.cache` when unset, empty or relative. Sessions use `agent-method/sessions/` beneath it. |

Set the model variable when running setup to change generated Claude builder definitions; rerun
setup for each project to update its copy. Command variables take a single executable, without
extra arguments.

## Upgrade, rollback and removal

To upgrade, run `git pull` in this clone. For each project, preview and then apply setup with the
same ID and paths used originally:

```sh
python3 scripts/setup.py --project /path/to/project --id my-project --dry-run
python3 scripts/setup.py --project /path/to/project --id my-project
```

Include your custom `--private` and `--method` on both commands if you used them. Private records
are kept. To roll back, check out the previous release tag in this clone and rerun setup for each
project with those same arguments.

To remove the method from a project, remove its generated `AGENTS.md` and
`.claude/agents/builder.md`, the `/.claude/agents/builder.md` line from Git's `info/exclude`, and any
`@AGENTS.md` line you added. Remove the shared method folder and session cache only after no
project uses them and no session is running. Private records stay unless you delete them.

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
