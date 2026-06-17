"""
graphify-sf: Permission / Field-Level Security analysis pass (ADR-028).

Optional graph-wide analysis pass that cross-references Profile / Permission Set
Field-Level Security against CPQ rules to surface access risks. The Profile
parser (profiles.py) stores FLS as the ``sf_fls_permissions`` attribute on the
profile / permission_set node (NOT a separate node, per ADR-028); this pass
reads that attribute and flags CPQ (``SBQQ__``) fields that a profile cannot
read but a CPQ rule on the same object depends on.

Each detected risk becomes a ``gov_permission_violation`` diagnostic edge
(CPQ rule -> Profile, registered in validate_sf.py), tagged ``INFERRED`` (0.75):
field-overlap signals a likely conflict, but the actual failure depends on
runtime values / which fields the rule reads (ADR-028 / ADR-030 limitation).

Scope (ADR-028 / step spec):
    - Only ``SBQQ__`` (CPQ) fields are analyzed — generic FLS is out of scope.
    - A field is matched to a CPQ rule by its SObject (the part before the
      first ``.``); the rule's CPQ object is read from ``sf_cpq_object`` / label.
    - OWD (org-wide defaults) and User-level permission inference are NOT
      supported (explicitly out of scope).

CRITICAL (ADR-016): graph-wide O(N) pass, NOT cached per file — always re-run on
the full merged graph. Like ``governor_limit_analysis_pass`` it is a pure
function: it returns the diagnostic edges for the caller to
``all_edges.extend(...)`` and mutates neither input list.
"""

from __future__ import annotations

import re

#: Matches a Salesforce CPQ (SteelBrick) object/field API name token, e.g.
#: ``SBQQ__Quote__c`` inside a label like "SBQQ__PriceRule__c Discount".
_SBQQ_TOKEN_RE = re.compile(r"SBQQ__\w+")


def _cpq_object_name(node: dict) -> str | None:
    """Return the ``SBQQ__`` object API name a cpq_rule node represents.

    Reads ``sf_cpq_object`` (set by cpq_analysis_pass) falling back to ``label``,
    and extracts the first ``SBQQ__…`` token. Returns ``None`` when absent.
    """
    text = node.get("sf_cpq_object") or node.get("label", "")
    match = _SBQQ_TOKEN_RE.search(text)
    return match.group(0) if match else None


def permission_analysis_pass(
    all_nodes: list[dict], all_edges: list[dict]
) -> list[dict]:
    """Analyze Profile/FLS constraints on CPQ rules.

    Detects FLS-restricted (non-readable) ``SBQQ__`` fields whose SObject is a
    CPQ rule, and emits one ``gov_permission_violation`` risk edge per
    (cpq_rule, profile, restricted field) — from the CPQ rule to the profile
    that restricts it (ADR-028).

    Pure function: ``all_edges`` is unused (kept for a uniform pass signature)
    and neither list is mutated; the caller does ``all_edges.extend(...)``.

    Args:
        all_nodes: Merged node list (read-only).
        all_edges: Merged edge list (read-only; present for signature parity).

    Returns:
        List of ``gov_permission_violation`` risk diagnostic edges.
    """
    # Index CPQ rule nodes by the SObject they represent (SBQQ__ object name).
    cpq_rules_by_object: dict[str, list[str]] = {}
    for node in all_nodes:
        if node.get("file_type") != "cpq_rule":
            continue
        object_name = _cpq_object_name(node)
        if object_name is None:
            continue
        cpq_rules_by_object.setdefault(object_name, []).append(node["id"])

    if not cpq_rules_by_object:
        return []

    risk_edges: list[dict] = []
    for node in all_nodes:
        if node.get("file_type") not in ("profile", "permission_set"):
            continue
        fls_permissions = node.get("sf_fls_permissions", {})
        for field_name, perms in fls_permissions.items():
            # Only CPQ fields; readable fields carry no access risk.
            if "SBQQ__" not in field_name or perms.get("readable"):
                continue
            # The field's SObject is the part before the first "." (Object.Field).
            object_token = _SBQQ_TOKEN_RE.search(field_name.split(".")[0])
            if object_token is None:
                continue
            for cpq_rule_id in cpq_rules_by_object.get(object_token.group(0), []):
                risk_edges.append(
                    {
                        "source": cpq_rule_id,
                        "target": node["id"],
                        "relation": "gov_permission_violation",
                        "confidence": "INFERRED",
                        "confidence_value": 0.75,
                        "sf_risk_type": "fls_restricted",
                        "sf_field": field_name,
                        "sf_severity": "HIGH",
                    }
                )

    return risk_edges
