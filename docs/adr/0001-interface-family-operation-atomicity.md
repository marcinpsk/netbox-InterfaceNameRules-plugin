---
status: accepted
---

# Preserve interface-family operation atomicity

Structural family changes are atomic. A family rename proceeds only after its parent succeeds, but a collision or error on one channel leaves only that channel unchanged. This preserves useful renames and permits later repair without allowing a partial topology. A fully atomic family rename would discard unrelated successful channel renames, while best-effort structural changes could leave an invalid topology.
