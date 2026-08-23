---
status: accepted
---

# Revalidate family plans before execution

An interface-family plan is never trusted across a change to its interfaces. Execution revalidates identity, names, membership, and topology against live rows, then rejects a stale plan instead of silently constructing a replacement or applying only the members that still match. Interactive apply constructs a fresh plan from live rows rather than executing the earlier preview. This keeps the executed change equal to the selected plan and prevents concurrent edits from being overwritten.
