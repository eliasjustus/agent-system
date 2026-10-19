# Verification

Read by the orchestrator before sending the verifier, and given to the verifier with its contract.

## Environment

Use the cheapest real environment that exercises the behavior: the running service over its real
interface, a real browser for a component, a real repository or filesystem. A narrower environment
never proves a broader promise. A boundary failure is checked where it occurs, end to end.

Admit evidence only once you know which build ran and what state it used. A stale installed build or
a test that reached real per-machine state proves nothing about the candidate. Tests use their own
temporary resources and clean them up.

Before a long timing or resource measurement, have the same candidate reviewed or probed cheaply
first; a blocker found that way saves the measurement.

Run checks so failures propagate. Never hide a failed command behind `tail` or another pipe.

## Before and after

- Your working directory is your own copy of the candidate. Build the **before** state from the
  baseline commit in a shared clone inside $TMPDIR
  (`git clone --quiet --shared --no-checkout . "$TMPDIR/before" && git -C "$TMPDIR/before" checkout --quiet --detach <baseline>`).
  Put known-good and other temporary checkouts there too, using shared clones that write nothing
  into the source repository. Never stash, revert or edit the candidate's files.
  Make each extra checkout once and reuse it between cases, resetting it between cases; never
  create a checkout per case.
- Each check of new or repaired behavior must **fail on the before state for the reason under
  test**, then pass on the candidate. Keep the first failing output.
- Where a known-good version exists, the check also passes on it.
- Each must-not-break check passes on both.
- Checks test behavior, not one implementation: another correct fix must also pass.

## Your own cases

The scenario list is someone else's framing. Add at least one case aimed at what it most likely
missed: another path to the same result, state over time, an input the wording implies. Report
variations you chose not to test as untested.

## Report

```
Candidate: <commit you verified>
| ID | Before | After | Evidence |
| S1 | fail: <why> | pass | <file in your evidence folder> |
```

Then your own cases and what they showed, anything unverified and why, and your verdict: pass, fail
(with the scenario) or inconclusive (with the next question to answer).

Stop every process you started and remove your temporary checkouts.
