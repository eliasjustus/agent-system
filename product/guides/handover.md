# Handover: builders and reviews

## Build or hand over

Who builds: the entry's invariant 1. Hand over to **Codex** when outcome, design and acceptance
checks are settled, otherwise to a **Claude** builder.

## Briefs

Use only `templates/packets.md`; briefs must suffice without private records, except for investigators
without `--web`. Builder briefs state promises, reasons, exclusions and checks deciding
done, not how to build.

## Run reviews

Verifiers, challengers and reviewers run in their own session, using the model named in the user's
`models.md`, through the launcher:

`python3 {{METHOD}}/run.py --role verifier --family codex --brief <brief> --out <task folder>/results/verifier-1.md --before <baseline commit>`

Run any review on the first verification's candidate, not after; cheaply probe or review before long measurements.

Commit before verification (`guides/verification.md`); challengers and reviewers see uncommitted work too. Verifiers need
`--before <commit>` or `--before none`. Give Codex builders and verifiers `--network` or
`--writable DIR` only when the work needs them. Give each repair sequence its own results folder
(`guides/recovery.md`). Headers record session inputs; result files decide completion.
See `run.py --help` and the Agent System README (github.com/eliasjustus/agent-system).
Read reviews in full before relaying or summarising.

The statements of a readme, report or design are not a verifier's scenarios: a reviewer of a text
(`templates/packets.md`) lists every one its sources do not support, at once.
A corrected sentence starts no new verification: the reviewer rechecks the changed passages.

Without the launcher: a detached worktree at the exact committed candidate
(`git worktree add --detach <checkout> <commit>`), `.agents/preflight` first if present
(an executable regular file that exits 0 and leaves tracked files matching the candidate), a
fresh session using the user's `models.md` model, with contract, brief and, for a verifier,
`guides/verification.md`; its answer and evidence kept unedited in `results/`; the same written
decision from the third verification on; the worktree removed afterwards.

Read-only investigations run likewise with `--role investigator`: in the repository, with private
records when needed. Add `--web` for web research: private records are then hidden, but readable
files can leave the machine, so never for projects whose files must stay local.

## Checks first (above the floor)

Before the build, launch a verifier on the baseline (`--before none`, its own results folder) with
the checks-first line in its brief. Treat the cases it adds like a challenger's. Keep its checks,
and name a copy outside the private folder in the builder's brief and the judging verifier's, or
that none could be written. Whoever builds never changes a check: a builder brings one it thinks wrong back to you.

## Long work

Before repeating
something that timed out, check whether it already took effect. Stop only the processes you
started, by their recorded identity, never by name matching. Wait for launcher sessions with
`python3 {{METHOD}}/run.py --wait <out>`, which ends however they end, never with a timed check-in:
the result then sits until the timer.

Wait on the outcome, not on one hoped-for state: a pull request whose required check failed stays
open with auto-merge on. Use the project's own wait command where it has one. When it ends, for
any reason, read why, act, and arm the next wait if something is still pending. Tell the user what changed for them; an unchanged state or a question
already asked is not news (see `guides/delivery.md`).

## After each delegation

- Save the agent's answer in the task folder's `results/`, unedited.
- Read the answer before acting on it. Answer every must-fix finding in the task record: fixed, or
  rejected with its reason. A later cleanup error does not erase a completed answer.
- A prerequisite a check proved (an environment variable, a path limit, a lock, a launch option)
  goes into every later brief of the task; what a worker's sandbox refuses, with the working
  alternative, into the project's `worker-notes.md`, which the launcher attaches to builders'
  and verifiers' briefs (Agent System README).
- A plan or synthesis you write from agents' answers is itself a candidate: before delivering it,
  have its consequential transformations (units, identities, arithmetic, rankings, dropped
  constraints) checked against those answers.
- Weaker models judge their limits poorly: review at checkpoints.
