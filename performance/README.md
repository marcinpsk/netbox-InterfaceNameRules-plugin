# Automatic naming performance evidence

The performance runner records the current implementation before an interface-family refactor. It
uses real NetBox models, signals, committed callbacks, and PostgreSQL. It is not part of default test
discovery or CI.

Start in this plugin checkout, and use a fixed NetBox `feature` source checkout that supports
channelized interfaces. Use the same NetBox revision, PostgreSQL version, planner settings, sample
counts, and hardware for the after-run. NetBox `main` does not contain the channelization feature
while its development takes place on `feature`.

Point NetBox's task queue at an isolated Redis database with no live RQ workers. NetBox chooses
between queued and inline search-cache writes based on worker availability. Mixing those paths
changes the SQL profile between identical saves. The runner checks this condition before it starts.

The command below records a measured run with the retained configuration of 15 timing samples and
3 warmups per scenario.

```bash
export PYTHONPATH="$PWD/.devcontainer/config"
export TEST_DB_NAME="inr_performance_74"
export INTERFACE_FAMILY_PERFORMANCE_SOURCE_REVISION="$(git rev-parse HEAD)"
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
| `plain_rename` | 36 | 38 | +2 |
| `structural_creation` | 113 | 113 | 0 |
| `existing_family` | 254 | 168 | -86 |
| `reconciliation` | 255 | 257 | +2 |
| `vc.reapply_1` | 37 | 36 | -1 |
| `vc.reapply_8` | 254 | 239 | -15 |

One scenario improves substantially: naming a module into a family that already exists costs 86
fewer statements, a third of the callback's work. Virtual-chassis reapplication is slightly cheaper.
Two scenarios cost two statements more.

Count the statements, but read the work. The direct-callback database and process-CPU changes are:

| Scenario | SQL calls | Planner cost | Shared hits | CPU median |
| --- | ---: | ---: | ---: | ---: |
| `plain_rename` | +5.6% | +3.1% | -46.0% | -9.1% |
| `structural_creation` | 0.0% | -23.3% | -29.6% | -3.1% |
| `existing_family` | -33.9% | -59.9% | -41.2% | -34.2% |
| `reconciliation` | +0.8% | +2.4% | +35.8% | +3.1% |
| `vc.reapply_1` | -2.7% | -6.6% | +13.5% | +6.7% |
| `vc.reapply_8` | -5.9% | -18.6% | -7.2% | -4.0% |

No shared-buffer reads were observed in any direct-callback scenario after the refactor. The before
run recorded one shared read, in `structural_creation`.

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
supports the large existing-family improvement: its median falls 34.2%. The smaller changes are
diagnostic, not pass/fail limits. Host load was not comparable: the before run moved from 25.02 to
9.59, and the after run moved from 9.24 to 16.28. Treat wall time, especially p95, as contextual
evidence and use the deterministic SQL profile as the verdict.
