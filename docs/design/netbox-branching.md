# Design record: netbox-branching support (#143)

Status: ratified at r6 (2026-09-30, round 6). ADR 0016 records the decision and revises ADR 0015.

## 0. Review record

Refuted claims, each with its evidence and reopening condition:

- None stands. R1-1b (round 1) claimed the plugin documents no API that runs inside a caller's
  transaction. Round 2 contradicted it: `docs/examples.md:404-412` documents a direct
  `apply_device_interface_rules` call. The refutation is withdrawn and finding 1b is handled in r3.

## Decision

How the plugin's writes and its rename-trigger lifecycle (ADR 0015) work when netbox-branching is
installed and a branch is active. Behaviour on main (no branch, or netbox-branching not installed)
does not change.

## Constraints decided by the operator (2026-09-30)

1. The rename triggers do not act during a netbox-branching merge, revert or sync. The design picks
   the mechanism and keeps it correct when those operations fail or return early.
2. Supported: netbox-branching 1.2.x on NetBox 4.7. The plugin still supports NetBox 4.3 to 4.7
   without netbox-branching, with no change in behaviour.
3. A session-level `lock_timeout` (10 s) on both connections for the whole plugin operation,
   including the commit callbacks. It is set before either transaction opens and restored in a
   `finally`. A connection whose restore fails is discarded. A nested operation inherits the outer
   setting.
   **Revised by the operator (2026-09-30, round 3):** the timeout covers the plugin's own
   transactions and every callback that runs before the plugin's outermost scope exits. When a
   caller already holds a transaction on a connection, the plugin sets and restores the timeout
   inside it, and the callbacks that run at the caller's commit are the caller's. NetBox's generic
   views and API for the rule model are NetBox's, as for every NetBox model. Reason: a request in a branch holds two Postgres sessions, and Postgres does not detect a
   lock cycle through both sessions of one request.
4. Delivery: stacked PRs into `develop`, this record and the ADR first.

## Acceptance conditions

The acceptance criteria of #143, and: the existing suite passes on every CI leg.

**Narrowed by the operator (2026-09-30, round 3):** "merging gives main the branch's names" excludes
a channel child that the plugin kept at its old name (its configured target was taken) while its
parent was renamed. When a merge, revert or sync replays the parent's rename, NetBox's channel
cascade (NB dcim/models/mixins.py:302-337) runs again at the commit and renames that child if the
cascade's name is free. The plugin does not change NetBox's cascade; the behaviour is documented and a
test pins it.

## Evidence

Source-read: netbox-branching v1.2.1 (and v1.1.3 for comparison), NetBox 4.7.0, the plugin at
`develop` ad4a979. netbox-branching was not installed in the development environment, so no claim
below is from a run with netbox-branching until the CI leg exists. The reviewers executed three
narrower checks without netbox-branching: the `reconcile_after_parent_cascade` check in round 3 and
the queue-level proofs in rounds 5 and 6. The record marks each of them "executed".

## Process

- Round 0 (blind co-design): two designers, one brief, isolated inputs. Designer A: Claude Opus
  (xhigh effort), fresh context. Designer B: codex gpt-6-astra (high effort), read-only sandbox.
  Neither saw the other's design, the sibling plugins' designs, or the coordinator's research notes.
- Merge: divergence table below.
- Ratify rounds: codex gpt-6-astra (high), read-only.

## Divergence table

A = designer A (Opus), B = designer B (codex). NB = NetBox 4.7.0 `netbox/`, BR = netbox-branching 1.2.1
`netbox_branching/`, INR = this plugin's package.

| # | Decision | A | B | Evidence | Disposition | Consequence (a test that differs) |
|---|---|---|---|---|---|---|
| 1 | Merge/revert/sync skip | `pre_*` sets a ContextVar, `post_*` resets it; a stale value resets when the branch no longer has the transitional status (one `default` query per trigger while set) | wrap `Branch.merge/revert/sync`; ContextVar token reset in `finally` | `post_*` is not sent on the "no changes" return or on failure (BR models/branches.py:917-925, 1114-1121, 1187-1197). A's reset reads BR status values and leaves its own open question of a stale skip beside a concurrent operation | **B**. The `finally` covers every exit exactly; no status coupling, no extra query. The wrapper is the one patch of another plugin's class; the version gate pins it to 1.2.x and a contract test checks it | after a failed merge, a save in the same context before any trigger query: A skips if the status restore has not landed, B never skips |
| 2 | Alias of plugin commit callbacks (`names.py:101`) | `default` (runs after both commit) | the write alias, after NetBox's own channel cascade | NB queues the channel cascade on the saved interface's alias (NB dcim/models/mixins.py:291-307) | **A (reversed in r3; r1 and r2 chose B), made independent of nesting in r6**. With B, the callback runs inside `default`'s open transaction, so a failed reconciliation rolls back the ChangeDiff rows of a branch family that already committed (round 2, finding 5). With A, it runs after both commit, so ChangeDiff stays intact, and the order cascade → reconciliation holds (round 1, Q-C). Registering on `default` alone ran it before the cascade when a caller held only the branch transaction (round 4, finding 1), so r6 joins an acknowledgement from each connection (r5's forwarding lost a `default` rollback, round 5). Channels stay unreconciled when `default` fails after the branch commit (under A and B) and when the reconciliation itself fails (failure table) | a reconciliation lock timeout: ChangeDiff kept (A) or lost (B) |
| 3 | How a job finds its branch | `copy_safe_request` of the enqueuing request (session cookie stored in Redis) + expected alias | branch schema id + expected alias, derived server side; the runner puts the id on its synthetic request | BR reads the schema id from a header only on an API path (BR utilities.py:542), from a query parameter with flash messages (:551-571), or from its cookie (:574-581); A lists the session cookie in Redis as its own open question | **B**, with BR's branch cookie as the carrier (the header needs an API path, the query parameter writes flash messages). It carries only the identity the job needs | a job's stored kwargs contain no session cookie (B) |
| 4 | NetBox swallows a failed branch activation (NB utilities/request.py:132-142) | document only | a middleware check before every view | the swallow affects every core write in that request, which INR cannot make safe | **A**. Upstream-owned; INR's trigger checks `using` against the router for its own writes | none for INR's acceptance conditions |
| 5 | `raw` saves | not skipped | skipped | INR does not read `raw` today (INR signals.py:18-50); replayed creates are covered by #1 | **A**. Skipping raw saves changes behaviour on main (fixture loads) | `loaddata` of a module on main: A reapplies as today |
| 6 | Timeout scope entered while `default` is already in a transaction (NB ScriptJob: extras/jobs.py:316-326) | not addressed | refuse | Postgres undoes a session `SET` when its transaction aborts and keeps it when it commits | **Neither**: allow it, with the set and the restore in the same transaction state on each connection (r2, "Timeout scope"; confirmed in round 1, Q-A) | a script in a branch: runs (proposed) or refused (B) |
| 7 | NetBox version check | none | refuse NetBox outside 4.7 | BR 1.2 declares NetBox 4.7 as its minimum (BR __init__.py:17-21) | **A**. BR already enforces it | none |
| 8 | CI cells | one: NetBox 4.7.0 + BR 1.2.x | two: minimum 1.2.0 and latest 1.2.x | reproducible pins; 1.2.0 and 1.2.1 share the paths INR uses | **A, pinned to 1.2.1** | none |
| 9 | Slicing | 6 PRs, CI leg second | 4 PRs, CI inside the write-lifecycle PR | a harness first lets later slices start red | **A** | none |
| 10 | Scope name | `plugin_operation(expected_write_alias=)` | `write_scope(using=)` | naming only | `write_scope(expected_alias=)` | none |
| 11 | netbox-branching 1.3 or later | refuse (open) | refuse | operator: 1.2.x only | **refuse**; reopen if the operator prefers a warning | NetBox refuses to start with an unsupported version |

Round 1 resolved #6 and the rule-cache question (r2).

## Merged design r6

Changes from r5 (round 5):

1. `on_commit` is a join of the two commits: at the call it registers one acknowledgement on each
   connection with Django's own savepoint-tagged `on_commit`, and runs the callback once, when both
   acknowledgements have fired. A rollback on either connection, of the transaction or of a savepoint
   around the call, drops that connection's acknowledgement, so the callback never runs (finding 1).
   This replaces the r5 forwarding, which lost the history of a `default` rollback before the branch
   commit. It is the general rule for this mechanism: rounds 3, 4 and 5 each found one nesting that a
   single registration mishandled.

Changes from r4 (round 4), carried into r5:

1. `on_commit` runs a plugin callback after every open transaction on both connections has
   committed, in any nesting: it registers on the branch connection, and when it fires while
   `default` is still in a transaction, it registers again on `default` (finding 1).
2. The validation names NetBox's branch-side channel cascades, not the plugin's callbacks, for the
   timeout test of a trigger in a script, and adds a sync case for the replayed cascade.
3. Divergence #2 names every case that leaves channels unreconciled.

Changes from r3 (round 3), carried into r4:

1. The merge, revert and sync acceptance conditions exclude a channel child that NetBox's replayed
   cascade renames (operator decision; finding 1). A test pins the behaviour.
2. The recovery of a failed channel reconciliation is qualified: Apply Rules does not restore the kept
   name while the configured target stays taken (finding 2).
3. Only the public `on_commit` helper registers on `default`. The internal commit marker of
   `atomic_with_events` stays on the branch connection, so it records the branch commit before a
   later callback or the `default` commit can fail (round-3 callback inventory).

Changes from r2 (round 2), carried into r3:

1. Plugin commit callbacks register on `default` and run after both connections commit
   (divergence #2 reversed; finding 5).
2. The timeout contract names what it covers and what belongs to a caller or to NetBox, as the
   operator's revision of constraint 3 (findings 1, 2, 3).
3. The documented engine functions (`docs/examples.md:404-412`) are an entry point, in the table.
4. The rule model's REST endpoints refuse a background (`AsyncAPIJob`) request while a branch is
   active; NetBox's other endpoints keep the upstream gap, documented (finding 4).
5. The failure table separates a failure before the branch commit, a failure in a callback on the
   branch connection, and a failure in a plugin callback after both commit (finding 5).
6. The entry-point table lists every entry point the round-2 audit named.

Changes from r1 (round 1), carried into r2:

1. The timeout scope rule no longer requires autocommit at entry. When a caller already holds a
   transaction on a connection, the set and the restore run inside it (finding 1, Q-A).
2. The supported entry points of an operation are listed, each with the transaction state at entry.
3. A failed restore discards the connection in every entry state (finding 2).
4. Upgrade: pending plugin jobs must finish before the upgrade (finding 3).
5. The rule cache keys its snapshot and memo by read alias, and a pinned cache checks its alias (Q-D).
6. "Events kept" now says that NetBox dispatches them only when its event context exits cleanly (Q-F).

### Modules and interfaces

**`transactions.py`** stays the one owner of connections and transaction state.

- `write_alias() -> str`: `router.db_for_write(Interface)`.
- `write_scope(expected_alias=None)`: pins the operation's aliases, `("default",)` on main or
  `("default", <branch alias>)` in a branch. With `expected_alias`, it raises before any query when
  `write_alias()` differs. A nested scope inherits the outer one and raises when its alias differs.
  In a branch only, the outermost scope reads `lock_timeout` on both connections and sets it to 10 s
  (`set_config('lock_timeout', '10s', false)`), and restores each value in a `finally` after the last
  commit callback of the plugin's own transactions. On main it runs no query. Restores are
  independent. A restore that fails closes the raw DB-API connection, then Django's connection, and
  raises from the original error, whatever the transaction state at entry. Partial setup restores
  what it changed. See "Timeout scope" for the transaction state at entry.
- `atomic_with_events()`: joins the open scope or opens one. Its first block opens after the scope
  set the timeout. It opens `atomic("default")` and, with
  two aliases, `atomic(<branch alias>)` inside it, so the branch commits first and `default` second,
  as NB scripts (extras/jobs.py:316-326) and BR sync (models/branches.py:936-940) nest them. The
  plugin keeps its queued events when the branch block commits (outermost) or is released (nested)
  with no alias marked for rollback. NetBox dispatches them only when its event context exits
  cleanly (NB netbox/context_managers.py:22-33); a later failure can still drop them. `Block.set_rollback()` marks every alias. The `using` parameter goes.
- `on_commit(callback)`: runs *callback* once, after the transactions open at the call commit on both
  connections, whatever their nesting. At the call it registers one acknowledgement on each
  connection with Django's `on_commit` (non-robust), which tags it with that connection's current
  savepoints (Django db/backends/base/base.py:727-732). A one-shot gate runs *callback* when both
  acknowledgements have fired. A connection in autocommit at the call acknowledges at once (Django
  runs the callback immediately, :737-747). A rollback of the transaction, or of a savepoint around
  the call, on either connection drops that acknowledgement (:405 and the rollback path), so
  *callback* never runs. The branch acknowledgement queues after NetBox's channel cascade, which the
  parent save registered earlier on the branch connection (NB dcim/models/mixins.py:302-307), so
  *callback* runs after that cascade. On main the two connections are one: one registration, as
  today. It raises without an open scope. The gate holds the complete set of pending connections
  before it registers the first acknowledgement, and marks itself fired before it runs *callback*.
  Trigger deferral does not use it: the trigger registers on
  the save's connection directly (below). The internal commit marker of `atomic_with_events` stays
  on the branch connection (INR transactions.py:29-43).
- Invariant: INR writes only through an alias of the open scope.

**`branching.py`** is the one module that imports `netbox_branching`, loaded only when it is
installed. `AppConfig.ready()` calls it.

- Version gate: raise `ImproperlyConfigured` unless the installed netbox-branching is 1.2.x.
- Replay suppression: wrap `Branch.merge`, `Branch.revert` and `Branch.sync` once (idempotent,
  `functools.wraps`). Each wrapper sets a ContextVar token and resets it in `finally`.
  `replay_in_progress() -> bool`.
- Job identity: `branch_identity() -> str | None` (the active branch's schema id) and
  `activate_on(request, identity)`, which sets BR's branch cookie on a synthetic request.

### Timeout scope

A session `SET` issued inside a transaction is undone when that transaction rolls back and kept
when it commits (PostgreSQL `SET`). So when the set and the restore run in the same transaction on a
connection, the connection ends with its value from before that transaction after either outcome,
also when the restore itself cannot run in an aborted transaction. The scope is lexical, so its set
and restore run in the same transaction state on each connection. Entry points of an operation:

| Entry point | `default` at entry | Branch connection at entry | Covered by the plugin's scope |
|---|---|---|---|
| Apply Rules / Convert view, foreground POST; rule toggle; Convert GET dry run (rolls back both aliases) | autocommit | autocommit | all its transactions and callbacks |
| Plugin job (Apply Rules, Convert) | autocommit | autocommit | all |
| Rename-trigger plan, UI / REST / bulk import / bulk edit (incl. a bulk module-type change) / `AsyncViewJob` | autocommit | autocommit (the plan runs at the commit of NetBox's branch block) | all |
| Rename-trigger plan, NetBox script | inside the script's transaction (NB extras/jobs.py:316-326) | autocommit (the plan runs at the commit of the script's branch block) | the plugin's transactions and its branch-side work; plugin callbacks on `default` run at the script's commit, the caller's |
| Documented engine function (`apply_device_interface_rules`, `docs/examples.md:404-412`) in a shell | autocommit | autocommit | all |
| The same function inside a caller's transaction (a script) | inside the caller's transaction | inside the caller's transaction | the plugin's statements; every callback runs at the caller's commit, the caller's |
| Rule create / edit / import / bulk edit / delete through NetBox's generic views and API | NetBox's (branch block open at the model save) | NetBox's | not a plugin operation: NetBox owns it, as for every NetBox model. The partial save of `models.py:364-375` joins NetBox's transaction |
| Rule REST request in the background (`AsyncAPIJob`) with a branch active | refused with an error before any write (r3 change 4) | | |
| Plugin migrations; the management command | the migration's transaction / no write | | not a runtime operation |

NetBox's own callbacks that core saves registered are NetBox's. The upgrade notes and
`docs/examples.md` say that the documented engine function, called inside a transaction the caller
holds, leaves the waits of the commit callbacks to the caller.

### Trigger lifecycle (revises ADR 0015)

- The receivers pass the signal's `using` (INR signals.py:18-50). `raw` stays unread.
- `before_save` and `after_save` return at once while `replay_in_progress()`: no previous-state read,
  no trigger, no plan.
- Otherwise the trigger raises when `using` differs from `write_alias()`.
- It defers on `transaction.get_connection(using)`: it checks that connection's atomic state, scans
  its `run_on_commit` for the plan, registers each trigger with `on_commit(..., using=using)` and
  appends the untagged runner there. ADR 0015's coalescing, ordering and savepoint filtering stay;
  only the connection changes. In a branch the plan runs after NetBox's branch transaction commits,
  after `Module._save_new()` created the interfaces (NB dcim/models/modules.py:524-593).
- The plan records its alias; the runner opens `write_scope(expected_alias=plan.alias)`. Request and
  `event_tracking` are still active, so each rename gets an ObjectChange with `prechange_data` in the
  branch.

### Jobs

`_enqueue` stores `branch_identity()` and `write_alias()`, derived server side. The runner builds its
synthetic request with BR's branch cookie (`branching.activate_on`), enters the request processors strictly (INR jobs.py:16-38),
and opens `write_scope(expected_alias=...)` before it reads the rule. A branch that is not ready makes BR's
cookie path return no branch (BR utilities.py:574-581), so the router returns `default`, and the job fails naming both aliases. A job enqueued
before the upgrade fails; there is no compatibility path, so the upgrade notes say to let pending
plugin jobs finish first.

### Rule cache

The rule cache (INR rule_selection.py:128-172) adds the read alias to its snapshot and memo keys, and
a pinned cache checks its alias before its fast return. Round 1 found no wrong name from the missing
key (the fingerprint covers the fields a rename reads), but a cached instance must come from the alias
it serves.

### Failure behaviour

| Event | Result | Left behind |
|---|---|---|
| Name collision in a member | savepoints on both aliases roll back; member BLOCKED; the rest continue | nothing for that member |
| Lock timeout (55P03) or other error before the branch commit | the family rolls back on both aliases; the plan reports FAILED in the journal; a view shows an error; a job errors | families committed before it |
| Failure in a callback on the branch connection after the branch commit (NetBox's channel cascade) | the error leaves the branch block after its commit and rolls `default` back | the branch family and its ObjectChanges without ChangeDiff rows (the partial commit below); names of the channels the cascade did not rename. Reported as FAILED |
| Failure in a plugin callback after both commit (channel reconciliation) | the callback's own transaction rolls back; the error is reported as for a failure after commit | the family, its ObjectChanges and ChangeDiff rows; each child the cascade renamed carries the cascade's name. Apply Rules does not restore the kept name while its configured target stays taken: the parent is unchanged, so no reconciliation is scheduled (INR family/execution.py:120-127, family/names.py:92-93). The journal entry names each child and the name it kept, and the operator frees the target and reapplies or renames the child back |
| Branch COMMIT fails | branch rolls back; the error rolls `default` back | nothing |
| `default` COMMIT fails after the branch committed | error raised and reported; the plugin keeps its queued events for the committed rows, and NetBox may not dispatch them | branch rows and ObjectChanges without ChangeDiff rows. A merge still replays them (it reads ObjectChanges, BR models/branches.py:427-433); the branch diff view misses them. Accepted: nested Django atomics give no cross-session atomicity |
| Restore of `lock_timeout` fails | see `write_scope` | no session with the setting in a pool |
| Merge/revert/sync fails or returns early | the wrapper's `finally` resets the skip | nothing |

### Guards

- Extend `TransactionBlockTest` (INR tests/test_module_boundaries.py:737-778): outside
  `transactions.py`, forbid `atomic`, `savepoint*`, `set_rollback`, `get_rollback`, `on_commit`,
  `get_connection`, `mark_for_rollback_on_error`, and importing `connection`, `connections` or
  `router` from `django.db`. Named exceptions: `rename_triggers.py` (`on_commit`/`get_connection`
  with an explicit alias only); `models.py` (`router` for its own row).
- An AST test: only `branching.py` imports `netbox_branching`.
- CI: the branch leg sets `EXPECT_NETBOX_BRANCHING=1`, which turns the branch tests' skip into a
  failure (the `EXPECT_NETBOX_CHANNELIZATION` pattern).

### Validation

`tests/test_branching.py`, `TransactionTestCase`. A helper provisions as BR's tests do
(`save(provision=False)`, `provision(user)`, `refresh_from_db()`, BR tests/utils.py:12-28);
`tearDown` deprovisions and closes the branch connection (`--reuse-db` keeps schemas). Requests use
the test client with BR's cookie or header, so middleware, NB's view transaction, the trigger and the
plan run for real. One test per acceptance condition, plus: bulk import; merge with the iterative
and the squash strategy; merge with no changes, a dry run and an injected failure, each followed by
a save that must reapply; a flat family blocked after partial writes leaves no rows and no
ChangeDiff; `lock_timeout` is 10 s inside and restored after, nested scopes do not restore, a failed
restore discards (also inside a caller's transaction); a trigger in a NetBox script in a branch keeps
10 s on both connections through NetBox's branch-side channel cascades and restores both before the
script's `default` callbacks run (`test_script_trigger_restores_timeouts_before_caller_default_callbacks`);
the documented engine function inside a branch-only caller transaction, and inside both nestings of
the two connections, keeps a blocked child's name after the caller frees its target and commits;
`test_on_commit_orders_cascade_and_reconciliation_for_each_transaction_nesting`;
`test_branch_outer_default_rollback_discards_reconciliation`;
`test_default_savepoint_rollback_discards_reconciliation_before_branch_commit`;
`test_trigger_savepoint_rollback_filters_only_rolled_back_triggers`;
`test_trigger_runner_reconciliation_during_branch_callback_drain`; the timeout after an
aborted caller transaction; a reconciliation lock timeout keeps the ChangeDiff rows, and the journal
names each child and its kept name; a merge and a revert (iterative and squash) and a sync after a
blocked channel child was kept pin the documented cascade behaviour, asserting names after the
callbacks ran; a background rule REST request in a branch is refused and writes nothing on either
alias; the Convert GET dry run leaves no row and no ChangeDiff; the rule cache in main and in an identical branch, then after a
branch-only rule change; the version gate.

CI: one new cell, NetBox 4.7.0 + netbox-branching 1.2.1, `DynamicSchemaDict`, `BranchAwareRouter`,
`netbox_branching` last in `PLUGINS`, `EXPECT_NETBOX_BRANCHING=1`, and the whole suite. Query counts
are recorded, not asserted, on that leg (`record_change_diff` adds queries). The plain 4.7.0 leg
still asserts `query_counts.json`, which proves main adds no query.

### Slicing (stacked into `develop`)

1. ADR 0016 + this record. AC: ADR.
2. Branch CI leg, test helper, `EXPECT_NETBOX_BRANCHING`, version gate, a provisioning smoke test.
   AC: CI leg.
3. Two-alias transactions: `write_scope`, `atomic_with_events`, `Block.set_rollback`, `on_commit`,
   `lock_timeout`, view scopes, `models.py`, guard extension, rule-cache alias key, callbacks on
   `default`,
   `template_names.py` docstring.
   AC: Apply/Convert in the foreground; collision via Apply; docstring.
4. Trigger on the write alias. AC: UI/REST install; collision on install.
5. Jobs in the branch, and the refusal of background rule REST requests in a branch. AC: jobs and
   the start check.
6. Replay suppression and `docs/configuration.md`. AC: merge, revert, sync, docs.

### Accepted limits (documented)

- No cross-session atomicity: a `default` failure after the branch commit, or a failure in a
  callback on the branch connection, leaves branch rows without ChangeDiff rows. NetBox scripts and
  netbox-branching's sync nest the connections the same way.
- NetBox's `AsyncAPIJob` drops the branch identity (NB netbox/api/viewsets/mixins.py:345-351,
  netbox/jobs.py:292-326), so a background REST request made in a branch runs on main. The plugin
  refuses it for the rule model; NetBox's other endpoints, including DCIM saves that fire the rename
  triggers, keep the gap.
- NetBox swallows a failed branch activation in its request processors; core writes share it.
- A session `SET` does not survive transaction-mode connection pooling (PgBouncer); unsupported.
- Upgrade: pending plugin jobs must finish before the upgrade.
- A replayed parent rename re-runs NetBox's channel cascade at the commit of a merge, revert or
  sync; a child that the plugin kept at its old name can then differ from the branch (acceptance
  conditions, narrowed).

## Rounds

### Round 1 (r1, codex gpt-6-astra high): NOT RATIFIED

| # | Finding | Severity | Disposition |
|---|---|---|---|
| 1a | The plan runner in a NetBox script enters while `default` is in the script's transaction, so "set before either transaction opens" cannot hold | blocker | CLOSED in r2 (changes 1, 2): the rule now rests on PostgreSQL's transactional `SET`, which the reviewer confirmed (Q-A); verified NB extras/jobs.py:316-326, 398-403 |
| 1b | A script calling the apply operation inside its transactions leaves callbacks unbounded | blocker | REFUTED (section 0, R1-1b) |
| 2 | Discard only when the entry was autocommit weakens the operator's constraint | major | CLOSED in r2 (change 3) |
| 3 | Queued jobs fail after the upgrade | minor | CLOSED in r2 (change 4) |
| Q-B | Wrapper covers every stock BR 1.2.1 merge/revert/sync path | answer | confirms divergence #1 |
| Q-C | Callback order with #2: cascade before reconciliation on success | answer | confirms divergence #2 |
| Q-D | Rule cache serves main instances in a branch; no wrong name found | answer | r2 change 5 |
| Q-E | No supported path saves with `using` other than the router's alias | answer | trigger check stays |
| Q-F | "Events kept" overstated dispatch | answer | r2 change 6 |

### Round 2 (r2, codex gpt-6-astra high): NOT RATIFIED

| # | Finding | Severity | Disposition |
|---|---|---|---|
| 1a | Constraint 3 still says "before either transaction opens" while r2 allows setup inside the caller's | (with 2) | NOT-CLOSED in r2; r3 change 2 (constraint 3 revised by the operator) |
| 1b / 1 | `docs/examples.md:404-412` documents a direct engine call, so the refutation fails | blocker | ACCEPTED (verified); refutation withdrawn; r3 changes 2, 3 |
| 2 | r2 holds two timeout contracts at once | major | r3 change 2 |
| 3 | Rule CRUD through NetBox's generic views has no timeout owner | major | r3 change 2: NetBox-owned, excluded explicitly |
| 4 | `AsyncAPIJob` runs a branch request on main | major | ACCEPTED (verified NB netbox/api/viewsets/mixins.py:345-351, netbox/jobs.py:292-326); r3 change 4 |
| 5 | A callback timeout after the branch commit cannot roll the family back | major | ACCEPTED; r3 changes 1, 5 (divergence #2 reversed) |
| audit | Missing entry points | answer | r3 change 6 |
| 2, Q-D, Q-F (round 1) | | | CLOSED (reviewer) |

### Round 3 (r3, codex gpt-6-astra high): NOT RATIFIED A, NOT RATIFIED B

Two verdicts: A on the core, B on the timeout and callback mechanism.

| # | Finding | Severity | Disposition |
|---|---|---|---|
| 1 (A) | A replayed parent rename re-runs NetBox's channel cascade at commit and renames a child the plugin kept (verified BR utilities.py:512, models/changes.py:134-140; NB dcim/models/mixins.py:302-337) | blocker | ACCEPTED as a documented limit by the operator; acceptance condition narrowed; r4 change 1 |
| 2 (B) | Apply Rules does not repair a failed reconciliation while the target stays taken (executed: `reconcile_after_parent_cascade` returns at once for an unchanged parent) | major | ACCEPTED; r4 change 2 |
| inventory | Internal commit marker must stay on the write connection | note | r4 change 3 |
| round-2 1, 1a, 2, 3, 4, entry points | | | CLOSED (reviewer, under the revised constraint 3) |
| round-2 5 | | | NOT-CLOSED in r3; r4 change 2 |
| constraint 3 original text | not met for scripts, the engine call inside a caller's transaction, and rule CRUD | | constraint 3 revised by the operator |

### Round 4 (r4, codex gpt-6-astra high): RATIFY A, NOT RATIFIED B

| # | Finding | Severity | Disposition |
|---|---|---|---|
| round-3 1, 2, inventory, round-2 5 | | | CLOSED (reviewer) |
| 1 (B) | A caller that holds only the branch transaction gets the reconciliation before NetBox's cascade, which then moves the kept child (verified: plugin `default` block is outermost and commits first; the cascade waits for the caller's branch commit) | major | ACCEPTED; r5 change 1 (the reviewer's second option: an order that holds for every nesting, no entry state rejected) |
| clarification | Script timeout test wording; divergence #2 wording; a sync replay test | minor | r5 changes 2, 3 |

### Round 5 (r5, codex gpt-6-astra high): RATIFY A, NOT RATIFIED B

| # | Finding | Severity | Disposition |
|---|---|---|---|
| round-4 1, wording, sync test | | | CLOSED (reviewer) |
| 1 (B) | r5's forwarding registers on `default` only after the branch commit, so a `default` rollback (or savepoint rollback) before it does not drop the reconciliation (executed: a queue-only proof with Django's own `on_commit`/`rollback`/`savepoint_rollback` printed `RECONCILED` for both cases) | major | ACCEPTED; r6 change 1, the reviewer's closing change, stated as the general rule |
| Django | Registration from a running callback runs at once on an autocommit connection; a drain aborts on a non-robust exception | note | used in r6 |

### Round 6 (r6, codex gpt-6-astra high): RATIFY A, RATIFY B

Round-5 finding CLOSED. No new finding. Two implementation notes, added to r6 without a design
change: the gate holds the complete pending set before the first registration, and marks itself fired
before it runs the callback. Executed: a queue-level proof with Django 6.1.1's own `on_commit`,
`run_and_clear_commit_hooks`, `rollback` and `savepoint_rollback` and a model of the r6 gate, over
every nesting of rounds 4 and 5, a commit, a rollback and a savepoint rollback of each connection
before and after the other commits, the trigger runner during a callback drain, and main: 77 outcomes
as specified, and the two r5 counterexamples still reproduce under the old rule. Not executed:
final names, ChangeDiff rows, timeouts and connection disposal; the branch CI leg (slice 2) runs them.

First increment: this record and ADR 0016 (slice 1). First executable increment: the branch CI leg
(slice 2): a real branch provisions and deprovisions and its connection closes, the branch leg cannot
skip its branch tests, an unsupported netbox-branching version stops startup, and the existing legs
stay green.
