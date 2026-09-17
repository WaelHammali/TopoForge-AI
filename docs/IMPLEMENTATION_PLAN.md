# Implementation Plan

Phase-ordered. Each phase lists the exact files created and the tests that prove it.
Checked boxes are complete in this repository.

---

## Phase 0 — Discovery ✅
- `docs/CURRENT_SYSTEM_ANALYSIS.md`, `docs/ARCHITECTURE.md`, `docs/IMPLEMENTATION_PLAN.md`

## Phase 1 — Domain foundation
```
src/shared/errors/__init__.py            TopoForgeError tree
src/shared/config/settings.py            typed Settings (pydantic-settings)
src/shared/logging/                      structured logging + correlation context
src/shared/telemetry/                    tracer/meter facades, stage timing
src/domain/common/                       Provenance, Evidence, Confidence, BoundingBox
src/domain/network/                      IPAddressSpec, SubnetSpec, Interface, Network, VLAN
src/domain/topology/                     Node, Edge, NodeType, Topology
src/domain/routing/                      OSPFConfiguration, OSPFArea, StaticRoute, RoutingPlan
src/domain/architecture/models.py        NetworkArchitecture + metadata + revisions
src/domain/architecture/issues.py        ValidationIssue, UnresolvedField, MissingInformation,
                                         EnrichmentOption, ClarificationQuestion
src/domain/deployment/                   DeploymentArtifact/Plan/State, ApprovalRecord
tests/unit/domain/                       value objects, revisions, schema round-trip, layering
```

## Phase 2 — Ports
```
src/application/ports/{vision,llm,rag,deployment,storage,jobs,events,repositories,models}.py
tests/contract/                          reusable contract suites per port
```

## Phase 3 — YOLO
```
src/infrastructure/vision/yolo/{detector.py,stub.py,model_loader.py}
src/infrastructure/models/{registry.py,loader.py}
models/README.md                         # INTEGRATION POINT: weights drop location
tests/integration/vision/test_yolo_adapter.py   (skipped without weights)
tests/contract/test_object_detector_contract.py
```

## Phase 4 — OCR + network text parsing
```
src/infrastructure/vision/ocr/{paddle.py,tesseract.py,stub.py}
src/domain/network/parsing.py            deterministic IP/CIDR/mask/interface/VLAN parsing
tests/unit/domain/test_network_parsing.py
```

## Phase 5 — Connectors
```
src/infrastructure/vision/edges/{hough.py,skeleton.py,stub.py}
src/application/ports/vision.py::ConnectorDetector
tests/unit/vision/test_connector_geometry.py
```

## Phase 6 — Topology
```
src/infrastructure/vision/spatial/geometric.py
src/application/services/topology_builder.py
tests/unit/application/test_spatial_association.py, test_topology_builder.py
```

## Phase 7 — Completeness + enrichment foundation
```
src/application/services/completeness_analyzer.py
src/application/services/clarification/{policy.py,default_policy.py}
src/application/services/enrichment.py
tests/unit/application/test_completeness_analyzer.py
```

## Phase 8 — Validation
```
src/domain/architecture/validators/*.py  (ip, subnet, topology, interface, duplicate,
                                          connectivity, routing, ospf, static_route, vlan)
src/domain/architecture/validation_runner.py
tests/unit/domain/test_validators.py
```

## Phase 9 — Prompt mode
```
src/infrastructure/llm/{base.py,openai/,anthropic/,stub.py,factory.py}
src/application/services/prompt_architecture.py
tests/unit/application/test_prompt_architecture.py
tests/contract/test_llm_provider_contract.py
```

## Phase 10 — LangGraph
```
src/infrastructure/orchestration/langgraph/{state.py,nodes/,graphs/,checkpointing.py,runner.py}
tests/unit/orchestration/test_routing_functions.py
tests/contract/test_pipeline_invariant.py     ← guards the non-negotiable rule
tests/e2e/test_prompt_to_architecture.py
```

## Phase 11 — RAG integration (network → cloud translation ONLY)
```
external/rag/README.md                   # INTEGRATION POINT: vendored net2tf_v3
scripts/sync_external_rag.sh
src/domain/cloud/                        CloudArchitecture: infrastructure | configuration
src/infrastructure/rag/{loader.py,descriptor.py,network_mapper.py,cloud_assembler.py,
                        provider.py,stub.py}
tests/unit/cloud/test_cloud_architecture.py
tests/unit/rag/test_descriptor_renderer.py, test_cloud_assembler.py
tests/integration/rag/test_legacy_provider.py  (skipped without the vendored RAG)
```
The RAG's Terraform and Ansible rendering are deliberately NOT invoked.

## Phase 11b — Deterministic generators
```
src/domain/generation/                   GeneratedFile/Project, TerraformProject,
                                         AnsibleProject, GeneratorValidationResult
src/application/ports/generation.py      CodeGenerator, InventoryGenerator,
                                         ProjectValidator, executors
src/infrastructure/generators/terraform/{generator.py,mappings/,templates/,validator.py}
src/infrastructure/generators/ansible/{generator.py,mappings/,templates/,validator.py}
src/infrastructure/generators/inventory/generator.py
tests/unit/generators/  + tests/golden/   deterministic snapshot tests
tests/e2e/test_network_to_cloud_to_iac.py
```
No generator may import an LLM or RAG symbol; a test asserts this.

## Phase 12 — Backend
```
apps/api/{main.py,dependencies.py,routers/*,schemas/*,middleware/*,sse.py}
src/application/use_cases/*.py
src/infrastructure/persistence/{models.py,repositories/*,session.py,migrations/}
src/infrastructure/storage/{local.py,s3.py}
src/infrastructure/jobs/{inprocess.py,sqs.py}
workers/{vision,rag,deployment}/worker.py
tests/integration/api/*
```

## Phase 13 — Frontend
```
apps/web/  Next.js App Router, TypeScript strict, Tailwind, shadcn/ui, React Flow
           workspace shell · left asset panel · canvas + overlays · right inspector ·
           tabs: Input · Detection · Topology · Network JSON · Cloud Architecture ·
                 Terraform · Ansible · Deployment · Logs
           prompt mode · clarification panel · approval dialog · SSE client
```

## Phase 14 — Deployment workflow
```
src/infrastructure/deployment/{terraform_executor.py,ansible_executor.py,approval.py,stub.py}
src/application/use_cases/{plan_deployment.py,approve_deployment.py,execute_deployment.py,
                           generate_inventory.py,run_configuration.py}
tests/unit/deployment/test_approval_gate.py
```
Order is fixed: validate → plan → approval → apply → outputs → inventory → ansible run.

## Phase 15 — Production quality
```
docker/{Dockerfile.api,Dockerfile.worker-vision,Dockerfile.worker-rag,
        Dockerfile.worker-deployment,Dockerfile.web}
docker-compose.yml · .github/workflows/ci.yml · pyproject.toml · .pre-commit-config.yaml
docs/{LANGGRAPH_WORKFLOW,ARCHITECTURE_JSON_SCHEMA,CLOUD_ARCHITECTURE_SCHEMA,
      RAG_INTEGRATION,VISION_PIPELINE,TERRAFORM_GENERATOR,ANSIBLE_GENERATOR,
      DEPLOYMENT_FLOW,DEVELOPMENT,DECISIONS}.md · docs/adr/*.md
```

---

## Refactoring of existing assets

Nothing existing is moved or rewritten.

- `Advanced-RAG-For-Net_To_Cloud-Translation` stays where it is. `scripts/sync_external_rag.sh`
  vendors a pinned copy into `external/rag/`; `TOPOFORGE_RAG_PATH` can point at the original
  checkout instead. The adapter never edits RAG source — it injects configuration (paths) by
  setting module attributes after import, which leaves the file on disk untouched.
- YOLO weights are not in the repository and are not committed. They are loaded from
  `TOPOFORGE_YOLO_WEIGHTS_PATH` (dev) or object storage (prod).
