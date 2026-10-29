# Takeover and pausing

## Taking over a task

1. Read the task record's current view, then only the parts your next action needs.
2. Compare the record with reality: `git status`, the branch and the candidate it names, uncommitted
   work, and any builder or review still running or finished but unread. Read finished answers
   before acting.
3. Write yourself in as owner, with the date. A handoff note or an old owner line transfers nothing
   by itself.
4. Write down any discrepancy and how you resolved it. Keep rejected hypotheses and open findings; do
   not restart the investigation.

## Pausing or stopping

Before stopping, bring the current view up to date so a stranger could continue: what is done, what
is running, what is next and what would let it resume. Create temporary checkouts as shared clones
that write nothing into the source repository, inside your session's temporary folder
($TMPDIR in sessions the launcher starts). Remove them before stopping, after keeping anything
unique, and stop the processes you started. Leave launcher pool slots registered and in place.
Treat interrupted checks as not run.

## Sessions

Your context grows with every turn and costs more each time. Start a fresh session for substantial
independent work or a change of responsibility; it takes over from the record. The user's explicit
choice to continue in the current session wins. Never continue orchestrating on a weaker model.
