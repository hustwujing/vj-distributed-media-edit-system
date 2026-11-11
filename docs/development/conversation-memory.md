# Conversation memory and retry routing

`session_messages` is the immutable conversation ledger for a Session. Neither
compaction nor Run transitions delete its rows.

The context sent to the Manager is assembled in this order:

1. current server-owned Run state, authorization, contract and pending decisions;
2. the current user message;
3. raw ledger messages after the committed summary cursor;
4. the committed historical summary.

Lower-priority history cannot grant authority or overwrite current contracts.

Compaction is scheduled when the estimated token count of the committed summary
plus messages after its cursor reaches
`VIDEO_USE_CONVERSATION_COMPACT_TOKENS` (default `12000`). The worker leaves at
most `VIDEO_USE_CONVERSATION_TAIL_TOKENS` estimated raw tokens after the new
cursor (default `4000`). Scheduling is on the write path, while construction is
performed by workflow reconciliation. A source digest fences the job. Only a
successful summary commit advances `(through_created_at, through_message_id)`;
failure keeps both the old cursor and every raw ledger row.

User language never creates recovery authority. The server derives retry options
from the current Run, failed subject, graph and authorization versions, active
leases, cleanup and pending decisions. The Message Manager sees
`retry_current_failure` only when such an option exists. Applying it revalidates
the opaque option ID and submits the normal durable `resume` command.
