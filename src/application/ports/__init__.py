"""Application ports.

Every interface the application depends on. Implementations live in
``src/infrastructure`` and are bound at the composition root — never imported from here.
"""

from src.application.ports.deployment import (
    ConfigurationExecutor,
    ExecutionResult,
    InfrastructureExecutor,
    WorkspaceRef,
)
from src.application.ports.generation import (
    AnsibleGenerator,
    CodeGenerator,
    DeploymentOutputs,
    InventoryGenerator,
    ProjectValidator,
    TerraformGenerator,
)
from src.application.ports.jobs import (
    EventPublisher,
    Job,
    JobQueue,
    JobStatus,
    JobType,
    WorkflowEvent,
)
from src.application.ports.llm import ChatMessage, LLMProvider, LLMResponse, MessageRole
from src.application.ports.rag import (
    RAGProvider,
    RAGTranslationRequest,
    RAGTranslationResult,
    RetrievalHit,
)
from src.application.ports.repositories import (
    ArchitectureRepository,
    AssetRepository,
    AuditEvent,
    AuditRepository,
    CloudArchitectureRepository,
    DeploymentRepository,
    JobRepository,
    Project,
    ProjectAsset,
    ProjectRepository,
)
from src.application.ports.storage import ModelLoader, ObjectStorage, StoredObject
from src.application.ports.vision import (
    AssociationResult,
    ConnectorDetector,
    DetectedConnector,
    Detection,
    ImagePreprocessor,
    ModelInfo,
    OCRProvider,
    ObjectDetector,
    PreprocessedImage,
    SpatialAssociator,
    TextObservation,
)

__all__ = [
    "AnsibleGenerator",
    "ArchitectureRepository",
    "AssetRepository",
    "AssociationResult",
    "AuditEvent",
    "AuditRepository",
    "ChatMessage",
    "CloudArchitectureRepository",
    "CodeGenerator",
    "ConfigurationExecutor",
    "ConnectorDetector",
    "DeploymentOutputs",
    "DeploymentRepository",
    "DetectedConnector",
    "Detection",
    "EventPublisher",
    "ExecutionResult",
    "ImagePreprocessor",
    "InfrastructureExecutor",
    "InventoryGenerator",
    "Job",
    "JobQueue",
    "JobRepository",
    "JobStatus",
    "JobType",
    "LLMProvider",
    "LLMResponse",
    "MessageRole",
    "ModelInfo",
    "ModelLoader",
    "OCRProvider",
    "ObjectDetector",
    "ObjectStorage",
    "PreprocessedImage",
    "Project",
    "ProjectAsset",
    "ProjectRepository",
    "ProjectValidator",
    "RAGProvider",
    "RAGTranslationRequest",
    "RAGTranslationResult",
    "RetrievalHit",
    "SpatialAssociator",
    "StoredObject",
    "TerraformGenerator",
    "TextObservation",
    "WorkflowEvent",
    "WorkspaceRef",
]
