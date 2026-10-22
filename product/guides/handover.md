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
It runs the candidate's committed `.agents/preflight`, if present, before starting the verifier,
in the same checkout with build output kept. The file must be regular and executable; a failure,
timeout or tracked change refuses the launch. The header records `Preflight: passed in <s> s` or
`Preflight: none`. Full output is in the sibling `.log`, with the last 50 lines in a refusal.
Preflight has its own 900-second timeout (`AGENT_PREFLIGHT_TIMEOUT`); `--timeout` applies to the
agent. Only the candidate checkout runs it. Never relay or summarise a review before reading it in full.

`python3 {{METHOD}}/run.py --role verifier --family codex --author-family claude --brief <brief> --out <task folder>/results/verifier-1.md --before <baseline commit>`

Verifiers require `--before <commit>` to lease a second writable checkout for the baseline, or
`--before none` to start without one. If a before state is needed after all, the verifier makes
an ad-hoc shared clone inside $TMPDIR and builds it into its own build folder.
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

Codex challengers and reviewers start without plugins, apps, skill instructions or discovered MCP
servers. Their role contract and the project's `AGENTS.md` still load. If discovery fails or times
out, the session starts with MCP servers unchanged and reports this in stderr and the header.
Servers also stay enabled when their names cannot be written as bare keys (ASCII letters, digits,
`_` and `-`); stderr and the header list those names. Other Codex roles keep the user's configured
environment. Claude sessions use a fixed tool list and no MCP servers.

Builders and verifiers wait for memory and IO room before taking checkout leases or slot locks;
investigators, challengers and reviewers never wait. The launcher polls every 10 seconds, waits at
most 1800 seconds (`AGENT_ADMISSION_MAX_WAIT`), then starts under pressure and records why in the
`Admission:` header. Missing readings are ignored; without any readings it starts immediately.
Project build gates use `python3 {{METHOD}}/run.py --wait-for-room` with no other arguments; it
prints one status line to stderr and exits 0 when room is available, after the maximum wait, or
when readings are unavailable. Admission does not count against `--timeout`, which applies to the
agent session. Server discovery adds at most 30 seconds before it.
The header records the executable and, for Codex, `Requested sandbox:`, `Network:` and
`Requested environment:`. The requested environment is `restricted` (with any MCP fallback reason)
or `user configuration`. If snapshot or worktree creation fails before discovery, it is
`restricted; discovery did not run`. Discovery stderr is saved in the session's sibling `.log` file
alongside the agent's stderr.

For a committed candidate, you can instead create a detached worktree at the exact commit:
`git worktree add --detach <checkout> <commit>`. Start a fresh session of the other model family
there; before a verifier session, run `.agents/preflight` if present, requiring an executable
regular file, exit 0 and tracked files still matching the candidate. Keep its build output.
Give the session the role contract and the brief (and `guides/verification.md` for a verifier), and
save its full answer unedited in the task folder's `results/`. Give a verifier an evidence folder
and keep its evidence too. Remove the worktree when checking is finished. Uncommitted or untracked candidates may go through `run.py` for
challenger or reviewer snapshots.

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
