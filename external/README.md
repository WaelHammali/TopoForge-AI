# External dependencies

## `rag/` — INTEGRATION POINT

The knowledge-base RAG that translates a validated network architecture into a cloud
architecture. It is a **separate repository** and is not tracked here; `external/rag/` is
git-ignored apart from this README.

Populate it with:

```bash
scripts/sync_external_rag.sh [source-checkout]
```

or point the application at a checkout directly:

```bash
export TOPOFORGE_RAG__PATH=/path/to/Advanced-RAG-For-Net_To_Cloud-Translation
export TOPOFORGE_RAG__BACKEND=legacy_net2tf
```

### The contract this platform relies on

```python
from app import plan_architecture
plan = plan_architecture(architecture_dict, retriever=KnowledgeRetriever(...))
# -> {"cloud_plan", "ansible_plan", "rule_ids", "limitations", "architecture", "knowledge"}
```

`src/infrastructure/rag/loader.py` asserts those symbols exist at load time, so an upstream
change surfaces as a clear error here rather than as a failure deep inside a workflow.

### What is deliberately not used

- `legacy/` — the archived original implementation, including its Terraform templates and
  Ansible builders. Generating infrastructure code is this platform's job now, and
  `tests/unit/test_layering.py` fails the build if any RAG module imports a generator.

### Running without it

`TOPOFORGE_RAG__BACKEND=stub` selects an offline deterministic translator. It performs no
cloud reasoning and says so in its own output; it exists so the platform can be developed
and tested without a Groq key or a downloaded embedding model.
