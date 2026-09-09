---
status: accepted
---

# Report an unsupported topology as a family outcome

The family package owns the probe that decides whether the active NetBox data model can hold a channelized family, and it probes the Interface model rather than comparing versions. A rule that describes a topology the release cannot hold produces a plan whose precondition is `unsupported` and an outcome that says so. Callers map that outcome to their own reporting; they never branch on the NetBox version and never fall back to a different topology. A caller-side version check would let one entry point build a flat family that another refuses, and silently give the operator a topology the rule did not ask for.
