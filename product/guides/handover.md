# Handover: builders and reviews

## Choose the builder

Give the work to **Codex** when the outcome, design and acceptance checks are settled and the
assignment is self-contained. Otherwise give it to a **Claude** builder.

## Briefs

Write every brief from `templates/packets.md`: it lists what each role receives and what it never
receives. Put nothing else in. Every role but investigator is shut out of the private folder, so a
brief carries everything the role needs.

## Run reviews from the other family

Verifiers, challengers and reviewers run in their own session, from the model family the entry's
"Roles and models" names for them, through the launcher:

`python3 {{METHOD}}/run.py --role verifier --family codex --author-family claude --brief <brief> --out <task folder>/results/verifier-1.md --before <baseline commit>`

Before verification, commit the candidate: the launcher refuses tracked changes, tests the
committed HEAD in a clean leased checkout and names left-out untracked files in the answer's
header; challengers and reviewers read a snapshot that includes uncommitted work. Verifiers need
`--before <commit>` or `--before none`. Give Codex builders and verifiers `--network` or
`--writable DIR` only when the work needs them; retry with `--clean-checkout` after an
environment-looking failure, keeping both results. The third verifier launch into one `--out`
directory needs a fresh `round-decision.md` there (`guides/recovery.md`), so give each repair
sequence its own results folder. The header records what each session was given; the result file
decides what completed. Everything else the launcher does, from the private-folder shield and its
limits to result states, is in `run.py --help` and the launcher section of the Agent System README
(github.com/eliasjustus/agent-system). Never relay or summarise a review before reading it in full.

Without the launcher, the same safeguards apply: a detached worktree at the exact committed
candidate (`git worktree add --detach <checkout> <commit>`), `.agents/preflight` first if present
(an executable regular file that exits 0 and leaves tracked files matching the candidate), a
fresh session of the family the entry's "Roles and models" names, with the role contract, the brief and, for a verifier,
`guides/verification.md`; its answer and evidence kept unedited in `results/`; the same written
decision from the third verification on; the worktree removed afterwards.

Investigations, research and analyses that change nothing run the same way with `--role
investigator` and no `--author-family`: in the repository itself, read-only, and allowed to read the
private records when the brief needs them.

## Long work

Run builders and reviews in the background and stay available to the user. Before repeating
something that timed out, check whether it already took effect. Stop only the processes you
started, by their recorded identity, never by name matching. Wait for them the same way: on the
started process itself or its exact process ID, never with a `pgrep -f` loop: a pattern that also
appears in the loop's own command line matches the loop itself, and the loop never ends.

Wait on the outcome, not on one hoped-for state. A wait that ends only on success never ends on a
failure: a pull request whose required check failed stays open with auto-merge on. Use the
project's own wait command where it has one. Give the wait a deadline from the expected duration;
when it ends, for any reason, read why, act, and arm the next wait if
something is still pending. Tell the user what changed for them; an unchanged state or a question
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
