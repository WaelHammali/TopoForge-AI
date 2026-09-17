"""Vision ports.

The application depends on these, never on Ultralytics, OpenCV or PaddleOCR. Every
implementation must pass the corresponding suite in ``tests/contract``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from src.domain.common.geometry import BoundingBox, Point
from src.domain.common.provenance import Evidence

__all__ = [
    "ConnectorDetector",
    "Detection",
    "DetectedConnector",
    "ImagePreprocessor",
    "ModelInfo",
    "OCRProvider",
    "ObjectDetector",
    "PreprocessedImage",
    "TextObservation",
]


@dataclass(frozen=True, slots=True)
class ModelInfo:
    """What a loaded detection model actually is.

    ``class_names`` is read from the weights at load time. It is never hard-coded, because
    until weights are supplied the trained classes are unknown.
    """

    name: str
    version: str
    framework: str
    #: Index -> class name, exactly as the model reports it.
    class_names: dict[int, str]
    device: str = "cpu"
    checksum: str | None = None
    input_size: tuple[int, int] | None = None

    @property
    def class_list(self) -> list[str]:
        return [self.class_names[key] for key in sorted(self.class_names)]


@dataclass(frozen=True, slots=True)
class PreprocessedImage:
    """The result of preprocessing. The original is always preserved separately."""

    #: URI of the image that was processed (the original).
    source_uri: str
    #: URI of the processed copy, when one was written.
    processed_uri: str | None
    width: int
    height: int
    original_width: int
    original_height: int
    #: Operations applied, in order, for the UI and for reproducibility.
    operations: tuple[str, ...] = ()

    @property
    def scale_x(self) -> float:
        return self.original_width / self.width if self.width else 1.0

    @property
    def scale_y(self) -> float:
        return self.original_height / self.height if self.height else 1.0

    def to_original_coordinates(self, box: BoundingBox) -> BoundingBox:
        """Map a box detected on the processed image back onto the original.

        Overlays are always drawn on the original image, so every adapter must map back
        before returning a bounding box.
        """
        return box.scaled(self.scale_x, self.scale_y)


@dataclass(frozen=True, slots=True)
class Detection:
    """One detected component, normalised away from any vendor's output format."""

    id: str
    #: The detector's own class string, verbatim. Mapping to a NodeType happens later.
    class_name: str
    confidence: float
    bbox: BoundingBox
    model_version: str
    class_index: int | None = None
    asset_id: str | None = None

    def to_evidence(self) -> Evidence:
        from src.domain.common.provenance import ProvenanceSource

        return Evidence(
            source=ProvenanceSource.YOLO,
            confidence=self.confidence,
            bbox=self.bbox,
            producer=self.model_version,
            observation_ids=(self.id,),
            asset_id=self.asset_id,
            detail=f"detected as {self.class_name!r}",
        )


@dataclass(frozen=True, slots=True)
class TextObservation:
    """One piece of text found on an image.

    OCR reports what it saw. It does **not** decide whether a value is a valid IP — that
    is :mod:`src.domain.network.parsing`'s job.
    """

    id: str
    text: str
    confidence: float
    bbox: BoundingBox
    provider: str
    asset_id: str | None = None
    language: str | None = None
    #: Rotation in degrees, when the provider reports it.
    angle: float | None = None

    def to_evidence(self) -> Evidence:
        from src.domain.common.provenance import ProvenanceSource

        return Evidence(
            source=ProvenanceSource.OCR,
            confidence=self.confidence,
            bbox=self.bbox,
            producer=self.provider,
            observation_ids=(self.id,),
            asset_id=self.asset_id,
            detail=f"OCR text {self.text!r}",
        )


@dataclass(frozen=True, slots=True)
class DetectedConnector:
    """A line/connector found between components.

    Endpoints are raw pixel coordinates. Deciding *which components* they belong to is the
    spatial association engine's job, not the detector's.
    """

    id: str
    start: Point
    end: Point
    confidence: float
    detector: str
    #: Intermediate points for polyline connectors.
    waypoints: tuple[Point, ...] = ()
    #: Arrow direction, when an arrowhead was detected: "start", "end", "both" or None.
    arrow: str | None = None
    asset_id: str | None = None
    attributes: dict[str, str] = field(default_factory=dict)

    def to_evidence(self) -> Evidence:
        from src.domain.common.provenance import ProvenanceSource

        return Evidence(
            source=ProvenanceSource.EDGE_DETECTOR,
            confidence=self.confidence,
            producer=self.detector,
            observation_ids=(self.id,),
            asset_id=self.asset_id,
        )


class ImagePreprocessor(ABC):
    """Prepares an image for detection without destroying information.

    Implementations must never modify the original asset, and must record every operation
    they applied so that coordinates can be mapped back.
    """

    @abstractmethod
    async def preprocess(self, image_uri: str) -> PreprocessedImage: ...


class ObjectDetector(ABC):
    """Detects network components in a diagram."""

    @abstractmethod
    async def detect(
        self, image: PreprocessedImage, *, confidence_threshold: float | None = None
    ) -> list[Detection]: ...

    @abstractmethod
    async def model_info(self) -> ModelInfo:
        """Describe the loaded model, including its real trained class names."""


class OCRProvider(ABC):
    """Extracts text. Makes no judgement about what the text means."""

    @abstractmethod
    async def extract_text(self, image: PreprocessedImage) -> list[TextObservation]: ...

    @property
    @abstractmethod
    def provider_name(self) -> str: ...


class ConnectorDetector(ABC):
    """Finds links between components.

    The strategy behind this port is expected to change — Hough lines today, segmentation
    or a learned model later — so nothing above it may depend on how detection works.
    """

    @abstractmethod
    async def detect_connectors(
        self, image: PreprocessedImage, *, known_components: list[Detection] | None = None
    ) -> list[DetectedConnector]: ...

    @property
    @abstractmethod
    def strategy_name(self) -> str: ...


@runtime_checkable
class SpatialAssociator(Protocol):
    """Binds text and connector endpoints to components using geometry first."""

    def associate(
        self,
        detections: list[Detection],
        texts: list[TextObservation],
        connectors: list[DetectedConnector],
    ) -> "AssociationResult": ...


@dataclass(frozen=True, slots=True)
class AssociationResult:
    """Deterministic associations, plus what remained ambiguous.

    Ambiguity is returned, not resolved by guessing. An LLM tie-break is an explicit,
    separate step that a caller may choose to run.
    """

    #: text observation id -> detection id
    text_to_component: dict[str, str] = field(default_factory=dict)
    #: connector id -> (detection id, detection id)
    connector_to_components: dict[str, tuple[str, str]] = field(default_factory=dict)
    #: Observation ids that could not be assigned confidently, with the reason.
    unassigned: dict[str, str] = field(default_factory=dict)
    #: Association confidences, keyed the same way as the maps above.
    confidences: dict[str, float] = field(default_factory=dict)
