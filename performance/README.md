# Automatic naming performance evidence

The performance runner records the current implementation before an interface-family refactor. It
uses real NetBox models, signals, committed callbacks, and PostgreSQL. It is not part of default test
discovery or CI.

Run against a NetBox that supports channelized interfaces. Release 4.7.0 ships the feature, so a
source checkout of the NetBox `feature` branch is no longer needed. Use the same NetBox revision,
PostgreSQL version, planner settings, sample counts, and hardware for both sides of a comparison.

`PYTHONPATH` decides which plugin checkout is measured. Put the checkout root first, ahead of any
editable install: it supplies both the `netbox_interface_name_rules` package under measurement and
the `performance` package the runner imports. Measure a before and an after side by pointing the
same runner at two checkouts, one per side.

The runner lives on the refactor branch, so a before-run against a revision that predates it needs
the runner copied into that checkout. `git archive <runner-revision> performance
netbox_interface_name_rules/tests/signal_performance.py | tar -x -C <before-checkout>` does that
without changing the plugin source under measurement.

Point NetBox's task queue at an isolated Redis database with no live RQ workers. NetBox chooses
between queued and inline search-cache writes based on worker availability. Mixing those paths
changes the SQL profile between identical saves. The runner checks this condition before it starts.
`TEST_REDIS_DB` moves the queues to their own Redis database, which is what makes the run possible
beside a devcontainer worker that holds the shared one.

The command below records a measured run with the retained configuration of 15 timing samples and
3 warmups per scenario.

```bash
export PYTHONPATH="/path/to/measured/checkout:$PWD/.devcontainer/config"
export TEST_DB_NAME="inr_performance_74"
export TEST_REDIS_DB="9"
export INTERFACE_FAMILY_PERFORMANCE_SOURCE_REVISION="$(git -C /path/to/measured/checkout rev-parse HEAD)"
export NETBOX_PERFORMANCE_SOURCE_REVISION="$(git -C /path/to/netbox rev-parse HEAD)"
export INTERFACE_FAMILY_PERFORMANCE_OUTPUT="$PWD/performance/baselines/existing-feature.json"
export INTERFACE_FAMILY_PERFORMANCE_SAMPLES="15"
export INTERFACE_FAMILY_PERFORMANCE_WARMUPS="3"
export INTERFACE_FAMILY_PERFORMANCE_KIND="existing_implementation"

cd /path/to/netbox/netbox
python manage.py test \
  netbox_interface_name_rules.tests.signal_performance \
  --settings=isolated_test_settings \
  --verbosity=2 \
  --noinput
```

Set `NETBOX_PERFORMANCE_SOURCE_REVISION` to the container image digest when NetBox does not run
from a Git checkout. The runner refuses to start without a value for it.

The temporary JSON artifact contains normalized statements, PostgreSQL plans, rows and loops, buffer
and WAL work, fixture sizes, planner settings, and raw wall and process CPU samples. Do not retain it
in Git. Retain only its generated Markdown summary in `performance/baselines/`. PostgreSQL node
timing stays disabled. The runner observes the statements issued by the operation and does not replay
mutating SQL.

The complete model-save scenarios include NetBox model creation, plugin signal scheduling, and the
committed callback. Direct-callback scenarios isolate the deferred callback for diagnosis. Compare
the complete model-save scenarios first when deciding whether the refactor changed production-path
performance.

## Comparing two runs

`performance/compare.py BEFORE.json AFTER.json OUT.md` writes a readable comparison of database
work and machine time, and breaks down by statement source any scenario whose statement count rose.

Set `INTERFACE_FAMILY_PERFORMANCE_KIND` to name what a run measured, such as `family_package` for
an after run. It labels the artifact and titles its summary.

## Result of the interface-family comparison

`comparisons/family-package-vs-existing.md` compares the pre-refactor baseline with the family
package. Both sides were measured with the same runner, against the same NetBox revision, on the
same host, and each recorded count reproduced twice before it was written.

The refactor moves the committed callback's database work as follows:

| Scenario | Before | After | Change |
| --- | ---: | ---: | ---: |
| `no_matching_rule` | 7 | 7 | 0 |
| `plain_rename` | 41 | 43 | +2 |
| `structural_creation` | 118 | 114 | -4 |
| `existing_family` | 291 | 193 | -98 |
| `reconciliation` | 300 | 302 | +2 |
| `vc.reapply_1` | 46 | 45 | -1 |
| `vc.reapply_8` | 326 | 311 | -15 |

One scenario improves substantially: naming a module into a family that already exists costs 98
fewer statements, a third of the callback's work. Structural creation costs four fewer and
virtual-chassis reapplication is slightly cheaper. Two scenarios cost two statements more.

Count the statements, but read the work. The direct-callback database and process-CPU changes are:

| Scenario | SQL calls | Planner cost | Shared hits | CPU median |
| --- | ---: | ---: | ---: | ---: |
| `plain_rename` | +4.9% | +4.6% | -2.5% | +6.3% |
| `structural_creation` | -3.4% | +9.4% | -2.1% | -0.4% |
| `existing_family` | -33.7% | -52.9% | -33.1% | -36.4% |
| `reconciliation` | +0.7% | -5.1% | -16.4% | +0.0% |
| `vc.reapply_1` | -2.2% | -9.1% | -9.7% | -1.6% |
| `vc.reapply_8` | -4.6% | -5.9% | -5.2% | -10.7% |

No shared-buffer reads were observed in any direct-callback scenario after the refactor, and the
before run recorded none either. Both runs read every page they needed from the buffer cache.

The net change of two statements has four sources: `transaction: SAVEPOINT` (+1),
`transaction: RELEASE` (+1), `dcim_interface` (+1) and `dcim_moduletype` (-1),
which sum to +2. The savepoint pair comes from the transaction the executor opens
per family. On `dcim_interface`, two new reads replace one: a read of the module's
interfaces ordered by interface ID, and the `... WHERE id IN (...) FOR UPDATE`
read that locks and revalidates the planned rows. The read they replace joined
`dcim_device` and ordered by a collated name. The aggregate planner costs above
do not attribute cost to individual statements, so this report does not compare
the planner work of those reads. The module type is now read once instead of
twice. The plugin buys stale-plan revalidation and per-family rollback for two
round trips and 108 bytes of WAL on a rename.

That revalidation is the part to keep. This callback runs after commit, so another actor, including
another plugin, can rename an interface between the moment a family is planned and the moment it is
written. The locked re-read is what detects that.

Read SQL deltas at the `direct_callback` layer. Every scenario's `complete_model_save` SQL delta
equals its direct-callback delta, which says the changed statement count belongs to this plugin.
`no_matching_rule` is the control: the plugin returns before doing anything and issues the same 7
statements either way.

Both runs recorded 15 machine-time samples after 3 warmups on the same hardware. Process CPU
supports the large existing-family improvement: its median falls 36.4%. The smaller changes are
diagnostic, not pass/fail limits. Both runs were taken on an otherwise-idle host: the before run
moved from a 1-minute load of 1.10 to 1.18, and the after run from 1.23 to 0.99. Wall time,
especially p95, stays contextual evidence, and the deterministic SQL profile carries the verdict.
