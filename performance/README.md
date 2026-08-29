# Automatic naming performance evidence

The performance runner records the current implementation before an interface-family refactor. It
uses real NetBox models, signals, committed callbacks, and PostgreSQL. It is not part of default test
discovery or CI.

Run it from a fixed NetBox `feature` source checkout that supports channelized interfaces. Use the
same NetBox revision, PostgreSQL version, planner settings, sample counts, and hardware for the
after run. NetBox `main` does not contain the channelization feature while its development takes
place on `feature`.

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
work and machine time, and breaks down by table any scenario whose statement count rose.

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

Those two increases are this refactor's, not the environment's. The per-table breakdown gives the
same shape for each: one more `SAVEPOINT` and `RELEASE`, one more `dcim_interface` read, and one
fewer `dcim_moduletype` read. That is the locked family executor: it opens a savepoint per family
and re-reads the rows it locked before it writes them. The plugin buys stale-plan revalidation and
per-family rollback for two statements on a rename.

Read the deltas at the `direct_callback` layer. Every scenario's `complete_model_save` delta equals
its `direct_callback` delta exactly, which says NetBox's own per-save bookkeeping costs the same on
both sides and every difference above belongs to this plugin. `no_matching_rule` is the control: the
plugin returns before doing anything and issues the same 7 statements either way.

Machine time is not measured in these artifacts. Both runs used
`INTERFACE_FAMILY_PERFORMANCE_SAMPLES=0`, so every timing column reads `not measured` rather than
reporting a number taken under an unknown load. This host carries unrelated work at load 8 to 33.
Rerun both sides with samples on an otherwise-idle host to fill those columns in; each artifact
records the load span it ran under, so a reader can check rather than trust the prose.
