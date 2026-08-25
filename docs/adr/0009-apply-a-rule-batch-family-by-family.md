---
status: accepted
---

# Apply a rule batch family by family

Retroactive apply and virtual-chassis reapplication plan every family a rule intends on each module and execute each family on its own. A module's installed families claim their members first; what is left over is planned as the family the rule would build there, so no interface belongs to two plans and two bases that intend one family's names build it once. Execution returns one explicit family result per family, and the Apply view and the background job read their counts and their skips from those results instead of a mutable conflict list. A family that is blocked, stale or unnamable costs the batch only itself: the batch keeps planning and executing the families after it.

One batch shares what its modules share. The module rows come with the relations template resolution dereferences, their interfaces are read in one query, and one module type's interface templates are read once however many modules carry it. Every rename therefore goes through the locked, revalidated family executor, which costs a savepoint pair and a row lock per family; that price buys stale-plan rejection and per-family isolation on the paths that previously had neither.
