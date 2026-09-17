"""Terraform project validation.

Runs the real toolchain when it is installed, and is explicit when it is not: a missing
binary produces a *skipped* check, never a passing one. Pretending an unvalidated project
was validated would be worse than saying nothing.

Optional tools (TFLint, Checkov) sit behind the same interface and are used when present.

Failures come back as structured errors. They never trigger an automatic model-driven
repair: generated code does not go back into an LLM.
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
import time
from pathlib import Path

from src.application.ports.generation import ProjectValidator
from src.domain.generation.models import (
    GeneratedProject,
    GeneratorValidationResult,
    ProjectKind,
    ValidationCheck,
)
from src.shared.errors import UnsupportedInputError
from src.shared.logging import get_logger

__all__ = ["TerraformProjectValidator"]

_logger = get_logger("topoforge.generators.terraform.validator")


class TerraformProjectValidator(ProjectValidator):
    """``fmt -check`` → ``init -backend=false`` → ``validate``, then optional linters."""

    def __init__(
        self,
        *,
        terraform_binary: str = "terraform",
        tflint_binary: str = "tflint",
        checkov_binary: str = "checkov",
        workspace_root: Path | None = None,
        command_timeout_seconds: float = 300.0,
        run_init: bool = True,
    ):
        self._terraform = terraform_binary
        self._tflint = tflint_binary
        self._checkov = checkov_binary
        self._workspace_root = workspace_root
        self._timeout = command_timeout_seconds
        self._run_init = run_init

    @property
    def name(self) -> str:
        return "TerraformProjectValidator"

    async def validate(self, project: GeneratedProject) -> GeneratorValidationResult:
        if project.kind is not ProjectKind.TERRAFORM:
            raise UnsupportedInputError(
                f"{self.name} validates Terraform projects, not {project.kind.value}"
            )

        checks: list[ValidationCheck] = []
        with tempfile.TemporaryDirectory(
            prefix="topoforge-tf-", dir=self._workspace_root
        ) as directory:
            workspace = Path(directory)
            _materialise(project, workspace)

            if shutil.which(self._terraform) is None:
                checks.append(
                    ValidationCheck(
                        name="terraform",
                        passed=False,
                        executed=False,
                        skipped_reason=(
                            f"{self._terraform!r} is not installed; the project was "
                            "generated but has not been checked by Terraform"
                        ),
                    )
                )
            else:
                checks.append(
                    await self._run("terraform fmt", [self._terraform, "fmt", "-check", "-recursive"], workspace)
                )
                if self._run_init:
                    # -backend=false keeps this offline: no remote state, no credentials.
                    checks.append(
                        await self._run(
                            "terraform init",
                            [self._terraform, "init", "-backend=false", "-input=false"],
                            workspace,
                        )
                    )
                checks.append(
                    await self._run(
                        "terraform validate", [self._terraform, "validate", "-no-color"], workspace
                    )
                )

            for label, binary, argv in (
                ("tflint", self._tflint, [self._tflint, "--chdir", str(workspace)]),
                ("checkov", self._checkov, [self._checkov, "-d", str(workspace), "--compact"]),
            ):
                if shutil.which(binary) is None:
                    checks.append(
                        ValidationCheck(
                            name=label,
                            passed=False,
                            executed=False,
                            skipped_reason=f"{binary!r} is not installed",
                        )
                    )
                else:
                    checks.append(await self._run(label, argv, workspace))

        return GeneratorValidationResult.from_checks(
            ProjectKind.TERRAFORM,
            tuple(checks),
            project_fingerprint=project.fingerprint(),
            warnings=project.warnings,
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
                exit_code=None,
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


def _materialise(project: GeneratedProject, workspace: Path) -> None:
    """Write a project into a directory.

    ``GeneratedFile`` already refuses traversing paths, and the resolved destination is
    re-checked here: this is the point where a bad path would become a real write.
    """
    root = workspace.resolve()
    for file in project.files:
        destination = (root / file.path).resolve()
        if not destination.is_relative_to(root):
            raise UnsupportedInputError(
                f"generated file path escapes the workspace: {file.path!r}"
            )
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(file.content, encoding="utf-8")
