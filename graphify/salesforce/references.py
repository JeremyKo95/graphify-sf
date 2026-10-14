"""
graphify-sf: named-component references (Custom Labels, Static Resources,
child components).

Apex, Flow, formulas, LWC and Aura all name Custom Labels and Static Resources
by API name (``System.Label.X``, ``$Label.X``, ``@salesforce/label/c.X``,
``$Label.c.X``, ``@salesforce/resourceUrl/X``, ``$Resource.X``). Parsers turn
each mention into a ``uses_label`` / ``uses_resource`` edge plus a stub target
node (no dangling edges per file); ``extract_sf`` merges the stub with the real
``custom_label`` / ``static_resource`` node when the repo defines it.

A mention is *strict* when its syntax can also be something else — Apex
``Label.X`` may be a local variable named ``label``. ``resolve_named_refs``
drops strict edges whose target is still a stub after the merge, then removes
stubs nothing references.

Component composition (``<c-child>`` / ``import … from 'c/child'`` in LWC,
``<c:Child>`` in Aura) becomes ``uses_component`` edges resolved like ``hosts``:
the transient ``_component`` name is pointed at the LWC or Aura node of that
name by ``pages.resolve_hosted_components``.
"""

from __future__ import annotations

import re

from graphify.salesforce.constants import metadata_nid

#: ``System.Label.X`` / ``Label.X`` in comment- and string-stripped Apex.
APEX_LABEL_RE = re.compile(r"(?<![\w.])(?:System\s*\.\s*)?Label\s*\.\s*(\w+)\b(?!\s*\()",
                           re.IGNORECASE)

#: ``$Label.X`` in formulas, Flow and Visualforce; ``$Label.c.X`` in Aura;
#: ``$Label.ns.X`` for a packaged label.
#: A dynamic ``"$Label.c." + name`` (Aura) names no label and does not match.
GLOBAL_LABEL_RE = re.compile(r"\$Label\s*\.\s*(?:c\s*\.\s*)?(\w+(?:\.\w+)?)(?![\w.])")

#: ``import x from '@salesforce/label/c.X'`` (also ``ns.X`` for packaged labels).
LWC_LABEL_RE = re.compile(r"['\"]@salesforce/label/(?:c\.)?([\w.]+)['\"]")

#: ``import x from '@salesforce/resourceUrl/X'``.
LWC_RESOURCE_RE = re.compile(r"['\"]@salesforce/resourceUrl/(\w+)['\"]")

#: ``$Resource.X`` in Aura markup / Visualforce.
GLOBAL_RESOURCE_RE = re.compile(r"\$Resource\s*\.\s*(\w+)")

#: JS block comments and ``//`` line comments (not the ``//`` of a URL).
JS_COMMENT_RE = re.compile(r"/\*.*?\*/|(?<![:\\])//[^\n]*", re.DOTALL)

#: Reference kind -> (relation, target file_type, node-ID kind).
_KINDS = {
    "label": ("uses_label", "custom_label", "label"),
    "resource": ("uses_resource", "static_resource", "static_resource"),
}


def label_nid(name: str) -> str:
    """Node ID for a Custom Label (``label_<name>``, as ``extract_custom_labels``)."""
    return f"label_{name.lower()}"


def resource_nid(name: str) -> str:
    """Node ID for a Static Resource: ``static_resource_<name>``."""
    return metadata_nid("static_resource", name)


def _target_nid(kind: str, name: str) -> str:
    return label_nid(name) if kind == "label" else resource_nid(name)


def named_refs(source: str, kind: str, names, path, *, strict: bool = False
               ) -> tuple[list[dict], list[dict]]:
    """``(stub nodes, edges)`` for ``source`` naming each of *names*.

    *kind* is ``"label"`` or ``"resource"``. Names are deduped case-insensitively
    in first-seen order.
    """
    relation, file_type, _ = _KINDS[kind]
    nodes: list[dict] = []
    edges: list[dict] = []
    seen: set[str] = set()
    for name in names:
        target = _target_nid(kind, name)
        if target in seen:
            continue
        seen.add(target)
        nodes.append({"id": target, "label": name, "file_type": file_type,
                      "source_file": str(path), "sf_qualified_name": name,
                      "sf_stub": True})
        edge = {"source": source, "target": target, "relation": relation,
                "confidence": "EXTRACTED", "source_file": str(path)}
        if strict:
            edge["_strict"] = True
        edges.append(edge)
    return nodes, edges


def component_edges(source: str, components, path) -> list[dict]:
    """``uses_component`` edges (resolved later by ``resolve_hosted_components``)."""
    return [{"source": source, "target": None, "_component": name,
             "relation": "uses_component", "confidence": "EXTRACTED",
             "source_file": str(path)}
            for name in dict.fromkeys(components)]


def resolve_named_refs(all_nodes: list[dict], all_edges: list[dict]) -> None:
    """Drop strict references to undefined labels / resources, then orphan stubs.

    Mutates both lists in place. Must run after every file is merged so a
    definition anywhere in the repo has replaced its stub.
    """
    stub_ids = {n["id"] for n in all_nodes
                if n.get("sf_stub") and n.get("file_type") in ("custom_label", "static_resource")}
    kept: list[dict] = []
    for edge in all_edges:
        if edge.pop("_strict", False) and edge["target"] in stub_ids:
            continue
        kept.append(edge)
    all_edges[:] = kept
    if not stub_ids:
        return
    referenced = {e["target"] for e in all_edges} | {e["source"] for e in all_edges}
    all_nodes[:] = [n for n in all_nodes
                    if n["id"] not in stub_ids or n["id"] in referenced]
