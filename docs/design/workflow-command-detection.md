# Detect pytest commands in workflows

The workflow plugin check reads YAML step `run` values. It uses Tree-sitter's
Bash grammar to distinguish executable commands from argument text, comments,
quoted operators, and heredoc bodies. It does not execute workflow code.

Text matching cannot distinguish `echo pytest` from a pytest invocation.
Shell tokenization alone loses whether an operator token was quoted.
The Bash syntax tree supplies that distinction. `shlex` decodes static words
only after the parser establishes their boundaries.

The detector recognizes literal `pytest` commands, Python module invocations,
and the `poetry run`, `uv run`, and `xvfb-run` prefixes. It inspects command
substitutions separately. GitHub expressions become opaque shell expansions
before parsing. Variable values, ANSI-C quoting, arbitrary wrappers, aliases, `eval`, and
programs embedded in `bash -c` are outside this static check.

Syntax errors raise an error that names the workflow. The check does not fall
back to text matching. Real YAML fixtures cover command lists, quotations,
heredocs, metadata, installations, and invalid shell input. The checked-in
workflow test also verifies that only the two test workflows invoke pytest.

The `workflow-tests` dependency group owns the parser versions. The development
group includes it. Both test workflows and devcontainer setup install it.
These are test dependencies.
