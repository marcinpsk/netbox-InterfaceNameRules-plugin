---
status: accepted
---

# Reapply from a rename trigger with its previous state

A rename trigger compares the previous state with the saved row, and reapplies the rules after commit. Three decisions shape it.

A save fails when its previous state cannot be read. The receivers used to catch the error and store no previous state, and the module path then read that as "no change" and dropped the reapply; the device path read it as a change. NetBox runs every edit view, bulk view and REST write inside a transaction, and PostgreSQL refuses every later statement in a transaction once one fails, so the save failed anyway and the catch only hid the cause. Reading an unknown state as "changed" was rejected because it still hides the error and does work nobody asked for.

A reapply recognises the interfaces the plugin renamed earlier by rebuilding their earlier names from the previous state: the old module bay, the old device and the old virtual-chassis position. NetBox 4.7 renames a moved module's interfaces only while they carry their raw template name, so after a move or a bay-position edit an interface the plugin renamed carries a name built from a position that no longer exists. Matching any bay position, as the `{vc_position}` claim does, was rejected because a wider match makes more rows ambiguous, and an ambiguous row is left alone. Not recognising them was rejected because nothing else can: Apply Rules has no previous state, so a missed move leaves the name wrong until an operator fixes it by hand. Names left wrong by moves made before this change stay wrong for the same reason, and the documentation says so.

A reapply that fails, or that leaves an interface unrenamed although a rule matched it, writes one journal entry on the module or device, as well as the log line. The save has committed by then and cannot be undone, and raising would show an error page for a save that succeeded. A log line alone was rejected because operators do not read the server log. A background job was rejected for now because it makes the rename no longer immediate.
