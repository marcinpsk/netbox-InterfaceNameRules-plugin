---
status: accepted
---

# Use immutable family-plan boundaries

The interface-family module exposes immutable installed and prospective plan sets. Each set contains exactly one plan per family. The installed adapter owns bulk ORM loading, family discovery, and row snapshots. The prospective adapter owns rowless template inputs. Callers provide semantic operation roots instead of querysets or discovered members. Only installed plans can execute, and execution returns explicit family and member outcomes. Existing engine entry points remain thin adapters to this boundary.
