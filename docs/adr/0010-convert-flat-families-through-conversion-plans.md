---
status: accepted
---

# Convert flat families through conversion plans

Assisted flat to channelized conversion plans and executes one immutable conversion plan per family, in the family package, beside the planners that install and rename families. A plan carries the row snapshots the ch-0 split needs and nothing else: the base row that becomes the parent, the siblings that become channels 2..N, and the parent name the rule resolves. Execution locks the planned rows, compares them against those snapshots, and refuses a family whose identity, names, membership or topology moved since the scan. The scan itself performs each conversion inside a savepoint it always rolls back, so a candidate reports the reason NetBox would refuse the family rather than a guess at its rules.

Conversion recovers its families through the base names the rename path recovers, so the two cannot drift on which rows belong to which family, a family named before a virtual-chassis renumber included. It identifies a family by its ch-0 row, the way the flat apply that installed it named that row, and then takes whatever the module still carries for the rest of the family. A family with a gap is offered and refused, naming the row it is missing, because dropping it from the page would read as "nothing here to convert" and hide an edit the operator needs to see. A sibling that already belongs to another parent is refused the same way rather than taken from that family. The parent takes the name the rule resolves for the module now, which is the name an apply would give it; the channels keep the names they carry, because converting retypes those rows in place.

A family the plan can already refuse carries the refusal as a plan precondition, so the scan reports it without a dry run and execution rejects it without locking a row. Everything a snapshot cannot settle stays in the locked preflight beside NetBox's own validation.

Conversion returns one explicit family outcome per family. The Apply view and the background job read their converted and skipped counts from those outcomes, so no conversion tuple and no mutable conflict list crosses the boundary. The candidate an operator confirms carries its names, roles and blocking reason from the plan; it keeps the live module and ch-0 rows only so the page can link to them.

The addresses and FHRP group assignments on the ch-0 row move onto the new channel through model saves, one row at a time. A queryset update relocated them with no validation, no signal and no changelog entry, so a device's history showed the family rewritten and the objects on it moved by nobody. They are objects an operator owns and audits, and they are now written to the standard the rest of the family write already meets.
