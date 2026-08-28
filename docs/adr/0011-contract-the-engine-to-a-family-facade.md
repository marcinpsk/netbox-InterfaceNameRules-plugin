---
status: accepted
---

# Contract the engine to a family facade

The engine no longer holds a family implementation. Automatic installation, prediction, interactive preview and apply, bulk operations, virtual-chassis reapplication, device-level renaming, conversion and deferred reconciliation all reach the family package, and every rename, creation and rewrite goes through one locked, revalidating executor. This completes the direction set in ADR 0006 and the remaining step named in ADR 0009.

What stays in the engine is what was never about families: which rule wins, how a rule's variables are built, and the raw-name idempotency guard that decides which interfaces an automatic path may touch on this run. The guard runs while it can still see every interface a rule claimed, before two of them that intend one family are collapsed into it, because an ambiguous pair is only visible while both are present. The device-level path keeps its own rule selection and its claim bookkeeping for the same reason, and asks the package for lockstep names directly: a device rule never builds a family, so its channel count says nothing about one it finds.

Module installation now reads a module's interfaces once and plans the whole module, rather than planning installed families, executing them, and then re-reading what was left. Device-level renaming gained revalidation and row locking it never had.

The private helpers the engine used are deleted rather than wrapped, so no second implementation can drift from the package. The family package does not import the engine facade, and the shared dependencies point only at rule selection and naming.
