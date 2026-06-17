"""
graphify-sf: Flow metadata parser.

Parses Salesforce ``*.flow-meta.xml`` (Flow / Process Builder) metadata into
graph nodes / edges:

    - ``flow`` node per Flow file, carrying the record-trigger object / type
      read from ``<start>`` (when present).
    - ``queries`` edge per ``recordLookup`` element -> referenced SObject.
    - ``dml_operates_on`` edge per ``recordCreate`` / ``recordUpdate`` /
      ``recordDelete`` element -> referenced SObject (with ``sf_dml_type``).
    - ``flow_invokes`` edge per ``apexAction`` element -> invoked Apex class.

CRITICAL (ADR-002): SObject node IDs are built ONLY via
``constants.sobject_nid()`` so the Apex, Flow and Object parsers converge on the
same node in ``build_graph()`` (cross-file resolution).

Error handling (ARCHITECTURE.md "XML 파싱 실패"): XML parse failures degrade
gracefully — a single ``concept`` error node is returned and the file is
skipped, never raising so the rest of the repo keeps analyzing (ADR-009 lenient
mode). Unknown element types are ignored silently (no error) — Flow logic
(Decision conditions, Assignments) is intentionally not analyzed.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

from graphify.salesforce.constants import sobject_nid

#: Salesforce metadata XML namespace.
_NS = {"md": "http://soap.sforce.com/2006/04/metadata"}

#: Flow record-DML element type -> Apex-style DML verb.
_DML_TYPE_BY_ELEMENT = {
    "recordCreate": "INSERT",
    "recordUpdate": "UPDATE",
    "recordDelete": "DELETE",
}


def _apex_nid(class_name: str) -> str:
    """Build a node ID for an Apex class referenced from a Flow apexAction.

    Matches the convention shared with the LWC / Apex parsers
    (``apex_<lowercased-class-name>``) so ``flow_invokes`` edges resolve to the
    same Apex node in ``build_graph()``.
    """
    return f"apex_{class_name.lower()}"


def _flow_name(path: Path) -> str:
    """Derive the Flow API name from its file name.

    ``Path.stem`` only strips the final ``.xml`` suffix, leaving the
    ``.flow-meta`` infix (e.g. ``"AccountFlow.flow-meta"``). Flow API names
    never contain dots, so the name is the part before the first dot.
    """
    return path.name.split(".", 1)[0]


def _parse_error_node(path: Path, error: Exception) -> dict:
    """Return a single ``concept`` error node for an unparseable Flow file."""
    return {
        "nodes": [
            {
                "id": f"flow_{_flow_name(path).lower()}",
                "label": f"[Parse Error] {path.name}",
                "file_type": "concept",
                "source_file": str(path),
                "sf_error_type": "xml_parse_error",
                "sf_error_message": str(error),
            }
        ],
        "edges": [],
    }


def _ensure_node(nodes: list[dict], node: dict) -> None:
    """Append *node* to *nodes* unless a node with the same ID already exists."""
    if not any(n["id"] == node["id"] for n in nodes):
        nodes.append(node)


def extract_flow(path: Path) -> dict:
    """Parse a ``*.flow-meta.xml`` (Flow) file.

    Extracts the Flow definition (trigger object / type), record operations
    (``recordLookup`` -> ``queries``; ``recordCreate`` / ``recordUpdate`` /
    ``recordDelete`` -> ``dml_operates_on``) and Apex Action invocations
    (``apexAction`` -> ``flow_invokes``).

    Returns:
        ``{"nodes": [...], "edges": [...]}``. On XML parse failure a single
        ``concept`` error node is returned with no edges (graceful degradation).
        Referenced SObject / Apex nodes are included as stubs so the result
        carries no dangling edges; ``build_graph()`` merges them with the real
        nodes via the shared ``sobject_nid`` / ``apex_`` IDs (ADR-002, ADR-012).
    """
    path = Path(path)
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as exc:
        return _parse_error_node(path, exc)

    # 1. Flow node ---------------------------------------------------------
    flow_name = _flow_name(path)
    flow_id = f"flow_{flow_name.lower()}"

    start_elem = root.find("md:start", namespaces=_NS)
    trigger_object = (
        start_elem.findtext("md:object", namespaces=_NS)
        if start_elem is not None
        else None
    )
    trigger_type = (
        start_elem.findtext("md:recordTriggerType", namespaces=_NS)
        if start_elem is not None
        else None
    )

    nodes: list[dict] = [
        {
            "id": flow_id,
            "label": flow_name,
            "file_type": "flow",
            "source_file": str(path),
            "sf_trigger_object": trigger_object,
            "sf_trigger_type": trigger_type,
        }
    ]
    edges: list[dict] = []

    # 2. Flow elements -----------------------------------------------------
    for elem in root.findall("md:elements", namespaces=_NS):
        elem_type = elem.findtext("md:type", namespaces=_NS)
        elem_name = elem.findtext("md:name", namespaces=_NS)

        # recordLookup -> queries (SOQL site)
        if elem_type == "recordLookup":
            target_object = elem.findtext("md:targetObject", namespaces=_NS)
            if target_object:
                target_id = sobject_nid(target_object)
                _ensure_node(
                    nodes,
                    {
                        "id": target_id,
                        "label": target_object,
                        "file_type": "sobject",
                        "source_file": str(path),
                        "sf_object_type": "custom"
                        if "__c" in target_object
                        else "standard",
                    },
                )
                edges.append(
                    {
                        "source": flow_id,
                        "target": target_id,
                        "relation": "queries",
                        "confidence": "EXTRACTED",
                        "source_file": str(path),
                        "sf_flow_element": elem_name,
                    }
                )

        # recordCreate / recordUpdate / recordDelete -> dml_operates_on
        elif elem_type in _DML_TYPE_BY_ELEMENT:
            target_object = elem.findtext("md:object", namespaces=_NS)
            if target_object:
                target_id = sobject_nid(target_object)
                _ensure_node(
                    nodes,
                    {
                        "id": target_id,
                        "label": target_object,
                        "file_type": "sobject",
                        "source_file": str(path),
                        "sf_object_type": "custom"
                        if "__c" in target_object
                        else "standard",
                    },
                )
                edges.append(
                    {
                        "source": flow_id,
                        "target": target_id,
                        "relation": "dml_operates_on",
                        "sf_dml_type": _DML_TYPE_BY_ELEMENT[elem_type],
                        "confidence": "EXTRACTED",
                        "source_file": str(path),
                        "sf_flow_element": elem_name,
                    }
                )

        # apexAction -> flow_invokes (Flow -> Apex class)
        elif elem_type == "apexAction":
            apex_class = elem.findtext("md:apexClass", namespaces=_NS)
            if apex_class:
                apex_id = _apex_nid(apex_class)
                _ensure_node(
                    nodes,
                    {
                        "id": apex_id,
                        "label": apex_class,
                        "file_type": "code",
                        "source_file": str(path),
                    },
                )
                edges.append(
                    {
                        "source": flow_id,
                        "target": apex_id,
                        "relation": "flow_invokes",
                        "confidence": "EXTRACTED",
                        "source_file": str(path),
                        "sf_flow_element": elem_name,
                    }
                )

        # Unknown / unhandled element types (Decision, Assignment, Loop, …):
        # ignored silently — Flow logic is intentionally not analyzed.

    return {"nodes": nodes, "edges": edges}
