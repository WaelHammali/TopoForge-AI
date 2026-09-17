"""The canonical network architecture and its revision history."""

from __future__ import annotations

import pytest
from pydantic import ValidationError as PydanticValidationError

from src.domain.architecture import (
    CURRENT_SCHEMA_VERSION,
    ArchitectureRevision,
    ArchitectureStatus,
    IssueSeverity,
    MissingInformation,
    NeedClass,
    NetworkArchitecture,
    RevisionChangeKind,
    ValidationIssue,
    ValidationReport,
)
from src.domain.topology import Edge, LinkEndpoint, Node, NodeType
from src.shared.errors import SchemaVersionError


class TestSchema:
    def test_round_trip_is_lossless(self, simple_network: NetworkArchitecture) -> None:
        restored = NetworkArchitecture.from_json(simple_network.to_json())
        assert restored.nodes == simple_network.nodes
        assert restored.interfaces == simple_network.interfaces
        assert restored.edges == simple_network.edges

    def test_schema_version_is_recorded(self, simple_network: NetworkArchitecture) -> None:
        assert simple_network.to_json_dict()["schema_version"] == CURRENT_SCHEMA_VERSION

    def test_unknown_schema_version_is_refused(self) -> None:
        with pytest.raises(SchemaVersionError):
            NetworkArchitecture.from_json_dict({"schema_version": "99.0"})


class TestReferentialIntegrity:
    def test_edge_to_unknown_node_is_impossible(self) -> None:
        with pytest.raises(PydanticValidationError, match="unknown node"):
            NetworkArchitecture(
                nodes=(Node(id="R1", type=NodeType.ROUTER),),
                edges=(
                    Edge(
                        id="e",
                        source=LinkEndpoint(node_id="R1"),
                        target=LinkEndpoint(node_id="GHOST"),
                    ),
                ),
            )

    def test_duplicate_node_ids_are_impossible(self) -> None:
        with pytest.raises(PydanticValidationError, match="duplicate node ids"):
            NetworkArchitecture(
                nodes=(Node(id="R1", type=NodeType.ROUTER), Node(id="R1", type=NodeType.SWITCH))
            )

    def test_edge_to_unknown_interface_is_impossible(
        self, simple_network: NetworkArchitecture
    ) -> None:
        with pytest.raises(PydanticValidationError, match="unknown interface"):
            simple_network.model_copy(
                update={
                    "edges": (
                        Edge(
                            id="bad",
                            source=LinkEndpoint(node_id="R1", interface_id="nope"),
                            target=LinkEndpoint(node_id="SW1"),
                        ),
                    )
                }
            ).model_validate(
                simple_network.model_copy(
                    update={
                        "edges": (
                            Edge(
                                id="bad",
                                source=LinkEndpoint(node_id="R1", interface_id="nope"),
                                target=LinkEndpoint(node_id="SW1"),
                            ),
                        )
                    }
                ).model_dump()
            )


class TestRAGGate:
    """The single gate between network_architecture.json and the RAG."""

    def test_clean_architecture_may_proceed(self, simple_network: NetworkArchitecture) -> None:
        assert simple_network.can_proceed_to_rag is True

    def test_validation_errors_block(self, simple_network: NetworkArchitecture) -> None:
        report = ValidationReport.from_issues(
            [ValidationIssue(code="duplicate_ip", severity=IssueSeverity.ERROR, message="dup")]
        )
        blocked = simple_network.with_validation(report)
        assert blocked.can_proceed_to_rag is False
        assert blocked.status is ArchitectureStatus.INVALID
        assert blocked.blocking_reasons

    def test_warnings_do_not_block(self, simple_network: NetworkArchitecture) -> None:
        report = ValidationReport.from_issues(
            [ValidationIssue(code="no_routing", severity=IssueSeverity.WARNING, message="hm")]
        )
        assert simple_network.with_validation(report).can_proceed_to_rag is True

    def test_required_missing_information_blocks(
        self, simple_network: NetworkArchitecture
    ) -> None:
        blocked = simple_network.model_copy(
            update={
                "missing_information": (
                    MissingInformation(
                        id="m1",
                        need=NeedClass.REQUIRED,
                        code="missing_interface_address",
                        summary="R1 Gi0/1 has no address",
                    ),
                )
            }
        )
        assert blocked.can_proceed_to_rag is False

    def test_optional_missing_information_does_not_block(
        self, simple_network: NetworkArchitecture
    ) -> None:
        allowed = simple_network.model_copy(
            update={
                "missing_information": (
                    MissingInformation(
                        id="m1", need=NeedClass.OPTIONAL, code="no_ospf", summary="no OSPF"
                    ),
                )
            }
        )
        assert allowed.can_proceed_to_rag is True

    def test_status_becomes_needs_input_when_valid_but_incomplete(
        self, simple_network: NetworkArchitecture
    ) -> None:
        incomplete = simple_network.model_copy(
            update={
                "missing_information": (
                    MissingInformation(
                        id="m1", need=NeedClass.REQUIRED, code="x", summary="y"
                    ),
                )
            }
        )
        assert (
            incomplete.with_validation(ValidationReport()).status
            is ArchitectureStatus.NEEDS_INPUT
        )


class TestRevisions:
    def test_initial_revision(self, simple_network: NetworkArchitecture) -> None:
        revision = ArchitectureRevision.initial(
            simple_network, RevisionChangeKind.EXTRACTION, "YOLO + OCR extraction"
        )
        assert revision.revision == 1
        assert revision.parent_revision is None
        assert revision.architecture.metadata.revision == 1

    def test_history_is_a_chain_and_the_past_is_immutable(
        self, simple_network: NetworkArchitecture
    ) -> None:
        first = ArchitectureRevision.initial(
            simple_network, RevisionChangeKind.EXTRACTION, "extracted"
        )
        second = first.succeed(
            simple_network,
            RevisionChangeKind.USER_CORRECTION,
            "corrected R1 address",
            changed_paths=("interfaces[R1:Gi0/0].addresses[0]",),
        )
        third = second.succeed(
            simple_network, RevisionChangeKind.ENRICHMENT, "added OSPF"
        )

        assert [r.revision for r in (first, second, third)] == [1, 2, 3]
        assert [r.parent_revision for r in (second, third)] == [1, 2]
        # The earlier records are untouched by later ones.
        assert first.revision == 1
        assert first.change_kind is RevisionChangeKind.EXTRACTION
        assert third.architecture.metadata.revision == 3
        assert second.changed_paths == ("interfaces[R1:Gi0/0].addresses[0]",)

    def test_all_revisions_share_one_architecture_id(
        self, simple_network: NetworkArchitecture
    ) -> None:
        first = ArchitectureRevision.initial(
            simple_network, RevisionChangeKind.EXTRACTION, "extracted"
        )
        second = first.succeed(
            # deliberately pass an architecture carrying a *different* id
            simple_network.model_copy(
                update={
                    "metadata": simple_network.metadata.model_copy(
                        update={"architecture_id": "someone-elses-id"}
                    )
                }
            ),
            RevisionChangeKind.MANUAL_JSON_EDIT,
            "user pasted JSON",
        )
        assert second.architecture_id == first.architecture_id
        assert second.architecture.metadata.architecture_id == first.architecture_id

    def test_revision_number_mismatch_is_refused(
        self, simple_network: NetworkArchitecture
    ) -> None:
        with pytest.raises(PydanticValidationError, match="revision number mismatch"):
            ArchitectureRevision(
                architecture_id=simple_network.architecture_id,
                revision=5,
                change_kind=RevisionChangeKind.EXTRACTION,
                change_reason="inconsistent",
                architecture=simple_network,
                parent_revision=4,
            )

    def test_revision_one_cannot_have_a_parent(
        self, simple_network: NetworkArchitecture
    ) -> None:
        with pytest.raises(PydanticValidationError, match="revision 1 cannot have a parent"):
            ArchitectureRevision(
                architecture_id=simple_network.architecture_id,
                revision=1,
                change_kind=RevisionChangeKind.EXTRACTION,
                change_reason="x",
                architecture=simple_network,
                parent_revision=0,
            )


class TestValidationReport:
    def test_issues_are_bucketed_by_severity(self) -> None:
        report = ValidationReport.from_issues(
            [
                ValidationIssue(code="a", severity=IssueSeverity.ERROR, message="a"),
                ValidationIssue(code="b", severity=IssueSeverity.WARNING, message="b"),
                ValidationIssue(code="c", severity=IssueSeverity.RECOMMENDATION, message="c"),
                ValidationIssue(code="d", severity=IssueSeverity.INFO, message="d"),
            ]
        )
        assert (len(report.errors), len(report.warnings), len(report.recommendations)) == (1, 1, 2)
        assert report.valid is False
        assert report.blocks_rag is True

    def test_merge_keeps_every_issue_and_dedupes_validator_names(self) -> None:
        first = ValidationReport.from_issues(
            [ValidationIssue(code="a", severity=IssueSeverity.ERROR, message="a")], ("IPValidator",)
        )
        second = ValidationReport.from_issues(
            [ValidationIssue(code="b", severity=IssueSeverity.WARNING, message="b")],
            ("IPValidator", "VLANValidator"),
        )
        merged = first.merged_with(second)
        assert len(merged.all_issues) == 2
        assert merged.validators_run == ("IPValidator", "VLANValidator")
