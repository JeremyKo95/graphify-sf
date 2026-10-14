"""
graphify-sf: additional metadata parsers.

Extends coverage beyond the core parsers to common Enterprise metadata that
connects into the existing graph:

    - ``extract_record_type``  — ``objects/<Obj>/recordTypes/<RT>.recordType-meta.xml``
      -> ``record_type`` node + ``record_type_of`` edge to its SObject. Record
      types are referenced by Flows (e.g. ``Get_Builder_Quote_RT``), Apex and
      layouts, so they are real graph endpoints.
    - ``extract_permission_set_group`` — ``*.permissionsetgroup-meta.xml`` ->
      ``permission_set_group`` node + ``contains_permission_set`` edges to each
      member Permission Set (resolving to the same ``permission_set_<name>`` ID
      ``profiles.py`` emits).
    - ``extract_workflow`` — ``workflows/<Object>.workflow-meta.xml`` ->
      ``workflow`` node + a ``dml_operates_on`` (UPDATE) edge per ``<fieldUpdates>``
      so legacy Workflow automation joins the OoE / recursion / impact analysis
      alongside triggers and flows.
    - ``extract_custom_metadata_record`` — ``customMetadata/<Type>.<Record>.md-meta.xml``
      -> ``cmt_record`` node + ``cmt_record_of`` edge to its ``<Type>__mdt`` SObject.
      The ``<values>`` field/value pairs are config DATA (e.g.
      ``Opportunity_To_Quote_Mapping`` records that drive Opp->Quote field mapping),
      so they are kept on the node as ``sf_values``.
    - ``extract_sharing_rules`` — ``<Object>.sharingRules-meta.xml`` ->
      ``sharing_rule`` node per owner/criteria rule + ``shares`` edge to the SObject.
    - ``extract_custom_labels`` — ``*.labels-meta.xml`` -> one ``custom_label`` node
      per ``<labels>`` (reference targets for ``$Label.X`` in Apex/Flow/LWC).

    - ``extract_rule_entries`` — ``assignmentRules`` / ``escalationRules`` /
      ``autoResponseRules`` -> one node per rule + ``rule_of`` its SObject; the
      fields in ``criteriaItems`` become field reads.
    - ``extract_duplicate_rule`` / ``extract_matching_rules`` — duplicate rules
      (``rule_of`` + ``uses_matching_rule``) and matching rules (``rule_of``);
      filter / matching fields become field reads.
    - ``extract_auth_provider`` — ``authproviders/<Name>.authprovider-meta.xml``
      -> ``auth_provider`` node (+ ``apex_handler`` to its registration handler
      / plugin class). Consumer keys and secrets are never read into the graph.
    - ``extract_layout`` — ``layouts/<Obj>-<Name>.layout-meta.xml`` -> ``layout``
      node, ``layout_for`` its SObject, ``places_field`` per placed field.

SObject IDs resolve via ``sobject_nid`` (ADR-002); parsers emit stub target nodes
so results carry no dangling edges (ADR-012). XML parse failures degrade to a
single ``concept`` error node (ADR-009 lenient).
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import unquote

from graphify.salesforce.constants import apex_nid, metadata_nid, sobject_nid, sobject_stub
from graphify.salesforce.field_refs import field_ref

_NS = {"md": "http://soap.sforce.com/2006/04/metadata"}


def _normalize(name: str) -> str:
    return name.lower().replace("__c", "").replace("__", "_")


def _error_node(node_id: str, path: Path, exc: Exception) -> dict:
    return {
        "nodes": [{
            "id": node_id, "label": f"[Parse Error] {path.name}",
            "file_type": "concept", "source_file": str(path),
            "sf_error_type": "xml_parse_error", "sf_error_message": str(exc),
        }],
        "edges": [],
    }


def _sobject_stub(nodes: list[dict], object_api: str, path: Path) -> str:
    sid = sobject_nid(object_api)
    if not any(n["id"] == sid for n in nodes):
        nodes.append(sobject_stub(object_api, path))
    return sid


def _first_child_text(elem) -> str | None:
    """Return the first non-empty child text of ``elem`` (e.g. the ``<group>`` /
    ``<role>`` inside a ``<sharedTo>``). ``None`` if absent."""
    if elem is None:
        return None
    for child in elem:
        text = (child.text or "").strip()
        if text:
            return text
    return None


def _parent_from_path(path: Path, marker: str) -> str | None:
    """Return the ``<Object>`` dir in ``objects/<Object>/<marker>/<file>``."""
    parts = path.parts
    if marker in parts:
        idx = parts.index(marker)
        if idx >= 1:
            return parts[idx - 1]
    return None


def extract_record_type(path: Path) -> dict:
    """Parse ``*.recordType-meta.xml`` -> record_type node + record_type_of edge."""
    path = Path(path)
    object_api = _parent_from_path(path, "recordTypes")
    rt_name = path.name.split(".")[0]
    rt_id = f"recordtype_{_normalize(object_api or path.stem)}_{rt_name.lower()}"
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as exc:
        return _error_node(rt_id, path, exc)

    label = root.findtext("md:label", namespaces=_NS) or rt_name
    active = root.findtext("md:active", namespaces=_NS) != "false"
    nodes: list[dict] = [{
        "id": rt_id, "label": label, "file_type": "record_type",
        "source_file": str(path), "sf_active": active,
        "sf_object": object_api,
        "sf_qualified_name": f"{object_api}.{rt_name}" if object_api else rt_name,
    }]
    edges: list[dict] = []
    if object_api:
        edges.append({
            "source": rt_id, "target": _sobject_stub(nodes, object_api, path),
            "relation": "record_type_of", "confidence": "EXTRACTED",
            "source_file": str(path),
        })
    return {"nodes": nodes, "edges": edges}


def extract_permission_set_group(path: Path) -> dict:
    """Parse ``*.permissionsetgroup-meta.xml`` -> PSG node + member edges."""
    path = Path(path)
    name = path.name.split(".")[0]
    psg_id = f"psg_{name.lower()}"
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as exc:
        return _error_node(psg_id, path, exc)

    label = root.findtext("md:label", namespaces=_NS) or name
    nodes: list[dict] = [{
        "id": psg_id, "label": label, "file_type": "permission_set_group",
        "source_file": str(path),
    }]
    edges: list[dict] = []
    for ps in root.findall("md:permissionSets", namespaces=_NS):
        ps_name = (ps.text or "").strip()
        if not ps_name:
            continue
        ps_id = f"permission_set_{ps_name.lower()}"
        if not any(n["id"] == ps_id for n in nodes):
            nodes.append({
                "id": ps_id, "label": ps_name, "file_type": "permission_set",
                "source_file": str(path),
            })
        edges.append({
            "source": psg_id, "target": ps_id, "relation": "contains_permission_set",
            "confidence": "EXTRACTED", "source_file": str(path),
        })
    return {"nodes": nodes, "edges": edges}


def extract_workflow(path: Path) -> dict:
    """Parse ``workflows/<Object>.workflow-meta.xml``.

    The file is named after its SObject. Each ``<fieldUpdates>`` becomes a
    ``dml_operates_on`` (UPDATE) edge from the workflow node to that SObject, so
    Workflow field updates participate in automation / recursion / impact
    analysis the same way trigger and flow DML does.
    """
    path = Path(path)
    object_api = path.name.split(".")[0]
    wf_id = f"workflow_{object_api.lower()}"
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as exc:
        return _error_node(wf_id, path, exc)

    nodes: list[dict] = [{
        "id": wf_id, "label": object_api, "file_type": "workflow",
        "source_file": str(path), "sf_object": object_api,
        "sf_active_rules": sum(
            1 for rule in root.findall("md:rules", namespaces=_NS)
            if rule.findtext("md:active", namespaces=_NS) == "true"),
    }]
    edges: list[dict] = []
    field_updates = root.findall("md:fieldUpdates", namespaces=_NS)
    if field_updates:
        target = _sobject_stub(nodes, object_api, path)
        for fu in field_updates:
            edges.append({
                "source": wf_id, "target": target, "relation": "dml_operates_on",
                "sf_dml_type": "UPDATE", "confidence": "EXTRACTED",
                "source_file": str(path),
                "sf_workflow_field": fu.findtext("md:field", namespaces=_NS),
                "sf_automation": "workflow",
            })
    return {"nodes": nodes, "edges": edges}


def extract_custom_metadata_record(path: Path) -> dict:
    """Parse ``customMetadata/<Type>.<Record>.md-meta.xml``.

    Filename convention: ``<TypeDeveloperName>.<RecordDeveloperName>.md-meta.xml``.
    The type resolves to the ``<Type>__mdt`` SObject (its own object-meta.xml
    becomes that node). Each ``<values>`` (``<field>``/``<value>``) pair is config
    data kept on the node as ``sf_values`` so impact analysis can see, e.g., the
    Opportunity->Quote field mappings the record encodes.
    """
    path = Path(path)
    suffix = ".md-meta.xml"
    base = path.name[: -len(suffix)] if path.name.endswith(suffix) else path.stem
    type_dev, _, record_dev = base.partition(".")
    type_api = f"{type_dev}__mdt"
    rec_id = f"cmt_{type_dev.lower()}_{record_dev.lower()}" if record_dev else f"cmt_{type_dev.lower()}"
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as exc:
        return _error_node(rec_id, path, exc)

    label = root.findtext("md:label", namespaces=_NS) or record_dev or type_dev
    protected = root.findtext("md:protected", namespaces=_NS) == "true"
    values: dict[str, str] = {}
    for v in root.findall("md:values", namespaces=_NS):
        field = (v.findtext("md:field", namespaces=_NS) or "").strip()
        if field:
            values[field] = (v.findtext("md:value", namespaces=_NS) or "").strip()
    nodes: list[dict] = [{
        "id": rec_id, "label": label, "file_type": "cmt_record",
        "source_file": str(path), "sf_mdt_type": type_api,
        "sf_record_name": record_dev, "sf_protected": protected,
        "sf_values": values,
    }]
    edges: list[dict] = [{
        "source": rec_id, "target": _sobject_stub(nodes, type_api, path),
        "relation": "cmt_record_of", "confidence": "EXTRACTED",
        "source_file": str(path),
    }]
    return {"nodes": nodes, "edges": edges}


#: Sharing rule container tags (one file may mix several kinds).
_SHARING_RULE_TAGS = (
    "sharingOwnerRules", "sharingCriteriaRules",
    "sharingGuestRules", "sharingTerritoryRules",
)


def extract_sharing_rules(path: Path) -> dict:
    """Parse ``<Object>.sharingRules-meta.xml`` -> sharing_rule nodes + shares edges.

    The file is named after its SObject. Each owner/criteria/guest/territory rule
    becomes a ``sharing_rule`` node with a ``shares`` edge to that SObject, so the
    access model joins permission / impact analysis alongside Profiles and
    Permission Sets.
    """
    path = Path(path)
    object_api = path.name.split(".")[0]
    set_id = f"sharingrules_{object_api.lower()}"
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as exc:
        return _error_node(set_id, path, exc)

    nodes: list[dict] = []
    edges: list[dict] = []
    target: str | None = None
    for tag in _SHARING_RULE_TAGS:
        for rule in root.findall(f"md:{tag}", namespaces=_NS):
            full = (rule.findtext("md:fullName", namespaces=_NS) or "").strip()
            if not full:
                continue
            rid = f"sharing_{object_api.lower()}_{full.lower()}"
            access = rule.findtext("md:accessLevel", namespaces=_NS)
            label = rule.findtext("md:label", namespaces=_NS) or full
            nodes.append({
                "id": rid, "label": label, "file_type": "sharing_rule",
                "source_file": str(path), "sf_object": object_api,
                "sf_access_level": access, "sf_rule_type": tag,
                "sf_shared_to": _first_child_text(rule.find("md:sharedTo", namespaces=_NS)),
            })
            if target is None:
                target = _sobject_stub(nodes, object_api, path)
            edges.append({
                "source": rid, "target": target, "relation": "shares",
                "confidence": "EXTRACTED", "source_file": str(path),
                "sf_access_level": access,
            })
    return {"nodes": nodes, "edges": edges}


#: Cap stored label text so a verbose label can't bloat a node (ADR-012 token-aware).
_LABEL_VALUE_CAP = 200


def extract_custom_labels(path: Path) -> dict:
    """Parse ``*.labels-meta.xml`` -> one ``custom_label`` node per ``<labels>``.

    Labels are reference targets (``$Label.X`` / ``System.Label.X``) for Apex,
    Flow and LWC. Nodes are emitted without edges until reference extraction
    resolves callers; the stored value is capped to keep nodes compact.
    """
    path = Path(path)
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as exc:
        return _error_node(f"labelset_{path.name.split('.')[0].lower()}", path, exc)

    nodes: list[dict] = []
    for lbl in root.findall("md:labels", namespaces=_NS):
        full = (lbl.findtext("md:fullName", namespaces=_NS) or "").strip()
        if not full:
            continue
        value = (lbl.findtext("md:value", namespaces=_NS) or "").strip()
        nodes.append({
            "id": f"label_{full.lower()}", "label": full,
            "file_type": "custom_label", "source_file": str(path),
            "sf_value": value[:_LABEL_VALUE_CAP],
            "sf_categories": lbl.findtext("md:categories", namespaces=_NS),
            "sf_language": lbl.findtext("md:language", namespaces=_NS),
            "sf_protected": lbl.findtext("md:protected", namespaces=_NS) == "true",
        })
    return {"nodes": nodes, "edges": []}


#: Rule-entry metadata files: root child tag -> node ``file_type``.
_RULE_ENTRY_TAGS = {
    "assignmentRule": "assignment_rule",
    "escalationRule": "escalation_rule",
    "autoResponseRule": "auto_response_rule",
}


def _text(elem, tag: str) -> str | None:
    value = elem.findtext(f"md:{tag}", namespaces=_NS)
    return value.strip() if value and value.strip() else None


def _qualified_field_ref(source: str, qualified: str, path: Path, default_object: str,
                         relation: str = "reads_field", **attrs) -> dict:
    """Field reference for ``Obj.Field`` (or a bare field on *default_object*)."""
    obj, _, field = qualified.rpartition(".")
    return field_ref(source, obj or default_object, field, relation, path,
                     confidence="EXTRACTED", **attrs)


def extract_rule_entries(path: Path) -> dict:
    """Parse ``<Obj>.assignmentRules|escalationRules|autoResponseRules-meta.xml``.

    The file is named after its SObject and holds several named rules, each
    with ordered ``ruleEntry`` criteria. A rule becomes a node (``rule_of`` its
    object, ``sf_assigned_to`` = the queues / users its entries route to); the
    ``criteriaItems`` fields (``Lead.Region__c``) become ``reads_field``.
    """
    path = Path(path)
    object_api = path.name.split(".")[0]
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as exc:
        return _error_node(metadata_nid("rules", object_api), path, exc)

    nodes: list[dict] = []
    edges: list[dict] = []
    field_refs: list[dict] = []
    for tag, file_type in _RULE_ENTRY_TAGS.items():
        for rule in root.findall(f"md:{tag}", namespaces=_NS):
            full = _text(rule, "fullName")
            if not full:
                continue
            rid = metadata_nid(file_type, object_api, full)
            entries = rule.findall("md:ruleEntry", namespaces=_NS)
            nodes.append({
                "id": rid, "label": full, "file_type": file_type,
                "source_file": str(path), "sf_object": object_api,
                "sf_qualified_name": f"{object_api}.{full}",
                "sf_active": _text(rule, "active") == "true",
                "sf_entry_count": len(entries),
                "sf_assigned_to": sorted({
                    a for a in (_text(e, "assignedTo") for e in entries) if a}),
            })
            edges.append({
                "source": rid, "target": _sobject_stub(nodes, object_api, path),
                "relation": "rule_of", "confidence": "EXTRACTED",
                "source_file": str(path),
            })
            seen: set[str] = set()
            for item in rule.findall("md:ruleEntry/md:criteriaItems", namespaces=_NS):
                field = _text(item, "field")
                if field and field not in seen:
                    seen.add(field)
                    field_refs.append(_qualified_field_ref(rid, field, path, object_api))
    return {"nodes": nodes, "edges": edges, "field_refs": field_refs}


def _matching_rule_stub(object_api: str, name: str, path: Path) -> dict:
    return {
        "id": metadata_nid("matching_rule", object_api, name), "label": name,
        "file_type": "matching_rule", "source_file": str(path),
        "sf_object": object_api, "sf_qualified_name": f"{object_api}.{name}",
        "sf_stub": True,
    }


def extract_duplicate_rule(path: Path) -> dict:
    """Parse ``duplicateRules/<Obj>.<Rule>.duplicateRule-meta.xml``.

    One ``duplicate_rule`` node, ``rule_of`` its object and
    ``uses_matching_rule`` each matching rule it applies (stubbed until the
    matching rule's own file — or a standard rule — supplies it).
    """
    path = Path(path)
    object_api, _, rest = path.name.partition(".")
    rule_name = rest[: -len(".duplicateRule-meta.xml")]
    rid = metadata_nid("duplicate_rule", object_api, rule_name)
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as exc:
        return _error_node(rid, path, exc)

    nodes: list[dict] = [{
        "id": rid, "label": _text(root, "masterLabel") or rule_name,
        "file_type": "duplicate_rule", "source_file": str(path),
        "sf_object": object_api, "sf_qualified_name": f"{object_api}.{rule_name}",
        "sf_active": _text(root, "isActive") == "true",
        "sf_action_on_insert": _text(root, "actionOnInsert"),
        "sf_action_on_update": _text(root, "actionOnUpdate"),
    }]
    edges: list[dict] = [{
        "source": rid, "target": _sobject_stub(nodes, object_api, path),
        "relation": "rule_of", "confidence": "EXTRACTED", "source_file": str(path),
    }]
    for match in root.findall("md:duplicateRuleMatchRules", namespaces=_NS):
        name = _text(match, "matchingRule")
        if not name:
            continue
        stub = _matching_rule_stub(_text(match, "matchRuleSObjectType") or object_api,
                                   name, path)
        if not any(n["id"] == stub["id"] for n in nodes):
            nodes.append(stub)
        edges.append({
            "source": rid, "target": stub["id"], "relation": "uses_matching_rule",
            "confidence": "EXTRACTED", "source_file": str(path),
        })
    field_refs = [
        field_ref(rid, _text(item, "table") or object_api, _text(item, "field"),
                  "reads_field", path, confidence="EXTRACTED")
        for item in root.findall("md:duplicateRuleFilter/md:duplicateRuleFilterItems",
                                 namespaces=_NS)
        if _text(item, "field")
    ]
    return {"nodes": nodes, "edges": edges, "field_refs": field_refs}


def extract_matching_rules(path: Path) -> dict:
    """Parse ``matchingRules/<Obj>.matchingRule-meta.xml`` (several rules)."""
    path = Path(path)
    object_api = path.name.split(".")[0]
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as exc:
        return _error_node(metadata_nid("matching_rules", object_api), path, exc)

    nodes: list[dict] = []
    edges: list[dict] = []
    field_refs: list[dict] = []
    for rule in root.findall("md:matchingRules", namespaces=_NS):
        full = _text(rule, "fullName")
        if not full:
            continue
        node = _matching_rule_stub(object_api, full, path)
        del node["sf_stub"]
        node["label"] = _text(rule, "label") or full
        node["sf_active"] = _text(rule, "ruleStatus") == "Active"
        nodes.append(node)
        edges.append({
            "source": node["id"], "target": _sobject_stub(nodes, object_api, path),
            "relation": "rule_of", "confidence": "EXTRACTED", "source_file": str(path),
        })
        for item in rule.findall("md:matchingRuleItems", namespaces=_NS):
            field = _text(item, "fieldName")
            if field:
                field_refs.append(field_ref(node["id"], object_api, field, "reads_field",
                                            path, confidence="EXTRACTED"))
    return {"nodes": nodes, "edges": edges, "field_refs": field_refs}


def extract_auth_provider(path: Path) -> dict:
    """Parse ``authproviders/<Name>.authprovider-meta.xml``.

    Only identity and type are kept; ``consumerKey`` / ``consumerSecret`` and
    endpoint URLs are deliberately never read. A ``registrationHandler`` or
    custom ``plugin`` class becomes an ``apex_handler`` edge.
    """
    path = Path(path)
    name = path.name.split(".")[0]
    pid = metadata_nid("auth_provider", name)
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as exc:
        return _error_node(pid, path, exc)

    nodes: list[dict] = [{
        "id": pid, "label": _text(root, "friendlyName") or name,
        "file_type": "auth_provider", "source_file": str(path),
        "sf_qualified_name": name, "sf_provider_type": _text(root, "providerType"),
    }]
    edges: list[dict] = []
    for tag in ("registrationHandler", "plugin"):
        cls = _text(root, tag)
        if not cls:
            continue
        cid = apex_nid(cls)
        if not any(n["id"] == cid for n in nodes):
            nodes.append({"id": cid, "label": cls, "file_type": "code",
                          "source_file": str(path), "sf_stub": True})
        edges.append({
            "source": pid, "target": cid, "relation": "apex_handler",
            "confidence": "EXTRACTED", "source_file": str(path), "sf_handler_role": tag,
        })
    return {"nodes": nodes, "edges": edges}


def extract_layout(path: Path) -> dict:
    """Parse ``layouts/<Obj>-<Layout Name>.layout-meta.xml`` (URL-encoded name).

    The ``layout`` node is ``layout_for`` its object; every field placed in a
    section becomes a ``places_field`` reference carrying its ``behavior``
    (Edit / Required / Readonly). Related-list columns are not resolved.
    """
    path = Path(path)
    full = unquote(path.name[: -len(".layout-meta.xml")])
    object_api, _, layout_name = full.partition("-")
    lid = metadata_nid("layout", full)
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as exc:
        return _error_node(lid, path, exc)

    nodes: list[dict] = [{
        "id": lid, "label": layout_name or full, "file_type": "layout",
        "source_file": str(path), "sf_object": object_api, "sf_qualified_name": full,
    }]
    edges: list[dict] = [{
        "source": lid, "target": _sobject_stub(nodes, object_api, path),
        "relation": "layout_for", "confidence": "EXTRACTED", "source_file": str(path),
    }]
    behaviors: dict[str, str | None] = {}
    for item in root.findall("md:layoutSections/md:layoutColumns/md:layoutItems",
                             namespaces=_NS):
        field = _text(item, "field")
        if field and field not in behaviors:
            behaviors[field] = _text(item, "behavior")
    field_refs = [
        field_ref(lid, object_api, field, "places_field", path,
                  confidence="EXTRACTED", sf_behavior=behavior)
        for field, behavior in behaviors.items()
    ]
    return {"nodes": nodes, "edges": edges, "field_refs": field_refs}
