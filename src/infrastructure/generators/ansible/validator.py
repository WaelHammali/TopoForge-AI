"""Ansible project validation.

``ansible-playbook --syntax-check`` plus ``ansible-lint`` when installed. A missing binary
is a skipped check, never a pass.

Syntax checking needs an inventory that parses, but a real inventory only exists after
apply — so the placeholder is used, and a check that fails *only* because hosts are
unreachable is not a syntax failure.
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
import time
from pathlib import Path

from src.application.ports.generation import ProjectValidator
from src.domain.generation.models import (
    AnsibleProject,
    GeneratedFile,
    GeneratedProject,
    GeneratorValidationResult,
    ProjectKind,
    ValidationCheck,
)
from src.infrastructure.generators.terraform.validator import _materialise
from src.shared.errors import UnsupportedInputError

__all__ = ["AnsibleProjectValidator"]


class AnsibleProjectValidator(ProjectValidator):
    def __init__(
        self,
        *,
        ansible_playbook_binary: str = "ansible-playbook",
        ansible_lint_binary: str = "ansible-lint",
        workspace_root: Path | None = None,
        command_timeout_seconds: float = 300.0,
    ):
        self._playbook = ansible_playbook_binary
        self._lint = ansible_lint_binary
        self._workspace_root = workspace_root
        self._timeout = command_timeout_seconds

    @property
    def name(self) -> str:
        return "AnsibleProjectValidator"

    async def validate(self, project: GeneratedProject) -> GeneratorValidationResult:
        if project.kind is not ProjectKind.ANSIBLE:
            raise UnsupportedInputError(
                f"{self.name} validates Ansible projects, not {project.kind.value}"
            )

        playbook = (
            project.playbook_path if isinstance(project, AnsibleProject) else "site.yml"
        )
        inventory = (
            project.inventory_path if isinstance(project, AnsibleProject) else None
        ) or "inventory/hosts.yml"

        checks: list[ValidationCheck] = [self._structure_check(project, playbook)]

        with tempfile.TemporaryDirectory(
            prefix="topoforge-ansible-", dir=self._workspace_root
        ) as directory:
            workspace = Path(directory)
            _materialise(project, workspace)

            if shutil.which(self._playbook) is None:
                checks.append(
                    ValidationCheck(
                        name="ansible syntax-check",
                        passed=False,
                        executed=False,
                        skipped_reason=(
                            f"{self._playbook!r} is not installed; the project was "
                            "generated but has not been checked by Ansible"
                        ),
                    )
                )
            else:
                checks.append(
                    await self._run(
                        "ansible syntax-check",
                        [self._playbook, playbook, "-i", inventory, "--syntax-check"],
                        workspace,
                    )
                )

            if shutil.which(self._lint) is None:
                checks.append(
                    ValidationCheck(
                        name="ansible-lint",
                        passed=False,
                        executed=False,
                        skipped_reason=f"{self._lint!r} is not installed",
                    )
                )
            else:
                checks.append(await self._run("ansible-lint", [self._lint, playbook], workspace))

        return GeneratorValidationResult.from_checks(
            ProjectKind.ANSIBLE,
            tuple(checks),
            project_fingerprint=project.fingerprint(),
            warnings=project.warnings,
        )

    @staticmethod
    def _structure_check(project: GeneratedProject, playbook: str) -> ValidationCheck:
        """Structural checks that need no toolchain, so CI always validates something."""
        problems: list[str] = []
        paths = set(project.file_paths)

        if playbook not in paths:
            problems.append(f"the declared playbook {playbook!r} is not in the project")

        for file in project.files:
            if not file.path.endswith((".yml", ".yaml")):
                continue
            if file.path.startswith("roles/") and "/tasks/" in file.path:
                if "- name:" not in file.content:
                    problems.append(f"{file.path} declares no named task")

        for role_file in (path for path in paths if path.startswith("roles/")):
            parts = role_file.split("/")
            if len(parts) >= 3 and parts[2] == "tasks":
                meta = f"roles/{parts[1]}/meta/main.yml"
                if meta not in paths:
                    problems.append(f"role {parts[1]!r} has tasks but no {meta}")

        return ValidationCheck(
            name="project structure",
            passed=not problems,
            exit_code=0 if not problems else 1,
            stderr="\n".join(problems),
        )

    async def _run(self, label: str, argv: list[str], cwd: Path) -> ValidationCheck:
        started = time.perf_counter()
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                cwd=str(cwd),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=self._timeout)
            exit_code = process.returncode
        except TimeoutError:
            return ValidationCheck(
                name=label,
                passed=False,
                stderr=f"{label} timed out after {self._timeout:.0f}s",
                duration_ms=(time.perf_counter() - started) * 1000.0,
            )
        except OSError as error:
            return ValidationCheck(
                name=label, passed=False, executed=False, skipped_reason=str(error)
            )

        return ValidationCheck(
            name=label,
            passed=exit_code == 0,
            exit_code=exit_code,
            stdout=stdout.decode(errors="replace")[:8000],
            stderr=stderr.decode(errors="replace")[:8000],
            duration_ms=(time.perf_counter() - started) * 1000.0,
        )


def _unused(_: GeneratedFile) -> None:  # pragma: no cover - keeps the import honest
    return None
