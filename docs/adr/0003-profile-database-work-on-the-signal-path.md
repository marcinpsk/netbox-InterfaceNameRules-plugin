---
status: accepted
---

# Profile database work on the signal path

The test suite records a PostgreSQL work profile for each representative automatic naming scenario before the interface-family refactor begins. The profile comes from the real Django signal path and records statement counts, normalized SQL, execution plans, actual rows and loops, buffer access, temporary storage, WAL activity, and scaling behavior. PostgreSQL collects the execution data with node timing disabled. The test suite also runs a separate uninstrumented timing pass and records raw samples, wall time, and process CPU time. Run both passes before the refactor and rerun them afterward on the same hardware with the same PostgreSQL version, NetBox revision, fixtures, planner settings, and statistics. Retain the result as review evidence. CI does not use the machine-time comparison as a recurring regression gate.
