"""
graphify-sf: Custom Object / Custom Field metadata parser.

Parses Salesforce ``*.object-meta.xml`` (CustomObject) and
``*.field-meta.xml`` (CustomField) metadata into graph nodes / edges:

    - ``sobject`` node per CustomObject (ID from ``sobject_nid()`` — ADR-002).
    - ``field`` node per field, linked to its object with a ``field_of`` edge.
    - ``references`` edge for Lookup / Master-Detail relationships, resolved to
      the referenced SObject via ``sobject_nid()`` (cross-file resolution).

CRITICAL (ADR-002): SObject node IDs are built ONLY via
``constants.sobject_nid()`` so that the Apex, Flow and Object parsers all
converge on the same node in ``build_graph()``.

Error handling (ARCHITECTURE.md "파서 레벨 에러"): XML parse failures degrade
gracefully — a single ``concept`` error node is returned and the file is
skipped, never raising so that the rest of the repo keeps analyzing (ADR-009
lenient mode).
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

from graphify.salesforce.constants import sobject_nid

#: Salesforce metadata XML namespace.
_NS = {"md": "http://soap.sforce.com/2006/04/metadata"}

#: Field types that reference another SObject.
_RELATIONSHIP_TYPES = {"Lookup", "MasterDetail"}


def _field_nid(api_name: str) -> str:
    """Build a stable node ID for a Custom Field.

    Example: ``"BillingCity__c"`` -> ``"field_billingcity"``.
    """
    normalized = api_name.lower().replace("__c", "").replace("__", "_")
    return f"field_{normalized}"


def _parse_error_node(path: Path, error: Exception) -> dict:
    """Return a single ``concept`` error node for an unparseable XML file."""
    return {
        "nodes": [
            {
                "id": f"sobject_{path.stem.lower()}",
                "label": f"[Parse Error] {path.name}",
                "file_type": "concept",
                "source_file": str(path),
                "sf_error_type": "xml_parse_error",
                "sf_error_message": str(error),
            }
        ],
        "edges": [],
    }


def extract_custom_object(path: Path) -> dict:
    """Parse a ``*.object-meta.xml`` (CustomObject) file.

    Returns:
        ``{"nodes": [...], "edges": [...]}``. On XML parse failure a single
        ``concept`` error node is returned with no edges (graceful degradation).
    """
    path = Path(path)
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as exc:
        return _parse_error_node(path, exc)

    # 1. SObject node ------------------------------------------------------
    label = root.findtext("md:label", namespaces=_NS) or path.stem
    plural_label = root.findtext("md:pluralLabel", namespaces=_NS) or label

    sobject_id = sobject_nid(label)  # CRITICAL: ADR-002 single source of truth
    nodes: list[dict] = [
        {
            "id": sobject_id,
            "label": label,
            "file_type": "sobject",
            "source_file": str(path),
            "sf_plural_label": plural_label,
            "sf_object_type": "custom" if "__c" in label else "standard",
        }
    ]
    edges: list[dict] = []

    # 2. Field nodes + field_of edges -------------------------------------
    for field_elem in root.findall("md:fields", namespaces=_NS):
        field_name = field_elem.findtext("md:fullName", namespaces=_NS)
        if not field_name:
            continue
        field_label = field_elem.findtext("md:label", namespaces=_NS) or field_name
        field_type = field_elem.findtext("md:type", namespaces=_NS)

        field_id = _field_nid(field_name)
        nodes.append(
            {
                "id": field_id,
                "label": field_label,
                "file_type": "field",
                "source_file": str(path),
                "sf_field_type": field_type,
                "sf_api_name": field_name,
            }
        )

        edges.append(
            {
                "source": field_id,
                "target": sobject_id,
                "relation": "field_of",
                "confidence": "EXTRACTED",
                "source_file": str(path),
            }
        )

        # 3. Lookup / Master-Detail relationship --------------------------
        if field_type in _RELATIONSHIP_TYPES:
            referenced = field_elem.findtext("md:referenceTo", namespaces=_NS)
            if referenced:
                referenced_id = sobject_nid(referenced)
                # Add a stub node for the referenced SObject so the edge is not
                # dangling within this result. build_graph() merges this with the
                # real node (from the referenced object's own *.object-meta.xml)
                # via the shared sobject_nid (ADR-002, ADR-012).
                _ensure_sobject_node(nodes, referenced_id, referenced, path)
                edges.append(
                    {
                        "source": sobject_id,
                        "target": referenced_id,
                        "relation": "references",
                        "sf_relationship_type": field_type,
                        "sf_field_api_name": field_name,
                        "confidence": "EXTRACTED",
                        "source_file": str(path),
                    }
                )

    return {"nodes": nodes, "edges": edges}


def _ensure_sobject_node(
    nodes: list[dict], sobject_id: str, api_name: str, path: Path
) -> None:
    """Append a stub ``sobject`` node for *sobject_id* if not already present."""
    if any(n["id"] == sobject_id for n in nodes):
        return
    nodes.append(
        {
            "id": sobject_id,
            "label": api_name,
            "file_type": "sobject",
            "source_file": str(path),
            "sf_object_type": "custom" if "__c" in api_name else "standard",
        }
    )


def _parent_object_from_field_path(path: Path) -> str | None:
    """Infer the owning object's API name from a field-meta.xml path.

    Salesforce layout: ``objects/<Object>/fields/<Field>.field-meta.xml``.
    Returns the ``<Object>`` directory name, or ``None`` if the layout differs.
    """
    parts = path.parts
    if "fields" in parts:
        idx = parts.index("fields")
        if idx >= 1:
            return parts[idx - 1]
    return None


def extract_custom_field(path: Path) -> dict:
    """Parse a standalone ``*.field-meta.xml`` (CustomField) file.

    The owning object is inferred from the directory layout
    (``objects/<Object>/fields/<Field>.field-meta.xml``). Returns the field
    node, a ``field_of`` edge to the parent object (when resolvable), and a
    ``references`` edge for Lookup / Master-Detail fields.
    """
    path = Path(path)
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as exc:
        return _parse_error_node(path, exc)

    # The field API name lives in <fullName>, falling back to the file stem
    # (e.g. "BillingCity__c.field-meta.xml" -> "BillingCity__c").
    field_name = root.findtext("md:fullName", namespaces=_NS) or path.name.split(".")[0]
    field_label = root.findtext("md:label", namespaces=_NS) or field_name
    field_type = root.findtext("md:type", namespaces=_NS)

    field_id = _field_nid(field_name)
    nodes: list[dict] = [
        {
            "id": field_id,
            "label": field_label,
            "file_type": "field",
            "source_file": str(path),
            "sf_field_type": field_type,
            "sf_api_name": field_name,
        }
    ]
    edges: list[dict] = []

    parent_api = _parent_object_from_field_path(path)
    if parent_api:
        parent_id = sobject_nid(parent_api)
        # Ensure the field_of edge is not dangling: include the parent SObject
        # node so this dict is self-consistent (build merges it with any other
        # parser's node for the same object via the shared sobject_nid).
        nodes.append(
            {
                "id": parent_id,
                "label": parent_api,
                "file_type": "sobject",
                "source_file": str(path),
                "sf_object_type": "custom" if "__c" in parent_api else "standard",
            }
        )
        edges.append(
            {
                "source": field_id,
                "target": parent_id,
                "relation": "field_of",
                "confidence": "EXTRACTED",
                "source_file": str(path),
            }
        )

        if field_type in _RELATIONSHIP_TYPES:
            referenced = root.findtext("md:referenceTo", namespaces=_NS)
            if referenced:
                referenced_id = sobject_nid(referenced)
                _ensure_sobject_node(nodes, referenced_id, referenced, path)
                edges.append(
                    {
                        "source": parent_id,
                        "target": referenced_id,
                        "relation": "references",
                        "sf_relationship_type": field_type,
                        "sf_field_api_name": field_name,
                        "confidence": "EXTRACTED",
                        "source_file": str(path),
                    }
                )

    return {"nodes": nodes, "edges": edges}
