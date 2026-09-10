---
status: accepted
---

# Centralize template claim ambiguity

The engine and installed-family discovery each implemented the same two-sided uniqueness rule.
Exhaustive checks over 4096 relations established their equivalence.
The family package owns this rule through immutable `TemplateClaim` values and
`resolve_template_claims`, exported at the package boundary.

The primitive accepts the complete relation for one selection scope.
Callers must retain claims from templates that claim multiple labels.
Repeated edges count once, and duplicate claimant IDs are invalid.
A template with multiple labels or a label with multiple templates disqualifies
every involved claim. Accepted pairs retain input claimant order.
The primitive returns accepted pairs and rendered collision messages.
It uses only the standard library and does not discover candidates.

The engine retains regex matching, comparison forms, exact-name precedence,
forced-base ordering and the preference for channel `:0`.
Its admission guard stays at the same point in `_collect_unrenamed`, before
interfaces that intend one family collapse, as required by ADR 0011.

Message construction belongs to the primitive so callers cannot drift in wording.
The primitive does not log. Callers emit its messages through their own logger,
which preserves the engine warning source and keeps the decision free of side effects.

Increment 1 adopts the primitive only in the engine.
Installed-family adoption belongs to increment 2.
The `_singly_claimed` rule counts member primary keys across complete family
candidates and remains separate.
