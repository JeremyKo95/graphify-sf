"""Salesforce-specific parser / analysis-pass tests (graphify-sf)."""

from __future__ import annotations

from pathlib import Path

from graphify.salesforce.constants import sobject_nid
from graphify.salesforce.objects import extract_custom_object

FIXTURES = Path(__file__).parent / "fixtures"


def _assert_no_dangling_edges(result: dict) -> None:
    """Every edge source/target must reference a node present in the result."""
    node_ids = {n["id"] for n in result["nodes"]}
    for edge in result["edges"]:
        assert edge["source"] in node_ids, f"dangling source: {edge}"
        assert edge["target"] in node_ids, f"dangling target: {edge}"


def test_objects_parser() -> None:
    fixture = FIXTURES / "sf_Account.object-meta.xml"
    result = extract_custom_object(fixture)

    # 1. No error
    assert "error" not in result

    # 2. No dangling edges
    _assert_no_dangling_edges(result)

    # 3. SObject node ID uses sobject_nid()
    account_id = sobject_nid("Account")
    sobjects = [n for n in result["nodes"] if n["file_type"] == "sobject"]
    sobject_ids = {n["id"] for n in sobjects}
    assert account_id in sobject_ids
    account_node = next(n for n in sobjects if n["id"] == account_id)
    assert account_node["label"] == "Account"
    assert account_node["sf_plural_label"] == "Accounts"
    assert account_node["sf_object_type"] == "standard"

    # 4. field_of edges created (one per field), pointing at the Account node
    field_of = [e for e in result["edges"] if e["relation"] == "field_of"]
    assert len(field_of) == 2
    assert all(e["target"] == account_id for e in field_of)
    assert all(e["confidence"] == "EXTRACTED" for e in field_of)

    field_nodes = [n for n in result["nodes"] if n["file_type"] == "field"]
    assert {n["sf_api_name"] for n in field_nodes} == {
        "BillingCity__c",
        "RelatedOpportunity__c",
    }

    # 5. Lookup relationship detected -> references edge to Opportunity
    references = [e for e in result["edges"] if e["relation"] == "references"]
    assert len(references) == 1
    ref = references[0]
    assert ref["source"] == account_id
    assert ref["target"] == sobject_nid("Opportunity")
    assert ref["sf_relationship_type"] == "Lookup"
    assert ref["sf_field_api_name"] == "RelatedOpportunity__c"


def test_objects_parser_xml_parse_error(tmp_path: Path) -> None:
    """Malformed XML degrades gracefully to a single concept error node."""
    bad = tmp_path / "Broken.object-meta.xml"
    bad.write_text("<CustomObject><label>Broken</CustomObject>")  # mismatched tag

    result = extract_custom_object(bad)

    assert result["edges"] == []
    assert len(result["nodes"]) == 1
    err = result["nodes"][0]
    assert err["file_type"] == "concept"
    assert err["sf_error_type"] == "xml_parse_error"
