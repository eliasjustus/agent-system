# Delivery and verdict

## Complete the change

Before proof: address indirect effects, simplify where justified and retire displaced code. A path
still needed by stored data, an outside consumer or rollback stays, with its removal condition
written down as open work.

## Proof

The delivery record is the pull request, or the task record where the project has none. Where the
project has its own publication rules (for example, proof in a review comment rather than the pull
request body), they decide where each part goes:

1. **What changed and why**, in the words of a future commit message.
2. **Proof:** each agreed scenario with its result, or `unverified: <reason>`, with the verifier's
   report table and the decisive output for each scenario pasted in (short excerpts, secrets
   removed), so a reviewer can judge it without the private folder. Full outputs stay in the task
   folder, linked from the task record; nothing in the pull request names the private folder. Say
   which model family verified.
3. **Limits:** anything unverified, deferred or newly risky, and any departure from these rules.
4. **Cost:** the estimate and the actual effort.

A failed agreed scenario stays failed here. It may also be recorded as a finding, but accepting the
failure or narrowing the outcome needs the user's yes, in their words.

If the code changed after verification, verify again. A linked file is not proof by itself: say what
the evidence shows. The required CI checks of the exact candidate are green, or a red or pending one
is a named limit.

## Tell the user

One message, at most about 250 words, in plain language:

1. First line: what now works.
2. **What you'll see:** the delivered behavior, as the user would try it.
3. **How it was checked:** one or two lines.
4. **Limits.**
5. Only if the change breaks something the user uses, changes what they see, or touches their
   data: one question, is the behavior right? Otherwise ask nothing: the request, or the merge the
   commit policy allows, is the user's sign-off.

Claim only what happened. Never promise future behavior. Settle interpretation before building, or
state it as a limit the user can correct. Leave out internal bookkeeping.

**Outward acts need the user's yes:** merging into a default branch where the project's knowledge
does not allow it, releases and tags, sending mail, sharing files, deleting a repository, changing
secrets, making something public. Commit and merge only as the project's commit policy says; if none
is recorded, do not commit and say so.

## On the verdict

Work that needed no question is signed off by its request once merged: record that, then finish
as for accepted work. Where you asked, keep only that change waiting (a draft pull request, the
question in the task record's `Open for user`) and carry on with the rest; ask it in the next
conversation with the user, one question, never a list of pending items. Record the reply word for
word in the task record with the date. Silence is not acceptance.

- **Accepted:** finish cleanup, add the delivered scenarios to the project's `checks.md` with how to
  run them, close the task record. Reply in at most three sentences.
- **Reported wrong later:** reopen the same task record, with a new candidate and pull request.
- **Changes requested:** continue under the same task record and the same pull request or a
  follow-up under the same number.

## Findings

A problem with the project's knowledge or checks goes into `findings/` in the project's private
folder (`templates/finding.md`). A problem with the method is recorded there as a finding, marked
`core`, and may also be reported to the method's upstream repository. Record the evidence, then
continue the product work.
