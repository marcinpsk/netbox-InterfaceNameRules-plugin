---
status: accepted
---

# Execute each family in its own transaction

Each installed family executes in its own database transaction. Execution locks member rows in stable primary-key order and revalidates the plan after locking. Structural changes roll back the complete family. Existing-family renames use child savepoints after the parent succeeds, so one blocked child does not undo unrelated member renames. A blocked family does not roll back other families in the plan set. An unrelated integrity or infrastructure failure rolls back its own family and propagates to the operation boundary. Target-name checks run after locking, and NetBox's device-and-name uniqueness constraint remains the final collision guard without a device-wide interface lock.
