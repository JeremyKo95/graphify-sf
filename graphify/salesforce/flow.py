"""
graphify-sf: Flow metadata parser.

Parses Salesforce ``*.flow-meta.xml`` (Flow / Process Builder) metadata into
graph nodes / edges:

    - ``flow`` node per Flow file, carrying the record-trigger object / type
      read from ``<start>`` (when present).
    - ``queries`` edge per ``<recordLookups>`` element -> referenced SObject.
    - ``dml_operates_on`` edge per ``<recordCreates>`` / ``<recordUpdates>`` /
      ``<recordDeletes>`` element -> referenced SObject (with ``sf_dml_type``).
    - ``flow_invokes`` edge per ``<actionCalls>`` with ``actionType=apex`` ->
      invoked Apex (resolved from ``actionName``; INFERRED — the invocable name
      is not guaranteed to equal the Apex class name).

    Real Salesforce Flow metadata expresses elements as **typed plural tags**
    (``<recordCreates>``, ``<recordUpdates>``, ``<recordLookups>``,
    ``<recordDeletes>``, ``<actionCalls>``) — NOT a generic ``<elements>`` with a
    ``<type>`` child. A DML element names its object either directly via
    ``<object>`` or indirectly via ``<inputReference>`` (a variable whose
    ``<objectType>`` is declared in ``<variables>``); both are resolved here.

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

#: Flow record-DML element tag (typed plural) -> Apex-style DML verb.
_DML_TYPE_BY_ELEMENT = {
    "recordCreates": "INSERT",
    "recordUpdates": "UPDATE",
    "recordDeletes": "DELETE",
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

    # Variable name -> SObject API name, for resolving DML <inputReference>s
    # (e.g. a recordCreate of variable ``CPQ_Quote`` whose objectType is
    # ``SBQQ__Quote__c``). Only sObject-typed variables carry an ``objectType``.
    variables: dict[str, str] = {}
    for var in root.findall("md:variables", namespaces=_NS):
        var_name = var.findtext("md:name", namespaces=_NS)
        obj_type = var.findtext("md:objectType", namespaces=_NS)
        if var_name and obj_type:
            variables[var_name] = obj_type

    def _add_sobject_stub(object_api: str) -> str:
        target_id = sobject_nid(object_api)
        _ensure_node(
            nodes,
            {
                "id": target_id,
                "label": object_api,
                "file_type": "sobject",
                "source_file": str(path),
                "sf_object_type": "custom" if "__c" in object_api else "standard",
            },
        )
        return target_id

    def _resolve_object(elem) -> str | None:
        """Object an element targets: direct ``<object>`` or a variable's type."""
        obj = elem.findtext("md:object", namespaces=_NS)
        if obj:
            return obj
        ref = elem.findtext("md:inputReference", namespaces=_NS)
        if ref:
            # inputReference may be "Var" or "Var.field"; resolve the root var.
            return variables.get(ref.split(".")[0])
        return None

    # 2a. recordLookups -> queries (SOQL site)
    for elem in root.findall("md:recordLookups", namespaces=_NS):
        target_object = elem.findtext("md:object", namespaces=_NS)
        if not target_object:
            continue
        edges.append(
            {
                "source": flow_id,
                "target": _add_sobject_stub(target_object),
                "relation": "queries",
                "confidence": "EXTRACTED",
                "source_file": str(path),
                "sf_flow_element": elem.findtext("md:name", namespaces=_NS),
            }
        )

    # 2b. recordCreates / recordUpdates / recordDeletes -> dml_operates_on
    for tag, dml_type in _DML_TYPE_BY_ELEMENT.items():
        for elem in root.findall(f"md:{tag}", namespaces=_NS):
            target_object = _resolve_object(elem)
            if not target_object:
                continue
            edges.append(
                {
                    "source": flow_id,
                    "target": _add_sobject_stub(target_object),
                    "relation": "dml_operates_on",
                    "sf_dml_type": dml_type,
                    "confidence": "EXTRACTED",
                    "source_file": str(path),
                    "sf_flow_element": elem.findtext("md:name", namespaces=_NS),
                }
            )

    # 2c. actionCalls with actionType=apex -> flow_invokes (Flow -> Apex)
    for elem in root.findall("md:actionCalls", namespaces=_NS):
        if (elem.findtext("md:actionType", namespaces=_NS) or "").lower() != "apex":
            continue
        action_name = elem.findtext("md:actionName", namespaces=_NS)
        if not action_name:
            continue
        apex_id = _apex_nid(action_name)
        _ensure_node(
            nodes,
            {
                "id": apex_id,
                "label": action_name,
                "file_type": "code",
                "source_file": str(path),
            },
        )
        edges.append(
            {
                "source": flow_id,
                "target": apex_id,
                "relation": "flow_invokes",
                # The invocable action name is not guaranteed to equal the Apex
                # class name, so cross-file resolution is best-effort (ADR-020).
                "confidence": "INFERRED",
                "confidence_value": 0.75,
                "source_file": str(path),
                "sf_flow_element": elem.findtext("md:name", namespaces=_NS),
            }
        )

    # Other elements (decisions, assignments, loops, screens, subflows):
    # intentionally not analyzed — Flow branching logic is out of scope.

    return {"nodes": nodes, "edges": edges}
