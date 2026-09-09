---
status: accepted
---

# Separate structural atomicity from partial rename handling

Each family operation owns one database transaction, which isolates it from other families and rolls back unexpected failures. Structural creation and conversion commit or roll back the complete topology together. An installed-family rename uses nested savepoints for expected member-level validation and name collisions: the parent must succeed first, but a blocked channel leaves only that channel unchanged while successful channel renames commit. This preserves useful names without permitting a partial structural topology.
