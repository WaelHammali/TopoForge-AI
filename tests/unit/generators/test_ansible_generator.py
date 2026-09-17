"""Deterministic Ansible generation, and the inventory honesty rule."""

from __future__ import annotations

import pytest

from src.application.ports.generation import DeploymentOutputs
from src.domain.cloud import CloudArchitecture, Configuration
from src.domain.generation.models import ProjectKind
from src.infrastructure.generators.ansible.generator import (
    INVENTORY_PLACEHOLDER_PATH,
    DefaultAnsibleGenerator,
)
from src.infrastructure.generators.ansible.yaml_writer import dump_yaml
from src.infrastructure.generators.inventory.generator import (
    TerraformOutputInventoryGenerator,
)
from src.shared.errors import DeploymentError, ValidationError
from tests.fixtures.cloud_architectures import full_cloud, minimal_cloud


@pytest.fixture
def generator() -> DefaultAnsibleGenerator:
    return DefaultAnsibleGenerator()


class TestYAMLWriter:
    def test_key_order_is_preserved(self) -> None:
        assert dump_yaml({"z": 1, "a": 2}, document_start=False) == "z: 1\na: 2\n"

    def test_yaml_1_1_booleans_are_quoted(self) -> None:
        # Unquoted "yes" would be read as True by Ansible.
        assert '"yes"' in dump_yaml({"answer": "yes"}, document_start=False)
        assert '"no"' in dump_yaml({"answer": "no"}, document_start=False)

    def test_numeric_looking_strings_are_quoted(self) -> None:
        assert dump_yaml({"v": "123"}, document_start=False) == 'v: "123"\n'
        assert dump_yaml({"v": 123}, document_start=False) == "v: 123\n"

    def test_jinja_expressions_are_quoted(self) -> None:
        assert dump_yaml({"v": "{{ x }}"}, document_start=False) == 'v: "{{ x }}"\n'

    def test_human_phrases_stay_unquoted(self) -> None:
        assert dump_yaml({"name": "Install nginx"}, document_start=False) == (
            "name: Install nginx\n"
        )

    def test_colons_inside_a_phrase_force_quoting(self) -> None:
        assert dump_yaml({"name": "note: careful"}, document_start=False) == (
            'name: "note: careful"\n'
        )

    def test_empty_collections(self) -> None:
        assert dump_yaml({"a": [], "b": {}}, document_start=False) == "a: []\nb: {}\n"

    def test_header_lines_have_no_trailing_whitespace(self) -> None:
        for line in dump_yaml({"a": 1}, header=("x", "")).splitlines():
            assert line == line.rstrip()


class TestDeterminism:
    def test_two_runs_produce_identical_bytes(self, generator: DefaultAnsibleGenerator) -> None:
        first = generator.generate(full_cloud())
        second = generator.generate(full_cloud())
        assert first.fingerprint() == second.fingerprint()


class TestProjectShape:
    def test_expected_files(self, generator: DefaultAnsibleGenerator) -> None:
        paths = set(generator.generate(full_cloud()).file_paths)
        assert {"site.yml", "ansible.cfg", INVENTORY_PLACEHOLDER_PATH} <= paths
        assert "roles/nginx/tasks/main.yml" in paths
        assert "roles/nginx/handlers/main.yml" in paths
        assert "roles/base/meta/main.yml" in paths

    def test_kind_and_metadata(self, generator: DefaultAnsibleGenerator) -> None:
        project = generator.generate(full_cloud())
        assert project.kind is ProjectKind.ANSIBLE
        assert set(project.role_names) == {"base", "nginx", "frr"}
        assert set(project.group_names) == {"bastion", "web", "routers"}


class TestRoleMapping:
    def test_packages_become_one_package_task(self, generator: DefaultAnsibleGenerator) -> None:
        tasks = generator.generate(full_cloud()).file("roles/base/tasks/main.yml").content
        assert "ansible.builtin.package:" in tasks
        assert "- curl" in tasks
        assert "- jq" in tasks

    def test_services_become_service_tasks_and_handlers(
        self, generator: DefaultAnsibleGenerator
    ) -> None:
        project = generator.generate(full_cloud())
        assert "ansible.builtin.service:" in project.file("roles/nginx/tasks/main.yml").content
        assert "restart nginx" in project.file("roles/nginx/handlers/main.yml").content

    def test_files_are_copied_and_notify_their_service(
        self, generator: DefaultAnsibleGenerator
    ) -> None:
        project = generator.generate(full_cloud())
        tasks = project.file("roles/nginx/tasks/main.yml").content
        assert "dest: /etc/nginx/conf.d/app.conf" in tasks
        assert "notify:" in tasks
        assert project.file("roles/nginx/files/etc_nginx_conf.d_app.conf") is not None

    def test_role_variables_become_defaults(self, generator: DefaultAnsibleGenerator) -> None:
        assert (
            "worker_processes: auto"
            in generator.generate(full_cloud()).file("roles/nginx/defaults/main.yml").content
        )

    def test_role_requirements_become_meta_dependencies(
        self, generator: DefaultAnsibleGenerator
    ) -> None:
        meta = generator.generate(full_cloud()).file("roles/nginx/meta/main.yml").content
        assert "dependencies:" in meta
        assert "- base" in meta


class TestPlaybook:
    def test_one_play_per_group(self, generator: DefaultAnsibleGenerator) -> None:
        site = generator.generate(full_cloud()).file("site.yml").content
        assert "hosts: bastion" in site
        assert "hosts: web" in site
        assert "hosts: routers" in site

    def test_roles_are_dependency_ordered(self, generator: DefaultAnsibleGenerator) -> None:
        site = generator.generate(full_cloud()).file("site.yml").content
        web_play = site.split("hosts: web")[1]
        assert web_play.index("- base") < web_play.index("- nginx")

    def test_group_and_host_variables(self, generator: DefaultAnsibleGenerator) -> None:
        project = generator.generate(full_cloud())
        assert "http_port: 80" in project.file("group_vars/web.yml").content
        assert "app_port: 8080" in project.file("host_vars/app1.yml").content
        assert "ansible_python_interpreter" in project.file("group_vars/all.yml").content


class TestOwnershipBoundary:
    """Ansible configures; it never provisions. No overlap with Terraform."""

    def test_no_cloud_provisioning_modules_are_generated(
        self, generator: DefaultAnsibleGenerator
    ) -> None:
        content = "\n".join(f.content for f in generator.generate(full_cloud()).files)
        for module in ("amazon.aws.ec2_instance", "amazon.aws.ec2_vpc_net", "community.aws"):
            assert module not in content

    def test_no_infrastructure_sizing_or_addressing_leaks_in(
        self, generator: DefaultAnsibleGenerator
    ) -> None:
        content = "\n".join(f.content for f in generator.generate(full_cloud()).files)
        assert "t3.small" not in content
        assert "10.0.1.0/24" not in content


class TestInventoryIsPendingBeforeApply:
    def test_placeholder_has_no_addresses(self, generator: DefaultAnsibleGenerator) -> None:
        project = generator.generate(full_cloud())
        inventory = project.file(INVENTORY_PLACEHOLDER_PATH).content
        assert project.inventory_pending is True
        assert "ansible_host" not in inventory
        assert "topoforge_address_pending: true" in inventory

    def test_placeholder_says_why_it_is_empty(self, generator: DefaultAnsibleGenerator) -> None:
        inventory = generator.generate(full_cloud()).file(INVENTORY_PLACEHOLDER_PATH).content
        assert "PLACEHOLDER INVENTORY" in inventory
        assert "terraform apply" in inventory

    def test_groups_are_still_described(self, generator: DefaultAnsibleGenerator) -> None:
        inventory = generator.generate(full_cloud()).file(INVENTORY_PLACEHOLDER_PATH).content
        assert "web:" in inventory
        assert "app1:" in inventory


class TestRejections:
    def test_architecture_without_configuration_is_refused(
        self, generator: DefaultAnsibleGenerator
    ) -> None:
        # minimal_cloud has no configuration: the graph skips the Ansible node entirely.
        assert minimal_cloud().requires_configuration_stage is False
        with pytest.raises(ValidationError, match="no configuration"):
            generator.generate(minimal_cloud())

    def test_unresolved_roles_are_warned_about_not_invented(
        self, generator: DefaultAnsibleGenerator
    ) -> None:
        architecture = full_cloud()
        stripped = architecture.model_copy(
            update={
                "configuration": architecture.configuration.model_copy(
                    update={"roles": architecture.configuration.roles[:1]}
                )
            }
        )
        project = generator.generate(stripped)
        assert any(warning.code == "unresolved_roles" for warning in project.warnings)
        assert project.file("roles/nginx/tasks/main.yml") is None

    def test_hosts_without_a_group_are_reported(
        self, generator: DefaultAnsibleGenerator
    ) -> None:
        architecture = full_cloud()
        hosts = tuple(
            host.model_copy(update={"groups": ()}) if host.name == "app1" else host
            for host in architecture.configuration.hosts
        )
        modified = architecture.model_copy(
            update={"configuration": architecture.configuration.model_copy(update={"hosts": hosts})}
        )
        project = generator.generate(modified)
        assert any(warning.code == "hosts_without_group" for warning in project.warnings)


class TestInventoryGeneratorAfterApply:
    def test_real_addresses_are_used(self) -> None:
        architecture = full_cloud()
        outputs = DeploymentOutputs(
            values={"c_app1_private_ip": "10.0.2.15"},
            compute_addresses={"c-bastion": "52.1.2.3", "c-app1": "10.0.2.15", "c-r1": "10.0.1.9"},
            deployment_id="deploy_1",
        )
        inventory = TerraformOutputInventoryGenerator().generate(architecture, outputs).content
        assert "ansible_host: 10.0.2.15" in inventory
        assert "ansible_host: 52.1.2.3" in inventory

    def test_bastion_becomes_a_proxy_command(self) -> None:
        architecture = full_cloud()
        outputs = DeploymentOutputs(
            compute_addresses={"c-bastion": "52.1.2.3", "c-app1": "10.0.2.15"},
        )
        inventory = TerraformOutputInventoryGenerator().generate(architecture, outputs).content
        assert "ProxyCommand" in inventory
        assert "ec2-user@52.1.2.3" in inventory

    def test_a_host_with_no_reported_address_is_marked_unreachable_not_guessed(self) -> None:
        architecture = full_cloud()
        outputs = DeploymentOutputs(compute_addresses={"c-bastion": "52.1.2.3"})
        inventory = TerraformOutputInventoryGenerator().generate(architecture, outputs).content
        assert "topoforge_unreachable_reason" in inventory
        assert "app1" in inventory
        # No fabricated address anywhere.
        assert "10.0.2." not in inventory

    def test_generating_an_inventory_before_apply_is_refused(self) -> None:
        with pytest.raises(DeploymentError, match="no outputs"):
            TerraformOutputInventoryGenerator().generate(full_cloud(), DeploymentOutputs())

    def test_the_real_inventory_replaces_the_placeholder_path(self) -> None:
        outputs = DeploymentOutputs(compute_addresses={"c-app1": "10.0.2.15"})
        generated = TerraformOutputInventoryGenerator().generate(full_cloud(), outputs)
        assert generated.path == INVENTORY_PLACEHOLDER_PATH

    def test_inventory_is_deterministic(self) -> None:
        outputs = DeploymentOutputs(compute_addresses={"c-app1": "10.0.2.15"})
        first = TerraformOutputInventoryGenerator().generate(full_cloud(), outputs)
        second = TerraformOutputInventoryGenerator().generate(full_cloud(), outputs)
        assert first.checksum == second.checksum
