# {{TITLE}}

<!-- task {{TASK_ID}} · project {{PROJECT}} · created {{DATE}} -->

## Current view

<!-- At most 150 words. Rewrite in place when the owner, a blocker or the state changes. -->
- Request: "{{REQUEST}}" ({{DATE}}, from the user)
- State: active  <!-- active | awaiting verdict | paused: <reason>; resume when <condition> | closed: <signed off by the request | accepted | abandoned | superseded by <id>> -->
- Depth: undecided  <!-- above | below, with the three answers below -->
- Owner: <session, date>
- Estimate: <time range>; started <UTC time from date -u>  <!-- actual effort is added at close -->
- Plan:
- Now:
- Next:
- Open for user: nothing

<!-- Below the floor, the depth answers, contract, proof and verdict may be one short paragraph each
instead of these sections. -->

## Depth

Boundary: yes | no | unknown. <behavior, file or passage>
Saved state: yes | no | unknown. <data and owner, source>
Transition: yes | no | unknown. <transition, source>

## Contract (below the floor; delete above)

Outcome: ... Boundary and exclusions: ... Design: ... Escalate if: ...

| ID | Kind | Check | Evidence |
| --- | --- | --- | --- |
| C1 | deliver | ... | pending |

## Scenarios (above the floor; delete below)

Settled: pending  <!-- date; date the user was told; any reply word for word -->

| ID | Kind | Scenario | Evidence |
| --- | --- | --- | --- |
| S1 | deliver | ... | pending |
| P1 | must not break | ... | pending |

## Decisions

<!-- Dated lines: design decision, user choices and their exact words, depth changes, challenger
cases adopted or rejected. -->

## Assignments

<!-- One line each: role, model family, brief file, result file. -->

## Deliveries

<!-- One block per delivery; never rewrite an earlier one. -->
### Delivery 1, {{DATE}}

- Candidate: <commit verified>, pull request: <link>
- Scenarios: <ID: pass | fail | unverified: reason>, ...
- Caught before delivery: <defects the challenger, verifier or reviewer found, one line each, or none>
- Limits:
- Cost: estimate <range>, actual <time>
- Verdict: signed off by the request  <!-- or, where you asked: pending, then the user's words and date -->

## Log

<!-- Dated notes. Keep the current view up to date. -->
