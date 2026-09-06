---
status: accepted
---

# Apply a rule batch family by family

Retroactive apply plans every family a rule intends on each module and executes each family on its own. A module's installed families claim their members first; what is left over is planned as the family the rule would build there, so no interface belongs to two plans and two bases that intend one family's names build it once. Execution returns one explicit family result per family, and the Apply view and the background job read their counts and their skips from those results instead of a mutable conflict list. A family that is blocked, stale or unnamable costs the batch only itself: the batch keeps planning and executing the families after it.

One batch shares what its modules share. The module rows come with the relations template resolution dereferences, and one module type's interface templates are read once however many modules carry it. Retroactive apply also reads every module's interfaces in one query. Virtual-chassis reapplication is a batch of the same kind, but it reaches the families through the installation entry point, so it still reads one module's interfaces at a time and still renames a leftover interface outside the family executor. Contracting that entry point onto the same batch was the remaining step; ADR 0011 completes it.

Every rename retroactive apply performs goes through the locked, revalidated family executor, which costs a savepoint pair and a row lock per family. That price buys stale-plan rejection and per-family isolation on a path that had neither.

A module's plain interface count decides whether an earlier flat breakout already expanded it, because converting one sibling into a family parent would strand the others. A channel belongs to the parent that declares it, so it never counts toward that surplus: counting one would stop a module's second port from gaining a family of its own.
