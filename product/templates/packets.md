# Briefs

Save each brief in the task folder as `briefs/<role>-<n>.md`. A brief never names the private folder
or anything in it, except an investigator's brief that needs the records. Each role also receives its contract from `roles/`. Include the user's decisions that bear on the work, word for
word with their conditions, within what each role may receive.

## Investigator

```
You are an investigator. Follow the investigator contract.
The user asked, in their words: "<exact words>"
Question: <what to find out, and what the answer is for>
Look at: <sources and paths>
Leave alone: <running processes, worktrees or files not to touch>
```

## Builder

```
You are a builder. Follow the builder contract.
Outcome: <one sentence>
Scenarios or contract: <the agreed list, IDs and text>
Design: <the decision, two to four sentences>
Assignment: <exactly what to change, and where>
Acceptance checks: <commands or observations, with expected results>
Estimate: <time range>; come back at about twice its upper end
Come back if: <task-specific conditions>
Proven failures: <approaches an earlier builder proved fail, or none>
```

## Challenger, pass 1

```
You are a challenger. Follow the challenger contract.
The user asked, in their words: "<exact words>"
The relevant code: <paths or entry points>
```

Nothing else: no design, no draft scenarios, no proposed fix.

## Challenger, pass 2

```
You are a challenger, pass 2. Follow the challenger contract.
The user asked, in their words: "<exact words>"
The relevant code: <paths or entry points>
Your pass 1 answer: <unedited>
The orchestrator's scenario list: <IDs, kinds and text only>
Compare it with your pass 1 cases, as your contract describes.
```

Never the design or your reasoning.

## Verifier

```
You are a verifier. Follow the verifier contract and the verification guide.
The user asked, in their words: "<exact words>"
Deliver: <IDs and text>
Must not break: <IDs and text>
Candidate: <commit>   Baseline: <commit>
Start the system: <commands, ports, data>
Evidence folder: named by the launcher
```

Never add the builder's report, rationale or an explanation of the diff.

## Reviewer

```
You are a reviewer. Follow the reviewer contract.
The user asked, in their words: "<exact words>"
Review: <pull request, commit range, design or document; exact scope>
Look especially at: <counterexamples or risks to check>
```
