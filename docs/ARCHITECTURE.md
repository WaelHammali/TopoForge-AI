# TopoForge-AI — Target Architecture

**Status:** authoritative. Every module in this repository is answerable to this document.

---

## 1. The invariant

One rule governs the whole system. It is not negotiable and no shortcut around it is permitted.

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
                                            ►  architecture.json  ◄
                                                            ▼
                                                        R A G
                                                            ▼
                                                 DEPLOYMENT LOGIC
                                                            ▼
                                              VALIDATION / PLAN
                                                            ▼
                                                 USER APPROVAL
                                                            ▼
                                                          AWS
```

Consequences, enforced structurally and by test:

- `architecture.json` is the **only** input the RAG ever receives (via its adapter).
- The RAG is the **last** AI component before infrastructure. Nothing intelligent sits between
  `architecture.json` and the RAG, and nothing bypasses the RAG.
- The image pipeline cannot deploy. The prompt LLM cannot deploy. Neither can call the RAG
  directly — both terminate at a validated `NetworkArchitecture`.
- A contract test (`tests/contract/test_pipeline_invariant.py`) asserts the graph topology
  itself: the only edge into the RAG node comes from the validation node, and the only path to
  deployment comes from the RAG node.

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
| `domain/architecture/` | `NetworkArchitecture` (the canonical aggregate), `ArchitectureMetadata`, `ArchitectureRevision`, `UnresolvedField`, `ValidationIssue`, `ValidationReport`, `MissingInformation`, `EnrichmentOption`, `ClarificationQuestion`. |
| `domain/deployment/` | `DeploymentArtifact`, `DeploymentPlan`, `DeploymentState`, `ApprovalRecord`, `DeploymentTarget`. |

The domain also owns the **validators** (`domain/architecture/validators/`), because network
correctness is domain knowledge, not infrastructure.

### 3.2 Ports (`application/ports/`)

`ObjectDetector` · `OCRProvider` · `ConnectorDetector` · `ImagePreprocessor` ·
`SpatialAssociator` · `LLMProvider` · `RAGProvider` · `DeploymentProvider` · `ObjectStorage` ·
`JobQueue` · `EventPublisher` · `ModelLoader` · `Clock` · `IdGenerator` · and the repositories
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
| `rag/LegacyNet2TFRAGProvider` | `RAGProvider` | Wraps `net2tf_v3` unchanged. |
| `deployment/TerraformDeploymentProvider` | `DeploymentProvider` | `fmt`/`init`/`validate`/`plan` always; `apply` only with an approval token. |
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

The existing `net2tf_v3` project is wrapped, never rewritten. Because its deterministic
compiler parses the *original prose*, the adapter pairs the canonical architecture with a
**deterministically rendered descriptor** (a pure template — no LLM) that expresses exactly the
facts the compiler reads: base CIDR, `SW = CIDR` bindings, bastion/public/NAT intent, firewall
mode, addressing authorization. Loss at the boundary is computed and returned as `unmapped[]`.
Detail and rationale: [`docs/RAG_INTEGRATION.md`](RAG_INTEGRATION.md), ADR-0004.

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
