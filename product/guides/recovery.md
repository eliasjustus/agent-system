# Recovery

A failed or inconclusive check stays attached to its scenario in the task record, with an owner and
a next action. The builder owns progress; the verifier judges whether a check is valid. A
consequential question about intent goes to the orchestrator, and to the user if it changes what
was agreed.

## Diagnose

Write down in a few lines: expected and observed behavior, which candidate and check, how it was
run, the original output, and the next question. First establish whether the behavior under test
actually ran. Then classify it: implementation defect, invalid check, environment failure, unclear
intent or unknown. Prefer one small observation that separates the explanations over another broad
run. Compare with the before state in a shared clone inside $TMPDIR that writes nothing into the
source repository, never by stashing or reverting.

## Correct a check only on evidence

Only when the agreed scenario, the actual interface or a demonstrated fixture error contradicts it.
Keep the old result and say which assumption changed. Builder and verifier agreeing does not make a
check wrong. Where feasible, the corrected check still rejects the original defect.

## Recheck

Send all open findings together, grouped into one repair plan per cause, not per symptom. When a
failure belongs to a class of cases (a race, a late or stale reply, a reload), add its neighbouring
cases to the scenarios before the repair, and have the builder exercise them before handing back,
rather than letting verification find them one at a time. After a
repair, independently rerun the reproducer, the affected must-not-break checks and the required
checks on the repaired candidate. Close a finding with recheck evidence or with evidence it was
mistaken.

## Retry only with a purpose

A changed hypothesis, a restored prerequisite or an uncertainty to resolve. One cheap retry of a
plausibly transient failure is fine; keep both results. Repeated identical failures call for a
different method, not more retries. Repeated rounds of fix and review are an overrun: check the
estimate and tell the user.

## Continue or stop

Ordinary technical recovery needs no approval. If recovery cannot finish now, write down the blocker
and what would let it resume, and offer the user a reviewable candidate or a verified subset with its
known gaps. Reducing scope or accepting a risk is the user's choice and never turns missing
verification into a pass.
