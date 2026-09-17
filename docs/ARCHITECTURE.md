# TopoForge-AI — Target Architecture

**Status:** authoritative. Every module in this repository is answerable to this document.

---

## 1. The invariant

Two rules govern the whole system. Neither is negotiable and no shortcut around either is
permitted.

**Rule 1 — everything converges on `network_architecture.json`.**
**Rule 2 — the RAG consumes it, produces `cloud_architecture.json`, and then stops.**

```
IMAGE ──► YOLO + OCR + EDGE DETECTION ──► TOPOLOGY BUILDER ─┐
                                                            │
PROMPT ─► LLM ─────────────────────────► TOPOLOGY ──────────┤
                                                            ▼
                                            COMPLETENESS ANALYSIS
                                                            ▼
                                             NETWORK ENRICHMENT
                                                            ▼
                                             NETWORK VALIDATION
                                                            ▼
                                     ►  network_architecture.json  ◄
                                                            ▼
                                             R A G   (translation)
                                                            ▼
                                     ►   cloud_architecture.json   ◄
                                                            │
                                    ┌───────────────────────┴───────────┐
                                    ▼                                   ▼
                          TerraformGenerator                   AnsibleGenerator
                          (deterministic)                      (deterministic)
                                    ▼                                   ▼
                            TerraformProject                    AnsibleProject
                                    ▼                                   ▼
                          terraform validate                  ansible syntax-check
                                    └───────────────┬───────────────────┘
                                                    ▼
                                            DEPLOYMENT REVIEW
                                                    ▼
                                             TERRAFORM PLAN
                                                    ▼
                                             USER APPROVAL
                                                    ▼
                                             TERRAFORM APPLY
                                                    ▼
                                            TERRAFORM OUTPUTS
                                                    ▼
                                           INVENTORY GENERATOR
                                                    ▼
                                             ANSIBLE EXECUTE
```

Consequences, enforced structurally and by test:

- `network_architecture.json` is the **only** input the RAG ever receives.
- `cloud_architecture.json` is the **only** output the RAG ever produces. The RAG writes
  no files, no HCL, no YAML, no shell.
- The generators are **pure deterministic functions** of `cloud_architecture.json`. They
  never call an LLM, never call the RAG, and produce byte-identical output for identical
  input — which is what makes golden-file testing possible.
- Generated code never flows back into the RAG. Syntax problems surface as structured
  validator errors, not as an LLM repair loop. AI-assisted remediation, if ever added, is
  an explicitly opt-in separate feature.
- The image pipeline cannot deploy. The prompt LLM cannot deploy. Neither can call the RAG
  directly — both terminate at a validated `NetworkArchitecture`.
- Contract tests (`tests/contract/test_pipeline_invariant.py`) assert the graph topology
  itself: the only edge into the RAG node comes from network validation; the only edge out
  of it goes to cloud-architecture validation; every generator node is reachable only from
  there; and no generator module imports an LLM or RAG symbol.

## 2. System context

```
                         USER (browser)
                              │
                    Next.js 15 · TypeScript · Tailwind · shadcn/ui · React Flow
                              │  REST + Server-Sent Events
                              ▼
                     FastAPI  (thin controllers, no business logic)
                              │
                 ┌────────────┴────────────┐
                 ▼                         ▼
        Application use cases        Job queue (JobQueue port)
                 │                         │
                 ▼                         ▼
        LangGraph orchestrator       Workers: vision · rag · deployment
                 │
     ┌───────────┼───────────┬──────────────┬──────────────┐
     ▼           ▼           ▼              ▼              ▼
  Vision      LLM         RAG          Persistence      Storage
 (YOLO/OCR/  (OpenAI/   (net2tf_v3    (PostgreSQL     (local FS /
  edges)     Anthropic)  wrapped)      + JSONB)          S3)
```

The API process holds **no** AWS credentials and **no** Terraform binary. Only the deployment
worker does.

## 3. Layering (Clean / Hexagonal)

```
src/
  domain/          pure Python + pydantic. Zero infrastructure imports.
  application/     use cases, services, ports (interfaces), workflows.
  infrastructure/  adapters implementing the ports. All heavy deps live here.
  shared/          config, logging, telemetry, errors — dependency-free utilities.
```

**Dependency rule:** arrows point inward only. `domain` imports nothing from
`application` or `infrastructure`. `application` imports `domain` and its own ports, never a
concrete adapter. Only the composition root (`src/infrastructure/container.py` and
`apps/api/main.py`) knows which adapter is bound to which port.

Enforced by `tests/unit/test_layering.py`, which walks the AST of every module under
`src/domain` and `src/application` and fails on a forbidden import
(`fastapi`, `sqlalchemy`, `ultralytics`, `cv2`, `torch`, `openai`, `anthropic`, `boto3`,
`langgraph`, `paddleocr`, `faiss`, …).

### 3.1 Domain

| Package | Contents |
| --- | --- |
| `domain/network/` | `IPAddressSpec`, `SubnetSpec`, `Interface`, `Network`, `VLAN`, `MACAddress` value objects; canonical IP/CIDR arithmetic on `ipaddress`. |
| `domain/topology/` | `Node`, `Edge`, `NodeType`, `Topology`, `BoundingBox`, `Evidence`, `Provenance`. |
| `domain/routing/` | `OSPFConfiguration`, `OSPFArea`, `StaticRoute`, `RoutingPlan`, `RoutingProtocol`. |
| `domain/architecture/` | `NetworkArchitecture` (the canonical *network* aggregate — the RAG's input), `ArchitectureMetadata`, `ArchitectureRevision`, `UnresolvedField`, `ValidationIssue`, `ValidationReport`, `MissingInformation`, `EnrichmentOption`, `ClarificationQuestion`. |
| `domain/cloud/` | `CloudArchitecture` (the RAG's *output*), split into `infrastructure` (networks, subnets, route tables, gateways, compute, databases, load balancers, security) and `configuration` (hosts, roles, services, packages), plus relationships, IAM, assumptions, warnings. Provider-neutral core with an AWS vocabulary. Contains **no** Terraform or Ansible concepts. |
| `domain/generation/` | `GeneratedFile`, `GeneratedProject`, `TerraformProject`, `AnsibleProject`, `GeneratorWarning`, `GeneratorError`, `GeneratorValidationResult`. |
| `domain/deployment/` | `DeploymentArtifact`, `DeploymentPlan`, `DeploymentState`, `ApprovalRecord`, `DeploymentTarget`, `DeploymentOutputs`. |

The domain also owns the **validators** (`domain/architecture/validators/`), because network
correctness is domain knowledge, not infrastructure.

### 3.2 Ports (`application/ports/`)

`ObjectDetector` · `OCRProvider` · `ConnectorDetector` · `ImagePreprocessor` ·
`SpatialAssociator` · `LLMProvider` · `RAGProvider` (network → cloud translation only) ·
`CloudArchitectureValidator` · `CodeGenerator` / `TerraformGenerator` / `AnsibleGenerator` ·
`InventoryGenerator` · `ProjectValidator` · `InfrastructureExecutor` ·
`ConfigurationExecutor` · `ObjectStorage` · `JobQueue` · `EventPublisher` · `ModelLoader` ·
`Clock` · `IdGenerator` · and the repositories
`ProjectRepository`, `AssetRepository`, `ArchitectureRepository`, `JobRepository`,
`DeploymentRepository`, `AuditRepository`.

Every port is an ABC with a matching **contract test suite** that any implementation must pass.

### 3.3 Infrastructure

| Adapter | Port | Notes |
| --- | --- | --- |
| `vision/yolo/YOLOObjectDetector` | `ObjectDetector` | Ultralytics, lazy import; classes read from the weights. |
| `vision/yolo/StubObjectDetector` | `ObjectDetector` | Fixture-driven; used until weights are supplied and in CI. |
| `vision/ocr/PaddleOCRProvider` | `OCRProvider` | Lazy; replaceable. |
| `vision/ocr/TesseractOCRProvider` | `OCRProvider` | Alternate. |
| `vision/edges/HoughConnectorDetector` | `ConnectorDetector` | OpenCV mask → skeleton → endpoints → arrowheads. |
| `vision/spatial/GeometricSpatialAssociator` | `SpatialAssociator` | Deterministic geometry first; LLM tie-break only on ambiguity. |
| `llm/openai/OpenAIProvider`, `llm/anthropic/AnthropicProvider` | `LLMProvider` | Structured output → pydantic validation before acceptance. |
| `rag/LegacyNet2TFRAGProvider` | `RAGProvider` | Wraps `net2tf_v3`'s retrieval, advisory planning and deterministic cloud mapping. Its Terraform and Ansible rendering are **not** invoked. |
| `generators/terraform/` | `TerraformGenerator` | Deterministic Jinja2 rendering from `CloudArchitecture`, with a centralised resource mapping table. No LLM. |
| `generators/ansible/` | `AnsibleGenerator` | Deterministic roles/playbooks/group_vars from the `configuration` half. No LLM. |
| `generators/inventory/` | `InventoryGenerator` | Runs only *after* apply, from real Terraform outputs. |
| `deployment/TerraformExecutor` | `InfrastructureExecutor` | `fmt`/`init`/`validate`/`plan` always; `apply` only with an approval token. |
| `deployment/AnsibleExecutor` | `ConfigurationExecutor` | `--syntax-check` always; run only after apply. |
| `persistence/` | repositories | SQLAlchemy 2.0 async + Alembic; JSONB for architecture documents. |
| `storage/LocalObjectStorage`, `storage/S3ObjectStorage` | `ObjectStorage` | |
| `orchestration/langgraph/` | — | Graphs, state, checkpointers, node wrappers. |

## 4. Orchestration

LangGraph coordinates; it does not compute. Every node is a thin adapter that calls an
application service and writes typed results into `NetworkWorkflowState`.

```
MainWorkflowGraph
├── ImageExtractionSubgraph      preprocess → (yolo ‖ ocr ‖ edges) → spatial → topology
├── PromptArchitectureSubgraph   prompt_llm → generate_topology
├── NetworkEnrichmentSubgraph    completeness → [interrupt] → enrichment → validation
└── RAGDeploymentSubgraph        rag → deployment_generation → deployment_validation →
                                 deployment_plan → [interrupt] → approve|reject
```

State is a `TypedDict` carrying **references, never bytes** — `image_uri`, not image data.
Checkpointing: `MemorySaver` (tests) → `SqliteSaver` (dev) → `PostgresSaver` (prod), selected
by configuration. Thread id is `f"{project_id}:{architecture_id}"` so a workflow survives a
process restart, a clarification pause, and an approval pause.

Full detail: [`docs/LANGGRAPH_WORKFLOW.md`](LANGGRAPH_WORKFLOW.md).

## 5. The canonical contract

`architecture.json` — versioned, strongly typed, identical for both input modes. Every field
that came from a machine carries `Evidence` (source, confidence, bbox, extractor version).
Specified in [`docs/ARCHITECTURE_JSON_SCHEMA.md`](ARCHITECTURE_JSON_SCHEMA.md).

Architectures are **immutable revisions**. A correction, an enrichment, a manual JSON edit, or
a conversational modification creates revision *n+1* with a recorded `change_reason` and a
diff. Revision history is a product feature stored in PostgreSQL, independent of LangGraph
checkpoints (which are an orchestration concern and may be pruned).

## 6. RAG integration

The existing `net2tf_v3` project is wrapped, never rewritten — but only the half of it that
is genuinely cloud reasoning is invoked:

| Invoked | Not invoked |
| --- | --- |
| `enrich_with_manual_addressing` — addressing knowledge | `extractor.py` — we already have a validated architecture |
| `retrieve_context` — FAISS + rerank over the KB | `terraform_builder.render_project` — syntax |
| `plan_with_rag` — advisory cloud plan | `ansible_planner` / `ansible_builder` — syntax |
| `validate_architecture` | `quality_checks` / `deploy_check` — execution |
| `build_domain_plan` — routers→VPCs, switches→subnets, host placement, peering/TGW | |

Because its deterministic compiler parses the *original prose*, the adapter pairs the
canonical network architecture with a **deterministically rendered descriptor** (a pure
template — no LLM) that expresses exactly the facts the compiler reads. A
`CloudArchitectureAssembler` then turns the resulting `DomainPlan` + advisory plan +
retrieved context into a typed `CloudArchitecture`. Loss at either boundary is computed and
reported as `unmapped[]` — never hidden.

Detail: [`docs/RAG_INTEGRATION.md`](RAG_INTEGRATION.md),
[`docs/CLOUD_ARCHITECTURE_SCHEMA.md`](CLOUD_ARCHITECTURE_SCHEMA.md), ADR-0004, ADR-0005.

## 6a. Generation

`cloud_architecture.json` fans out to independent deterministic generators:

```
                      ┌── TerraformGenerator  → TerraformProject  (provisioning)
cloud_architecture ───┼── AnsibleGenerator    → AnsibleProject    (configuration)
                      └── future: CloudFormation / Pulumi / Bicep
```

Terraform provisions; Ansible configures. Ownership never overlaps: a concern belongs to
exactly one of them. Ansible's inventory is *not* generated up-front, because real
addresses exist only after apply — `InventoryGenerator` runs on Terraform outputs.

Artifacts are stored at `generated/{architecture_id}/{revision}/` in object storage, which
is the business source of truth; local paths are a working directory, never authority.

## 7. Deployment safety

```
RAG output → artifacts → fmt/init/validate → plan → PERSISTED APPROVAL → apply
```

- `apply` is unreachable without an `ApprovalRecord` row (actor, timestamp, plan checksum).
- The plan checksum is re-verified at apply time; a changed plan invalidates the approval.
- `-auto-approve` is never emitted by TopoForge code except against a saved plan file that a
  human already approved.
- The API process cannot execute Terraform. Only `workers/deployment` can.
- Translation (image/prompt → `architecture.json`) works with **no AWS credentials at all**.

## 8. Cross-cutting

**Errors** — a single `TopoForgeError` tree; infrastructure exceptions are translated at the
adapter boundary. Nothing is swallowed.
**Logging** — structured JSON, correlation ids (`request_id`, `project_id`, `architecture_id`,
`workflow_id`, `job_id`) propagated through a `contextvars` context.
**Telemetry** — OpenTelemetry-shaped `Tracer`/`Meter` facades with a no-op default; stage
durations recorded for every pipeline step.
**Security** — uploads validated by extension, sniffed MIME, magic bytes, size, and decoded
dimensions; never executed. Secrets only from environment; redacted in logs.
**Config** — one typed `Settings` tree (pydantic-settings), `.env.example` committed, secrets
never.

## 9. Scaling

A modular monolith (domain + application + API) plus independently scalable workers. YOLO
scales separately from API traffic. `JobQueue` is a port: in-process for dev, SQS in
production, with no domain change.
