<!--
SPDX-License-Identifier: Apache-2.0
Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
-->

# Interface Name Rules

This context describes naming rules and the related interface topologies they manage.

## Language

**Interface family**:
A set of related interfaces that represents one physical port and its breakout channels in either a flat or channelized topology.
_Avoid_: Channel group, breakout set

**Flat breakout family**:
An interface family whose channels are sibling interfaces without a physical parent relationship.
_Avoid_: Flat channels

**Channelized family**:
An interface family with one physical parent that declares channel capacity and zero or more channel interfaces bound to it. The family remains channelized when it is incomplete.
_Avoid_: Parented breakout

**Installed interface family**:
An interface family represented by the current NetBox interface rows.
_Avoid_: Persisted family

**Prospective interface family**:
An intended interface family described before its interface rows exist.
_Avoid_: Predicted family

**Family plan**:
An immutable description of one intended interface-family operation, its required live state, and its expected member outcomes.
_Avoid_: Rename instructions

**Installed family plan**:
A family plan that includes a validated snapshot of installed NetBox rows and can be executed after live-state revalidation.
_Avoid_: Executable prediction

**Family plan set**:
An immutable batch that contains exactly one family plan for each interface family in an operation.
_Avoid_: Plan list

**Family rename**:
A change to the names of existing interface-family members that does not change their topology.
_Avoid_: Family conversion

**Structural family change**:
A change that creates an interface family or changes it between flat and channelized topologies.
_Avoid_: Family rename

**Flat-to-channelized conversion**:
A structural family change that rebuilds one installed flat breakout family as a channelized family, keeping the physical interface row and moving its logical identity onto the channel that takes its name. It is always an explicit operator action, never a side effect of applying a rule.
_Avoid_: Migration, upgrade

**Out-of-band rename**:
A change to an interface name made by an actor other than this plugin, such as an operator edit or an import. A family plan is stale when one arrives between planning and execution.
_Avoid_: External rename, manual fix

**Engine facade**:
The compatibility surface downstream callers import. It selects rules, builds template variables and decides which interfaces an automatic path may touch on a run; it holds no family discovery, planning or mutation.
_Avoid_: Engine layer, core

**Stored rule pattern**:
An operator-provided RE2 expression saved on an Interface Name Rule. It matches the complete module type model or current device-interface name, depending on the rule mode.
_Avoid_: Python regex, partial regex

**Unsupported topology**:
An interface-family topology that the active NetBox data model cannot represent.
_Avoid_: Legacy fallback

**Blocked family operation**:
A valid requested family change that current device state prevents, such as when a required name is already occupied.
_Avoid_: Failed family operation

**Automatic naming signal path**:
The complete path from a NetBox model save, through the committed callback, to the resulting interface-family rows.
_Avoid_: Signal handler performance

**Signal-path performance baseline**:
Test-suite measurements of the automatic naming signal path before an implementation change. The baseline includes query counts, PostgreSQL work profiles, scaling behavior, and repeated machine-time samples collected on the hardware used for the after measurement. Shared-runner elapsed time is not a recurring CI metric.
_Avoid_: Runtime limit, CI speed

**PostgreSQL work profile**:
A test-suite record of the database work performed by one automatic naming scenario. It includes normalized statements, execution plans, rows, loops, buffer access, temporary storage, and WAL activity.
_Avoid_: Standalone query benchmark

**Implementation performance comparison**:
A one-time comparison produced by running the same test-suite performance scenarios before and after an implementation change on the same hardware. It includes database work, uninstrumented wall time, process CPU time, and raw timing samples. It is review evidence, not a recurring CI gate.
_Avoid_: Performance CI
