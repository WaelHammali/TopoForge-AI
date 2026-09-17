"""Generated artifact models, including the determinism contract."""

from __future__ import annotations

import pytest
from pydantic import ValidationError as PydanticValidationError

from src.domain.generation import (
    AnsibleProject,
    GeneratedFile,
    GeneratorValidationResult,
    ProjectKind,
    TerraformProject,
    ValidationCheck,
)


class TestGeneratedFilePathSafety:
    """Generated paths are joined onto real directories and storage keys."""

    @pytest.mark.parametrize(
        "path",
        [
            "/etc/passwd",
            "../outside.tf",
            "roles/../../escape.yml",
            "~/.ssh/authorized_keys",
            "",
            "   ",
            "./main.tf",
        ],
    )
    def test_unsafe_paths_are_refused(self, path: str) -> None:
        with pytest.raises(PydanticValidationError):
            GeneratedFile(path=path, content="x")

    @pytest.mark.parametrize(
        "path", ["main.tf", "roles/nginx/tasks/main.yml", "group_vars/all.yml"]
    )
    def test_normal_paths_are_accepted(self, path: str) -> None:
        assert GeneratedFile(path=path, content="x").path == path

    def test_backslashes_are_normalised(self) -> None:
        assert GeneratedFile(path="roles\\nginx\\main.yml", content="x").path == (
            "roles/nginx/main.yml"
        )

    def test_checksum_tracks_content(self) -> None:
        first = GeneratedFile(path="a.tf", content="one")
        second = GeneratedFile(path="a.tf", content="two")
        assert first.checksum != second.checksum
        assert first.checksum == GeneratedFile(path="b.tf", content="one").checksum


class TestDeterminism:
    """Same input, byte-identical output — the contract that makes golden tests work."""

    def _project(self, order: int) -> TerraformProject:
        files = [
            GeneratedFile(path="main.tf", content="resource {}"),
            GeneratedFile(path="variables.tf", content="variable {}"),
            GeneratedFile(path="modules/vpc/main.tf", content="module {}"),
        ]
        if order:
            files.reverse()
        return TerraformProject(
            files=tuple(files), generator_name="TerraformGenerator", generator_version="1.0.0"
        )

    def test_fingerprint_is_order_independent(self) -> None:
        assert self._project(0).fingerprint() == self._project(1).fingerprint()

    def test_fingerprint_ignores_generation_timestamp(self) -> None:
        first = self._project(0)
        second = first.model_copy(update={"generated_at": first.generated_at.replace(year=2030)})
        assert first.fingerprint() == second.fingerprint()

    def test_fingerprint_changes_when_content_changes(self) -> None:
        first = self._project(0)
        changed = first.model_copy(
            update={
                "files": (
                    GeneratedFile(path="main.tf", content="resource { changed }"),
                    *first.files[1:],
                )
            }
        )
        assert first.fingerprint() != changed.fingerprint()

    def test_fingerprint_changes_when_a_file_moves(self) -> None:
        first = self._project(0)
        moved = first.model_copy(
            update={
                "files": (
                    GeneratedFile(path="renamed.tf", content="resource {}"),
                    *first.files[1:],
                )
            }
        )
        assert first.fingerprint() != moved.fingerprint()


class TestProjectStructure:
    def test_duplicate_paths_are_refused(self) -> None:
        with pytest.raises(PydanticValidationError, match="duplicate generated file paths"):
            TerraformProject(
                files=(
                    GeneratedFile(path="main.tf", content="a"),
                    GeneratedFile(path="main.tf", content="b"),
                ),
                generator_name="g",
                generator_version="1",
            )

    def test_tree_is_nested_for_the_ui(self) -> None:
        project = TerraformProject(
            files=(
                GeneratedFile(path="main.tf", content="a"),
                GeneratedFile(path="modules/vpc/main.tf", content="b"),
            ),
            generator_name="g",
            generator_version="1",
        )
        tree = project.tree()
        assert set(tree) == {"main.tf", "modules"}
        assert "main.tf" in tree["modules"]["vpc"]

    def test_kinds_are_fixed_per_project_type(self) -> None:
        terraform = TerraformProject(generator_name="g", generator_version="1")
        ansible = AnsibleProject(generator_name="g", generator_version="1")
        assert terraform.kind is ProjectKind.TERRAFORM
        assert ansible.kind is ProjectKind.ANSIBLE

    def test_inventory_is_pending_until_terraform_has_been_applied(self) -> None:
        # Addresses do not exist before apply, so an Ansible project starts without them.
        assert AnsibleProject(generator_name="g", generator_version="1").inventory_pending is True


class TestValidationResult:
    def test_failed_check_becomes_a_structured_error(self) -> None:
        result = GeneratorValidationResult.from_checks(
            ProjectKind.TERRAFORM,
            (
                ValidationCheck(name="terraform fmt", passed=True),
                ValidationCheck(
                    name="terraform validate", passed=False, exit_code=1, stderr="bad block"
                ),
            ),
        )
        assert result.valid is False
        assert [error.code for error in result.errors] == ["terraform_validate_failed"]
        assert "bad block" in result.errors[0].message

    def test_a_missing_tool_is_skipped_not_failed(self) -> None:
        result = GeneratorValidationResult.from_checks(
            ProjectKind.TERRAFORM,
            (
                ValidationCheck(
                    name="tflint",
                    passed=False,
                    executed=False,
                    skipped_reason="tflint is not installed",
                ),
            ),
        )
        assert result.valid is True
        assert result.errors == ()
        assert len(result.skipped_checks) == 1

    def test_result_records_which_project_it_validated(self) -> None:
        project = TerraformProject(
            files=(GeneratedFile(path="main.tf", content="x"),),
            generator_name="g",
            generator_version="1",
        )
        result = GeneratorValidationResult.from_checks(
            ProjectKind.TERRAFORM,
            (ValidationCheck(name="terraform validate", passed=True),),
            project_fingerprint=project.fingerprint(),
        )
        # A stale result is detectable because the fingerprint no longer matches.
        assert result.project_fingerprint == project.fingerprint()
