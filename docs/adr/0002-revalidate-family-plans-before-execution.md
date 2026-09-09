---
status: accepted
---

# Revalidate family plans before execution

An interface-family plan is never trusted across a change to its interfaces. Execution revalidates identity, names, membership, and topology against live rows, then rejects a stale plan instead of silently constructing a replacement or applying only the members that still match. A preview is advisory. Interactive apply confirms only the interfaces the operator selected, plans those again from live rows, and reports each family as changed or skipped. It never executes the plan the preview rendered, so a concurrent edit is refused rather than overwritten.
