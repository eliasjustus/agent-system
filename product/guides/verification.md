# Verification

Read by the orchestrator before sending the verifier, and given to the verifier with its contract.

## Environment

Commit before verification. The launcher refuses staged or unstaged tracked changes and tests only
the committed HEAD captured when it starts. Untracked files stay out of the checkout and are named
in the header: a forgotten file must fail. When the header names untracked source files left out
and your checks pass, say so in your answer. Check `Snapshot commit:` against the brief's candidate.
Pool checkouts are reset before use; ignored build output remains warm.
Every reset removes FIFO/socket files and ignored symlinks pointing outside the slot, together
with their top-level ignored folder; a top-level link is removed itself, and its target is untouched.
Internal dependency links, including dangling ones, remain. Files ignored only through the user's
global `core.excludesFile` are removed every time: projects should ignore build folders in their own
`.gitignore` to keep them warm. A default ACL on a tracked directory is not reset (a known limit).
Ignored files may come from earlier sessions; if a check depends on one other than build output the
check itself produces, ask the orchestrator for a rerun with `--clean-checkout`.
After an environment-looking failure, the verifier reports it and recommends an orchestrator rerun
with `--clean-checkout`, keeping both results. Custom build scripts may reuse stale ignored output,
so establish which build actually ran. Gradle can also retain stale output from removed tasks,
such as generated sources still referenced by a `srcDir` or copy/package output another task reads.
When a candidate removes Gradle tasks or generated source folders, verify with `--clean-checkout`.

Use the cheapest real environment that exercises the behavior: the running service over its real
interface, a real browser for a component, a real repository or filesystem. A narrower environment
never proves a broader promise. A boundary failure is checked where it occurs, end to end.

Admit evidence only once you know which build ran and what state it used. A stale installed build or
a test that reached real per-machine state proves nothing about the candidate. Tests use their own
temporary resources and clean them up. Name the shared resources a real-system check touches (the
owner's running apps, a dev stack, a GPU, shared dependency folders); never use another session's
running service or write into a dependency tree other checkouts use; restore the owner's apps you
stopped, from outside your own process scope, and show that they run again.

When the candidate includes other merged changes, first run the cheapest checks that cover code they
share (build, type check, compiling the tests) before any narrower recheck.

Before a long timing or resource measurement, have the same candidate reviewed or probed cheaply
first; a blocker found that way saves the measurement.

Run checks so failures propagate. Never hide a failed command behind `tail` or another pipe.

## Before and after

- Your working directory is your leased copy of the committed candidate. With `--before <commit>`,
  use the writable **Before checkout** named in the prompt and header for the baseline. The launcher
  resets and cleans it like the candidate; do not delete either pool checkout.
  Without `--before`, build the **before** state from the baseline commit in a shared clone inside $TMPDIR
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
variations you chose not to test as untested. To call a reported defect not real, address the
reported entry point and conditions; a safe path elsewhere does not refute it.

## Report

```
Candidate: <commit you verified>
| ID | Before | After | Evidence |
| S1 | fail: <why> | pass | <file in your evidence folder> |
```

Then your own cases and what they showed, anything unverified and why, and your verdict: pass, fail
(with the scenario) or inconclusive (with the next question to answer).

Stop every process you started and remove your temporary checkouts. Leave launcher pool slots
registered and in place for reuse.
