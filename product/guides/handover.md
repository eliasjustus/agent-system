# Handover: builders and reviews

## Choose the builder

Give the work to **Codex** when the outcome, design and acceptance checks are settled and the
assignment is self-contained. Otherwise give it to a **Claude** builder. Above the floor you never
build yourself. Builders never delegate further, commit, push or publish, and never read the
private folder.

## Briefs

Write every brief from `templates/packets.md`: it lists what each role receives and what it never
receives. Put nothing else in.

## Run reviews from the other family

Verifiers, challengers and reviewers run in their own session, on their own checkout of the exact
candidate, from the other model family than the author of what they check. One way to start these
checking sessions is the optional launcher `{{METHOD}}/run.py`: it snapshots the working tree,
uncommitted changes and untracked files up to 5 MB included, without moving any branch, gives the
reviewer a worktree of that snapshot, and saves the answer. Never relay a
review through yourself or summarise it before reading it in full.

`python3 {{METHOD}}/run.py --role verifier --family codex --author-family claude --brief <brief> --out <task folder>/results/verifier-1.md`

For a committed candidate, you can instead create a detached worktree at the exact commit:
`git worktree add --detach <checkout> <commit>`. Start a fresh session of the other model family
there, give it the role contract and the brief (and `guides/verification.md` for a verifier), and
save its full answer unedited in the task folder's `results/`. Give a verifier an evidence folder
and keep its evidence too. Remove the worktree when checking is finished. Uncommitted or untracked
candidates go through `run.py` so the checking session receives the exact snapshot.

Investigations, research and analyses that change nothing run the same way with `--role
investigator` and no `--author-family`: in the repository itself, read-only, and allowed to read the
private records when the brief needs them.

## Long work

Run builders and reviews in the background and stay available to the user. Before repeating
something that timed out, check whether it already took effect. Stop only the processes you
started, by their recorded identity, never by name matching. Wait for them the same way: on the
started process itself or its exact process ID, never with a loop over `pgrep -f`, whose pattern
matches the loop's own command line so the loop never ends.

## After each delegation

- Save the agent's answer in the task folder's `results/`, unedited.
- Read the answer before acting on it. A later cleanup error does not erase a completed answer.
- Builders come back when the work contradicts the design or scenarios, the scope changes, a check
  fails for a reason that is not plainly an implementation defect, or the same failure repeats after
  two attempts. Weaker models judge their limits poorly: review at checkpoints.
