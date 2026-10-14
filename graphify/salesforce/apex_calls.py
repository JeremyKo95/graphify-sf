"""
graphify-sf: Apex cross-class call resolution (review items 9 / 16).

Graph-wide pass that links Apex classes / triggers to the Apex classes they
invoke. A per-file parser cannot do this: whether ``Foo.bar()`` is a call into
an Apex class depends on every class in the repo, so the pass runs after all
files are merged and matches against the full set of class names.

Three invocation shapes count, matched case-insensitively (Apex identifiers are
case-insensitive) on comment- and string-stripped code:

    - ``Foo.bar(...)`` / ``Outer.Inner.bar(...)`` — static call (also an
      instance call through a variable named like its class);
    - ``new Foo(...)`` / ``new Outer.Inner(...)`` — instantiation;
    - ``Foo.class`` — type literal handed to a dispatcher / framework.

A bare type declaration (``Foo x;``) is not a call and is ignored. Each caller ->
callee pair becomes one class-level ``calls`` edge, ``INFERRED``: a name match
cannot rule out a local variable that shadows a class name. Dynamic dispatch
(``Type.forName``, handler classes named in Custom Metadata) stays unresolved.

Type declarations (``class A extends B implements C, D``, also on inner
classes) add ``extends`` / ``implements`` edges to the repo's own classes and
interfaces, ``EXTRACTED``; platform types (``Queueable``, ``Database.Batchable``)
are not linked.

CRITICAL (ADR-016): graph-wide pass, never cached per file. Reads the transient
``_source`` the Apex parser leaves on class / trigger nodes; mutates
``all_edges`` in place.
"""

from __future__ import annotations

import re

from graphify.salesforce.apex_enhanced import strip_apex

#: Class-level Apex code types that can be the target of a call.
_CALLABLE_TYPES = ("class", "interface", "enum")

_STATIC_CALL_RE = re.compile(r"(?<![\w.])(\w+)\s*\.\s*\w+(?:\s*\.\s*\w+)*\s*\(")
_NEW_RE = re.compile(r"\bnew\s+(\w+)(?:\s*\.\s*\w+)*\s*\(", re.IGNORECASE)
_CLASS_LITERAL_RE = re.compile(r"(?<![\w.])(\w+)\s*\.\s*class\b", re.IGNORECASE)
_TYPE_HEADER_RE = re.compile(
    r"\b(?:class|interface)\s+\w+\s*"
    r"(?:extends\s+(?P<extends>[\w.<>,\s]+?))?\s*"
    r"(?:implements\s+(?P<implements>[\w.<>,\s]+?))?\s*\{",
    re.IGNORECASE)


def _type_names(clause: str | None) -> list[str]:
    """Last segment of each type in an ``extends`` / ``implements`` list."""
    if not clause:
        return []
    clause = re.sub(r"<[^<>]*>", "", re.sub(r"<[^<>]*>", "", clause))
    return [part.strip().split(".")[-1] for part in clause.split(",") if part.strip()]


def apex_calls_pass(all_nodes: list[dict], all_edges: list[dict]) -> None:
    """Append ``calls`` / ``extends`` / ``implements`` edges between Apex code nodes (in place)."""
    callees = {
        str(n["label"]).lower(): n["id"]
        for n in all_nodes
        if n.get("sf_code_type") in _CALLABLE_TYPES and not n.get("sf_stub")
    }
    if not callees:
        return

    for node in all_nodes:
        source = node.get("_source")
        if not source or node.get("sf_code_type") not in (*_CALLABLE_TYPES, "trigger"):
            continue
        code, _strings = strip_apex(source)
        first_seen: dict[str, int] = {}
        for pattern in (_STATIC_CALL_RE, _NEW_RE, _CLASS_LITERAL_RE):
            for match in pattern.finditer(code):
                target = callees.get(match.group(1).lower())
                if target and target != node["id"]:
                    pos = match.start()
                    if pos < first_seen.get(target, len(code)):
                        first_seen[target] = pos
        inherited: dict[tuple[str, str], int] = {}
        for match in _TYPE_HEADER_RE.finditer(code):
            for relation in ("extends", "implements"):
                for name in _type_names(match.group(relation)):
                    target = callees.get(name.lower())
                    if target and target != node["id"]:
                        inherited.setdefault((relation, target), match.start())
        for (relation, target), pos in sorted(inherited.items()):
            all_edges.append({
                "source": node["id"],
                "target": target,
                "relation": relation,
                "confidence": "EXTRACTED",
                "source_location": f"L{code.count(chr(10), 0, pos) + 1}",
                "source_file": node.get("source_file", ""),
            })
        for target in sorted(first_seen):
            all_edges.append({
                "source": node["id"],
                "target": target,
                "relation": "calls",
                "confidence": "INFERRED",
                "confidence_value": 0.8,
                "source_location": f"L{code.count(chr(10), 0, first_seen[target]) + 1}",
                "source_file": node.get("source_file", ""),
            })
