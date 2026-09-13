# Existing-card Goal and exact-origin controls

These additive authenticated dashboard endpoints use the existing Kanban database, native transactions, and notification fence/cursor. They do not dispatch, unblock, replay, restart, or migrate cards by themselves. No new scheduler, authentication provider, model tool, or schema migration is required.

## Goal configuration

`PATCH /api/plugins/kanban/tasks/{task_id}?board=default`

```json
{"goal_mode": true, "goal_max_turns": 20}
```

Read back with `GET /api/plugins/kanban/tasks/{task_id}?board=default`; inspect `task.goal_mode` and `task.goal_max_turns`.

- Fields are strict JSON boolean and integer, respectively; strings/floats/boolean budgets are rejected (422).
- Omitted or null fields preserve the existing value. Zero budget selects the native inherited/default budget and reads back as null. Negative and out-of-SQLite-range integers are rejected. Positive budgets are preserved without an arbitrary new ceiling.
- Send Goal controls separately from other PATCH fields (400 on mixed payloads). The legacy endpoint commits its other operations independently; separating controls prevents a combined status edit from reclaiming a live run before the Goal guard runs, or a rejected control from leaving unrelated partial edits.
- The shared `hermes_cli.kanban_db_goal.set_goal_config` checks current status, claim and active run within the same IMMEDIATE transaction as its write. Changed values on running/claimed/active-run or archived cards fail with 409. Idempotent no-op writes remain allowed.
- Real changes emit `goal_config_set` audit data containing the changed Goal fields, then the existing `on_kanban_task_updated` observer after commit. No-op writes emit nothing. Existing delegated-child transaction guards remain in force. The setter cannot be nested inside another transaction.

## Exact notification binding

`PUT /api/plugins/kanban/tasks/{task_id}/notify-subscription?board=default`

All seven fields must be explicit (nullable thread/user, matching native storage semantics):

```json
{
  "platform": "discord",
  "chat_id": "EXACT_ORIGIN_CHAT_ID",
  "thread_id": "EXACT_ORIGIN_THREAD_ID",
  "user_id": "EXACT_ORIGIN_USER_ID",
  "chat_type": "thread",
  "notifier_profile": "default",
  "delivery_mode": "wake"
}
```

Use actual source data, never these placeholders. Telegram topics use their actual chat/topic IDs and source chat type; a topic is not a home-channel substitute. `chat_type` accepts dm/group/channel/thread. `delivery_mode` accepts notify/notify+wake/wake. Platform names and IDs are preserved as strings, not normalized. Unknown payload keys are rejected.

Read back with `GET /api/plugins/kanban/tasks/{task_id}/notify-subscription?board=default&platform=discord&chat_id=EXACT_ORIGIN_CHAT_ID&thread_id=EXACT_ORIGIN_THREAD_ID` (URL-encode values). Both endpoints return `subscription` with the seven routing fields, task ID, creation time, and last-event cursor; other internal delivery metadata is omitted. Absent thread is stored/read as the native empty string. Missing task/binding returns 404.

The native `kanban_db_notify.add_notify_sub(..., require_exact=True)` upsert preserves its existing semantics: chat type/mode are last-write-wins; notifier owner only fills an empty legacy owner; user ID is preserved. Conflicting preserved user/owner returns 409 atomically before writes, including attempting to replace an existing null user with a different identity. This is deliberate: the API must not falsely report a requested binding as persisted. Existing non-exact callers are unchanged. This endpoint does not transfer an already-owned subscription; reconcile such conflicts separately before migration rather than deleting/recreating rows and losing the notification fence.

New subscriptions begin at the current maximum task event ID, so past terminal events are not replayed. Existing subscriptions preserve creation time and cursor, including when metadata is updated. An idempotent rebind neither drops pending events nor replays consumed ones. No new subscription lifecycle event is introduced (native upsert has none).

## Authentication and origin safety

These routes are mounted by the real bundled plugin loader behind the existing dashboard token/cookie authentication, Host checks, and plugin-enable runtime gate. No security settings or guard markers are changed. Authenticate using the installed supported provider; keep session credentials in memory and never include them in review evidence.

The inspected base has no generic HTTP write-Origin check for these plugin paths (its origin helper is WebSocket-only). The new controls therefore add a narrow same-origin check, not an auth bypass: browser Origin must match the request scheme and authority, including port. Malformed/null/cross-origin writes get 403. Authenticated non-browser clients may omit Origin. Reverse proxies must preserve the external scheme/authority correctly; do not weaken this check by trusting arbitrary forwarded headers. Legacy PATCH operations without Goal fields are unchanged.

## Reproducible verification

Use the repository runner, not bare pytest:

```sh
scripts/run_tests.sh -j 4 tests/plugins/test_kanban_existing_controls.py tests/plugins/test_kanban_dashboard_plugin.py tests/plugins/test_kanban_dashboard_task_updated_hook.py tests/plugins/test_kanban_model_override.py tests/hermes_cli/test_kanban_goal_mode.py tests/hermes_cli/test_kanban_notify.py tests/hermes_cli/test_kanban_count_notify_subs.py
```

The new ASGI test imports the real dashboard app and its bundled plugin mount, uses temporary Hermes homes/boards and an isolated test credential, and exercises real SQLite and lifecycle mutators. It proves strict types, persistence/readback, omission, native zero/default semantics, post-commit observer visibility, no-op auditing, live claim protection, transaction guard retention, unauthenticated/origin denial, exact Discord/Telegram binding, terminal-event fencing, pending-event retention, consumed-event no-replay, and identity conflicts. No live messaging or provider calls are required.

## Review, activation, and rollback

This patch is prepared locally, not deployed. Primary independently reviews the exact local commit and reruns the suites before activation. The main checkout has unrelated changes; never overwrite/reset them. Review/cherry-pick this focused commit only into the intended release checkout after resolving any drift and recording a clean preactivation baseline. Preserve a format-patch copy outside the checkout for update-safe reapplication; do not rely on uncommitted edits surviving `hermes update`.

Activation requires the separately authorized dashboard process reload/restart so it imports the new routes. No default gateway restart is required merely to expose the HTTP controls. Identify the actual service and pending interactions first; do not stop arbitrary port owners. Verify authenticated OpenAPI contains the fields/routes and exercise a disposable isolated board before permitted production reconciliation. A readback from the staged test app does not prove activation.

Rollback is a reviewed `git revert` of this feature commit in the release checkout and the same authorized dashboard reload. No schema rollback is necessary; persisted Goal/subscription rows retain native compatibility. Code rollback does not undo authorized card changes: reconcile any operational rollback individually through supported controls, respecting current claims, cancellations, and already-consumed notifications. Never restore a stale whole-board database or replay historical events.
