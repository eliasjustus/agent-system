# Discovery, scenarios and design

## Discover

Find what the outcome depends on and what it displaces: the affected behavior, what produces and
consumes it, the lifecycle of any saved state, existing code to reuse and code the change retires.
For a defect, capture its conditions so a check can reproduce it, and reproduce it once on the real
system if that is cheap.

**Check the premise.** Ask what the user wants to achieve and whether the requested change serves
it. If the link is weak, recommend a quick diagnosis or a safer alternative first.

Stop when you have a credible route, representative scenarios and a clear boundary. Mention an
unrelated opportunity once, with its cost, and do not pursue it.

## Decide the depth

Use the three questions in the entry. Write each answer so a stranger could check it: "notes from a
folder's first import get no collection while later scans do", not "small change". Reassess when you
find another owner of the state, the design changes, or a check fails for an unexplained reason.
**Moving above:** stop dependent work, keep what is valid and add the missing safeguards; work you
built yourself goes to a separate builder and full verification. **Moving below** needs another
agent to confirm the trigger never applied. Agreed scenarios are never removed by a depth change.

## Scenarios (above the floor)

Draft them yourself, never the builder.

- Stated and **implied** cases. For a boundary failure, follow the result from where it is produced
  to where the user sees it, including other paths to it. For saved state, follow creation, defaults,
  updates, restart, recovery and removal.
- Mark each **deliver** or **must not break**. Must not break is only for behavior that works today.
- Then run the challenger (`guides/handover.md`) and adopt or reject each of its cases with a reason.

## Ask the user once

One message, at most about 250 words:

1. What you found, in two or three sentences of plain behavior.
2. **What I'll deliver:** a numbered list in the user's terms, with the scenario IDs.
3. **What stays as it is:** one line summarising the must-not-break list.
4. **Decisions:** numbered, each with your recommendation and its consequence.
5. **Estimate:** a time range.
6. "Reply 'agreed' to accept my recommendations."

Record the user's reply word for word in the task record with the date. After that, change an agreed
scenario only with the user's yes; until they say yes, the original stands. Never drop or weaken a
scenario to make a check pass.

Below the floor, write the short contract instead and ask only if a consequential question is open.

## Design

Name the problem addressed and the responsibilities the change creates or removes. Take the first
rung of the ladder that holds. Only for a material choice, compare keeping the approach, a bounded
repair and restructuring by their cost to build, run, verify and change later. Choose the cheapest
adequate approach and write the decision in two to four sentences.

## Plan

List the assignments in dependency order, each with its deliverable, acceptance evidence and
estimate. Name what would make you reassess.
