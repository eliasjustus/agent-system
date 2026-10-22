# Recovery

A failed or inconclusive check stays attached to its scenario in the task record, with an owner and
a next action. The builder owns progress; the verifier judges whether a check is valid. A
consequential question about intent goes to the orchestrator, and to the user only if the intent
itself is unclear.

## Diagnose

Write down in a few lines: expected and observed behavior, which candidate and check, how it was
run, the original output, and the next question. First establish whether the behavior under test
actually ran. Then classify it: implementation defect, invalid check, environment failure, unclear
intent or unknown. Prefer one small observation that separates the explanations over another broad
run. For a verifier, compare with the `--before` checkout named by the launcher, or a shared clone
inside $TMPDIR that writes nothing into the source repository, never by stashing or reverting.
After an environment-looking failure in a warm checkout, retry with `--clean-checkout` and keep both
results; do not silently count the retry as proof about the original failure.

## Correct a check only on evidence

Only when the settled scenario, the actual interface or a demonstrated fixture error contradicts it.
Keep the old result and say which assumption changed. Builder and verifier agreeing does not make a
check wrong. Where feasible, the corrected check still rejects the original defect.

## Recheck

Repair briefs carry all open findings at once, grouped into one repair plan per cause, not per
symptom, and only the latest failure output. Earlier outputs stay in the task's results folder. When a
failure belongs to a class of cases (a race, a late or stale reply, a reload), add its neighbouring
cases to the scenarios before the repair, and have the builder exercise them before handing back,
rather than letting verification find them one at a time.

After two verifier launches into one results folder (the `--out` directory), the launcher refuses
another before leasing a checkout unless `round-decision.md` there is a regular file, not a symlink,
non-empty after stripping whitespace, and modified after the newest counted result. Write one of:
the table of cases for the failure class, with the expected result per case and what must keep
working; a re-scope or redesign, with its reason, named in the delivery; or a retry with its reason
(environment, contention, flaky check). The launcher checks the file, not the decision's contents.
Each result whose header says `Role: verifier` counts once, including `.attempt-N` archives,
interrupted and failed runs, and `Result: running` markers even after their launcher was killed
(the agent may have run). `Result: launch refused`, other roles and other results folders do not
count. Neither do the decision itself, `.saving-*` temporaries, `.log` and `.lock` files and their
archived siblings, entries ending in `-evidence` (files or directories, including archived siblings),
subdirectories or their contents, symlinks, hard links, special files or unreadable files. Headers
that do not end within the 64 KiB scan bound are skipped too. Skipped links, special files,
unreadable files and overlong headers are named on stderr. The decision cannot be
used as `--out`; the launcher refuses that destination before writing or archiving anything.
Each later verification needs a decision newer than the newest counted result.

After a repair, independently rerun the reproducer, the affected must-not-break checks and the required
checks on the repaired candidate. Close a finding with recheck evidence or with evidence it was
mistaken.

## Retry only with a purpose

A changed hypothesis, a restored prerequisite or an uncertainty to resolve. One cheap retry of a
plausibly transient failure is fine; keep both results. Repeated identical failures call for a
different method, not more retries. Repeated rounds of fix and review are an overrun: check the
estimate and tell the user.

## Continue or stop

Ordinary technical recovery needs no approval. If recovery cannot finish now, write down the blocker
and what would let it resume, and deliver a reviewable candidate or a verified subset with its
known gaps. Reducing scope is your decision, recorded with its reason and named in the delivery as a
limit; it never turns missing verification into a pass, and a known security or data-loss defect
never ships. Above the floor, a round limit or a stop time never permits building it
yourself; at any depth it never permits weakening a scenario: stop and report instead.
