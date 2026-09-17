"""Issues, gaps and questions.

Four related but distinct concepts, kept apart on purpose:

``ValidationIssue``     something is *wrong* (or suspicious) in the architecture.
``UnresolvedField``     a field whose value is not known and is carried explicitly.
``MissingInformation``  a gap the completeness analyzer found, with a severity class.
``EnrichmentOption``    something the user *could* add; never an error.
``ClarificationQuestion`` a renderable question derived from the above by a policy.

The question *strategy* is deliberately not encoded here — only the structures a policy
needs. See :mod:`src.application.services.clarification`.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from src.domain.common.provenance import Evidence

__all__ = [
    "ClarificationAnswer",
    "ClarificationQuestion",
    "EnrichmentCategory",
    "EnrichmentOption",
    "IssueSeverity",
    "MissingInformation",
    "NeedClass",
    "QuestionKind",
    "UnresolvedField",
    "ValidationIssue",
    "ValidationReport",
]


class IssueSeverity(str, Enum):
    ERROR = "error"
    WARNING = "warning"
    RECOMMENDATION = "recommendation"
    INFO = "info"


class ValidationIssue(BaseModel):
    """One finding from a validator."""

    model_config = ConfigDict(frozen=True)

    code: str = Field(description="Stable machine-readable code, e.g. 'duplicate_ip_address'")
    severity: IssueSeverity
    message: str
    #: Where the problem is: e.g. "nodes[R1].interfaces[Gi0/0].addresses[0]".
    path: str | None = None
    #: Domain object ids the issue refers to, for UI highlighting.
    subject_ids: tuple[str, ...] = ()
    #: Which validator produced it.
    validator: str | None = None
    #: What the user could do about it.
    remediation: str | None = None
    context: dict[str, Any] = Field(default_factory=dict)

    @property
    def is_blocking(self) -> bool:
        """Errors block progression to the RAG. Nothing else does."""
        return self.severity is IssueSeverity.ERROR


class ValidationReport(BaseModel):
    """The result of running every validator over an architecture."""

    model_config = ConfigDict(frozen=True)

    valid: bool = True
    errors: tuple[ValidationIssue, ...] = ()
    warnings: tuple[ValidationIssue, ...] = ()
    recommendations: tuple[ValidationIssue, ...] = ()
    validators_run: tuple[str, ...] = ()

    @classmethod
    def from_issues(
        cls, issues: list[ValidationIssue], validators_run: tuple[str, ...] = ()
    ) -> ValidationReport:
        errors = tuple(i for i in issues if i.severity is IssueSeverity.ERROR)
        warnings = tuple(i for i in issues if i.severity is IssueSeverity.WARNING)
        recommendations = tuple(
            i for i in issues if i.severity in {IssueSeverity.RECOMMENDATION, IssueSeverity.INFO}
        )
        return cls(
            valid=not errors,
            errors=errors,
            warnings=warnings,
            recommendations=recommendations,
            validators_run=validators_run,
        )

    @property
    def all_issues(self) -> tuple[ValidationIssue, ...]:
        return (*self.errors, *self.warnings, *self.recommendations)

    @property
    def blocks_rag(self) -> bool:
        """The single gate between ``architecture.json`` and the RAG."""
        return bool(self.errors)

    def merged_with(self, other: ValidationReport) -> ValidationReport:
        return ValidationReport.from_issues(
            [*self.all_issues, *other.all_issues],
            tuple(dict.fromkeys((*self.validators_run, *other.validators_run))),
        )


class UnresolvedField(BaseModel):
    """A field deliberately carried as unknown rather than guessed."""

    model_config = ConfigDict(frozen=True)

    path: str
    reason: str
    subject_id: str | None = None
    #: Candidate values the system considered, with their confidence.
    candidates: tuple[tuple[str, float], ...] = ()
    evidence: Evidence | None = None
    #: True when the user was asked and chose to leave it unset.
    accepted_by_user: bool = False


class NeedClass(str, Enum):
    """How badly a missing item is needed.

    ``DERIVABLE`` matters: the system can compute the value itself and only needs
    permission, which is a different conversation from asking the user to supply it.
    """

    REQUIRED = "REQUIRED"
    RECOMMENDED = "RECOMMENDED"
    OPTIONAL = "OPTIONAL"
    DERIVABLE = "DERIVABLE"
    ERROR = "ERROR"


class MissingInformation(BaseModel):
    """A gap found by the completeness analyzer."""

    model_config = ConfigDict(frozen=True)

    id: str
    need: NeedClass
    code: str = Field(description="e.g. 'missing_interface_address'")
    summary: str
    detail: str | None = None
    #: Domain objects concerned.
    subject_ids: tuple[str, ...] = ()
    path: str | None = None
    #: Value the system would use if allowed to derive it (DERIVABLE only).
    derivable_value: str | None = None
    derivation_explanation: str | None = None
    #: Ordering hint for policies. Lower sorts first.
    priority: int = 100
    context: dict[str, Any] = Field(default_factory=dict)

    @property
    def blocks_progress(self) -> bool:
        return self.need in {NeedClass.REQUIRED, NeedClass.ERROR}


class EnrichmentCategory(str, Enum):
    ADDRESSING = "addressing"
    STATIC_ROUTING = "static_routing"
    OSPF = "ospf"
    VLAN = "vlan"
    NAT = "nat"
    # Extension points — modelled now, implemented as the product grows.
    ACL = "acl"
    DHCP = "dhcp"
    DNS = "dns"
    VPN = "vpn"
    BGP = "bgp"
    HSRP = "hsrp"
    VRRP = "vrrp"
    REDUNDANCY = "redundancy"


class EnrichmentOption(BaseModel):
    """Something the architecture could gain. Never required, never an error."""

    model_config = ConfigDict(frozen=True)

    id: str
    category: EnrichmentCategory
    title: str
    description: str
    #: Why the analyzer thinks this is worth offering here.
    rationale: str | None = None
    applicable_node_ids: tuple[str, ...] = ()
    #: JSON-schema-ish description of what the user would need to provide.
    parameters_schema: dict[str, Any] = Field(default_factory=dict)
    #: Whether the platform can propose sensible defaults for the parameters.
    has_suggested_defaults: bool = False
    suggested_defaults: dict[str, Any] = Field(default_factory=dict)
    priority: int = 100


class QuestionKind(str, Enum):
    TEXT = "text"
    IP_ADDRESS = "ip_address"
    CIDR = "cidr"
    SINGLE_CHOICE = "single_choice"
    MULTI_CHOICE = "multi_choice"
    BOOLEAN = "boolean"
    NUMBER = "number"
    STRUCTURED = "structured"


class ClarificationQuestion(BaseModel):
    """A renderable question.

    Produced by a :class:`~src.application.services.clarification.policy.ClarificationPolicy`
    from ``MissingInformation`` / ``EnrichmentOption``. The frontend renders these
    generically, so changing the questioning strategy never requires a UI change.
    """

    model_config = ConfigDict(frozen=True)

    id: str
    kind: QuestionKind
    question: str
    help_text: str | None = None
    #: Where the answer will be written, e.g. "nodes[R1].interfaces[Gi0/0].address".
    target_path: str | None = None
    subject_ids: tuple[str, ...] = ()
    #: Options for choice questions: (value, label).
    options: tuple[tuple[str, str], ...] = ()
    default_value: Any = None
    required: bool = True
    #: Link back to what prompted the question.
    missing_information_id: str | None = None
    enrichment_option_id: str | None = None
    #: Free-form schema for STRUCTURED questions (e.g. a whole OSPF process).
    schema_hint: dict[str, Any] = Field(default_factory=dict)
    priority: int = 100


class ClarificationAnswer(BaseModel):
    """A user's reply, fed back into the workflow on resume."""

    model_config = ConfigDict(frozen=True)

    question_id: str
    value: Any = None
    #: The user may explicitly decline; that is an answer, not a non-answer.
    skipped: bool = False
    answered_by: str | None = None
