# Handover: builders and reviews

## Choose the builder

Give the work to **Codex** when the outcome, design and acceptance checks are settled and the
assignment is self-contained. Otherwise give it to a **Claude** builder. Above the floor you never
build yourself. Builders never delegate further, commit, push or publish, and never read the
private folder.
Builders run the project's `.agents/preflight`, if present, before handing back.

## Briefs

Write every brief from `templates/packets.md`: it lists what each role receives and what it never
receives. Put nothing else in.

## Run reviews from the other family

Verifiers, challengers and reviewers run in their own session, from the other model family than
the author of what they check. Commit the candidate before sending a verifier: the launcher
`{{METHOD}}/run.py` refuses tracked changes, leases a clean detached checkout at the committed
HEAD captured at startup, and leaves untracked files out, naming them in the answer's header.
It runs the candidate's committed `.agents/preflight`, if present, before starting the verifier;
a failure refuses the launch. Never relay or summarise a review before reading it in full.

`python3 {{METHOD}}/run.py --role verifier --family codex --author-family claude --brief <brief> --out <task folder>/results/verifier-1.md --before <baseline commit>`

Verifiers require `--before <commit>` to lease a second writable checkout for the baseline, or
`--before none` to start without one. If a before state is needed after all, the verifier makes
an ad-hoc shared clone inside $TMPDIR and builds it into its own build folder.
The third verifier launch into the same `--out` directory, and each later one, requires a fresh
`round-decision.md` there (see `guides/recovery.md`). Give each repair sequence, such as one
package of a larger task, its own results folder, so unrelated verifications do not count against it.
Pool slots retain ignored build output; retry with `--clean-checkout`
after an environment-looking failure, keeping both results. The pool has five slots per
repository; a busy pool falls back to a temporary session worktree without waiting.

Challengers and reviewers keep snapshots of uncommitted changes and untracked files up to 5 MB,
without moving a branch. Codex reads code through `git show <snapshot>:<path>`,
`git grep <pattern> <snapshot>` and `git diff`; only root AGENTS.md/CLAUDE.md are checked out.
Claude keeps its checkout and optional leave-out list, with `--full-checkout` when needed.

For Codex builders and verifiers, add `--network` when the assignment needs local servers, child
processes from Node or network access; otherwise the user's network configuration applies. Use
`--writable DIR` to open an extra directory, for example `--writable ~/.gradle` for a build cache.
Network access does not open extra directories. There is no full-access option because these
cover the known needs.

Codex challengers and reviewers start without plugins, skills or MCP servers (best effort: servers
that cannot be disabled stay enabled and the header reports it); Claude sessions use a fixed tool
list and no MCP servers. Builders and verifiers wait up to 30 minutes for memory and IO
room before starting (`Admission:` header); a project's build gate can wait the same way with
`python3 {{METHOD}}/run.py --wait-for-room`. The header records what each session was given.

Every role but investigator is shut out of the private folder, shell included: Codex through a
permission profile, Claude inside bubblewrap (`Shield:` header). A launch that cannot set this up is
refused. Not covered: outside services a session asks to read for it, and existing hard links.

Without the launcher, the same safeguards apply: a detached worktree at the exact committed
candidate (`git worktree add --detach <checkout> <commit>`), `.agents/preflight` first if present
(an executable regular file that exits 0 and leaves tracked files matching the candidate), a
fresh session of the other family with the role contract, the brief and, for a verifier,
`guides/verification.md`; its answer and evidence kept unedited in `results/`; the same written
decision from the third verification on; the worktree removed afterwards.

The result file decides what completed. Earlier results are kept as `.attempt-N` siblings; a
saved answer survives later failures and signals; `Result: running` means the launcher is running or
was killed (`flock -n <stem>.lock true` fails while it lives). Result states, exit codes and refused
output names are in the launcher section of the Agent System README
(github.com/eliasjustus/agent-system).

Investigations, research and analyses that change nothing run the same way with `--role
investigator` and no `--author-family`: in the repository itself, read-only, and allowed to read the
private records when the brief needs them.

## Long work

Run builders and reviews in the background and stay available to the user. Before repeating
something that timed out, check whether it already took effect. Stop only the processes you
started, by their recorded identity, never by name matching. Wait for them the same way: on the
started process itself or its exact process ID, never with a `pgrep -f` loop: a pattern that also
appears in the loop's own command line matches the loop itself, and the loop never ends.

## After each delegation

- Save the agent's answer in the task folder's `results/`, unedited.
- Read the answer before acting on it. A later cleanup error does not erase a completed answer.
- A prerequisite a check proved (an environment variable, a path limit, a lock, a launch option)
  goes into every later brief of the task.
- A plan or synthesis you write from agents' answers is itself a candidate: before delivering it,
  have its consequential transformations (units, identities, arithmetic, rankings, dropped
  constraints) checked against those answers.
- Builders come back when the work contradicts the design or scenarios, the scope changes, a check
  fails for a reason that is not plainly an implementation defect, or the same failure repeats after
  two attempts. Weaker models judge their limits poorly: review at checkpoints.
