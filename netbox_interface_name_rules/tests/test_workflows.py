# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Every workflow that runs pytest must install the plugins `addopts` makes mandatory.

pytest fails during argument parsing when `addopts` names an option no installed plugin registers,
so a missing distribution breaks the job before a single test runs.
"""

import pathlib
import re
import shlex
import tempfile
import tomllib

import tree_sitter_bash
import yaml
from django.test import SimpleTestCase
from tree_sitter import Language, Parser

_PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[2]
_WORKFLOWS = _PROJECT_ROOT / ".github" / "workflows"
_SHELL_LANGUAGE = Language(tree_sitter_bash.language())

# The option each pytest plugin registers. `--no-cov` needs pytest-cov for the same reason `--cov`
# does: an unregistered option is a parse error, whether it turns coverage on or off.
_OPTION_OWNERS = {
    "--cov": "pytest-cov",
    "--no-cov": "pytest-cov",
    "-n": "pytest-xdist",
    "--dist": "pytest-xdist",
}


def _configured_addopts():
    """Return the `addopts` string pytest applies to every invocation in this repository."""
    with (_PROJECT_ROOT / "pyproject.toml").open("rb") as handle:
        return tomllib.load(handle)["tool"]["pytest"]["ini_options"]["addopts"]


def _required_distributions(*command_lines):
    """Return the pytest plugins the options in *command_lines* require."""
    text = " ".join(command_lines)
    # An option ends at whitespace, at `=`, or at end of input; `--cov-report` is not `--cov`.
    return {
        owner for option, owner in _OPTION_OWNERS.items() if re.search(rf"(?<!\S){re.escape(option)}(?=[\s=]|$)", text)
    }


def _literal_shell_word(node):
    """Decode a static shell word after the parser establishes its boundaries."""
    if node.type == "command_name":
        return _literal_shell_word(node.named_children[0])
    if node.type == "concatenation":
        parts = [_literal_shell_word(part) for part in node.named_children]
        return None if None in parts else "".join(parts)
    text = node.text.decode("utf-8")
    if node.type == "raw_string":
        return text[1:-1]
    if node.type == "string":
        if any(part.type != "string_content" for part in node.named_children):
            return None
    elif node.type != "word":
        return None
    return shlex.split(text.replace("\\\n", ""))[0]


def _is_pytest_invocation(words):
    """Recognize literal pytest commands and the supported runner prefixes."""
    if not words or words[0] is None:
        return False
    name = pathlib.PurePosixPath(words[0]).name
    if name == "pytest":
        return True
    if name in {"python", "python3"}:
        return words[1:3] == ["-m", "pytest"]
    if name in {"poetry", "uv"} and words[1:2] == ["run"]:
        return _is_pytest_invocation(words[2:])
    if name == "xvfb-run":
        return _is_pytest_invocation(words[1:])
    return False


def _shell_runs_pytest(command, workflow):
    """Inspect executable command nodes without interpreting arguments as shell programs."""
    command = re.sub(r"\$\{\{.*?\}\}", "${WORKFLOW_EXPRESSION}", command, flags=re.DOTALL)
    source = command.encode("utf-8")
    root = Parser(_SHELL_LANGUAGE).parse(source).root_node
    if root.has_error:
        raise ValueError(f"{workflow}: cannot parse workflow shell command")
    pending = [root]
    while pending:
        node = pending.pop()
        if node.type == "command":
            parts = [node.child_by_field_name("name"), *node.children_by_field_name("argument")]
            words = []
            previous_end = None
            for part in parts:
                word = _literal_shell_word(part)
                gap = source[previous_end : part.start_byte] if previous_end is not None else b""
                # The grammar can split one shell word at an escaped newline.
                if words and gap and not gap.replace(b"\\\n", b""):
                    words[-1] = words[-1] + word if words[-1] is not None and word is not None else None
                else:
                    words.append(word)
                previous_end = part.end_byte
            if _is_pytest_invocation(words):
                return True
        pending.extend(node.named_children)
    return False


def _workflows_running_pytest(directory=None):
    """Map each workflow in *directory* that invokes pytest to its full text."""
    found = {}
    for path in sorted((directory or _WORKFLOWS).glob("*.y*ml")):
        text = path.read_text(encoding="utf-8")
        workflow = yaml.safe_load(text)
        commands = [step["run"] for job in workflow["jobs"].values() for step in job.get("steps", []) if "run" in step]
        pytest_invocations = [_shell_runs_pytest(command, path.name) for command in commands]
        if any(pytest_invocations):
            found[path.name] = text
    return found


class RequiredDistributionTest(SimpleTestCase):
    """The option scan reads every spelling pytest accepts, including `--option=value`."""

    def test_a_long_option_with_a_value_is_recognised(self):
        """`addopts` uses `--cov=package`, so missing this form would disarm the whole check."""
        self.assertEqual(_required_distributions("--cov=netbox_interface_name_rules"), {"pytest-cov"})
        self.assertEqual(_required_distributions("--dist=loadscope"), {"pytest-xdist"})

    def test_a_separated_value_is_recognised(self):
        self.assertEqual(_required_distributions("--dist loadscope"), {"pytest-xdist"})
        self.assertEqual(_required_distributions("-n auto"), {"pytest-xdist"})

    def test_a_longer_option_that_merely_starts_the_same_is_not_a_match(self):
        """`--cov-report` alone registers nothing; only `--cov` and `--no-cov` do."""
        self.assertEqual(_required_distributions("--cov-report=term-missing"), set())

    def test_the_configured_addopts_require_both_plugins(self):
        self.assertEqual(_required_distributions(_configured_addopts()), {"pytest-cov", "pytest-xdist"})


class WorkflowPytestPluginTest(SimpleTestCase):
    """A workflow that runs pytest installs every plugin the run needs."""

    def test_devcontainer_installs_the_workflow_test_dependency_group(self):
        setup = (_PROJECT_ROOT / ".devcontainer" / "scripts" / "setup.sh").read_bytes()
        pending = [Parser(_SHELL_LANGUAGE).parse(setup).root_node]
        installation_arguments = []
        while pending:
            node = pending.pop()
            if node.type == "command" and node.child_by_field_name("name").text == b"$PIP_CMD":
                arguments = [_literal_shell_word(part) for part in node.children_by_field_name("argument")]
                if arguments[:1] == ["install"]:
                    installation_arguments.append(arguments)
            pending.extend(node.named_children)
        self.assertTrue(
            any(
                arguments[index : index + 2] == ["--group", "workflow-tests"]
                for arguments in installation_arguments
                for index in range(len(arguments) - 1)
            ),
            "devcontainer setup does not install the shared workflow test dependencies",
        )

    def test_every_pytest_workflow_installs_the_plugins_addopts_requires(self):
        workflows = _workflows_running_pytest()

        self.assertTrue(workflows, "no workflow was detected as running pytest")
        for name, text in workflows.items():
            pytest_lines = re.findall(r"^.*\bpytest\b.*$", text, re.MULTILINE)
            required = _required_distributions(_configured_addopts(), *pytest_lines)
            for distribution in sorted(required):
                with self.subTest(workflow=name, distribution=distribution):
                    self.assertRegex(
                        text,
                        rf"install[^\n]*\b{re.escape(distribution)}\b",
                        f"{name} runs pytest but never installs {distribution}",
                    )


class WorkflowDetectionTest(SimpleTestCase):
    def test_shell_syntax_distinguishes_commands_from_argument_text(self):
        commands = {
            "echo": ("echo pytest", False),
            "printf": ("printf pytest", False),
            "export": ("export LABEL=pytest", False),
            "assignment": ("LABEL=pytest", False),
            "python_code": ("python -c 'pytest'", False),
            "quoted_separator": ('echo ";" pytest', False),
            "escaped_separator": (r"echo \; pytest", False),
            "quoted_command": ("printf '%s' 'text; pytest'", False),
            "comment": ("echo ready # ; pytest", False),
            "multiline_argument": ('echo "ready\npytest tests"', False),
            "heredoc": ("cat <<'EOF'\npytest tests\nEOF\n", False),
            "heredoc_then_command": ("cat <<'EOF'\npytest tests\nEOF\npytest tests\n", True),
            "quoted_executable": ('"pytest" tests', True),
            "concatenated_executable": ('"py"test tests', True),
            "prefixed_assignment": ("LABEL=value pytest tests", True),
            "continuation": ("python -m \\\npytest tests", True),
            "executable_continuation": ("py\\\ntest tests", True),
            "quoted_continuation": ('"py\\\ntest" tests', True),
            "literal_continuation": ("'py\\\ntest' tests", False),
            "substitution": ("echo $(pytest tests)", True),
            "python_version": ("python3 -m pytest tests", True),
            "uv_runner": ("uv run pytest tests", True),
            "runner_module": ("uv run python -m pytest tests", True),
            "expanded_argument": ('pytest "$SELECTION"', True),
            "dynamic_executable": ("${{ inputs.runner }} tests", False),
        }
        with tempfile.TemporaryDirectory() as directory:
            workflows = pathlib.Path(directory)
            for name, (command, _) in commands.items():
                workflow = {"jobs": {"test": {"steps": [{"run": command}]}}}
                (workflows / f"{name}.yml").write_text(yaml.safe_dump(workflow), encoding="utf-8")
            found = _workflows_running_pytest(workflows)
        for name, (command, expected) in commands.items():
            with self.subTest(command=command):
                self.assertEqual(f"{name}.yml" in found, expected)

    def test_invalid_shell_reports_the_workflow(self):
        with tempfile.TemporaryDirectory() as directory:
            workflows = pathlib.Path(directory)
            for command in ("echo 'unterminated", "pytest tests\necho 'unterminated"):
                with self.subTest(command=command):
                    workflow = {"jobs": {"test": {"steps": [{"run": command}]}}}
                    (workflows / "invalid.yml").write_text(yaml.safe_dump(workflow), encoding="utf-8")
                    with self.assertRaisesRegex(ValueError, "invalid.yml"):
                        _workflows_running_pytest(workflows)

    def test_a_pytest_step_does_not_hide_invalid_shell_in_a_later_step(self):
        with tempfile.TemporaryDirectory() as directory:
            workflows = pathlib.Path(directory)
            workflow = {"jobs": {"test": {"steps": [{"run": "pytest tests"}, {"run": "echo 'unterminated"}]}}}
            (workflows / "invalid.yml").write_text(yaml.safe_dump(workflow), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "invalid.yml"):
                _workflows_running_pytest(workflows)

    def test_only_the_test_workflows_invoke_pytest(self):
        self.assertEqual(set(_workflows_running_pytest()), {"test.yaml", "test-netbox-main.yaml"})

    def test_command_prefixes_are_detected_and_installations_are_excluded(self):
        commands = {
            "python": "python -m pytest tests",
            "poetry": "poetry run pytest tests",
            "xvfb": "xvfb-run pytest tests",
            "plain": "pytest tests",
            "selection": "pytest -k install",
            "chained": "pip install -e . && pytest",
            "or_chain": "pip install -e . || pytest",
            "semicolon_chain": "pip install -e .; pytest",
            "pipeline": "pip install -e . | pytest",
            "pip": "pip install pytest-cov",
            "uv": "uv pip install pytest-xdist",
            "pip_pytest": "pip install pytest",
        }
        with tempfile.TemporaryDirectory() as directory:
            workflows = pathlib.Path(directory)
            for name, command in commands.items():
                (workflows / f"{name}.yml").write_text(
                    f"jobs:\n  test:\n    steps:\n      - run: |\n          {command}\n", encoding="utf-8"
                )
            found = _workflows_running_pytest(workflows)
        for name, command in commands.items():
            with self.subTest(command=command):
                self.assertEqual(f"{name}.yml" in found, name not in {"pip", "uv", "pip_pytest"})

    def test_workflow_metadata_does_not_count_as_a_pytest_command(self):
        workflows_by_name = {
            "workflow_name": "name: Document pytest behavior\njobs:\n  docs:\n    steps:\n      - run: echo ready\n",
            "step_name": 'jobs:\n  docs:\n    steps:\n      - name: "Document pytest behavior"\n        run: echo ready\n',
            "comment": "# Document pytest behavior\njobs:\n  docs:\n    steps:\n      - run: echo ready\n",
            "action_input": "jobs:\n  docs:\n    steps:\n      - uses: ./action\n        with:\n          description: pytest behavior\n",
            "reusable_job": "jobs:\n  docs:\n    uses: ./.github/workflows/docs.yml\n    with:\n      description: pytest behavior\n",
        }
        with tempfile.TemporaryDirectory() as directory:
            workflows = pathlib.Path(directory)
            for name, text in workflows_by_name.items():
                (workflows / f"{name}.yml").write_text(text, encoding="utf-8")
            found = _workflows_running_pytest(workflows)
        self.assertEqual(found, {})
