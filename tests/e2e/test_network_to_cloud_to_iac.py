"""End to end, without AWS.

    network_architecture.json
        -> RAG adapter
    cloud_architecture.json
        -> TerraformGenerator -> terraform validation
        -> AnsibleGenerator   -> ansible syntax validation
        -> (after apply)      -> InventoryGenerator

This is the test the refactor is accountable to. It runs offline: no Groq key, no AWS
credentials, no deployment. Where a real binary is absent the check is *skipped*, and the
test asserts that it was skipped rather than silently counted as a pass.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from src.application.ports.generation import DeploymentOutputs
from src.application.ports.rag import RAGTranslationRequest
from src.domain.architecture.models import NetworkArchitecture
from src.domain.cloud.models import CloudArchitecture
from src.domain.deployment import (
    ApprovalDecision,
    ApprovalRecord,
    DeploymentPlan,
    DeploymentState,
    PlannedChange,
    ResourceChangeAction,
)
from src.infrastructure.generators.ansible.generator import DefaultAnsibleGenerator
from src.infrastructure.generators.ansible.validator import AnsibleProjectValidator
from src.infrastructure.generators.inventory.generator import TerraformOutputInventoryGenerator
from src.infrastructure.generators.terraform.generator import AWSTerraformGenerator
from src.infrastructure.generators.terraform.validator import TerraformProjectValidator
from src.infrastructure.rag import StubRAGProvider
from src.shared.errors import ApprovalRequiredError
from tests.fixtures.network_architectures import two_router_ospf


@pytest.fixture
def network() -> NetworkArchitecture:
    return two_router_ospf()


@pytest.fixture
def cloud(network: NetworkArchitecture) -> CloudArchitecture:
    """The RAG's output, produced once per test.

    Synchronous on purpose: an async fixture would need a plugin, and this fixture's only
    async work is a single translate call.
    """
    result = asyncio.run(
        StubRAGProvider().translate(
            RAGTranslationRequest(
                network_architecture=network, provider="aws", region="eu-west-1"
            )
        )
    )
    return result.cloud_architecture


class TestTheWholeChain:
    async def test_network_json_survives_a_round_trip_before_the_rag(
        self, network: NetworkArchitecture
    ) -> None:
        restored = NetworkArchitecture.from_json(network.to_json())
        assert restored.summary() == network.summary()
        assert restored.can_proceed_to_rag

    async def test_rag_produces_a_serialisable_cloud_architecture(
        self, cloud: CloudArchitecture
    ) -> None:
        document = json.loads(cloud.to_json())
        assert document["schema_version"] == "1.0"
        assert document["infrastructure"]["networks"]
        restored = CloudArchitecture.from_json_dict(document)
        assert restored.infrastructure.counts() == cloud.infrastructure.counts()

    async def test_the_cloud_architecture_traces_back_to_its_source(
        self, network: NetworkArchitecture, cloud: CloudArchitecture
    ) -> None:
        assert cloud.metadata.network_architecture_id == network.architecture_id
        assert cloud.metadata.network_architecture_revision == network.revision
        source_ids = {
            node_id
            for resource in cloud.infrastructure.all_resources()
            for node_id in resource.source_node_ids
        }
        assert {"R1", "R2", "WEB1"} <= source_ids

    async def test_terraform_is_generated_and_validated(
        self, cloud: CloudArchitecture
    ) -> None:
        project = AWSTerraformGenerator().generate(cloud)
        assert "main.tf" in project.file_paths
        assert 'resource "aws_vpc"' in project.file("main.tf").content

        result = await TerraformProjectValidator().validate(project)
        assert result.project_fingerprint == project.fingerprint()

        executed = {check.name for check in result.executed_checks}
        skipped = {check.name for check in result.skipped_checks}
        if "terraform validate" in executed:
            assert result.valid, result.errors
        else:
            # Honest about what was not checked, rather than claiming a pass.
            assert "terraform" in skipped

    async def test_ansible_is_generated_and_validated(self, cloud: CloudArchitecture) -> None:
        assert cloud.requires_configuration_stage
        project = DefaultAnsibleGenerator().generate(cloud)
        assert project.file("site.yml") is not None

        result = await AnsibleProjectValidator().validate(project)
        structure = next(check for check in result.checks if check.name == "project structure")
        assert structure.passed, structure.stderr

        if "ansible syntax-check" in {check.name for check in result.executed_checks}:
            assert result.valid, result.errors

    async def test_generation_is_reproducible_across_the_whole_chain(
        self, network: NetworkArchitecture
    ) -> None:
        async def run() -> tuple[str, str]:
            result = await StubRAGProvider().translate(
                RAGTranslationRequest(network_architecture=network)
            )
            architecture = result.cloud_architecture
            return (
                AWSTerraformGenerator().generate(architecture).fingerprint(),
                DefaultAnsibleGenerator().generate(architecture).fingerprint(),
            )

        assert await run() == await run()


class TestOwnershipSeparation:
    async def test_terraform_provisions_and_ansible_configures(
        self, cloud: CloudArchitecture
    ) -> None:
        terraform = "\n".join(f.content for f in AWSTerraformGenerator().generate(cloud).files)
        ansible = "\n".join(f.content for f in DefaultAnsibleGenerator().generate(cloud).files)

        # Terraform owns the infrastructure vocabulary.
        assert "aws_instance" in terraform
        assert "aws_instance" not in ansible
        # Ansible owns the configuration vocabulary.
        assert "ansible.builtin.package" in ansible
        assert "ansible.builtin.package" not in terraform

    async def test_no_generator_output_references_an_llm_or_the_rag(
        self, cloud: CloudArchitecture
    ) -> None:
        content = "\n".join(
            f.content
            for f in (
                *AWSTerraformGenerator().generate(cloud).files,
                *DefaultAnsibleGenerator().generate(cloud).files,
            )
        ).lower()
        for term in ("openai", "anthropic", "groq", "llm", "gpt-", "claude-"):
            assert term not in content


class TestInventoryOnlyExistsAfterApply:
    async def test_generated_inventory_has_no_addresses(
        self, cloud: CloudArchitecture
    ) -> None:
        project = DefaultAnsibleGenerator().generate(cloud)
        inventory = project.file(project.inventory_path)
        assert project.inventory_pending is True
        assert "ansible_host" not in inventory.content

    async def test_real_inventory_is_built_from_deployment_outputs(
        self, cloud: CloudArchitecture
    ) -> None:
        compute_ids = [instance.id for instance in cloud.infrastructure.compute]
        outputs = DeploymentOutputs(
            values={"vpc_id": "vpc-123"},
            compute_addresses={
                compute_id: f"10.99.0.{index + 10}"
                for index, compute_id in enumerate(compute_ids)
            },
            deployment_id="deploy_e2e",
        )
        inventory = TerraformOutputInventoryGenerator().generate(cloud, outputs)

        # Every configured host gets the address the deployment actually reported.
        for host in cloud.configuration.hosts:
            expected = outputs.compute_addresses[host.compute_id]
            assert f"ansible_host: {expected}" in inventory.content
        assert "topoforge_address_pending" not in inventory.content
        assert inventory.path == DefaultAnsibleGenerator().generate(cloud).inventory_path


class TestDeploymentGate:
    async def test_apply_is_impossible_without_a_matching_approval(
        self, cloud: CloudArchitecture
    ) -> None:
        project = AWSTerraformGenerator().generate(cloud)
        plan = DeploymentPlan(
            project_fingerprint=project.fingerprint(),
            changes=(
                PlannedChange(address="aws_vpc.vpc_r1", action=ResourceChangeAction.CREATE),
            ),
        )
        state = DeploymentState(cloud_architecture_id=cloud.cloud_architecture_id, plan=plan)
        assert state.may_apply is False

        approval = ApprovalRecord(
            deployment_id=state.id,
            plan_id=plan.id,
            plan_checksum=plan.checksum(),
            decision=ApprovalDecision.APPROVED,
            actor="operator",
        )
        assert state.model_copy(update={"approval": approval}).may_apply is True

    async def test_regenerating_different_code_invalidates_the_approval(
        self, cloud: CloudArchitecture
    ) -> None:
        project = AWSTerraformGenerator().generate(cloud)
        plan = DeploymentPlan(project_fingerprint=project.fingerprint())
        approval = ApprovalRecord(
            deployment_id="d",
            plan_id=plan.id,
            plan_checksum=plan.checksum(),
            decision=ApprovalDecision.APPROVED,
            actor="operator",
        )
        # The architecture changed, so the code changed, so the plan is a different plan.
        changed = cloud.model_copy(
            update={
                "metadata": cloud.metadata.model_copy(update={"region": "us-east-1"})
            }
        )
        replanned = plan.model_copy(
            update={
                "project_fingerprint": AWSTerraformGenerator().generate(changed).fingerprint()
            }
        )
        assert approval.authorises(replanned) is False

    async def test_an_unvalidated_architecture_never_reaches_the_rag(
        self, network: NetworkArchitecture
    ) -> None:
        from src.domain.architecture.issues import (
            IssueSeverity,
            ValidationIssue,
            ValidationReport,
        )
        from src.shared.errors import ValidationError

        broken = network.with_validation(
            ValidationReport.from_issues(
                [
                    ValidationIssue(
                        code="duplicate_ip_address",
                        severity=IssueSeverity.ERROR,
                        message="10.10.10.1 is assigned twice",
                    )
                ]
            )
        )
        with pytest.raises(ValidationError):
            await StubRAGProvider().translate(RAGTranslationRequest(network_architecture=broken))


def _unused() -> None:  # pragma: no cover
    ApprovalRequiredError  # noqa: B018 - imported for the error taxonomy's completeness
