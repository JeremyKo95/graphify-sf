"""
graphify-sf: Enhanced Apex parser (classes, triggers, SOQL/DML, governor hints).

Regex-based parser for Apex ``*.cls`` / ``*.trigger`` files (ADR-019 — regex
first, tree-sitter deferred to Phase 3). It extracts:

    - The class / trigger definition node. Identity comes from the file name
      (``<Name>.cls`` / ``<Name>.trigger``) and the kind from the extension, so
      a ``class`` / ``trigger`` word inside a comment can never rename or
      re-type the node. The raw source rides along as the transient ``_source``
      attribute for the CPQ / governor passes; ``extract_sf`` drops it.
    - Method signatures (``member_of`` edge back to the owning class).
    - ``triggers_on`` edge from a trigger to its SObject (with the DML events).
    - SOQL queries (``queries`` edge -> SObject, ``sf_in_loop`` tagged).
    - DML operations (``dml_operates_on`` edge -> SObject, ``sf_in_loop`` tagged).
    - ``implements`` of well-known interfaces (QCP, Database.Batchable hint).

CRITICAL (ADR-002): every SObject node ID is built via ``constants.sobject_nid()``
so the Apex, Flow and Object parsers converge on the same node in
``build_graph()``. Cross-file resolution depends on it.

The ``sf_in_loop`` flags feed the governor-limit analysis pass (Pass SF-4):
SOQL/DML detected inside a ``for``/``while`` body is the signal for
``governor_violation`` diagnostics.

Limitations (ADR-019, accepted): nested classes, generic-type element
resolution, and method overloads are not fully modelled; DML targets are
resolved from light variable-type tracking and fall back to an ``unknown``
SObject (``sf_ambiguous``) when the type cannot be inferred. No method-body AST
analysis — regex only.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from graphify.salesforce.constants import (
    CPQ_QCP_INTERFACE,
    apex_method_nid,
    apex_nid,
    sobject_nid,
    sobject_stub,
)

# ---------------------------------------------------------------------------
# Regexes
# ---------------------------------------------------------------------------

#: Type declaration in a ``.cls`` file, matched on comment/string-stripped code.
_TYPE_DECL_RE = re.compile(r"\b(class|interface|enum)\s+(\w+)", re.IGNORECASE)

#: ``trigger <Name> on <SObject> (<events>)``, matched on stripped code.
_TRIGGER_DECL_RE = re.compile(
    r"\btrigger\s+(\w+)\s+on\s+(\w+)\s*\(([^)]*)\)", re.IGNORECASE
)

#: Method signature. A visibility modifier is REQUIRED to avoid matching control
#: flow (``else if (...)``, ``for (...)``) as methods. Captures return type,
#: name and the raw parameter list.
_METHOD_RE = re.compile(
    r"\b(public|private|protected|global)\s+"
    r"(?:static\s+)?(?:override\s+)?(?:virtual\s+)?"
    r"(\w+(?:<[^>]+>)?)\s+(\w+)\s*\((.*?)\)"
)

#: SOQL: ``[SELECT ... FROM <SObject> ...]``. Single-line (no DOTALL).
_SOQL_RE = re.compile(r"SELECT\s+.*?\s+FROM\s+(\w+)", re.IGNORECASE)

#: DML keyword followed by the target variable: ``update opps``.
_DML_RE = re.compile(r"\b(insert|update|delete)\s+(\w+)", re.IGNORECASE)

#: for / while loop header (used to bracket loop bodies for in-loop detection).
_LOOP_RE = re.compile(r"(?:for|while)\s*\([^)]*\)\s*\{")

#: Collection declaration: ``List<Opportunity> opps`` -> opps maps to Opportunity.
_COLLECTION_DECL_RE = re.compile(r"(?:List|Set)\s*<\s*(\w+)\s*>\s+(\w+)")

#: Simple declaration / parameter: ``Account acc`` (type must be capitalized).
_SIMPLE_DECL_RE = re.compile(r"\b([A-Z]\w*)\s+(\w+)\s*[;:=)]")

#: Apex / collection types that are not SObjects — excluded from DML resolution.
_NON_SOBJECT_TYPES = {
    "String", "Integer", "Long", "Decimal", "Double", "Boolean", "Id",
    "Date", "Datetime", "Time", "Blob", "Object", "List", "Set", "Map",
    "Trigger", "System", "Database", "void", "SObject",
}


def strip_apex(source: str) -> tuple[str, list[tuple[int, str]]]:
    """Blank out comments and string-literal contents, preserving offsets.

    Returns ``(code, strings)``: ``code`` has the same length and line breaks as
    ``source`` (so match offsets map to the same line numbers), with comments
    replaced by spaces and string contents blanked between their quotes;
    ``strings`` lists ``(offset, literal)`` for every string literal.
    """
    out: list[str] = []
    strings: list[tuple[int, str]] = []
    i, n = 0, len(source)
    while i < n:
        c = source[i]
        if c == "/" and source.startswith("/", i + 1):
            j = source.find("\n", i)
            j = n if j == -1 else j
            out.append(" " * (j - i))
            i = j
        elif c == "/" and source.startswith("*", i + 1):
            j = source.find("*/", i + 2)
            j = n if j == -1 else j + 2
            out.append(re.sub(r"[^\n]", " ", source[i:j]))
            i = j
        elif c == "'":
            j = i + 1
            while j < n and source[j] not in "'\n":
                j += 2 if source[j] == "\\" else 1
            j = min(j, n)
            literal = source[i + 1:j]
            strings.append((i, literal))
            if j < n and source[j] == "'":
                out.append("'" + " " * len(literal) + "'")
                i = j + 1
            else:
                out.append("'" + " " * len(literal))
                i = j
        else:
            out.append(c)
            i += 1
    return "".join(out), strings


def _find_loop_ranges(source: str) -> list[tuple[int, int]]:
    """Return ``(start_line, end_line)`` ranges for each for/while loop body.

    Uses simple brace matching from the loop header's opening ``{`` to its
    matching ``}``. Nested loops yield nested (overlapping) ranges, which is
    fine — a line is "in a loop" if it falls inside any range.
    """
    ranges: list[tuple[int, int]] = []
    for match in _LOOP_RE.finditer(source):
        start_line = source[: match.start()].count("\n") + 1
        depth = 1
        pos = match.end()
        while depth > 0 and pos < len(source):
            ch = source[pos]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
            pos += 1
        end_line = source[:pos].count("\n") + 1
        ranges.append((start_line, end_line))
    return ranges


def _in_loop(line_no: int, loop_ranges: list[tuple[int, int]]) -> bool:
    return any(start <= line_no <= end for start, end in loop_ranges)


def _build_var_types(source: str) -> dict[str, str]:
    """Map local/param variable names to their (capitalized) declared type.

    Light, regex-only type tracking to resolve DML targets like ``update opps``
    to a concrete SObject. Collections (``List<Opportunity> opps``) resolve to
    the element type. Best-effort: unresolved variables fall back to ``unknown``.
    """
    var_types: dict[str, str] = {}
    for m in _COLLECTION_DECL_RE.finditer(source):
        var_types[m.group(2)] = m.group(1)
    for m in _SIMPLE_DECL_RE.finditer(source):
        type_name, var_name = m.group(1), m.group(2)
        if type_name in _NON_SOBJECT_TYPES:
            continue
        var_types.setdefault(var_name, type_name)
    return var_types


def _is_sobject_name(name: str) -> bool:
    """Reject tokens that a ``FROM`` clause can name but are not SObject roots.

    A child-relationship subquery (``SELECT Id, (SELECT Id FROM Contacts__r) ...``)
    puts a ``__r`` relationship name after ``FROM`` — that is a traversal, never a
    queryable SObject, so it must not become an ``sobject`` node (real-org label
    mis-extraction; ADR-019). Non-SObject keywords are filtered too.
    """
    return not name.lower().endswith("__r") and name not in _NON_SOBJECT_TYPES


def _detect_soql_objects(source: str) -> list[tuple[str, int, bool]]:
    """Detect SObjects queried via SOQL.

    Returns a list of ``(sobject_name, line_no, in_loop)`` tuples. Child-relationship
    (``__r``) subquery targets are skipped (see ``_is_sobject_name``).
    """
    loop_ranges = _find_loop_ranges(source)
    results: list[tuple[str, int, bool]] = []
    for match in _SOQL_RE.finditer(source):
        sobject = match.group(1)
        if not _is_sobject_name(sobject):
            continue
        line_no = source[: match.start()].count("\n") + 1
        results.append((sobject, line_no, _in_loop(line_no, loop_ranges)))
    return results


def _detect_dml_operations(
    source: str, var_types: dict[str, str]
) -> list[tuple[str, str, int, bool]]:
    """Detect DML operations.

    Returns ``(dml_type, sobject_name, line_no, in_loop)``. ``sobject_name`` is
    resolved from variable-type tracking, or ``"unknown"`` when not inferable.
    """
    loop_ranges = _find_loop_ranges(source)
    results: list[tuple[str, str, int, bool]] = []
    for match in _DML_RE.finditer(source):
        dml_type = match.group(1).upper()
        var_name = match.group(2)
        sobject = var_types.get(var_name, "unknown")
        line_no = source[: match.start()].count("\n") + 1
        results.append((dml_type, sobject, line_no, _in_loop(line_no, loop_ranges)))
    return results


def _ensure_sobject_node(
    nodes: list[dict], sobject_id: str, api_name: str, path: Path
) -> None:
    """Append a stub ``sobject`` node for *sobject_id* if not already present.

    Keeps cross-file edges (queries / dml_operates_on) non-dangling within this
    result; ``extract_sf`` replaces the stub with the object's own definition via
    the shared ``sobject_nid`` (ADR-002, ADR-012).
    """
    if any(n["id"] == sobject_id for n in nodes):
        return
    node = sobject_stub(api_name, path)
    if api_name == "unknown":
        node["sf_ambiguous"] = True
    nodes.append(node)


def extract_apex_enhanced(path: Path) -> dict:
    """Parse an Apex class / trigger file into graph nodes and edges.

    Returns:
        ``{"nodes": [...], "edges": [...]}``. If no class/trigger declaration is
        found, returns empty lists plus an ``"error"`` key (ADR-009 lenient: the
        caller skips the file and keeps analyzing).
    """
    path = Path(path)
    with open(path, "r", encoding="utf-8") as f:
        source = f.read()
    code, _strings = strip_apex(source)

    nodes: list[dict] = []
    edges: list[dict] = []

    # 1. Class / trigger definition --------------------------------------
    name = path.name.split(".")[0]
    is_trigger = path.suffix == ".trigger"
    trigger_decl = _TRIGGER_DECL_RE.search(code) if is_trigger else None
    type_decl = None if is_trigger else _TYPE_DECL_RE.search(code)
    # Managed-package classes ship with the body "(hidden)": real classes
    # (profiles grant access to them) whose contents cannot be analyzed.
    hidden = source.strip() == "(hidden)"
    if not (trigger_decl or type_decl or hidden):
        return {"nodes": [], "edges": [], "error": "No class/trigger found"}

    class_id = apex_nid(name)
    nodes.append(
        {
            "id": class_id,
            "label": name,
            "file_type": "code",
            "source_file": str(path),
            "sf_qualified_name": name,
            "sf_code_type": (
                "trigger" if is_trigger
                else type_decl.group(1).lower() if type_decl else "class"
            ),
            "sf_loc": source.count("\n") + 1,
            "sf_source_hash": hashlib.sha256(source.encode("utf-8")).hexdigest()[:16],
            "_source": source,
        }
    )

    if hidden:
        nodes[0]["sf_managed_hidden"] = True
        return {"nodes": nodes, "edges": edges}

    if trigger_decl:
        sobject_name = trigger_decl.group(2)
        target_id = sobject_nid(sobject_name)
        _ensure_sobject_node(nodes, target_id, sobject_name, path)
        events = [" ".join(e.split()).lower() for e in trigger_decl.group(3).split(",")]
        edges.append(
            {
                "source": class_id,
                "target": target_id,
                "relation": "triggers_on",
                "confidence": "EXTRACTED",
                "source_file": str(path),
                "sf_trigger_events": [e for e in events if e],
            }
        )

    # 2. Method signatures ------------------------------------------------
    methods: dict[str, dict] = {}
    for method in _METHOD_RE.finditer(code):
        return_type, method_name, params = (
            method.group(2),
            method.group(3),
            method.group(4),
        )
        signature = f"{method_name}({' '.join(params.split())})"
        method_id = apex_method_nid(name, method_name)
        existing = methods.get(method_id)
        if existing is not None:
            existing["sf_signatures"].append(signature)
            continue
        methods[method_id] = {
            "id": method_id,
            "label": f"{name}.{method_name}",
            "file_type": "code",
            "source_file": str(path),
            "sf_qualified_name": f"{name}.{method_name}",
            "sf_return_type": return_type,
            "sf_method_type": "method",
            "sf_signatures": [signature],
            "source_location": f"L{source.count(chr(10), 0, method.start()) + 1}",
        }
        edges.append(
            {
                "source": method_id,
                "target": class_id,
                "relation": "member_of",
                "confidence": "EXTRACTED",
                "source_file": str(path),
            }
        )
    nodes.extend(methods.values())

    # 3. SOQL queries (cross-file resolution) -----------------------------
    for sobject_name, line_no, in_loop in _detect_soql_objects(source):
        target_id = sobject_nid(sobject_name)
        _ensure_sobject_node(nodes, target_id, sobject_name, path)
        edges.append(
            {
                "source": class_id,
                "target": target_id,
                "relation": "queries",
                "confidence": "EXTRACTED",
                "sf_in_loop": in_loop,
                "source_location": f"L{line_no}",
                "source_file": str(path),
            }
        )

    # 4. DML operations ---------------------------------------------------
    var_types = _build_var_types(source)
    for dml_type, sobject_name, line_no, in_loop in _detect_dml_operations(
        source, var_types
    ):
        target_id = sobject_nid(sobject_name)
        _ensure_sobject_node(nodes, target_id, sobject_name, path)
        edge: dict = {
            "source": class_id,
            "target": target_id,
            "relation": "dml_operates_on",
            "sf_dml_type": dml_type,
            "confidence": "INFERRED",
            "sf_in_loop": in_loop,
            "confidence_value": 0.85 if in_loop else 0.9,
            "source_location": f"L{line_no}",
            "source_file": str(path),
        }
        if sobject_name == "unknown":
            edge["sf_ambiguous"] = True
        edges.append(edge)

    # 5. Implements (QCP / Batchable hint) --------------------------------
    if (
        f"implements {CPQ_QCP_INTERFACE}" in code
        or "implements QuoteCalculatorPlugin" in code
    ):
        qcp_id = "sbqq_quotecalculatorplugin"
        if not any(n["id"] == qcp_id for n in nodes):
            nodes.append(
                {
                    "id": qcp_id,
                    "label": CPQ_QCP_INTERFACE,
                    "file_type": "code",
                    "source_file": str(path),
                    "sf_qualified_name": CPQ_QCP_INTERFACE,
                    "sf_code_type": "interface",
                    "sf_stub": True,
                }
            )
        edges.append(
            {
                "source": class_id,
                "target": qcp_id,
                "relation": "implements",
                "confidence": "EXTRACTED",
                "source_file": str(path),
            }
        )

    if "implements Database.Batchable" in code:
        nodes[0]["sf_async_pattern"] = "batchable"

    return {"nodes": nodes, "edges": edges}
