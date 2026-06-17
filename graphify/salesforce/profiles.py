"""
graphify-sf: Profile / Permission Set metadata parser.

Parses Salesforce ``*.profile-meta.xml`` (Profile) and
``*.permissionset-meta.xml`` (PermissionSet) metadata into graph nodes / edges:

    - A ``profile`` / ``permission_set`` node per file.
    - ``grants_access_to`` edges to each SObject the profile/permset can access
      (Create/Read/Edit/Delete recorded in ``sf_permissions``).
    - ``grants_access_to`` edges to each enabled Apex class.
    - Field-Level Security recorded as the ``sf_fls_permissions`` attribute on the
      profile node (per spec / ADR-028 — FLS is NOT a separate node).

CRITICAL (ADR-002): SObject node IDs are built ONLY via
``constants.sobject_nid()`` so the Profile, Apex, Flow and Object parsers all
converge on the same node in ``build_graph()``. Apex class access targets the
same ``apex_<class>`` node ID the Apex / Flow parsers emit (cross-file
resolution).

Stub target nodes (ADR-012): referenced SObject / Apex nodes are emitted as
stubs so this result has no dangling edges; ``build_graph()`` merges them with
the real nodes from the other parsers via the shared ID.

Error handling (ADR-009 lenient): an XML parse failure degrades to a single
``concept`` error node instead of raising, so the rest of the repo keeps
analyzing.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

from graphify.salesforce.constants import sobject_nid

#: Salesforce metadata XML namespace.
_NS = {"md": "http://soap.sforce.com/2006/04/metadata"}

#: Object-permission XML tag -> permission label, in canonical CRUD order.
_OBJECT_PERMS: list[tuple[str, str]] = [
    ("allowCreate", "CREATE"),
    ("allowRead", "READ"),
    ("allowEdit", "EDIT"),
    ("allowDelete", "DELETE"),
]


def _profile_meta(path: Path) -> tuple[str, str]:
    """Return ``(file_type, node_id)`` for *path*.

    PermissionSet files (``*.permissionset-meta.xml``) get ``permission_set``;
    everything else is treated as a ``profile``.
    """
    # ``*.profile-meta.xml`` -> stem still carries ".profile-meta"; take the
    # API name up to the first dot ("sf_Admin.profile-meta.xml" -> "sf_Admin").
    name = path.name.split(".")[0]
    file_type = "permission_set" if "permissionset" in path.name.lower() else "profile"
    return file_type, f"{file_type}_{name.lower()}"


def _parse_error_node(path: Path, error: Exception) -> dict:
    """Return a single ``concept`` error node for an unparseable XML file."""
    _, node_id = _profile_meta(path)
    return {
        "nodes": [
            {
                "id": node_id,
                "label": f"[Parse Error] {path.name}",
                "file_type": "concept",
                "source_file": str(path),
                "sf_error_type": "xml_parse_error",
                "sf_error_message": str(error),
            }
        ],
        "edges": [],
    }


def _ensure_stub_node(
    nodes: list[dict], node_id: str, label: str, file_type: str, path: Path
) -> None:
    """Append a stub node for *node_id* if not already present (ADR-012).

    Keeps the result free of dangling edges; ``build_graph()`` merges the stub
    with the real node from the owning parser via the shared ID.
    """
    if any(n["id"] == node_id for n in nodes):
        return
    nodes.append(
        {
            "id": node_id,
            "label": label,
            "file_type": file_type,
            "source_file": str(path),
        }
    )


def extract_profile(path: Path) -> dict:
    """Parse a Profile or PermissionSet metadata XML file.

    Extracts object permissions (Create/Read/Edit/Delete -> ``grants_access_to``
    with ``sf_permissions``), Field-Level Security (stored on the profile node as
    ``sf_fls_permissions``), and Apex class access (-> ``grants_access_to``).

    Returns:
        ``{"nodes": [...], "edges": [...]}``. On XML parse failure a single
        ``concept`` error node is returned with no edges (graceful degradation).
    """
    path = Path(path)
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as exc:
        return _parse_error_node(path, exc)

    file_type, profile_id = _profile_meta(path)
    profile_node: dict = {
        "id": profile_id,
        "label": path.name.split(".")[0],
        "file_type": file_type,
        "source_file": str(path),
    }
    nodes: list[dict] = [profile_node]
    edges: list[dict] = []

    # 1. Object permissions -> grants_access_to (SObject) -------------------
    for obj_perm in root.findall("md:objectPermissions", namespaces=_NS):
        sobject = obj_perm.findtext("md:object", namespaces=_NS)
        if not sobject:
            continue
        permissions = [
            label
            for tag, label in _OBJECT_PERMS
            if obj_perm.findtext(f"md:{tag}", namespaces=_NS) == "true"
        ]
        target_id = sobject_nid(sobject)  # CRITICAL: ADR-002 single source
        _ensure_stub_node(nodes, target_id, sobject, "sobject", path)
        edges.append(
            {
                "source": profile_id,
                "target": target_id,
                "relation": "grants_access_to",
                "confidence": "EXTRACTED",
                "source_file": str(path),
                "sf_permissions": permissions,
                "sf_object": sobject,
            }
        )

    # 2. Field-Level Security -> profile node attribute (not a node) --------
    fls_permissions: dict[str, dict] = {}
    for field_perm in root.findall("md:fieldPermissions", namespaces=_NS):
        field_name = field_perm.findtext("md:field", namespaces=_NS)
        if not field_name:
            continue
        fls_permissions[field_name] = {
            "readable": field_perm.findtext("md:readable", namespaces=_NS) == "true",
            "editable": field_perm.findtext("md:editable", namespaces=_NS) == "true",
        }
    if fls_permissions:
        profile_node["sf_fls_permissions"] = fls_permissions

    # 3. Apex class access -> grants_access_to (Apex class) -----------------
    for class_access in root.findall("md:classAccesses", namespaces=_NS):
        apex_class = class_access.findtext("md:apexClass", namespaces=_NS)
        enabled = class_access.findtext("md:enabled", namespaces=_NS) == "true"
        if not apex_class or not enabled:
            continue
        # Matches the Apex/Flow parsers' class node ID (apex_<class>).
        apex_id = f"apex_{apex_class.lower()}"
        _ensure_stub_node(nodes, apex_id, apex_class, "code", path)
        edges.append(
            {
                "source": profile_id,
                "target": apex_id,
                "relation": "grants_access_to",
                "confidence": "EXTRACTED",
                "source_file": str(path),
                "sf_access_type": "apex_class",
            }
        )

    return {"nodes": nodes, "edges": edges}


def extract_permission_set(path: Path) -> dict:
    """Parse a PermissionSet metadata XML file.

    PermissionSet and Profile share the same metadata shape for object
    permissions, FLS and class access, so this delegates to
    :func:`extract_profile` (the node prefix differs based on the file name).
    """
    return extract_profile(path)
