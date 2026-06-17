"""Salesforce-specific parser / analysis-pass tests (graphify-sf)."""

from __future__ import annotations

from pathlib import Path

from graphify.salesforce.apex_enhanced import extract_apex_enhanced
from graphify.salesforce.constants import sobject_nid
from graphify.salesforce.flow import extract_flow
from graphify.salesforce.lwc import extract_lwc_html, extract_lwc_js
from graphify.salesforce.objects import extract_custom_object
from graphify.salesforce.profiles import extract_permission_set, extract_profile

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
