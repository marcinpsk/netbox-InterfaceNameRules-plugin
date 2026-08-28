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
package on the same NetBox revision and the same host.

Database work carries the verdict, because it is deterministic. The plugin's own committed callback
issues fewer or the same statements in every scenario: unchanged where no rule matches, and down by
5% to 36% everywhere else, with the largest reductions on virtual-chassis reapplication (-29% for
one module, -36% for eight).

Two complete-model-save scenarios show more statements than the baseline. Most of the increase is
NetBox's own per-save bookkeeping for the object types and custom fields the test database holds,
which grew between the two runs: `core_objecttype`, `extras_customfield` and `extras_cachedvalue`
carry the whole net increase of 11 in `structural_creation`, where every other table nets to zero,
and 11 of the 17 in `no_matching_rule`. The remaining 6 there are 2 `RELEASE` and 2 `SAVEPOINT`
statements beside the new cached-value writes, and one extra read each of `dcim_interface` and
`dcim_module`.

The control is `module.complete_model_save.no_matching_rule`, where this plugin returns before doing
anything and its callback issues the same 7 statements in both runs, yet the surrounding save costs
17 more. On `structural_creation` the plugin's own callback got cheaper, from 119 statements to 94.
Neither increase is work this refactor added.

Machine time in that run is not evidence. The host was running unrelated workloads at a load average
between 17 and 44, and the same no-op scenario shows +72% wall time against identical SQL. The
virtual-chassis scenarios were still 32% to 35% faster despite the contention, which is consistent
with their statement reductions. Rerun both artifacts on an otherwise-idle host before quoting any
timing figure as evidence.
