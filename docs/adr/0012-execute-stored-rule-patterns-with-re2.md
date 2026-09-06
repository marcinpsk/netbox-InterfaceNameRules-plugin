---
status: accepted
---

# Execute stored rule patterns with RE2

Stored rule patterns are operator input that can execute on the automatic naming signal path, so the plugin compiles and executes them only with RE2 and never falls back to Python `re`. This deliberately rejects lookaround, backreferences, atomic groups and other Python-only syntax in exchange for linear matching time and bounded memory use. An upgrade migration audits existing rows for syntax and Unicode semantic differences before they can run under the new engine. Fixed internal expressions remain on Python `re` because they are not stored rule patterns.
