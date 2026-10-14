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

import re
import xml.etree.ElementTree as ET
from pathlib import Path

from graphify.salesforce.references import GLOBAL_LABEL_RE, named_refs
from graphify.salesforce.constants import (
    field_nid,
    sobject_nid,
    sobject_stub,
    validation_rule_nid,
)

#: Salesforce metadata XML namespace.
_NS = {"md": "http://soap.sforce.com/2006/04/metadata"}

#: Field types that reference another SObject.
_RELATIONSHIP_TYPES = {"Lookup", "MasterDetail", "Hierarchy", "MetadataRelationship"}

#: A field-path token in a formula: not preceded by ``$`` (global variables),
#: ``.`` (mid-path) or a word character.
_FORMULA_PATH_RE = re.compile(r"(?<![\w.$])([A-Za-z]\w*(?:\.[A-Za-z]\w*)*)")

#: ``$Setup.Setting__c.Field__c`` — a hierarchy Custom Setting value.
_FORMULA_SETUP_RE = re.compile(r"\$Setup\.(\w+)\.(\w+)")

#: String literals inside a formula (picklist values, text) — never fields.
_FORMULA_STRING_RE = re.compile(r"\"[^\"]*\"|'[^']*'")

#: Identifier (and dotted reference) token in a Salesforce formula, e.g.
#: ``SBQQ__Discount__c`` or ``Account.Owner.Name``.
_FORMULA_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*")

#: Formula functions / literals that are NOT field references. A token directly
#: followed by ``(`` is already treated as a function call; this set additionally
#: filters bare literals and a few common operators that take no parens.
_FORMULA_FUNCTIONS = {
    "AND", "OR", "NOT", "IF", "CASE", "ISCHANGED", "ISNEW", "ISBLANK",
    "ISNULL", "ISNUMBER", "ISPICKVAL", "PRIORVALUE", "TEXT", "VALUE", "LEN",
    "BEGINS", "CONTAINS", "INCLUDES", "REGEX", "TODAY", "NOW", "DATE",
    "DATEVALUE", "ABS", "ROUND", "MAX", "MIN", "MOD", "FLOOR", "CEILING",
    "BLANKVALUE", "NULLVALUE", "VLOOKUP", "TRUE", "FALSE", "NULL",
}


def _extract_formula_fields(formula: str | None) -> list[str]:
    """Extract field references from a Validation Rule formula.

    Returns the distinct field tokens (order-preserving) a formula reads, with
    function calls and literals removed. Dotted cross-object references
    (``Account.Name``) are kept whole; the consumer normalizes to the bare field
    token. Best-effort static parse (ADR-030): conditional / dynamic logic is not
    resolved, so the result is a superset of fields the rule may actually use.
    """
    if not formula:
        return []
    fields: list[str] = []
    seen: set[str] = set()
    for match in _FORMULA_TOKEN_RE.finditer(formula):
        token = match.group(0)
        # A token immediately followed by "(" is a function call, not a field.
        after = formula[match.end():].lstrip()
        if after.startswith("("):
            continue
        if token.upper() in _FORMULA_FUNCTIONS:
            continue
        if token not in seen:
            seen.add(token)
            fields.append(token)
    return fields


def _formula_refs(source_id: str, object_api: str, formula: str | None,
                  path: Path) -> list[dict]:
    """``reads_field`` references for every field path a formula mentions.

    Strict: the resolver keeps a reference only if it lands on a field defined
    in the repo, so function names and literals never become fields.
    """
    from graphify.salesforce.field_refs import field_ref

    if not formula:
        return []
    text = _FORMULA_STRING_RE.sub(" ", formula)
    refs: list[dict] = []
    seen: set[str] = set()
    for match in _FORMULA_PATH_RE.finditer(text):
        token = match.group(1)
        if text[match.end():].lstrip().startswith("(") or token.upper() in _FORMULA_FUNCTIONS:
            continue
        if token.lower() not in seen:
            seen.add(token.lower())
            refs.append(field_ref(source_id, object_api, token, "reads_field", path,
                                  confidence="EXTRACTED", strict=True))
    for match in _FORMULA_SETUP_RE.finditer(text):
        refs.append(field_ref(source_id, match.group(1), match.group(2), "reads_field",
                              path, confidence="EXTRACTED"))
    return refs


def _formula_labels(source_id: str, formula: str | None, path: Path,
                    nodes: list[dict], edges: list[dict]) -> None:
    """Append ``uses_label`` edges (+ label stubs) for ``$Label.X`` in a formula."""
    if not formula:
        return
    label_nodes, label_edges = named_refs(
        source_id, "label", (m.group(1) for m in GLOBAL_LABEL_RE.finditer(formula)), path)
    ids = {n["id"] for n in nodes}
    nodes.extend(n for n in label_nodes if n["id"] not in ids)
    edges.extend(label_edges)


def _add_field(elem, object_api: str | None, field_name: str, path: Path,
               nodes: list[dict], edges: list[dict], refs: list[dict]) -> None:
    """Field node + ``field_of`` / ``lookup_to`` edges + formula references."""
    field_type = elem.findtext("md:type", namespaces=_NS)
    reference_to = [e.text.strip() for e in elem.findall("md:referenceTo", namespaces=_NS)
                    if e.text and e.text.strip()]
    if field_type == "Hierarchy" and not reference_to and object_api:
        reference_to = [object_api]
    node = field_node(
        object_api, field_name, path,
        sf_label=elem.findtext("md:label", namespaces=_NS),
        sf_field_type=field_type,
        sf_reference_to=reference_to or None,
        sf_relationship_name=elem.findtext("md:relationshipName", namespaces=_NS),
    )
    nodes.append(node)
    if not object_api:
        return
    sobject_id = sobject_nid(object_api)
    _ensure_sobject_node(nodes, sobject_id, object_api, path)
    edges.append({"source": node["id"], "target": sobject_id, "relation": "field_of",
                  "confidence": "EXTRACTED", "source_file": str(path)})

    for referenced in reference_to:
        referenced_id = sobject_nid(referenced)
        _ensure_sobject_node(nodes, referenced_id, referenced, path)
        edges.append({"source": node["id"], "target": referenced_id,
                      "relation": "lookup_to", "sf_relationship_type": field_type,
                      "confidence": "EXTRACTED", "source_file": str(path)})
        edges.append({"source": sobject_id, "target": referenced_id,
                      "relation": "references", "sf_relationship_type": field_type,
                      "sf_field_api_name": field_name,
                      "confidence": "EXTRACTED", "source_file": str(path)})

    formula = elem.findtext("md:formula", namespaces=_NS)
    refs.extend(_formula_refs(node["id"], object_api, formula, path))
    _formula_labels(node["id"], formula, path, nodes, edges)
    # Roll-up summary: "OpportunityLineItem.TotalPrice" on the child object.
    summarized = elem.findtext("md:summarizedField", namespaces=_NS)
    if summarized and "." in summarized:
        from graphify.salesforce.field_refs import field_ref

        child_obj, child_field = summarized.split(".", 1)
        refs.append(field_ref(node["id"], child_obj, child_field, "reads_field", path,
                              confidence="EXTRACTED", sf_rollup=True))


def _object_api_from_path(path: Path) -> str:
    """Object API name from ``<Object>.object-meta.xml``.

    The file name — not ``<label>`` — carries the API name: ``<label>`` is the
    display label ("Quote Line" for ``SBQQ__QuoteLine__c``), so keying the node
    on it would split every object from the Apex / Flow references to it.
    """
    return path.name.split(".")[0]


def field_node(object_api: str | None, field_api: str, path: Path | str,
               **attrs) -> dict:
    """A ``field`` node keyed by ``Object.Field`` (shared by every field emitter)."""
    qualified = f"{object_api}.{field_api}" if object_api else field_api
    node = {
        "id": field_nid(object_api, field_api),
        "label": qualified,
        "file_type": "field",
        "source_file": str(path),
        "sf_api_name": field_api,
        "sf_qualified_name": qualified,
    }
    if object_api:
        node["sf_object"] = object_api
    else:
        node["sf_unresolved"] = True
    node.update({k: v for k, v in attrs.items() if v is not None})
    return node


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
    api_name = _object_api_from_path(path)
    display_label = root.findtext("md:label", namespaces=_NS) or api_name
    plural_label = root.findtext("md:pluralLabel", namespaces=_NS) or display_label

    sobject_id = sobject_nid(api_name)  # CRITICAL: ADR-002 single source of truth
    nodes: list[dict] = [
        {
            "id": sobject_id,
            "label": api_name,
            "file_type": "sobject",
            "source_file": str(path),
            "sf_api_name": api_name,
            "sf_qualified_name": api_name,
            "sf_label": display_label,
            "sf_plural_label": plural_label,
            "sf_object_type": "custom" if "__c" in api_name else "standard",
        }
    ]
    edges: list[dict] = []

    # 1b. Custom Setting enrichment — a CustomObject with <customSettingsType>
    # is a List/Hierarchy Custom Setting, not a real table. Tag the SObject node
    # so impact / governor analysis treats it as config storage (no SOQL limits).
    setting_type = root.findtext("md:customSettingsType", namespaces=_NS)
    if setting_type:
        nodes[0]["sf_is_custom_setting"] = True
        nodes[0]["sf_setting_type"] = setting_type

    # 2. Embedded fields (MDAPI layout): node, field_of, lookup_to, formula refs
    refs: list[dict] = []
    for field_elem in root.findall("md:fields", namespaces=_NS):
        field_name = field_elem.findtext("md:fullName", namespaces=_NS)
        if field_name:
            _add_field(field_elem, api_name, field_name, path, nodes, edges, refs)

    # 4. Embedded Validation Rules (<validationRules> children) ------------
    for vr_elem in root.findall("md:validationRules", namespaces=_NS):
        rule_name = vr_elem.findtext("md:fullName", namespaces=_NS)
        if not rule_name:
            continue
        formula = vr_elem.findtext("md:errorConditionFormula", namespaces=_NS)
        active = vr_elem.findtext("md:active", namespaces=_NS) != "false"
        rule_id = _add_validation_rule(
            nodes, edges, api_name, sobject_id, rule_name, formula, active, path
        )
        refs.extend(_formula_refs(rule_id, api_name, formula, path))
        _formula_labels(rule_id, formula, path, nodes, edges)

    return {"nodes": nodes, "edges": edges, "field_refs": refs}


def _add_validation_rule(
    nodes: list[dict],
    edges: list[dict],
    object_api: str,
    sobject_id: str,
    rule_name: str,
    formula: str | None,
    active: bool,
    path: Path,
) -> str:
    """Append a ``validation_rule`` node + a ``validates`` edge; return its ID.

    The node carries ``sf_referenced_fields`` (parsed from the error-condition
    formula) and ``sf_object`` so the CPQ ↔ Validation overlap pass
    (validation_cpq, ADR-030) and the Order-of-Execution pass (which consumes
    ``validates`` edges) both have what they need. ``_ensure_sobject_node`` keeps
    the ``validates`` edge from dangling.
    """
    rule_id = validation_rule_nid(object_api, rule_name)
    nodes.append(
        {
            "id": rule_id,
            "label": rule_name,
            "sf_qualified_name": f"{object_api}.{rule_name}",
            "file_type": "validation_rule",
            "source_file": str(path),
            "sf_object": object_api,
            "sf_active": active,
            "sf_referenced_fields": _extract_formula_fields(formula),
        }
    )
    _ensure_sobject_node(nodes, sobject_id, object_api, path)
    edges.append(
        {
            "source": rule_id,
            "target": sobject_id,
            "relation": "validates",
            "confidence": "EXTRACTED",
            "source_file": str(path),
        }
    )
    return rule_id


def _ensure_sobject_node(
    nodes: list[dict], sobject_id: str, api_name: str, path: Path
) -> None:
    """Append a stub ``sobject`` node for *sobject_id* if not already present."""
    if any(n["id"] == sobject_id for n in nodes):
        return
    nodes.append(sobject_stub(api_name, path))


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
    nodes: list[dict] = []
    edges: list[dict] = []
    refs: list[dict] = []
    _add_field(root, _parent_object_from_field_path(path), field_name, path,
               nodes, edges, refs)
    return {"nodes": nodes, "edges": edges, "field_refs": refs}


def _parent_object_from_validation_path(path: Path) -> str | None:
    """Infer the owning object's API name from a validationRule-meta.xml path.

    Salesforce layout:
    ``objects/<Object>/validationRules/<Rule>.validationRule-meta.xml``.
    Returns the ``<Object>`` directory name, or ``None`` if the layout differs.
    """
    parts = path.parts
    if "validationRules" in parts:
        idx = parts.index("validationRules")
        if idx >= 1:
            return parts[idx - 1]
    return None


def extract_validation_rule(path: Path) -> dict:
    """Parse a standalone ``*.validationRule-meta.xml`` (ValidationRule) file.

    The owning object is inferred from the directory layout
    (``objects/<Object>/validationRules/<Rule>.validationRule-meta.xml``). The
    rule name comes from ``<fullName>`` (falling back to the file stem). Produces
    a ``validation_rule`` node (with ``sf_referenced_fields`` from the formula)
    and a ``validates`` edge to its SObject. On XML parse failure a single
    ``concept`` error node is returned (graceful degradation, ADR-009).
    """
    path = Path(path)
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as exc:
        return _parse_error_node(path, exc)

    rule_name = (
        root.findtext("md:fullName", namespaces=_NS)
        or path.name.split(".")[0]
    )
    formula = root.findtext("md:errorConditionFormula", namespaces=_NS)
    active = root.findtext("md:active", namespaces=_NS) != "false"

    object_api = _parent_object_from_validation_path(path)
    if object_api is None:
        # Layout doesn't reveal the object: emit the rule node alone (no edge)
        # so the formula's fields are still recorded, never a dangling edge.
        rule_id = validation_rule_nid("?", rule_name)
        return {
            "nodes": [
                {
                    "id": rule_id,
                    "label": rule_name,
                    "file_type": "validation_rule",
                    "source_file": str(path),
                    "sf_active": active,
                    "sf_referenced_fields": _extract_formula_fields(formula),
                }
            ],
            "edges": [],
        }

    nodes: list[dict] = []
    edges: list[dict] = []
    rule_id = _add_validation_rule(
        nodes, edges, object_api, sobject_nid(object_api),
        rule_name, formula, active, path,
    )
    _formula_labels(rule_id, formula, path, nodes, edges)
    return {"nodes": nodes, "edges": edges,
            "field_refs": _formula_refs(rule_id, object_api, formula, path)}
