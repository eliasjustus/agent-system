# Handover: builders and reviews

## Choose the builder

Use **Codex** for self-contained assignments with settled outcome, design and acceptance checks.
Otherwise use a **Claude** builder.

## Briefs

Write every brief from `templates/packets.md` and put nothing else in. Every role but an
investigator without `--web` is shut out of the private folder, so a brief carries everything the
role needs.

## Run reviews

Verifiers, challengers and reviewers run in their own session, using the model named in the user's
`models.md`, through the launcher:

`python3 {{METHOD}}/run.py --role verifier --family codex --brief <brief> --out <task folder>/results/verifier-1.md --before <baseline commit>`

When the work will get a review, run it on the same candidate as its first verification, not after;
before a long measurement, review or probe the candidate cheaply first.

Before verification, commit the candidate (`guides/verification.md`); challengers and reviewers
read a snapshot that includes uncommitted work. Verifiers need
`--before <commit>` or `--before none`. Give Codex builders and verifiers `--network` or
`--writable DIR` only when the work needs them. The third verifier launch into one `--out`
directory needs a fresh `round-decision.md` there (`guides/recovery.md`), so give each repair
sequence its own results folder. The header records what each session was given; the result file
decides what completed. The rest is in `run.py --help` and the Agent System README
(github.com/eliasjustus/agent-system). Never relay or summarise a review before reading it in full.

Without the launcher, the same safeguards apply: a detached worktree at the exact committed
candidate (`git worktree add --detach <checkout> <commit>`), `.agents/preflight` first if present
(an executable regular file that exits 0 and leaves tracked files matching the candidate), a
fresh session using the model named in the user's `models.md`, with the role contract, the brief and, for a verifier,
`guides/verification.md`; its answer and evidence kept unedited in `results/`; the same written
decision from the third verification on; the worktree removed afterwards.

Investigations, research and analyses that change nothing run the same way with `--role
investigator`: in the repository itself, read-only, and allowed to read the
private records when the brief needs them. Add `--web` for web research: that session cannot read
the private folder, but can send out what it reads, so not for projects whose files must stay
local.

## Long work

Run builders and reviews in the background and stay available to the user. Before repeating
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
  goes into every later brief of the task.
- A plan or synthesis you write from agents' answers is itself a candidate: before delivering it,
  have its consequential transformations (units, identities, arithmetic, rankings, dropped
  constraints) checked against those answers.
- Builders come back when the work contradicts the design or scenarios, the scope changes, a check
  fails for a reason that is not plainly an implementation defect, or the same failure repeats after
  two attempts. Weaker models judge their limits poorly: review at checkpoints.
