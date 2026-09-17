# Refactor: moving IaC generation out of the RAG

**Date:** 2026-09-17
**Requirement:** the RAG stops at `cloud_architecture.json`. Terraform and Ansible are
produced by deterministic application components that never call an LLM or the RAG.

---

## 0. Honest starting position

The build is at **Phase 1 of 15**, not complete. What exists in `src/`:

```
src/shared/       errors · settings · structured logging · telemetry
src/domain/       common (provenance/geometry) · network · topology · routing · architecture
```

A grep for `terraform|ansible` across `src/**/*.py` returns **three hits, all of them
configuration field names** (`DeploymentSettings.terraform_binary`,
`DeploymentSettings.backend`, `RAGSettings.enable_ansible`). There is no generator, no RAG
adapter, no orchestration, no API and no frontend yet.

**Therefore this is 95% a forward-design change and 5% a refactor.** Nothing working is
destroyed, because the only things that exist to destroy are documents and two settings
fields. The value of doing it now is that the boundary lands before any code depends on
the wrong one.

The parts of the §31 report that concern "where Terraform generation currently happens"
are answered against the **external RAG** (`net2tf_v3`), which is where that code really
lives today.

---

## 1. Current RAG responsibilities (external `net2tf_v3`)

Its single entrypoint `compile_prompt(prompt: str, out_dir: str) -> dict` does **all** of:

| # | Stage | Module | Keep in RAG? |
| --- | --- | --- | --- |
| 1 | LLM extraction of an architecture from prose | `extractor.py` | **No** — our pipeline already produced a validated `NetworkArchitecture`. Bypassed. |
| 2 | Manual-addressing parsing | `addressing.py::enrich_with_manual_addressing` | Yes — cloud addressing knowledge. |
| 3 | Knowledge retrieval (FAISS + MiniLM + cross-encoder + metadata boost) | `retriever.py::retrieve_context` | **Yes** — this is the RAG's core. |
| 4 | Cloud reasoning / advisory plan (LLM) | `planner.py::plan_with_rag` | **Yes** — deployment pattern, connectivity mode, NAT/bastion decisions. |
| 5 | Structural validation | `validator.py::validate_architecture` | Yes. |
| 6 | **Deterministic cloud mapping** — routers→VPC domains, switches→subnets, host placement, router links, peering/TGW | `addressing.py::build_domain_plan` | **Yes** — this is *cloud architecture*, not syntax. |
| 7 | **Terraform rendering** | `terraform_builder.py::render_project` + `templates/*.j2` | **NO — extracted.** |
| 8 | **Ansible planning (LLM) and rendering** | `ansible_planner.py`, `ansible_builder.py` | **NO — extracted.** |
| 9 | `terraform fmt/init/validate` shell-out | `quality_checks.py` | No — becomes `TerraformValidator`. |
| 10 | `terraform plan/apply/destroy`, Ansible execution | `deploy_check.py` | No — becomes the executors, behind approval. |
| 11 | Plan/spec guards, response rendering | `plan_guard.py`, `spec_guard.py`, `response_renderer.py` | Partly — guard findings become `assumptions`/`warnings` on `CloudArchitecture`. |

**New RAG contract:** stages 2–6 only.

```
NetworkArchitecture ──► RAGCloudTranslator ──► CloudArchitecture
```

## 2. Where Terraform generation happens today

- `net2tf_v3/terraform_builder.py` — `render_project(architecture, templates_dir, out_dir)`,
  Jinja2 `StrictUndefined`, writes `main.tf`, `variables.tf`, `outputs.tf`,
  `terraform.tfvars.example`, `README.md`.
- `net2tf_v3/templates/*.j2` — the five templates.
- Called from `app.py::compile_prompt` step 6.
- Instance sizing heuristic (`_router_instance_type`) lives inside the builder's context
  construction — a *cloud architecture* decision currently trapped in the syntax layer.

**Migration:** the Jinja templates are a genuinely useful deterministic asset. They are
re-homed under `src/infrastructure/generators/terraform/templates/` and driven by
`CloudArchitecture` instead of the legacy `Architecture`. The sizing heuristic moves *up*
into the cloud-mapping layer (it decides an instance type, which is architecture, not
syntax). Nothing is copied from the RAG checkout at runtime.

## 3. Where Ansible generation happens today

- `net2tf_v3/ansible_planner.py` — LLM + heuristic task plan.
- `net2tf_v3/ansible_builder.py` — renders inventory and playbooks.
- `net2tf_v3/ansible_check.py` — `--syntax-check`.
- `app.py::generate_ansible_config` wires them, and — per the upstream project's own gap
  report — **passes keyword arguments `render_ansible_project` does not accept**, so this
  path is currently broken upstream.

**Migration:** the *intent* model (roles, packages, services per host) becomes the
`configuration` half of `CloudArchitecture`, which the RAG may populate. The rendering
becomes a deterministic `AnsibleGenerator`. The LLM task planner is **not** carried over:
under the new rule, configuration intent is part of cloud architecture (RAG side) and
rendering is deterministic (generator side). No LLM sits between them.

## 4. Files that must change

| File | Change |
| --- | --- |
| `docs/ARCHITECTURE.md` | New invariant, new layer map. |
| `docs/IMPLEMENTATION_PLAN.md` | Phases 11/14 rewritten; new generator phase. |
| `src/shared/config/settings.py` | `RAGSettings.enable_ansible` removed (the RAG no longer generates Ansible); new `GeneratorSettings`. |
| `src/domain/architecture/models.py` | Docstring only — it is now explicitly *network* architecture, the RAG's input. |

## 5. Files that stay unchanged

Everything else built so far: `src/shared/{errors,logging,telemetry}`,
`src/domain/{common,network,topology,routing}`, and the whole of
`src/domain/architecture` apart from a docstring. The first half of the pipeline —
image → YOLO/OCR/edges → topology → completeness → enrichment → validation →
`network_architecture.json` — is unaffected by this change and is built as originally
planned.

## 6. Proposed `CloudArchitecture` schema

New package `src/domain/cloud/`. Versioned (`schema_version: "1.0"`), provider-neutral
core with an AWS vocabulary, and a hard split between infrastructure and configuration:

```
CloudArchitecture
├── schema_version, metadata (id, source network architecture id + revision,
│     provider, region, generated_at, producers)
├── infrastructure
│   ├── networks[]        VPC / VNet: cidr, tenancy, dns
│   ├── subnets[]         network_id, cidr, availability_zone, public, purpose
│   ├── route_tables[]    + routes[] (destination, target_ref)
│   ├── gateways[]        internet | nat | transit | vpn | peering
│   ├── compute[]         instance type, image, subnet, interfaces, role tags
│   ├── databases[]       engine, version, class, storage, multi_az, subnet group
│   ├── load_balancers[]  scheme, type, listeners, targets
│   └── security[]        security groups + rules, network firewall policies
├── configuration
│   ├── hosts[]           compute_ref, connection, groups
│   ├── roles[]           name, packages, services, files, variables
│   ├── services[]        name, state, enabled
│   └── packages[]        name, version, target roles
├── relationships[]       typed edges between resources (attached_to, routes_via, …)
├── iam[]                 required roles/policies, when the mapping needs them
├── assumptions[]         what the RAG assumed, with rationale
├── warnings[]            non-blocking concerns
├── unresolved[]          fields the RAG could not determine
└── validation            ValidationReport (reused from the network domain)
```

Every resource carries `id`, `logical_name`, `provider_type` (e.g. `aws_vpc` as a *cloud*
type name, not a Terraform address), `source_node_ids` (traceability back to the network
architecture) and `Evidence` provenance. **No Terraform concepts** — no `resource`
addresses, no HCL, no module paths, no `depends_on`.

## 7. Proposed generator interfaces

`src/application/ports/generation.py`:

```python
class CodeGenerator(Protocol):
    name: str
    version: str
    def generate(self, architecture: CloudArchitecture) -> GeneratedProject: ...

class TerraformGenerator(CodeGenerator): ...   # -> TerraformProject
class AnsibleGenerator(CodeGenerator): ...     # -> AnsibleProject
class InventoryGenerator(Protocol):
    def generate(self, architecture: CloudArchitecture,
                 outputs: DeploymentOutputs) -> GeneratedFile: ...
class ProjectValidator(Protocol):
    def validate(self, project: GeneratedProject) -> GeneratorValidationResult: ...
class InfrastructureExecutor(Protocol): ...    # terraform init/validate/plan/apply
class ConfigurationExecutor(Protocol): ...     # ansible syntax-check/run
```

Domain models in `src/domain/generation/`: `GeneratedFile` (path, content, checksum,
mode), `GeneratedProject` (files, warnings, generator name/version, entrypoint),
`TerraformProject`, `AnsibleProject`, `GeneratorWarning`, `GeneratorError`,
`GeneratorValidationResult`.

Generators are **pure functions of `CloudArchitecture`**: same input, byte-identical
output. That is what makes golden-file tests possible.

## 8. Updated LangGraph nodes and edges

Unchanged: `detect_input_mode`, `preprocess_image`, `yolo`, `ocr`, `edges`,
`spatial_association`, `topology_builder`, `prompt_llm`, `generate_topology`,
`completeness_analyzer`, `network_enrichment`, `network_validator`.

Changed / new, replacing the old single `deployment_generation` node:

```
network_validator
      ▼
  rag_translate            → cloud_architecture (RAG ENDS HERE)
      ▼
  validate_cloud_architecture
      ├──────────────┬──────────────┐
      ▼              ▼              │  (fan-out, deterministic, no LLM)
generate_terraform  generate_ansible
      ▼              ▼
validate_terraform  validate_ansible
      └──────┬───────┘
             ▼
        deployment_review
             ▼
        terraform_plan
             ▼
      ── INTERRUPT: user approval ──
        reject ▼        ▼ approve
          END           terraform_apply
                             ▼
                        terraform_outputs
                             ▼
                      generate_inventory
                             ▼
                        ansible_execute
                             ▼
                            END
```

Ansible generation is skipped by a conditional edge when
`cloud_architecture.configuration` is empty, so a pure-infrastructure translation does not
produce an empty playbook.

State additions: `cloud_architecture`, `cloud_validation`, `terraform_project_ref`,
`terraform_validation`, `terraform_plan_ref`, `ansible_project_ref`, `ansible_validation`,
`terraform_outputs`, `ansible_inventory_ref`, `deployment_approved`, `deployment_status`.
Generated file **content** lives in object storage; state holds URIs.

## 9. Migration risks

| Risk | Mitigation |
| --- | --- |
| The legacy `DomainPlan` is the only place the RAG's cloud mapping knowledge exists, and it is shaped for the legacy Terraform templates. | A dedicated `CloudArchitectureAssembler` translates `DomainPlan` + advisory plan + retrieved context into `CloudArchitecture`, with an explicit `unmapped[]` report for anything that does not fit. Tested against the RAG's own `evaluations/rag_cases.jsonl`. |
| Re-homed Jinja templates drift from upstream. | They are driven by a *different* model (`CloudArchitecture`), so they are a fork by design, recorded in ADR-0005. Upstream templates stay untouched. |
| The instance-sizing heuristic is documented upstream as invalid. | It moves into the cloud-mapping layer where it is one overridable policy object, and the upstream caveat is carried into `warnings[]` rather than silently trusted. |
| Losing the upstream Ansible LLM planner could lose capability. | It was already broken upstream (bad keyword arguments). Its capability set is re-expressed as the typed `configuration` half of the schema, which is strictly more inspectable. |
| Two competing pipelines during migration. | There is only one: nothing consumed the RAG yet. `LegacyNet2TFRAGProvider` is written once, against the new contract. |
| The RAG's `compile_prompt` is the only tested upstream path; calling its stages individually is new. | The adapter calls the same stages in the same order as `compile_prompt`, minus rendering, and asserts the presence of every symbol it uses at startup. Contract-tested with a stub and integration-tested against a real checkout. |

## 10. Migration steps (executed incrementally, tests at each step)

1. Update `docs/ARCHITECTURE.md` + `docs/IMPLEMENTATION_PLAN.md` to the new invariant. ✅
2. Add `src/domain/cloud/` (`CloudArchitecture` and friends) + unit tests.
3. Add `src/domain/generation/` (`GeneratedFile`/`GeneratedProject`/…) + unit tests.
4. Add `src/application/ports/` including the generator, validator and executor ports.
5. Implement `TerraformGenerator` + mappings + templates + golden-file tests.
6. Implement `AnsibleGenerator` + mappings + templates + golden-file tests.
7. Implement `InventoryGenerator` (post-apply outputs only) + tests.
8. Implement the RAG adapter producing `CloudArchitecture`, never files.
9. Wire the LangGraph graph with the new nodes and the approval interrupt.
10. Validators and executors; API, workers, frontend tabs.
