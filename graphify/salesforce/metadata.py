"""
graphify-sf: additional metadata parsers (RecordType, Permission Set Group, Workflow).

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

All three resolve SObject IDs via ``sobject_nid`` (ADR-002) and emit stub target
nodes so results carry no dangling edges (ADR-012). XML parse failures degrade to
a single ``concept`` error node (ADR-009 lenient).
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

from graphify.salesforce.constants import sobject_nid

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
        nodes.append({
            "id": sid, "label": object_api, "file_type": "sobject",
            "source_file": str(path),
            "sf_object_type": "custom" if "__c" in object_api else "standard",
        })
    return sid


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
