# Native Kanban Goal checkpoint ownership

## Contract

`cli._run_quiet_single_query` delegates a Goal-enabled worker to
`hermes_cli.kanban_goal_worker.run_goal_query` before its first model call. The
owner validates the exact claimed run and card, scopes `_kanban_goal_loop_active`
to that agent, retains each returned history/session, and returns the last actual
result (including billing/failure). An environment flag alone never exempts a
classic agent from `agent.kanban_stop`.

A checkpoint is an unfinished turn, not a successful task transition. The native
Goal judge still decides continuation/finalization. Existing complete/review
judges, consent gates and board transitions remain unchanged. The loop checks run
state before and after judging, and the wrapper rechecks immediately before the
next model call. Reclaimed, cancelled/blocked and superseded workers stop.

Inner iteration caps remain hard. Exhausting one records a native failure and
ends that run; it does not refresh the budget via another Goal turn. The failure
write now atomically checks the originating run before touching task counters,
claims or events. Repeated finalization cannot close a successor or disturb a
completed/review/blocked task. Outer Goal turns and dispatcher runtime limits are
unchanged. Checkpoint before the cap (e.g. after a bounded implementation/test
slice), not after it. No model-compliance guarantee is implied.

## Evidence

- Canonical runner, 90 relevant Kanban/Goal/budget/CLI/tool files: 745 passed,
  zero failed, nine platform/optional skips. The 23 new integration checks include
  multipart/exception behavior and archive/interrupt during judging.
- Real AIAgent + quiet CLI over deterministic loopback HTTP, temporary board:
  two turns, two model calls, same session, retained history, stable system prefix.
  Both plain and multipart query inputs are exercised. Only transport responses
  and the judge verdict are scripted; no paid model call or customer data.
- Red-on-base b6e2013e3b: the transport case emitted two erroneous terminal nudges,
  used four model calls instead of two and resumed with history=0. All five
  budget-finalizer ownership/idempotence cases also failed. These are behavioral
  regressions, not source-text assertions.
- Fresh local Qwen Coder generated the bounded stop-gate edit using its direct
  standalone endpoint, no fallback. Owner reviewed/revised the documentation,
  integrated the lifecycle code and exercised the tests. Independent premium
  review identified the stale budget-finalizer race; it is covered by the fence.

Evidence logs and local-model receipt live in the owning task workspace. This is
a focused local repair, not a claim that the complete repository suite or all
platforms were tested.

## Activation / rollback

Use a narrow git commit/cherry-pick into the installed checkout after review;
retain unrelated dirty files. Fresh dispatcher-launched `hermes ... chat -q ... -Q`
workers import the updated quiet CLI and finalizer. Do not restart the gateway or
dispatcher merely for this worker-side change, and do not claim already-running
workers reloaded it. Verify installed module paths/hashes and run the same isolated
integration probes with the installed interpreter before releasing the dependent
outcome. No config/profile/database migration is required.

Rollback is a revert of the exact repair commit, preserving unrelated changes.
Drain affected workers before any separately authorized service restart. Reverting
reinstates terminal-only checkpoint behavior; do not tell an old worker that plain
checkpoints are supported. Preserve the original dependent outcome, authorization,
remaining work and test/spending budgets. Never fake completion to unblock it.

## Upstream reference

Official documentation consulted:
https://hermes-agent.nousresearch.com/docs/user-guide/features/kanban/

The documentation's completion-checkpoint section explicitly keeps the inner hard
cap, toolless final summary and consecutive-failure circuit. This repair preserves
those boundaries while making ordinary pre-cap Goal checkpoints usable.
