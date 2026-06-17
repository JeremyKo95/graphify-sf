"""Salesforce-specific parser / analysis-pass tests (graphify-sf)."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import networkx as nx

from graphify.salesforce.apex_enhanced import extract_apex_enhanced
from graphify.salesforce.constants import sobject_nid
from graphify.salesforce.cpq import cpq_analysis_pass
from graphify.salesforce.flow import extract_flow
from graphify.salesforce.flow_cpq_loops import detect_flow_cpq_loops
from graphify.salesforce.governor_limits import governor_limit_analysis_pass
from graphify.salesforce.lwc import extract_lwc_html, extract_lwc_js
from graphify.salesforce.neo4j_sf import (
    SF_NODE_TYPE_TO_NEO4J_LABEL,
    SF_RELATION_TO_NEO4J_TYPE,
    push_to_neo4j_sf,
    to_cypher_sf,
)
from graphify.salesforce.objects import extract_custom_object
from graphify.salesforce.order_of_execution import ooe_analysis_pass
from graphify.salesforce.permission_analysis import permission_analysis_pass
from graphify.salesforce.profiles import extract_permission_set, extract_profile
from graphify.salesforce.release_sync import check_staleness, sync_release_notes

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


def test_apex_parser() -> None:
    # --- Apex class: methods, SOQL (not in loop), DML --------------------
    cls_result = extract_apex_enhanced(FIXTURES / "sf_AccountService.cls")

    # 1. No error
    assert "error" not in cls_result

    # 2. No dangling edges
    _assert_no_dangling_edges(cls_result)

    # class node + sf_code_type
    classes = [n for n in cls_result["nodes"] if n.get("sf_code_type") == "class"]
    assert len(classes) == 1
    assert classes[0]["label"] == "AccountService"

    # method signatures parsed (getAccounts, updateAccount)
    methods = [n for n in cls_result["nodes"] if n.get("sf_method_type") == "method"]
    method_ids = {n["id"] for n in methods}
    assert f"{classes[0]['id']}_getaccounts" in method_ids
    assert f"{classes[0]['id']}_updateaccount" in method_ids
    # each method -> class via calls edge
    calls = [e for e in cls_result["edges"] if e["relation"] == "calls"]
    assert all(e["target"] == classes[0]["id"] for e in calls)
    assert len(calls) == len(methods)

    # 3. SOQL detected (FROM Account), CRITICAL: target uses sobject_nid()
    cls_queries = [e for e in cls_result["edges"] if e["relation"] == "queries"]
    account_q = [e for e in cls_queries if e["target"] == sobject_nid("Account")]
    assert len(account_q) == 1
    # SOQL here sits *before* the loop -> not in loop
    assert account_q[0]["sf_in_loop"] is False
    assert account_q[0]["confidence"] == "EXTRACTED"

    # 4. DML detected (update acc) and resolved to Account
    cls_dml = [e for e in cls_result["edges"] if e["relation"] == "dml_operates_on"]
    assert any(
        e["target"] == sobject_nid("Account") and e["sf_dml_type"] == "UPDATE"
        for e in cls_dml
    )

    # --- Apex trigger: SOQL/DML inside a loop ----------------------------
    trig_result = extract_apex_enhanced(FIXTURES / "sf_AccountTrigger.trigger")

    assert "error" not in trig_result
    _assert_no_dangling_edges(trig_result)

    triggers = [n for n in trig_result["nodes"] if n.get("sf_code_type") == "trigger"]
    assert len(triggers) == 1
    assert triggers[0]["label"] == "AccountTrigger"

    # 5. SOQL inside the loop is tagged sf_in_loop: true
    trig_queries = [e for e in trig_result["edges"] if e["relation"] == "queries"]
    opp_q = [e for e in trig_queries if e["target"] == sobject_nid("Opportunity")]
    assert len(opp_q) == 1
    assert opp_q[0]["sf_in_loop"] is True

    # DML inside the loop (update opps) also tagged in loop, resolved to Opportunity
    trig_dml = [e for e in trig_result["edges"] if e["relation"] == "dml_operates_on"]
    opp_dml = [e for e in trig_dml if e["target"] == sobject_nid("Opportunity")]
    assert len(opp_dml) == 1
    assert opp_dml[0]["sf_in_loop"] is True
    assert opp_dml[0]["sf_dml_type"] == "UPDATE"


def test_flow_parser() -> None:
    fixture = FIXTURES / "sf_AccountFlow.flow-meta.xml"
    result = extract_flow(fixture)

    # 1. No error
    assert "error" not in result

    # 2. No dangling edges
    _assert_no_dangling_edges(result)

    # Flow node carries trigger metadata from <start>
    flows = [n for n in result["nodes"] if n["file_type"] == "flow"]
    assert len(flows) == 1
    flow_node = flows[0]
    assert flow_node["label"] == "sf_AccountFlow"
    assert flow_node["sf_trigger_object"] == "Account"
    assert flow_node["sf_trigger_type"] == "CreateAndUpdate"
    flow_id = flow_node["id"]

    # 3. recordLookup -> queries edge, target resolved via sobject_nid()
    queries = [e for e in result["edges"] if e["relation"] == "queries"]
    assert len(queries) == 1
    q = queries[0]
    assert q["source"] == flow_id
    assert q["target"] == sobject_nid("Account")
    assert q["confidence"] == "EXTRACTED"
    assert q["sf_flow_element"] == "Get_Accounts"

    # 4. recordCreate -> dml_operates_on edge (INSERT) to Opportunity
    dml = [e for e in result["edges"] if e["relation"] == "dml_operates_on"]
    assert len(dml) == 1
    d = dml[0]
    assert d["source"] == flow_id
    assert d["target"] == sobject_nid("Opportunity")
    assert d["sf_dml_type"] == "INSERT"
    assert d["sf_flow_element"] == "Create_Opportunity"

    # 5. apexAction -> flow_invokes edge to the Apex class
    invokes = [e for e in result["edges"] if e["relation"] == "flow_invokes"]
    assert len(invokes) == 1
    inv = invokes[0]
    assert inv["source"] == flow_id
    assert inv["target"] == "apex_accountservice"
    assert inv["sf_flow_element"] == "Call_Apex"


def test_lwc_parser() -> None:
    # --- HTML template ---------------------------------------------------
    html_result = extract_lwc_html(FIXTURES / "sf_MyComponent.html")

    # 1. No error + no dangling edges
    assert "error" not in html_result
    _assert_no_dangling_edges(html_result)

    html_nodes = [n for n in html_result["nodes"] if n["file_type"] == "lwc_component"]
    assert len(html_nodes) == 1
    assert html_nodes[0]["sf_lwc_file_type"] == "html"

    # --- JavaScript ------------------------------------------------------
    js_result = extract_lwc_js(FIXTURES / "sf_MyComponent.js")

    # 1. No error + no dangling edges
    assert "error" not in js_result
    _assert_no_dangling_edges(js_result)

    lwc_nodes = [n for n in js_result["nodes"] if n["file_type"] == "lwc_component"]
    assert len(lwc_nodes) == 1
    lwc_node = lwc_nodes[0]
    assert lwc_node["label"] == "MyComponent"
    assert lwc_node["sf_lwc_file_type"] == "js"

    # 4. @api properties detected
    assert lwc_node["sf_api_property_recordId"] is True
    assert lwc_node["sf_api_property_label"] is True

    # 2/3. @wire decorator -> wire_to edge to the Apex method node
    wire_edges = [e for e in js_result["edges"] if e["relation"] == "wire_to"]
    assert len(wire_edges) == 1
    w = wire_edges[0]
    assert w["source"] == lwc_node["id"]
    # Resolves to the same Apex method node ID the Apex parser produces:
    # apex_<class>_<method>  (AccountService.getAccounts)
    assert w["target"] == "apex_accountservice_getaccounts"
    assert w["sf_wire_method"] == "getAccounts"
    assert w["confidence"] == "INFERRED"


def test_profile_parser() -> None:
    fixture = FIXTURES / "sf_Admin.profile-meta.xml"
    result = extract_profile(fixture)

    # 1. No error
    assert "error" not in result

    # 2. No dangling edges
    _assert_no_dangling_edges(result)

    # Profile node
    profiles = [n for n in result["nodes"] if n["file_type"] == "profile"]
    assert len(profiles) == 1
    profile_node = profiles[0]
    assert profile_node["id"] == "profile_sf_admin"
    assert profile_node["label"] == "sf_Admin"
    profile_id = profile_node["id"]

    grants = [e for e in result["edges"] if e["relation"] == "grants_access_to"]
    assert all(e["source"] == profile_id for e in grants)
    assert all(e["confidence"] == "EXTRACTED" for e in grants)

    # 3. Object permission -> grants_access_to edge, target via sobject_nid()
    obj_grants = [e for e in grants if e["target"] == sobject_nid("Account")]
    assert len(obj_grants) == 1
    og = obj_grants[0]
    assert og["sf_object"] == "Account"
    assert og["sf_permissions"] == ["CREATE", "READ", "EDIT", "DELETE"]

    # 4. FLS permissions stored as a node attribute (not a separate node)
    fls = profile_node["sf_fls_permissions"]
    assert fls["Account.BillingCity"] == {"readable": True, "editable": True}
    assert fls["SBQQ__Quote__c.SBQQ__NetPrice__c"] == {
        "readable": True,
        "editable": False,
    }

    # 5. Apex class access detected -> grants_access_to edge to apex_<class>
    apex_grants = [e for e in grants if e["target"] == "apex_accountservice"]
    assert len(apex_grants) == 1
    assert apex_grants[0]["sf_access_type"] == "apex_class"


def test_permission_set_parser(tmp_path: Path) -> None:
    """PermissionSet files yield a permission_set node (prefix differs)."""
    permset = tmp_path / "Sales.permissionset-meta.xml"
    permset.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<PermissionSet xmlns="http://soap.sforce.com/2006/04/metadata">\n'
        "    <objectPermissions>\n"
        "        <object>Account</object>\n"
        "        <allowRead>true</allowRead>\n"
        "    </objectPermissions>\n"
        "</PermissionSet>\n"
    )

    result = extract_permission_set(permset)

    assert "error" not in result
    _assert_no_dangling_edges(result)

    nodes = [n for n in result["nodes"] if n["file_type"] == "permission_set"]
    assert len(nodes) == 1
    assert nodes[0]["id"] == "permission_set_sales"

    grants = [e for e in result["edges"] if e["relation"] == "grants_access_to"]
    assert len(grants) == 1
    assert grants[0]["target"] == sobject_nid("Account")
    assert grants[0]["sf_permissions"] == ["READ"]


def test_profile_parser_xml_parse_error(tmp_path: Path) -> None:
    """Malformed Profile XML degrades gracefully to a single concept error node."""
    bad = tmp_path / "Broken.profile-meta.xml"
    bad.write_text("<Profile><objectPermissions></Profile>")  # mismatched tag

    result = extract_profile(bad)

    assert result["edges"] == []
    assert len(result["nodes"]) == 1
    err = result["nodes"][0]
    assert err["file_type"] == "concept"
    assert err["sf_error_type"] == "xml_parse_error"


def test_lwc_parser_encoding_error(tmp_path: Path) -> None:
    """Undecodable JS bytes degrade gracefully to a single concept error node."""
    bad = tmp_path / "Broken.js"
    bad.write_bytes(b"\xff\xfe invalid \x80 bytes")

    result = extract_lwc_js(bad)

    assert result["edges"] == []
    assert len(result["nodes"]) == 1
    err = result["nodes"][0]
    assert err["file_type"] == "concept"
    assert err["sf_error_type"] == "js_parse_error"


def test_flow_parser_xml_parse_error(tmp_path: Path) -> None:
    """Malformed Flow XML degrades gracefully to a single concept error node."""
    bad = tmp_path / "Broken.flow-meta.xml"
    bad.write_text("<Flow><start></Flow>")  # mismatched tag

    result = extract_flow(bad)

    assert result["edges"] == []
    assert len(result["nodes"]) == 1
    err = result["nodes"][0]
    assert err["file_type"] == "concept"
    assert err["sf_error_type"] == "xml_parse_error"


def test_cpq_analysis() -> None:
    """CPQ analysis pass: reclassify SBQQ__, detect QCP, order applies-to edges."""
    # Parse the QCP plugin -> a "code" node carrying its source (QCP detection input).
    qcp_result = extract_apex_enhanced(FIXTURES / "sf_QCPPlugin.cls")
    assert "error" not in qcp_result

    all_nodes: list[dict] = list(qcp_result["nodes"])
    all_edges: list[dict] = list(qcp_result["edges"])
    qcp_class_id = "apex_sf_qcpplugin"

    # CPQ rule nodes (labels carry the SBQQ__ prefix) + a plain SObject that must
    # NOT be reclassified.
    all_nodes.append(
        {
            "id": "cpq_price_rule",
            "label": "SBQQ__PriceRule__c Discount",
            "file_type": "sobject",
            "source_file": "objects/SBQQ__PriceRule__c.object-meta.xml",
        }
    )
    all_nodes.append(
        {
            "id": "cpq_product_rule",
            "label": "SBQQ__ProductRule__c Bundle",
            "file_type": "sobject",
            "source_file": "objects/SBQQ__ProductRule__c.object-meta.xml",
        }
    )
    quote_id = sobject_nid("SBQQ__Quote__c")
    all_nodes.append(
        {
            "id": quote_id,
            "label": "SBQQ__Quote__c",
            "file_type": "sobject",
            "source_file": "objects/SBQQ__Quote__c.object-meta.xml",
        }
    )
    account_id = sobject_nid("Account")
    all_nodes.append(
        {
            "id": account_id,
            "label": "Account",
            "file_type": "sobject",
            "source_file": "objects/Account.object-meta.xml",
        }
    )

    all_edges.append(
        {
            "source": "cpq_price_rule",
            "target": quote_id,
            "relation": "cpq_applies_to",
            "confidence": "INFERRED",
            "source_file": "x",
        }
    )
    all_edges.append(
        {
            "source": "cpq_product_rule",
            "target": quote_id,
            "relation": "cpq_applies_to",
            "confidence": "INFERRED",
            "source_file": "x",
        }
    )

    nodes_before = len(all_nodes)
    edges_before = len(all_edges)
    input_nodes_obj = all_nodes
    input_edges_obj = all_edges

    result = cpq_analysis_pass(all_nodes, all_edges)

    # In-place contract: returns None, mutates the same list objects.
    assert result is None
    assert all_nodes is input_nodes_obj
    assert all_edges is input_edges_obj
    assert len(all_edges) == edges_before  # pass adds no edges

    by_id = {n["id"]: n for n in all_nodes}

    # 1. SBQQ__ nodes reclassified to cpq_rule
    assert by_id["cpq_price_rule"]["file_type"] == "cpq_rule"
    assert by_id["cpq_price_rule"]["sf_cpq_object"] == "SBQQ__PriceRule__c Discount"
    assert by_id["cpq_product_rule"]["file_type"] == "cpq_rule"
    assert by_id[quote_id]["file_type"] == "cpq_rule"
    # plain SObject untouched
    assert by_id[account_id]["file_type"] == "sobject"

    # 2. QCP interface implementation detected
    assert by_id[qcp_class_id]["sf_qcp_implementation"] is True

    # 4. QCP method nodes created (one per QCP method present in the source)
    qcp_methods = [n for n in all_nodes if n.get("file_type") == "cpq_qcp_method"]
    assert len(all_nodes) > nodes_before
    qcp_method_names = {n["sf_qcp_method"] for n in qcp_methods}
    assert {
        "onBeforePriceRules",
        "calculate",
        "onAfterPriceRules",
        "onAfterCalculate",
    } <= qcp_method_names
    # onBeforeCalculate is not implemented in the fixture -> no node
    assert "onBeforeCalculate" not in qcp_method_names
    for m in qcp_methods:
        assert m["sf_qcp_class"] == qcp_class_id
        assert m["source_file"] == by_id[qcp_class_id]["source_file"]

    # 3. execution_order added to cpq_applies_to edges, existing attrs preserved
    applies = [e for e in all_edges if e["relation"] == "cpq_applies_to"]
    price_edge = next(e for e in applies if e["source"] == "cpq_price_rule")
    product_edge = next(e for e in applies if e["source"] == "cpq_product_rule")
    assert price_edge["execution_order"] == 2  # Price Rules
    assert product_edge["execution_order"] == 1  # Product Rules
    assert price_edge["confidence"] == "INFERRED"  # not overwritten


def test_ooe_analysis() -> None:
    """OoE pass: 18-step chain per triggered SObject, forming a DAG.

    SObjects with an Apex trigger or a Validation Rule qualify; an SObject
    referenced only by SOQL must NOT get an OoE chain (ADR-005 / ADR-017).
    """
    account_id = sobject_nid("Account")
    contact_id = sobject_nid("Contact")
    opp_id = sobject_nid("Opportunity")

    all_nodes: list[dict] = [
        {"id": account_id, "label": "Account", "file_type": "sobject",
         "source_file": "objects/Account.object-meta.xml"},
        {"id": contact_id, "label": "Contact", "file_type": "sobject",
         "source_file": "objects/Contact.object-meta.xml"},
        {"id": opp_id, "label": "Opportunity", "file_type": "sobject",
         "source_file": "objects/Opportunity.object-meta.xml"},
        {"id": "apex_accounttrigger", "label": "AccountTrigger",
         "file_type": "code", "source_file": "triggers/AccountTrigger.trigger",
         "sf_code_type": "trigger"},
        {"id": "validation_contact_amount", "label": "ContactAmount",
         "file_type": "validation_rule",
         "source_file": "objects/Contact/ContactAmount.validationRule-meta.xml"},
    ]
    all_edges: list[dict] = [
        # Account has a trigger -> qualifies for OoE
        {"source": "apex_accounttrigger", "target": account_id,
         "relation": "triggers_on", "confidence": "EXTRACTED", "source_file": "x"},
        # Contact has a Validation Rule -> qualifies for OoE
        {"source": "validation_contact_amount", "target": contact_id,
         "relation": "validates", "confidence": "EXTRACTED", "source_file": "x"},
        # Opportunity is referenced only by SOQL -> must NOT get OoE
        {"source": "apex_accounttrigger", "target": opp_id, "relation": "queries",
         "confidence": "EXTRACTED", "sf_in_loop": False, "source_file": "x"},
    ]

    nodes_before = len(all_nodes)
    input_nodes_obj = all_nodes
    input_edges_obj = all_edges

    result = ooe_analysis_pass(all_nodes, all_edges)

    # In-place contract: returns None, mutates the same list objects.
    assert result is None
    assert all_nodes is input_nodes_obj
    assert all_edges is input_edges_obj

    # 1. 18 OoE nodes per qualifying SObject (Account + Contact); none for Opportunity.
    ooe_nodes = [n for n in all_nodes
                 if n.get("file_type") == "concept" and "sf_ooe_step" in n]
    assert len(ooe_nodes) == 36
    assert len(all_nodes) == nodes_before + 36
    account_ooe = [n for n in ooe_nodes if n["sf_ooe_sobject"] == account_id]
    contact_ooe = [n for n in ooe_nodes if n["sf_ooe_sobject"] == contact_id]
    assert len(account_ooe) == 18
    assert len(contact_ooe) == 18
    # SOQL-only Opportunity excluded.
    assert not any(n["sf_ooe_sobject"] == opp_id for n in ooe_nodes)
    # Steps numbered 1..18.
    assert {n["sf_ooe_step"] for n in account_ooe} == set(range(1, 19))

    # 2. order_of_execution edges chain the steps sequentially.
    ooe_edges = [e for e in all_edges if e["relation"] == "order_of_execution"]
    assert all(e["confidence"] == "EXTRACTED" for e in ooe_edges)
    dag = nx.DiGraph((e["source"], e["target"]) for e in ooe_edges)
    for i in range(1, 18):
        assert dag.has_edge(f"ooe_{account_id}_{i}", f"ooe_{account_id}_{i + 1}")
        assert dag.has_edge(f"ooe_{contact_id}_{i}", f"ooe_{contact_id}_{i + 1}")

    # 3. The order_of_execution subgraph is a DAG (no cycle).
    assert nx.is_directed_acyclic_graph(dag)

    # No dangling edges introduced.
    node_ids = {n["id"] for n in all_nodes}
    for e in all_edges:
        assert e["source"] in node_ids, f"dangling source: {e}"
        assert e["target"] in node_ids, f"dangling target: {e}"

    # Re-run safety: a second pass does not duplicate the chains.
    ooe_analysis_pass(all_nodes, all_edges)
    ooe_nodes_2 = [n for n in all_nodes
                   if n.get("file_type") == "concept" and "sf_ooe_step" in n]
    assert len(ooe_nodes_2) == 36


def test_governor_analysis() -> None:
    """Governor pass: SOQL/DML-in-loop violations + one sentinel node per type.

    The pass aggregates ``sf_in_loop`` ``queries`` / ``dml_operates_on`` edges
    per offending method into ``governor_violation`` edges, and materializes one
    sentinel ``concept`` node per violation type (ADR-004, no node explosion).
    """
    account_id = sobject_nid("Account")
    contact_id = sobject_nid("Contact")

    all_nodes: list[dict] = [
        {"id": account_id, "label": "Account", "file_type": "sobject",
         "source_file": "objects/Account.object-meta.xml"},
        {"id": contact_id, "label": "Contact", "file_type": "sobject",
         "source_file": "objects/Contact.object-meta.xml"},
        {"id": "apex_loopservice", "label": "LoopService", "file_type": "code",
         "source_file": "classes/LoopService.cls"},
        {"id": "apex_dmlservice", "label": "DmlService", "file_type": "code",
         "source_file": "classes/DmlService.cls"},
        {"id": "apex_cleanservice", "label": "CleanService", "file_type": "code",
         "source_file": "classes/CleanService.cls"},
    ]
    all_edges: list[dict] = [
        # LoopService: two SOQL queries inside a loop -> one aggregated violation.
        {"source": "apex_loopservice", "target": account_id, "relation": "queries",
         "confidence": "EXTRACTED", "sf_in_loop": True, "source_location": "L42",
         "source_file": "classes/LoopService.cls"},
        {"source": "apex_loopservice", "target": contact_id, "relation": "queries",
         "confidence": "EXTRACTED", "sf_in_loop": True, "source_location": "L45",
         "source_file": "classes/LoopService.cls"},
        # CleanService: a query NOT in a loop -> no violation.
        {"source": "apex_cleanservice", "target": account_id, "relation": "queries",
         "confidence": "EXTRACTED", "sf_in_loop": False, "source_location": "L10",
         "source_file": "classes/CleanService.cls"},
        # DmlService: a DML in a loop -> one DML violation.
        {"source": "apex_dmlservice", "target": account_id,
         "relation": "dml_operates_on", "confidence": "INFERRED", "sf_in_loop": True,
         "source_location": "L87", "source_file": "classes/DmlService.cls"},
    ]

    nodes_before = len(all_nodes)
    edges_before = len(all_edges)
    input_nodes_obj = all_nodes
    input_edges_obj = all_edges

    violations = governor_limit_analysis_pass(all_nodes, all_edges)

    # Contract: returns the diagnostic edges, mutates only all_nodes (sentinels).
    assert isinstance(violations, list)
    assert all_nodes is input_nodes_obj
    assert all_edges is input_edges_obj
    assert len(all_edges) == edges_before  # pass does not append edges itself

    by_id = {n["id"]: n for n in all_nodes}

    # 1. SOQL-in-loop detected for LoopService, aggregated (count == 2).
    soql_v = [v for v in violations if v["sf_violation_type"] == "soql_in_loop"]
    assert len(soql_v) == 1
    soql_edge = soql_v[0]
    assert soql_edge["source"] == "apex_loopservice"
    assert soql_edge["target"] == "gov_limit_soql_in_loop"
    assert soql_edge["relation"] == "governor_violation"
    assert soql_edge["confidence"] == "EXTRACTED"
    assert soql_edge["sf_violation_count"] == 2
    assert soql_edge["sf_severity"] == "HIGH"
    assert soql_edge["sf_limit"] == 100  # GOVERNOR_LIMITS soql_queries_per_transaction

    # 2. DML-in-loop detected for DmlService.
    dml_v = [v for v in violations if v["sf_violation_type"] == "dml_in_loop"]
    assert len(dml_v) == 1
    assert dml_v[0]["source"] == "apex_dmlservice"
    assert dml_v[0]["target"] == "gov_limit_dml_in_loop"
    assert dml_v[0]["sf_limit"] == 150

    # CleanService's non-loop query produced no violation.
    assert not any(v["source"] == "apex_cleanservice" for v in violations)

    # 3. Exactly one sentinel node per violation type (no node explosion).
    sentinels = [n for n in all_nodes if str(n["id"]).startswith("gov_limit_")]
    assert {n["id"] for n in sentinels} == {
        "gov_limit_soql_in_loop", "gov_limit_dml_in_loop"
    }
    assert len(sentinels) == 2
    assert len(all_nodes) == nodes_before + 2
    for s in sentinels:
        assert s["file_type"] == "concept"

    # 4. Violation edges reference existing nodes (no dangling once extended).
    node_ids = {n["id"] for n in all_nodes}
    for v in violations:
        assert v["source"] in node_ids, f"dangling source: {v}"
        assert v["target"] in node_ids, f"dangling target: {v}"

    # Re-run safety: sentinels are not duplicated on a second pass.
    governor_limit_analysis_pass(all_nodes, all_edges)
    sentinels_2 = [n for n in all_nodes if str(n["id"]).startswith("gov_limit_")]
    assert len(sentinels_2) == 2


def test_permission_analysis() -> None:
    """Permission pass: FLS-restricted SBQQ__ fields on CPQ objects -> risk edges.

    Cross-analyzes Profile/PermSet Field-Level Security (``sf_fls_permissions``)
    against ``cpq_rule`` nodes: a non-readable ``SBQQ__`` field whose object is a
    CPQ rule yields a ``gov_permission_violation`` risk edge (ADR-028). Only CPQ
    (SBQQ__) fields are analyzed; readable fields and non-CPQ fields are ignored.
    """
    quote_id = "cpq_quote"
    price_rule_id = "cpq_price_rule"

    all_nodes: list[dict] = [
        # CPQ rule nodes (post cpq_analysis_pass reclassification).
        {"id": quote_id, "label": "SBQQ__Quote__c", "file_type": "cpq_rule",
         "sf_cpq_object": "SBQQ__Quote__c",
         "source_file": "objects/SBQQ__Quote__c.object-meta.xml"},
        {"id": price_rule_id, "label": "SBQQ__PriceRule__c Discount",
         "file_type": "cpq_rule", "sf_cpq_object": "SBQQ__PriceRule__c Discount",
         "source_file": "objects/SBQQ__PriceRule__c.object-meta.xml"},
        # An Admin profile restricting a CPQ field, plus an unrelated profile.
        {"id": "profile_admin", "label": "Admin", "file_type": "profile",
         "source_file": "profiles/Admin.profile-meta.xml",
         "sf_fls_permissions": {
             # Restricted CPQ field on a CPQ object -> risk.
             "SBQQ__Quote__c.SBQQ__NetPrice__c": {"readable": False, "editable": False},
             # Readable CPQ field -> no risk.
             "SBQQ__Quote__c.SBQQ__ListPrice__c": {"readable": True, "editable": True},
             # Non-CPQ field (no SBQQ__) -> ignored even if restricted.
             "Account.BillingCity": {"readable": False, "editable": False},
         }},
        {"id": "permission_set_sales", "label": "Sales",
         "file_type": "permission_set",
         "source_file": "permissionsets/Sales.permissionset-meta.xml"},
    ]
    all_edges: list[dict] = []

    input_nodes_obj = all_nodes
    input_edges_obj = all_edges
    nodes_before = len(all_nodes)

    risks = permission_analysis_pass(all_nodes, all_edges)

    # Contract: pure function — returns risk edges, mutates neither list.
    assert isinstance(risks, list)
    assert all_nodes is input_nodes_obj
    assert all_edges is input_edges_obj
    assert all_edges == []
    assert len(all_nodes) == nodes_before

    # 1. Exactly one risk edge: the restricted CPQ field on the Quote CPQ object.
    assert len(risks) == 1
    risk = risks[0]
    assert risk["source"] == quote_id  # CPQ rule -> Profile (ADR-028)
    assert risk["target"] == "profile_admin"
    assert risk["relation"] == "gov_permission_violation"
    assert risk["sf_risk_type"] == "fls_restricted"
    assert risk["sf_field"] == "SBQQ__Quote__c.SBQQ__NetPrice__c"
    assert risk["sf_severity"] == "HIGH"
    assert risk["confidence"] == "INFERRED"
    assert risk["confidence_value"] == 0.75

    # 2. Readable CPQ field produced no risk; non-CPQ field ignored.
    assert all(r["sf_field"] != "SBQQ__Quote__c.SBQQ__ListPrice__c" for r in risks)
    assert all("Account" not in r["sf_field"] for r in risks)

    # 3. The PriceRule CPQ object has no restricted field -> not a risk source.
    assert all(r["source"] != price_rule_id for r in risks)

    # 4. Risk edges reference existing nodes (no dangling once extended).
    node_ids = {n["id"] for n in all_nodes}
    for r in risks:
        assert r["source"] in node_ids, f"dangling source: {r}"
        assert r["target"] in node_ids, f"dangling target: {r}"

    # Re-run safety: a pure pass yields identical edges (idempotent).
    risks_2 = permission_analysis_pass(all_nodes, all_edges)
    assert len(risks_2) == 1


def test_flow_cpq_loops() -> None:
    """Flow-CPQ loop pass: Type A (Direct) detection + safe-pattern downgrade.

    A Record-Triggered Flow that performs DML on a Quote object re-triggers
    itself once CPQ saves the recalculated Quote (ADR-029 Type A, CRITICAL). A
    flow carrying a recursion-prevention marker is still reported but tagged
    ``sf_loop_prevention`` and downgraded to INFO. Only Record-Triggered flows
    that update a Quote object qualify; Screen flows and non-Quote DML are out.
    """
    quote_id = sobject_nid("SBQQ__Quote__c")
    account_id = sobject_nid("Account")

    all_nodes: list[dict] = [
        {"id": quote_id, "label": "SBQQ__Quote__c", "file_type": "sobject",
         "source_file": "objects/SBQQ__Quote__c.object-meta.xml"},
        {"id": account_id, "label": "Account", "file_type": "sobject",
         "source_file": "objects/Account.object-meta.xml"},
        # Record-Triggered Flow updating Quote -> Type A direct loop (CRITICAL).
        {"id": "flow_quotesync", "label": "QuoteSync", "file_type": "flow",
         "source_file": "flows/QuoteSync.flow-meta.xml",
         "sf_trigger_object": "SBQQ__Quote__c", "sf_trigger_type": "CreateAndUpdate"},
        # Record-Triggered Flow updating Quote BUT guarded (isFirstRun) -> INFO.
        {"id": "flow_quoteguard", "label": "QuoteGuard", "file_type": "flow",
         "source_file": "flows/QuoteGuard.flow-meta.xml",
         "sf_trigger_object": "SBQQ__Quote__c", "sf_trigger_type": "Update",
         "sf_entry_conditions": "isFirstRun == true"},
        # Record-Triggered Flow updating Account (not Quote) -> no loop.
        {"id": "flow_accountnotify", "label": "AccountNotify", "file_type": "flow",
         "source_file": "flows/AccountNotify.flow-meta.xml",
         "sf_trigger_object": "Account", "sf_trigger_type": "Create"},
        # Screen Flow (NOT record-triggered) updating Quote -> no Type A loop.
        {"id": "flow_screenquote", "label": "ScreenQuote", "file_type": "flow",
         "source_file": "flows/ScreenQuote.flow-meta.xml",
         "sf_trigger_object": None, "sf_trigger_type": None},
    ]
    all_edges: list[dict] = [
        {"source": "flow_quotesync", "target": quote_id,
         "relation": "dml_operates_on", "sf_dml_type": "UPDATE",
         "confidence": "EXTRACTED", "source_file": "flows/QuoteSync.flow-meta.xml"},
        # Second Quote DML on the same flow -> must NOT create a second loop edge.
        {"source": "flow_quotesync", "target": sobject_nid("SBQQ__QuoteLine__c"),
         "relation": "dml_operates_on", "sf_dml_type": "UPDATE",
         "confidence": "EXTRACTED", "source_file": "flows/QuoteSync.flow-meta.xml"},
        {"source": "flow_quoteguard", "target": quote_id,
         "relation": "dml_operates_on", "sf_dml_type": "UPDATE",
         "confidence": "EXTRACTED", "source_file": "flows/QuoteGuard.flow-meta.xml"},
        {"source": "flow_accountnotify", "target": account_id,
         "relation": "dml_operates_on", "sf_dml_type": "UPDATE",
         "confidence": "EXTRACTED", "source_file": "flows/AccountNotify.flow-meta.xml"},
        {"source": "flow_screenquote", "target": quote_id,
         "relation": "dml_operates_on", "sf_dml_type": "UPDATE",
         "confidence": "EXTRACTED", "source_file": "flows/ScreenQuote.flow-meta.xml"},
    ]

    input_nodes_obj = all_nodes
    input_edges_obj = all_edges
    nodes_before = len(all_nodes)
    edges_before = len(all_edges)

    loops = detect_flow_cpq_loops(all_nodes, all_edges)

    # Contract: pure function — returns loop edges, mutates neither list.
    assert isinstance(loops, list)
    assert all_nodes is input_nodes_obj
    assert all_edges is input_edges_obj
    assert len(all_nodes) == nodes_before
    assert len(all_edges) == edges_before

    by_source = {e["source"]: e for e in loops}

    # 1. Flow -> Quote update detected; exactly two loops (sync + guard).
    assert set(by_source) == {"flow_quotesync", "flow_quoteguard"}
    assert len(loops) == 2  # quotesync deduped despite two Quote DML edges

    # 2. Type A (Direct) loop for the unguarded flow -> CRITICAL self-edge.
    sync = by_source["flow_quotesync"]
    assert sync["source"] == sync["target"] == "flow_quotesync"  # self-loop
    assert sync["relation"] == "infinite_loop_risk"
    assert sync["confidence"] == "INFERRED"
    assert sync["confidence_value"] == 0.85
    assert sync["sf_loop_type"] == "A_DIRECT"
    assert sync["sf_severity"] == "CRITICAL"
    assert sync["sf_loop_prevention"] is False

    # 3. Safe-pattern detected on the guarded flow -> downgraded to INFO.
    guard = by_source["flow_quoteguard"]
    assert guard["sf_loop_type"] == "A_DIRECT"
    assert guard["sf_loop_prevention"] is True
    assert guard["sf_severity"] == "INFO"

    # 4. Non-Quote DML and Screen (non record-triggered) flows produce no loop.
    assert "flow_accountnotify" not in by_source
    assert "flow_screenquote" not in by_source

    # 5. Self-edges reference existing nodes (no dangling once extended).
    node_ids = {n["id"] for n in all_nodes}
    for loop in loops:
        assert loop["source"] in node_ids, f"dangling source: {loop}"
        assert loop["target"] in node_ids, f"dangling target: {loop}"

    # Re-run safety: a pure pass yields identical results (idempotent).
    loops_2 = detect_flow_cpq_loops(all_nodes, all_edges)
    assert len(loops_2) == 2


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


# ---------------------------------------------------------------------------
# Neo4j export (Cypher generation + live driver push)
# ---------------------------------------------------------------------------


def _sf_neo4j_graph() -> nx.DiGraph:
    """A small SF graph exercising every node type and relation mapping."""
    account_id = sobject_nid("Account")
    quote_id = sobject_nid("SBQQ__Quote__c")
    G = nx.DiGraph()
    G.add_node(account_id, label="Account", file_type="sobject",
               source_file="objects/Account.object-meta.xml")
    G.add_node(quote_id, label="SBQQ__Quote__c", file_type="cpq_rule",
               source_file="objects/SBQQ__Quote__c.object-meta.xml")
    G.add_node("apex_accountservice", label="AccountService", file_type="code",
               source_file="classes/AccountService.cls")
    G.add_node("flow_accountflow", label="AccountFlow", file_type="flow",
               source_file="flows/AccountFlow.flow-meta.xml")
    G.add_node("lwc_accountcard", label="accountCard", file_type="lwc_component",
               source_file="lwc/accountCard")
    G.add_node("profile_admin", label="Admin", file_type="profile",
               source_file="profiles/Admin.profile-meta.xml")
    G.add_node("gov_limit_soql_in_loop", label="SOQL in loop", file_type="concept")

    G.add_edge("apex_accountservice", account_id, relation="queries",
               confidence="EXTRACTED", sf_in_loop=True, source_location="L42")
    G.add_edge("flow_accountflow", "apex_accountservice", relation="flow_invokes",
               confidence="EXTRACTED")
    G.add_edge("lwc_accountcard", "apex_accountservice", relation="wire_to",
               confidence="EXTRACTED")
    G.add_edge("profile_admin", account_id, relation="grants_access_to",
               confidence="EXTRACTED")
    G.add_edge("apex_accountservice", quote_id, relation="cpq_applies_to",
               confidence="INFERRED", execution_order=4)
    G.add_edge("apex_accountservice", "gov_limit_soql_in_loop",
               relation="governor_violation", confidence="EXTRACTED",
               sf_violation_type="soql_in_loop", sf_severity="HIGH")
    # A second edge between the same pair would collapse in a DiGraph, so the
    # trigger relation is carried by a dedicated SObject-less trigger node.
    G.add_node("trigger_accounttrigger", label="AccountTrigger", file_type="code",
               source_file="triggers/AccountTrigger.trigger")
    G.add_edge("trigger_accounttrigger", account_id, relation="triggers_on",
               confidence="EXTRACTED")
    return G


def test_neo4j_export(tmp_path: Path) -> None:
    """to_cypher_sf writes Neo4j-import-ready MERGE statements for SF graphs."""
    G = _sf_neo4j_graph()
    out = tmp_path / "graph_sf.cypher"

    to_cypher_sf(G, str(out))

    # 1. Cypher file is created and non-empty.
    assert out.exists()
    text = out.read_text(encoding="utf-8")
    assert text.strip()

    statements = [s for s in text.split(";") if s.strip()]
    node_stmts = [s for s in statements if "MERGE (n:" in s]
    rel_stmts = [s for s in statements if "MERGE (a)" in s]

    # 2. Every node yields a MERGE statement with the SF-aware Neo4j label.
    assert len(node_stmts) == G.number_of_nodes()
    assert ":SObject " in text       # sobject -> SObject
    assert ":ApexClass " in text     # code -> ApexClass
    assert ":Flow " in text          # flow -> Flow
    assert ":LWCComponent " in text  # lwc_component -> LWCComponent
    assert ":CPQRule " in text       # cpq_rule -> CPQRule
    assert ":Profile " in text       # profile -> Profile
    assert ":Concept " in text       # concept -> Concept

    # 3. Every edge yields a relationship MERGE with the SF-aware Neo4j type.
    assert len(rel_stmts) == G.number_of_edges()
    for neo_type in ("TRIGGERS_ON", "QUERIES", "FLOW_INVOKES", "WIRE_TO",
                     "GRANTS_ACCESS_TO", "CPQ_APPLIES_TO", "GOVERNOR_VIOLATION"):
        assert f":{neo_type} " in text or f":{neo_type}]" in text, neo_type

    # 4. Mapping tables cover the declared SF vocabulary.
    assert SF_NODE_TYPE_TO_NEO4J_LABEL["sobject"] == "SObject"
    assert SF_RELATION_TO_NEO4J_TYPE["triggers_on"] == "TRIGGERS_ON"

    # 5. Edge attributes are carried into the relationship.
    assert "execution_order" in text
    assert "sf_violation_type" in text


def test_neo4j_push_live(monkeypatch) -> None:
    """push_to_neo4j_sf upserts every node/edge via a (mocked) driver session."""
    import sys
    import types

    runs: list[str] = []

    class _FakeSession:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def run(self, query, **params):
            runs.append(query)

    class _FakeDriver:
        def __init__(self):
            self.closed = False

        def session(self):
            return _FakeSession()

        def close(self):
            self.closed = True

    class _FakeGraphDatabase:
        @staticmethod
        def driver(uri, auth=None):
            return _FakeDriver()

    fake_neo4j = types.ModuleType("neo4j")
    fake_neo4j.GraphDatabase = _FakeGraphDatabase
    monkeypatch.setitem(sys.modules, "neo4j", fake_neo4j)

    G = _sf_neo4j_graph()
    result = push_to_neo4j_sf(G, "bolt://localhost:7687", "neo4j", "pw")

    assert result["success"] is True
    assert result["nodes_created"] == G.number_of_nodes()
    assert result["relationships_created"] == G.number_of_edges()
    # One MERGE per node + one per edge.
    assert len(runs) == G.number_of_nodes() + G.number_of_edges()
    # SF-aware labels reach the driver in node MERGE queries.
    assert any(":SObject" in q for q in runs)
    assert any(":CPQRule" in q for q in runs)
    # SF-aware relation types reach the driver in edge MERGE queries.
    assert any(":TRIGGERS_ON" in q for q in runs)


# ---------------------------------------------------------------------------
# Release sync (Phase 3, Step 1)
# ---------------------------------------------------------------------------


def test_release_sync(tmp_path, monkeypatch) -> None:
    """sync_release_notes writes a version file, detects staleness, dry-runs."""
    import graphify.salesforce.release_sync as rs

    # 1. Normal run writes a well-formed JSON version file.
    result = sync_release_notes(tmp_path)
    version_file = tmp_path / "sf_governor_limits_version.json"
    assert version_file.exists()

    data = json.loads(version_file.read_text(encoding="utf-8"))
    assert data["version"] == result["version"]
    assert isinstance(data["limits"], dict) and data["limits"]
    # last_synced is a valid ISO timestamp.
    datetime.fromisoformat(data["last_synced"])

    # Return contract.
    assert set(result) >= {"changed", "new_limits", "version", "days_stale", "status"}
    # Offline default mirrors hardcoded constants -> no drift.
    assert result["changed"] == 0
    assert result["status"] in {"FRESH", "WARNING", "ERROR"}

    # 2. Staleness is detected from the written file (FRESH right after write).
    staleness = check_staleness(version_file)
    assert staleness["days_stale"] == 0
    assert staleness["status"] == "FRESH"

    # Threshold mapping is correct at the WARNING / ERROR boundaries.
    assert rs._staleness_status(0) == "FRESH"
    assert rs._staleness_status(30) == "FRESH"
    assert rs._staleness_status(31) == "WARNING"
    assert rs._staleness_status(90) == "WARNING"
    assert rs._staleness_status(91) == "ERROR"

    # 3. --dry-run previews without writing a file.
    fresh_dir = tmp_path / "dry"
    dry = sync_release_notes(fresh_dir, dry_run=True)
    assert dry["dry_run"] is True
    assert not (fresh_dir / "sf_governor_limits_version.json").exists()

    # 4. A drift in the fetched limits is counted as a change (mock backend).
    monkeypatch.setattr(
        rs, "_fetch_release_limits", lambda: {"soql_queries_per_transaction": 200}
    )
    changed = sync_release_notes(tmp_path / "changed")
    assert changed["changed"] >= 1
