# Isolate concurrent test runs

## 0. Review record

The adversarial review refuted changes to Django settings, worker counts, and
direct CI. Those claims reopen if CI starts sharing a Redis service. See
[Ratification](#ratification).

## Brief

The `netbox-test` runner owns test-run isolation. Its interface is one command
that accepts pytest arguments and permits safe use from multiple shells in the
shared devcontainer.

One run can start six xdist workers. Each worker needs one PostgreSQL database
and two Redis databases. The shared Redis server has 16 databases. The live
NetBox worker and cache reserve databases 0 and 1. One full test run therefore
uses 12 of the 14 remaining databases. A second full run cannot receive a
disjoint allocation from the same Redis server.

The devcontainer has `flock`. It does not have `redis-server`, a Docker client,
or a Docker socket. The CI runners have isolated Redis services and invoke
pytest directly with explicit target variables.

The observable acceptance conditions are:

1. Two `netbox-test` invocations cannot use the shared PostgreSQL and Redis
   targets at the same time.
2. The waiting invocation keeps its full pytest argument list.
3. The runner releases its resource ownership after pytest succeeds or fails.
4. Direct CI pytest invocations keep their existing worker isolation.
5. The full six-worker pool and reusable test databases remain available.

The mechanical guard is a runner test that starts two real shell invocations
against a controlled pytest executable and proves that the second invocation
does not enter pytest until the first exits.

## Blind design record

The primary design used the root Codex session. The model and reasoning effort
were not exposed to the session. The blind design used `gpt-6-astra` with high
reasoning effort. It received only the brief, acceptance conditions, and source
file pointers. It was explicitly forbidden from reading this record.

Both designs completed before comparison.

## Divergence table

| Decision | Primary choice | Blind choice | Evidence | Disposition | Consequence |
| --- | --- | --- | --- | --- | --- |
| Run coordination | One container-wide exclusive lease | One container-wide `flock` | One full worker pool consumes 12 of 14 usable Redis databases. | Use `flock`. | Concurrent callers wait instead of sharing targets. |
| Lock scope | One fixed lock | One fixed lock across all target overrides | Partitioning by only one target can leave the other target shared. | Use one fixed path. | The guarantee is simple and conservative. |
| Shell scope | Keep the existing function execution scope | Move the function into a subshell | The finding does not concern the caller's directory or environment. The no-drive-by rule requires existing behavior to remain. | Keep the existing scope. | Only test-run admission changes. |
| Failure test | Prove exclusion | Also prove exit-status propagation and release after failure | Automatic lock release is part of the acceptance conditions. | Combine both in one bounded concurrent test. | One test covers wait, failure status, release, and the next admission. |
| Documentation | Record direct CI behavior | Also update runner help | Direct pytest is intentionally outside the runner seam. | Update the runner comment and this record. | The command interface stays unchanged. |

## Design r2

The runner is the seam because it is the only module that knows the lifetime of
one complete pytest invocation. It runs pytest through `flock` with one fixed
container-wide lock path. The operating system releases the lock when pytest
exits, including a nonzero exit. PostgreSQL and Redis worker mapping stay inside
the existing settings implementation.

This makes the runner a deep module: callers supply only pytest arguments, and
the runner owns shared-resource serialization, default targets, environment
activation, and process lifetime. Deleting the runner would spread those rules
back into every shell and automation caller.

### Candidate shapes

| Shape | Seam | Result |
| --- | --- | --- |
| Exclusive runner lease | `netbox-test` command | Preserves the six-worker pool and reusable databases. Concurrent callers wait. |
| Per-run Redis namespace | Django settings and a custom RQ adapter | Allows overlap, but requires coordinated cache and RQ key namespaces. It adds an adapter used only by tests. |
| Per-run Redis server | `netbox-test` command | Allows overlap, but the devcontainer cannot create a Redis process or container. |

The exclusive runner lease is the coherent candidate. The Redis namespace is a
new adapter with a larger interface and incomplete locality because NetBox,
Django cache, django-rq, and RQ each own keys. The per-run server has no local
implementation in the current devcontainer.

Lock acquisition, directory changes, environment activation, and executable
lookup fail before pytest can start. `flock` returns pytest's status. The lock
file remains in place because deleting it can let waiters and new callers lock
different inodes. Direct pytest invocations do not cross this seam and retain
their existing behavior.

### First increment

Add one exclusive runner lease around pytest. Extend the runner shell test with
two bounded concurrent invocations. The first invocation enters pytest, waits,
and exits with a nonzero status. The test proves the second invocation cannot
enter early, then proves it starts after the first exits and receives all of its
arguments. Keep the existing database and Redis worker tests unchanged.

## Ratification

An adversarial `gpt-6-astra` review at high reasoning effort executed the Redis
allocation and `flock` lifetime proofs. It closed the lock scope, shell scope,
failure behavior, argument preservation, and documentation dispositions. It
refuted changes to Django settings, worker counts, and direct CI because the
runner lease resolves the shared-host defect without changing those modules.
Those claims reopen if CI starts sharing a Redis service.

Verdict: **RATIFY design r2, scope: container-wide `netbox-test` admission and
its shell regression.**
