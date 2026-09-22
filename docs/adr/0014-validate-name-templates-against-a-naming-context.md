---
status: accepted
---

# Validate name templates against a naming context

One module owns the name-template language: brace-group parsing, the variable catalogue, expression safety, evaluation, and every rule check that reads a template. The topology coherence check moves there with it, because `forms.py` imported it privately from `models.py` and that import was the drift risk. The model, the rule tester, the REST serializer and the family package become adapters over one surface. `naming.py` keeps `build_variables`, which reads a module bay and a device to produce the language's inputs, and derives its keys from the catalogue.

The catalogue is per naming context, not one flat set. A module member name, a module parent name and a device interface name each provide a different set. A device rule receives only the virtual-chassis position, the base name and the port, so a device rule that names a bay position could never resolve it. The list view's help panel already documented this in a Rule type column; no code knew it. A flat set with a warning was rejected because it cannot answer whether a variable will resolve, which is the question an operator is asking.

Two variables are conditional, and the catalogue treats them differently because they are knowable at different times. The channel number belongs to the module member context when the rule declares channels, because the channel count is a field on the rule in front of the validator. The virtual-chassis position stays conditional, because it depends on the device the rule meets later. A template that uses it is valid when saved and may fail when evaluated, which is the graceful failure the documentation already describes.

Evaluation does not read the catalogue. It substitutes the variables it receives and evaluates the arithmetic that remains, so the work per rename is unchanged and the automatic naming signal path is not affected. Validation reads the catalogue and runs only on save, clean and form paths.

Stored rules are audited and reported, never rewritten. A migration lists the rules the new checks refuse and leaves them as written. A name template is operator intent, unlike the enum and flag columns that migrations 0015 and 0016 repaired, so no automatic rewrite is correct. This follows the audit that migration 0014 performed for stored rule patterns.

The variable reference comes from the catalogue. The two in-product tables render from it at request time, so they cannot drift. The two documentation tables and the agent instructions carry generated regions. The test suite checks that regeneration is a no-op, because a lint gate reads a diff and misses a file that a rebase carried in.

Three guards keep the seam from splitting again. Ruff bans the private validation helpers by qualified name, and the language module is exempt. A test refuses `ast.parse` with the eval mode outside the language module, which is exact: every other caller in the tree parses a source file in the default mode. The same test refuses an `ast` import in any production module outside it. Both permit lists are empty and must stay empty. Neither guard detects a parser written from regular expressions alone; a shared template corpus driven through every adapter covers that, and it is what proves the adapters agree.
