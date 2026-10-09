# Deliberate resume after draft PR evidence

The ready-lane `active_pr` guard remains the default. A native `unblock_task`
operation now captures the current task-comment ID watermark, in the same write
transaction as the unblock. The dispatcher acknowledges only comments at or below
that watermark and only until the next `claimed` event. New PR comments still
hold the card; automatic reclaim/promotion does not renew the acknowledgement.
Auth errors, rate-limit cooldowns, parent requirements, atomic claims and PR
completion acceptance are unchanged. No configuration toggle or schema migration.

Legacy unblock events without a watermark remain guarded. Do not infer a receipt
from timestamps: comments acquire their timestamps before locking, and cross-table
ordering cannot be recovered safely. Issue a fresh native deliberate unblock only
after inspecting the exact task, consent, prior PRs, active claims and budget.
Do not clear comments, close PRs or manufacture database events.

## Bounded recovery deployment gate

This repair is staged, not deployed. It does not itself start a worker or change a
production card. Import-cached gateway/dashboard processes must load the reviewed
version via an owner-coordinated supported restart. Do not restart from the worker,
kill a live lease, run a second dispatcher, or claim activation from a source edit.
Use the newly loaded native unblock tool; an old dashboard process still writes
legacy events until it too has been restarted.

For a dependency-held target, retain the prerequisite hold until the installed
runtime and native watermark creation have been verified. Once approved for
release, the legacy PR guard still protects the target. Primary can then explicitly
block/unblock that ready card through native operations, capturing the reviewed
existing PR evidence. Inspect the resulting watermark, guard result, exactly one
native claim/spawn, and the existing origin wake subscription. An arbitrary later
PR comment can invalidate the receipt; inspect it rather than deleting evidence.

Rollback: revert this isolated change and load the previous version through the
same supported lifecycle. Old readers ignore the extra event field and restore
normal active-PR guarding. Do not remove stored audit events or alter PR state.

## Verification

`tests/hermes_cli/test_kanban_pr_resume.py` uses real temporary native boards and
imports. It covers ordinary duplicate PR protection, explicit unblock, same-second
new comments, dependency holding/release, competing concurrent claims, consumed
receipt after reclaim, native dispatch selection, auth and cooldown priority,
legacy receipt refusal, and timestamps whose order differs from comment IDs.
The spawn callback records selection without launching a model worker. This is
not a live production activation test or an end-to-end model evaluation.
Run with the canonical `scripts/run_tests.sh` (isolated HOME/credentials).
