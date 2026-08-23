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
