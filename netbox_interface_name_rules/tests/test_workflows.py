# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Every workflow that runs pytest must install the plugins `addopts` makes mandatory.

pytest fails during argument parsing when `addopts` names an option no installed plugin registers,
so a missing distribution breaks the job before a single test runs.
"""

import functools
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
# Options that take their value as the next word, so it is not a requested package.
_GROUP_OPTIONS = {"--group"}
_REQUIREMENT_OPTIONS = {"-r", "--requirement", "--requirements"}
# Options taking their value as the next word; an omission reads that value as a package.
_VALUE_OPTIONS = {
    "--only-binary",
    "--no-binary",
    "--no-build-isolation-package",
    "--constraint",
    "--constraints",
    "-c",
    "--override",
    "--index",
    "--index-url",
    "-i",
    "--default-index",
    "--extra-index-url",
    "--index-strategy",
    "--find-links",
    "-f",
    "--python",
    "-p",
    "--python-version",
    "--python-platform",
    "--target",
    "--prefix",
    "--cache-dir",
    "--link-mode",
    "--resolution",
    "--prerelease",
    "--exclude-newer",
    "--timeout",
    "--extra",
    "--proxy",
    "--retries",
}
# Options that reach outside this project's own dependency declarations.
_FOREIGN_SOURCE_OPTIONS = ("--project", "--with", "--with-requirements", "--directory")
# Options that install a project from a path, so the path is the requirement.
_EDITABLE_OPTIONS = {"-e", "--editable"}
# Short options whose value may be written onto them, as in `-rfile`.
_ATTACHED_VALUE_OPTIONS = ("-c", "-e", "-f", "-i", "-p", "-r")
# Options that make the command install nothing, whatever its subcommand says.
_NON_INSTALLING_OPTIONS = frozenset({"--help", "-h", "--dry-run", "--version", "-V"})
# Requirements files this project does not own. NetBox's own file comes from its checkout.
_EXTERNAL_REQUIREMENTS = frozenset({"../netbox/requirements.txt"})

_XVFB_RUN_FLAGS = {"-a", "--auto-servernum", "-l", "--listen-tcp"}
_XVFB_RUN_VALUE_OPTIONS = {
    "-e",
    "--error-file",
    "-f",
    "--auth-file",
    "-n",
    "--server-num",
    "-p",
    "--xauth-protocol",
    "-s",
    "--server-args",
    "-w",
    "--wait",
}


def _configured_addopts():
    """Return the `addopts` string pytest applies to every invocation in this repository."""
    with (_PROJECT_ROOT / "pyproject.toml").open("rb") as handle:
        return tomllib.load(handle)["tool"]["pytest"]["ini_options"]["addopts"]


def _required_distributions(*argument_sequences):
    """Return the pytest plugins required by literal pytest arguments."""
    arguments = [
        argument
        for sequence in argument_sequences
        for argument in (shlex.split(sequence) if isinstance(sequence, str) else sequence)
        if argument is not None
    ]
    return {
        owner
        for option, owner in _OPTION_OWNERS.items()
        if any(argument == option or argument.startswith(f"{option}=") for argument in arguments)
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


def _is_python_executable(name):
    """Return whether *name* is a supported literal Python executable."""
    return name == "python" or re.fullmatch(r"python3(?:\.\d+)*", name) is not None


def _xvfb_run_command(words):
    """Return the command after supported xvfb-run options, or an empty tuple."""
    words = tuple(words)
    index = 0
    while index < len(words):
        option = words[index]
        if option is None:
            return ()
        if option == "--":
            return words[index + 1 :]
        if option in _XVFB_RUN_FLAGS:
            index += 1
            continue
        if option in _XVFB_RUN_VALUE_OPTIONS:
            if index + 1 >= len(words) or words[index + 1] is None:
                return ()
            index += 2
            continue
        if any(option.startswith(f"{name}=") for name in _XVFB_RUN_VALUE_OPTIONS if name.startswith("--")):
            index += 1
            continue
        if option.startswith("-"):
            return ()
        return words[index:]
    return ()


def _pytest_arguments(words):
    """Return literal pytest arguments after supported runner prefixes."""
    if not words or words[0] is None:
        return None
    name = pathlib.PurePosixPath(words[0]).name
    if name == "pytest":
        return tuple(words[1:])
    if _is_python_executable(name):
        return tuple(words[3:]) if tuple(words[1:3]) == ("-m", "pytest") else None
    if name in {"poetry", "uv"} and tuple(words[1:2]) == ("run",):
        return _pytest_arguments(words[2:])
    if name == "xvfb-run":
        return _pytest_arguments(_xvfb_run_command(words[1:]))
    return None


def _is_pytest_invocation(words):
    """Recognize literal pytest commands and the supported runner prefixes."""
    return _pytest_arguments(words) is not None


def _after_options(words):
    """Return *words* without the leading option words a tool takes before its subcommand."""
    index = 0
    while index < len(words) and (words[index] or "").startswith("-"):
        index += 1
    return tuple(words[index:])


def _is_package_manager(name):
    """Return whether *name* is a literal executable that installs packages."""
    return name == "uv" or _is_python_executable(name) or re.fullmatch(r"pip(?:3(?:\.\d+)*)?", name) is not None


def _installation_arguments(words):
    """Return arguments to a command that installs, or None.

    Recognition is strict, so an option's separate value before the subcommand, as in
    `uv --project ../other pip install`, is not recognised: a value cannot be told from a
    subcommand without a table of every option uv and pip take. Crediting a command that
    installs nothing would let a job run pytest without the plugins `addopts` makes mandatory,
    which is why this side errs towards recognising too little and `_installed_operands` errs
    the other way, and why `--help` and `--dry-run` disqualify a command outright. Forms that
    install without the `install` subcommand, such as `uv add`, `uv run --with` and `uvx`, are
    out of reach of both.
    """
    if not words or words[0] is None:
        return None
    if _NON_INSTALLING_OPTIONS.intersection(words):
        return None
    name = pathlib.PurePosixPath(words[0]).name
    rest = tuple(words[1:])
    if _is_python_executable(name):
        if "-m" not in rest:
            return None
        rest = rest[rest.index("-m") + 1 :]
        if not rest or rest[0] is None:
            return None
        name, rest = pathlib.PurePosixPath(rest[0]).name, rest[1:]
    if name == "uv":
        rest = _after_options(rest)
        if rest[:1] != ("pip",):
            return None
        rest = rest[1:]
    elif not re.fullmatch(r"pip(?:3(?:\.\d+)*)?", name):
        return None
    rest = _after_options(rest)
    return tuple(rest[1:]) if rest[:1] == ("install",) else None


def _installed_operands(words):
    """Return the operands after the `install` word of a package-manager command, or None.

    Recognition is permissive on purpose: the subcommand is found wherever it sits, so a value
    before it cannot hide the install, and a command that merely mentions the word is read as
    one rather than trusted. Missing an install is the worse error for the source question,
    because a package name would then pass unseen.
    """
    if not words or words[0] is None:
        return None
    if not _is_package_manager(pathlib.PurePosixPath(words[0]).name):
        return None
    return tuple(words[words.index("install") + 1 :]) if "install" in words else None


def _distribution_name(argument):
    """Return the normalized distribution name from one static requirement argument."""
    if argument is None or argument.startswith("-"):
        return None
    match = re.match(r"(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)(?:\[[^]]+\])?(?=[<>=!~@]|$)", argument)
    if match is None:
        return None
    return re.sub(r"[-_.]+", "-", match.group("name")).lower()


def _normalized(name):
    """Return *name* in the form PEP 735 and PEP 503 compare by."""
    return re.sub(r"[-_.]+", "-", name).lower()


@functools.cache
def _dependency_groups():
    """Expand every pyproject dependency group to the distribution names it installs.

    Group names are compared normalised, because the specification says `Test_Group` and
    `test-group` are the same group.
    """
    with (_PROJECT_ROOT / "pyproject.toml").open("rb") as handle:
        table = tomllib.load(handle)["dependency-groups"]
    groups = {_normalized(name): entries for name, entries in table.items()}

    def expand(name, pending):
        if name not in groups:
            raise ValueError(f"unknown dependency group {name!r}")
        if name in pending:
            raise ValueError(f"dependency group {name!r} includes itself")
        names = set()
        for entry in groups[name]:
            if isinstance(entry, str):
                names.add(_distribution_name(entry))
            else:
                names |= expand(_normalized(entry["include-group"]), pending | {name})
        return names

    return {name: frozenset(expand(name, frozenset())) for name in groups}


def _detach(argument):
    """Return *argument* as ``(option, value)``, with an attached or `=` value split out.

    pip and uv take a value written onto its option, so `-rfile` and `--editable=../other` name
    a source exactly as `-r file` does.
    """
    if not argument.startswith("-"):
        return argument, None
    option, separator, attached = argument.partition("=")
    if separator:
        return option, attached
    if not argument.startswith("--") and argument[:2] in _ATTACHED_VALUE_OPTIONS and len(argument) > 2:
        return argument[:2], argument[2:]
    return argument, None


def _operand_value(attached, pending):
    """Return an option's value: the one attached to it, or the next word."""
    if attached is not None:
        return attached
    return pending.pop(0) if pending else None


def _install_operands(arguments):
    """Yield ``(kind, value)`` for each operand of an install command.

    An option's value is consumed with the option, attached or separate, so `--only-binary
    pytest-cov` never reads its operand as a requested package. Kind is "group",
    "requirements", "project" or "package".
    """
    pending = list(arguments)
    while pending:
        argument = pending.pop(0)
        if argument is None:
            yield "package", None
            continue
        option, attached = _detach(argument)
        if option in _GROUP_OPTIONS:
            yield "group", _operand_value(attached, pending)
        elif option in _REQUIREMENT_OPTIONS:
            yield "requirements", _operand_value(attached, pending)
        elif option in _EDITABLE_OPTIONS:
            target = _operand_value(attached, pending)
            yield ("project" if target in {".", "./"} else "package"), target
        elif option in _VALUE_OPTIONS:
            _operand_value(attached, pending)
        elif option.startswith("-"):
            pass
        elif option in {".", "./"}:
            yield "project", option
        else:
            yield "package", option


def _installed_distributions(words):
    """Return what a literal install command provides, expanding each dependency group it names."""
    arguments = _installation_arguments(words)
    if arguments is None:
        return frozenset()
    groups = _dependency_groups()
    names = set()
    for kind, value in _install_operands(arguments):
        if kind == "group":
            group = _normalized(value) if value else None
            if group not in groups:
                raise ValueError(f"install command names unknown dependency group {value!r}")
            names |= groups[group]
        elif kind == "package" and (name := _distribution_name(value)) is not None:
            names.add(name)
    return frozenset(names)


def _unsourced_requirements(words):
    """Return install operands that name a package instead of taking it from pyproject.

    Every dependency this project controls is declared in `[dependency-groups]`, so a workflow
    names a group and never a package. That is what lets Dependabot update them: it reads
    `pyproject.toml` and cannot see a name written into a shell command.
    """
    arguments = _installed_operands(words)
    if arguments is None:
        return ()
    unsourced = [
        word
        for word in words
        for option in _FOREIGN_SOURCE_OPTIONS
        if word == option or (word or "").startswith(f"{option}=")
    ]
    for kind, value in _install_operands(arguments):
        if kind == "package":
            unsourced.append("<dynamic>" if value is None else value)
        elif kind == "requirements" and value not in _EXTERNAL_REQUIREMENTS:
            unsourced.append(f"-r {value}")
        elif kind == "group" and (value is None or ":" in value or "/" in value):
            # A path-qualified group comes from another project's pyproject.
            unsourced.append(f"--group {value}")
    return tuple(unsourced)


def _command_installs_distribution(words, distribution):
    """Return whether a literal install command provides *distribution*."""
    return re.sub(r"[-_.]+", "-", distribution).lower() in _installed_distributions(words)


def _shell_commands(command, workflow):
    """Return the literal executable commands in one shell program."""
    command = re.sub(r"\$\{\{.*?\}\}", "${WORKFLOW_EXPRESSION}", command, flags=re.DOTALL)
    source = command.encode("utf-8")
    root = Parser(_SHELL_LANGUAGE).parse(source).root_node
    if root.has_error:
        raise ValueError(f"{workflow}: cannot parse workflow shell command")
    commands = []
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
            commands.append((node.start_byte, tuple(words)))
        pending.extend(node.named_children)
    return tuple(words for _position, words in sorted(commands))


def _shell_runs_pytest(command, workflow):
    """Inspect executable command nodes without interpreting arguments as shell programs."""
    return any(_is_pytest_invocation(words) for words in _shell_commands(command, workflow))


def _workflow_commands(directory=None):
    """Map every workflow to the ordered literal commands in each of its jobs."""
    found = {}
    for path in sorted((directory or _WORKFLOWS).glob("*.y*ml")):
        workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
        found[path.name] = {
            job_name: tuple(
                words
                for step in job.get("steps", [])
                if "run" in step
                for words in _shell_commands(step["run"], path.name)
            )
            for job_name, job in workflow["jobs"].items()
        }
    return found


def _workflows_running_pytest(directory=None):
    """Map each workflow that invokes pytest to ordered literal commands per job."""
    return {
        name: jobs
        for name, jobs in _workflow_commands(directory).items()
        if any(_is_pytest_invocation(words) for commands in jobs.values() for words in commands)
    }


def _missing_workflow_distributions(directory=None):
    """Return required pytest distributions not installed earlier in the same job."""
    missing = []
    for workflow, jobs in _workflows_running_pytest(directory).items():
        for job, commands in jobs.items():
            for index, command in enumerate(commands):
                arguments = _pytest_arguments(command)
                if arguments is None:
                    continue
                required = _required_distributions(_configured_addopts(), arguments)
                missing.extend(
                    (workflow, job, distribution)
                    for distribution in sorted(required)
                    if not any(_command_installs_distribution(earlier, distribution) for earlier in commands[:index])
                )
    return tuple(missing)


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

    def test_only_pytest_arguments_create_requirements(self):
        commands = (("echo", "pytest --cov"), ("pytest", "tests"))
        pytest_arguments = tuple(
            arguments for command in commands if (arguments := _pytest_arguments(command)) is not None
        )

        self.assertEqual(_required_distributions(*pytest_arguments), set())


class DistributionInstallationTest(SimpleTestCase):
    def test_argument_text_is_not_an_installation(self):
        self.assertFalse(_command_installs_distribution(("echo", "install pytest-cov"), "pytest-cov"))
        self.assertFalse(_command_installs_distribution(("printf", "pip install pytest-cov"), "pytest-cov"))

    def test_a_package_manager_install_command_is_an_installation(self):
        self.assertTrue(
            _command_installs_distribution(
                ("uv", "pip", "install", "--system", "pytest-cov==7.0.0"),
                "pytest-cov",
            )
        )
        self.assertTrue(
            _command_installs_distribution(
                ("python3.14", "-m", "pip", "install", "pytest-cov"),
                "pytest-cov",
            )
        )


class WorkflowPytestPluginTest(SimpleTestCase):
    """A workflow that runs pytest installs every plugin the run needs."""

    def test_the_devcontainer_installs_everything_the_suite_needs(self):
        """The devcontainer and CI run the same suite, so they install from the same groups."""
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

        groups = _dependency_groups()
        installed = frozenset().union(
            *(_installed_distributions(("uv", "pip", *arguments)) for arguments in installation_arguments)
        )

        self.assertEqual(
            sorted((groups["ci-tests"] | groups["workflow-tests"]) - installed),
            [],
            "devcontainer setup does not install every distribution the suite needs",
        )

    def test_every_pytest_workflow_installs_the_plugins_addopts_requires(self):
        missing = _missing_workflow_distributions()

        self.assertTrue(_workflows_running_pytest(), "no workflow was detected as running pytest")
        self.assertEqual(
            missing,
            (),
            "\n".join(
                f"{workflow} job {job!r} runs pytest before installing {distribution}"
                for workflow, job, distribution in missing
            ),
        )


class WorkflowDetectionTest(SimpleTestCase):
    def test_workflow_scanner_preserves_job_and_command_order(self):
        workflow = {
            "jobs": {
                "install-elsewhere": {"steps": [{"run": "uv pip install pytest-cov pytest-xdist"}]},
                "test": {
                    "steps": [
                        {
                            "run": "pytest tests\nuv pip install pytest-cov pytest-xdist",
                        }
                    ]
                },
            }
        }
        with tempfile.TemporaryDirectory() as directory:
            workflows = pathlib.Path(directory)
            (workflows / "ordered.yml").write_text(yaml.safe_dump(workflow, sort_keys=False), encoding="utf-8")

            found = _workflows_running_pytest(workflows)
            missing = _missing_workflow_distributions(workflows)

        self.assertEqual(
            found,
            {
                "ordered.yml": {
                    "install-elsewhere": (("uv", "pip", "install", "pytest-cov", "pytest-xdist"),),
                    "test": (
                        ("pytest", "tests"),
                        ("uv", "pip", "install", "pytest-cov", "pytest-xdist"),
                    ),
                }
            },
        )
        self.assertEqual(
            missing,
            (
                ("ordered.yml", "test", "pytest-cov"),
                ("ordered.yml", "test", "pytest-xdist"),
            ),
        )

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
            "versioned_python": ("python3.14 -m pytest tests", True),
            "uv_runner": ("uv run pytest tests", True),
            "runner_module": ("uv run python -m pytest tests", True),
            "xvfb_options": ("xvfb-run -a pytest tests", True),
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


class CoverageMatrixTest(SimpleTestCase):
    """One matrix cell reports coverage, and every coverage step reads that one flag."""

    def _test_job(self):
        """Return the matrix job that runs the suite against each supported NetBox release."""
        with (_WORKFLOWS / "test.yaml").open(encoding="utf-8") as handle:
            return yaml.safe_load(handle)["jobs"]["test-netbox"]

    def test_exactly_one_matrix_cell_collects_coverage(self):
        include = self._test_job()["strategy"]["matrix"]["include"]

        self.assertEqual([cell["netbox-version"] for cell in include if cell.get("coverage")], ["v4.5.3"])

    def test_no_step_condition_names_a_netbox_release(self):
        """The reporting cell is named once, in the matrix, not again in every step condition."""
        conditions = [step["if"] for step in self._test_job()["steps"] if "if" in step]

        self.assertEqual([condition for condition in conditions if "netbox-version" in condition], [])

    def test_pytest_turns_coverage_off_outside_that_cell(self):
        steps = [step for step in self._test_job()["steps"] if "run" in step]
        step = next(step for step in steps if _shell_runs_pytest(step["run"], "test.yaml"))
        option = step["env"]["COVERAGE_OPTION"]

        self.assertIn("matrix.coverage", option)
        self.assertIn("--no-cov", option)
        self.assertIn("$COVERAGE_OPTION", step["run"])


class WorkflowPackageSourceTest(SimpleTestCase):
    """A workflow names a dependency group, never a package.

    Checking for a version is a deny list, and a deny list can be walked around: quoting
    `'ruff == 0.16.0'` makes one shell word, a requirements file hides its contents, and an
    option operand looks like a package. Requiring the group instead is decidable, and it is
    what lets Dependabot maintain the versions, because it reads `pyproject.toml` and
    `uv.lock` and cannot see a name written into a shell command.

    What it does not reach, so nobody reads more into a pass than it proves: a form that
    installs without the `install` subcommand (`uv add`, `uv run --with`, `uvx`), a command
    whose executable or arguments are not literal, and an install run after `cd` into another
    project. Adding one of those to a workflow needs review by eye.
    """

    def test_no_workflow_install_command_names_a_package(self):
        unsourced = [
            (workflow, job, operand)
            for workflow, jobs in _workflow_commands().items()
            for job, commands in jobs.items()
            for words in commands
            for operand in _unsourced_requirements(words)
        ]

        self.assertEqual(
            unsourced,
            [],
            "\n".join(
                f"{workflow} job {job!r} installs {operand!r} instead of naming a dependency group"
                for workflow, job, operand in unsourced
            ),
        )

    def test_a_named_package_is_reported_however_it_is_spelled(self):
        """Each of these walked past the previous version-detecting guard."""
        for command, expected in (
            (("uv", "pip", "install", "ruff==0.16.0"), ("ruff==0.16.0",)),
            (("uv", "pip", "install", "ruff"), ("ruff",)),
            (("pip", "install", "ruff == 0.16.0"), ("ruff == 0.16.0",)),
            (("pip", "install", "ruff[extra] == 1.0"), ("ruff[extra] == 1.0",)),
            (("uv", "pip", "install", "ruff@https://x.invalid/ruff.whl"), ("ruff@https://x.invalid/ruff.whl",)),
            (
                ("uv", "pip", "install", "ruff", "@", "https://x.invalid/ruff.whl"),
                ("ruff", "@", "https://x.invalid/ruff.whl"),
            ),
        ):
            with self.subTest(command=command):
                self.assertEqual(_unsourced_requirements(command), expected)

    def test_an_option_operand_is_not_a_package(self):
        """`--only-binary` takes the next word, so reading it as a package was a false positive."""
        command = ("uv", "pip", "install", "--only-binary", "pytest-cov", "--group", "ci-tests")

        self.assertEqual(_unsourced_requirements(command), ())

    def test_a_group_the_project_and_netbox_requirements_are_sourced(self):
        for command in (
            ("uv", "pip", "install", "--system", "--only-binary=:all:", "--group", "ci-tests"),
            ("uv", "pip", "install", "--system", "-e", "."),
            ("uv", "pip", "install", "--system", "-r", "../netbox/requirements.txt"),
        ):
            with self.subTest(command=command):
                self.assertEqual(_unsourced_requirements(command), ())

    def test_another_requirements_file_is_not_a_source(self):
        """Its contents are invisible to this scan and to Dependabot alike."""
        command = ("uv", "pip", "install", "-r", "requirements-dev.txt")

        self.assertEqual(_unsourced_requirements(command), ("-r requirements-dev.txt",))

    def test_a_global_option_before_the_subcommand_still_installs(self):
        """`uv --system-certs pip install ruff` installs ruff, so the scan has to see it."""
        for command in (
            ("uv", "--system-certs", "pip", "install", "ruff"),
            ("pip", "--isolated", "install", "ruff"),
            ("python3", "-I", "-m", "pip", "install", "ruff"),
        ):
            with self.subTest(command=command):
                self.assertEqual(_unsourced_requirements(command), ("ruff",))

    def test_a_group_taken_from_another_project_is_not_sourced(self):
        """A path-qualified group reads another project's pyproject, not this one's."""
        for command in (
            ("uv", "pip", "install", "--group", "../other/pyproject.toml:lint"),
            ("uv", "pip", "install", "--project", "../other", "--group", "lint"),
            ("uv", "pip", "install", "--with", "ruff", "--group", "lint"),
        ):
            with self.subTest(command=command):
                self.assertNotEqual(_unsourced_requirements(command), ())

    def test_a_global_option_value_does_not_hide_the_install(self):
        """A value before the subcommand used to stop the scan finding `pip` at all."""
        self.assertEqual(
            _unsourced_requirements(("uv", "--project", "../other", "pip", "install", "--group", "lint")),
            ("--project",),
        )
        self.assertEqual(
            _unsourced_requirements(("uv", "--python", "3.12", "pip", "install", "ruff")),
            ("ruff",),
        )

    def test_a_global_option_value_is_not_itself_a_finding(self):
        """`--python 3.12` is ordinary; only a foreign source counts."""
        command = ("uv", "--python", "3.12", "pip", "install", "--group", "lint")

        self.assertEqual(_unsourced_requirements(command), ())

    def test_a_boolean_global_flag_does_not_hide_the_install(self):
        """`--native-tls` takes no value, so the word after it is the subcommand itself."""
        for command in (
            ("uv", "--native-tls", "pip", "install", "ruff"),
            ("uv", "--offline", "pip", "install", "ruff"),
        ):
            with self.subTest(command=command):
                self.assertEqual(_unsourced_requirements(command), ("ruff",))

    def test_a_value_option_is_never_read_as_a_package(self):
        """An option missing from the table would fail a legitimate workflow."""
        for command in (
            ("uv", "pip", "install", "--group", "lint", "--default-index", "https://example.invalid/simple"),
            ("uv", "pip", "install", "--group", "lint", "--no-build-isolation-package", "pytest-cov"),
            ("uv", "pip", "install", "--requirements", "../netbox/requirements.txt"),
            ("uv", "pip", "install", "--group", "lint", "--index-strategy", "unsafe-best-match"),
        ):
            with self.subTest(command=command):
                self.assertEqual(_unsourced_requirements(command), ())

    def test_a_command_that_only_prints_is_not_an_install(self):
        """`--help` and `--dry-run` install nothing, so crediting them would hide a missing plugin."""
        for command in (
            ("uv", "--help", "pip", "install", "--group", "ci-tests"),
            ("uv", "pip", "install", "--dry-run", "--group", "ci-tests"),
        ):
            with self.subTest(command=command):
                self.assertIsNone(_installation_arguments(command))
                self.assertEqual(_installed_distributions(command), frozenset())

    def test_another_command_s_arguments_are_not_a_certain_install(self):
        """`uv run ... echo pip install` runs echo; crediting it would hide a missing plugin."""
        for command in (
            ("uv", "run", "--no-sync", "echo", "pip", "install", "--group", "ci-tests"),
            ("uv", "--offline", "run", "--no-sync", "echo", "pip", "install", "--group", "ci-tests"),
        ):
            with self.subTest(command=command):
                self.assertIsNone(_installation_arguments(command))
                self.assertEqual(_installed_distributions(command), frozenset())

    def test_an_attached_option_value_is_read_like_a_separate_one(self):
        """`-rfile` and `--editable=../other` walked past the scan; their spaced forms did not."""
        for command, expected in (
            (("uv", "pip", "install", "-rrequirements-dev.txt"), ("-r requirements-dev.txt",)),
            (("uv", "pip", "install", "--requirement=requirements-dev.txt"), ("-r requirements-dev.txt",)),
            (("uv", "pip", "install", "-e../other"), ("../other",)),
            (("uv", "pip", "install", "--editable=../other"), ("../other",)),
        ):
            with self.subTest(command=command):
                self.assertEqual(_unsourced_requirements(command), expected)

    def test_an_attached_value_on_a_sourced_install_stays_sourced(self):
        """An option table that misread an attached value would fail a legitimate workflow."""
        for command in (
            ("uv", "pip", "install", "--system", "--only-binary=:all:", "--group=ci-tests"),
            ("uv", "pip", "install", "-r../netbox/requirements.txt"),
            ("uv", "pip", "install", "--editable=."),
            ("uv", "pip", "install", "--group", "lint", "-c../netbox/requirements.txt"),
        ):
            with self.subTest(command=command):
                self.assertEqual(_unsourced_requirements(command), ())

    def test_argument_text_is_not_an_install(self):
        self.assertEqual(_unsourced_requirements(("echo", "install ruff==0.16.0")), ())


class DependencyGroupTest(SimpleTestCase):
    """The group expansion the install check reads has to follow `include-group` and reject typos."""

    def test_a_group_expands_through_include_group(self):
        groups = _dependency_groups()

        self.assertLessEqual(groups["ci-tests"], groups["dev"])
        self.assertLessEqual(groups["ci-tests"], groups["devcontainer"])

    def test_the_devcontainer_group_pins_no_framework(self):
        """It installs into the NetBox virtualenv, where a Django pin could downgrade the server."""
        self.assertNotIn("django", _dependency_groups()["devcontainer"])

    def test_a_group_install_provides_the_distributions_it_names(self):
        installed = _installed_distributions(("uv", "pip", "install", "--group", "ci-tests"))

        self.assertIn("pytest-cov", installed)
        self.assertIn("tblib", installed)

    def test_an_unknown_group_is_an_error(self):
        with self.assertRaisesRegex(ValueError, "no-such-group"):
            _installed_distributions(("uv", "pip", "install", "--group", "no-such-group"))
