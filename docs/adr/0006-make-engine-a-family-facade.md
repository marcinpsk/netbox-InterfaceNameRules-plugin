---
status: accepted
---

# Make the engine a family facade

The existing engine entry points remain as compatibility adapters. Shared rule selection, variable construction, and template evaluation move into lower-level modules. A dedicated family package owns immutable domain values, installed and prospective adapters, planning, revalidation, locking, execution, conversion, and deferred reconciliation. The family package never imports the engine facade. This dependency direction prevents circular imports and removes the old private family implementation when the replacement is complete.
