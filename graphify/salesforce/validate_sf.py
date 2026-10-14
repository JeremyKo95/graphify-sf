"""
graphify-sf: Salesforce-specific node / edge schema definitions and validation.

This module is a *schema-only* layer. It declares which Salesforce ``file_type``
and ``relation`` values are legal, what edge attributes each relation may carry,
and a graph-level integrity check (`validate_sf_graph`). It does NOT create any
nodes or edges — the parsers (apex_enhanced, flow, lwc, objects, profiles, …)
and analysis passes (cpq, order_of_execution, governor_limits) own that.

Companion to `graphify/validate.py`: that file validates the *base* graphify
schema (shared across all languages); this file adds the Salesforce-specific
vocabulary. The SF file types are mirrored into `validate.VALID_FILE_TYPES`
(step 1) so base validation accepts SF nodes too.

Schema source of truth: docs/ARCHITECTURE.md "스키마" section.
Confidence rubric: ADR-020. Risk relations: ADR-028~030.
"""

from __future__ import annotations

import networkx as nx

# ---------------------------------------------------------------------------
# 1. SF node file_type values (ARCHITECTURE.md schema table)
# ---------------------------------------------------------------------------

SF_FILE_TYPES = {
    "sobject",          # Salesforce Object (Account, SBQQ__Quote__c, …)
    "flow",             # Flow / Process Builder
    "lwc_component",    # Lightning Web Component
    "profile",          # Profile (user permissions)
    "permission_set",   # Permission Set
    "cpq_rule",         # CPQ Price/Product Rule object
    "cpq_qcp_method",   # QCP Calc Engine callback node (ADR-025)
    "cpq_condition",    # CPQ Price/Error Condition record (cpq_data)
    "cpq_action",       # CPQ Price/Product Action record (cpq_data)
    "aura_component",   # Aura Component (legacy)
    "validation_rule",  # Validation Rule (ADR-030)
    "record_type",          # Record Type (objects/<Obj>/recordTypes)
    "permission_set_group", # Permission Set Group
    "workflow",             # Workflow rule set (per SObject)
    "field",                # Custom Field (objects.py)
    "cmt_record",           # Custom Metadata record (customMetadata/<Type>.<Record>)
    "sharing_rule",         # Sharing Rule (owner/criteria/guest/territory)
    "custom_label",         # Custom Label ($Label.X reference target)
    "flexipage",            # Lightning App / Record / Home page
    "experience_page",      # Experience Cloud view (<site>/<view>)
    "experience_site",      # Experience Cloud site (experiences/<site>)
    "layout",               # Page Layout (<Obj>-<Layout Name>)
    "assignment_rule",      # Assignment Rule (assignmentRules/<Obj>)
    "escalation_rule",      # Escalation Rule (escalationRules/<Obj>)
    "auto_response_rule",   # Auto-Response Rule (autoResponseRules/<Obj>)
    "duplicate_rule",       # Duplicate Rule (duplicateRules/<Obj>.<Rule>)
    "matching_rule",        # Matching Rule (matchingRules/<Obj>)
    "auth_provider",        # Auth Provider (authproviders/<Name>)
    "quick_action",         # Quick Action ([<Obj>.]<Name>)
    "email_template",       # Email Template (<Folder>/<Name>)
    "static_resource",      # Static Resource (staticresources/<Name>)
}


# ---------------------------------------------------------------------------
# 2. SF edge relation values (ARCHITECTURE.md schema table)
# ---------------------------------------------------------------------------

SF_RELATIONS = {
    # Apex
    "triggers_on",          # Apex Trigger / record-triggered Flow -> SObject
    "member_of",            # Apex method -> owning class
    "implements",           # Apex class -> interface (e.g. SBQQ QCP)
    "calls",                # Apex class / trigger -> Apex class it invokes (INFERRED)
    "extends",              # Apex class / interface -> the repo class / interface it extends
    "reads_setting",        # Apex -> Custom Setting read via getInstance / getOrgDefaults
    "queries",              # SOQL site -> SObject
    "dml_operates_on",      # DML site -> SObject

    # Flow
    "flow_invokes",         # Flow ApexAction -> Apex class
    "invokes_subflow",      # Flow -> Flow it calls as a subflow

    # LWC
    "wire_to",              # LWC @wire -> Apex method
    "lwc_calls",            # LWC imperative @salesforce/apex import -> Apex method
    "aura_calls",           # Aura bundle -> Apex controller method it enqueues
    "uses_component",       # LWC / Aura -> child component or c/ module it uses
    "uses_label",           # Apex / Flow / formula / LWC / Aura / email -> Custom Label
    "uses_resource",        # LWC / Aura -> Static Resource

    # UI placement
    "hosts",                # FlexiPage / Experience page -> LWC / Aura component
    "page_for",             # Record FlexiPage -> SObject
    "aura_controller",      # Aura bundle -> Apex controller class
    "contains",             # Experience site -> its pages
    "layout_for",           # Page Layout -> SObject
    "places_field",         # Page Layout / Quick Action -> Field placed on it
    "action_for",           # Quick Action -> its SObject
    "launches",             # Quick Action -> Flow / LWC / Aura it opens
    "template_for",         # Email Template -> related entity SObject
    "uses_template",        # Workflow email alert -> Email Template

    # Permission
    "grants_access_to",     # Profile/PermSet -> Object/Field
    "field_of",             # Custom Field -> Custom Object
    "lookup_to",            # Lookup / Master-Detail field -> referenced SObject
    "reads_field",          # Apex / Flow / formula / Validation Rule -> Field read
    "writes_field",         # Apex / Flow -> Field written
    "contains_permission_set",  # Permission Set Group -> Permission Set

    # Metadata coverage
    "record_type_of",       # Record Type -> SObject
    "cmt_record_of",        # Custom Metadata record -> <Type>__mdt SObject
    "shares",               # Sharing Rule -> SObject
    "maps_to",              # field -> field (Custom Metadata field-mapping record)
    "rule_of",              # Assignment / Escalation / Auto-Response / Duplicate / Matching rule -> SObject
    "uses_matching_rule",   # Duplicate Rule -> Matching Rule
    "apex_handler",         # Auth Provider -> registration handler / plugin Apex class

    # CPQ
    "cpq_applies_to",       # CPQ Rule/QCP -> target object
    "cpq_has_condition",    # CPQ Rule -> Price/Error Condition (cpq_data)
    "cpq_has_action",       # CPQ Rule -> Price/Product Action (cpq_data)
    "cpq_reads_field",      # CPQ Condition -> Field (cpq_data)
    "cpq_writes_field",     # CPQ Action -> Field (cpq_data)

    # Order of Execution
    "order_of_execution",   # OoE Step N -> Step N+1 (DAG)
    "runs_in",              # Trigger / Flow / rule -> the OoE step it runs in

    # Diagnostics
    "governor_violation",       # Method -> Governor Limit sentinel
    "gov_permission_violation", # CPQ Rule -> Profile (FLS-restricted field, ADR-028)
    "validates",                # Validation Rule -> SObject

    # Relationship analysis (ADR-028~030)
    "publishes_event",      # Apex -> Platform Event
    "cpq_validation_risk",  # CPQ Rule <-> Validation Rule conflict
    "infinite_loop_risk",   # Flow <-> CPQ infinite loop
}


# ---------------------------------------------------------------------------
# 3. Per-relation edge attribute schema
# ---------------------------------------------------------------------------

#: Allowed edge attributes per relation type. ``required`` attrs MUST be present;
#: ``optional`` attrs MAY be present. Relations not listed here carry no
#: SF-specific attributes beyond the base edge fields.
EDGE_ATTRIBUTES_BY_RELATION: dict[str, dict[str, list[str]]] = {
    "queries": {
        "optional": ["sf_in_loop", "sf_dynamic", "sf_ambiguous"],
    },
    "dml_operates_on": {
        "optional": ["sf_in_loop"],
    },
    "cpq_applies_to": {
        "required": ["execution_order"],
        "optional": ["sf_cpq_rule_type", "sf_in_mass_action"],
    },
    "governor_violation": {
        "required": ["sf_violation_type", "sf_severity"],
        "optional": [
            "sf_in_loop", "sf_reason", "sf_violation_count", "sf_limit",
            # recursive_trigger violations (ADR-027)
            "sf_safe_recursion", "sf_cycle",
        ],
    },
    "cpq_validation_risk": {
        "optional": ["sf_risk_level", "sf_overlapping_fields", "sf_note"],
    },
    "infinite_loop_risk": {
        "optional": ["sf_loop_type", "sf_severity", "sf_reason", "sf_loop_prevention"],
    },
}


# ---------------------------------------------------------------------------
# 4. Graph-level validation
# ---------------------------------------------------------------------------

def validate_sf_graph(G: nx.DiGraph) -> list[str]:
    """SF-specific graph integrity checks.

    Complements `validate.validate_extraction` (base schema) with structural
    invariants that only make sense for the Salesforce graph.

    Checks:
        1. SObject node IDs follow the ``sobject_`` prefix convention (ADR-002).
        2. The ``order_of_execution`` subgraph is acyclic (must be a DAG).
        3. ``cpq_applies_to`` edges carry the required ``execution_order`` attr.
        4. ``governor_violation`` edges carry the required diagnostic attrs.
        5. Ontology integrity (``quality.graph_summary``): repo-relative paths,
           no source text on nodes, one owning object per field, Apex labels
           equal to their file names, ``cpq_rule`` only from CPQ rule data, and
           ``sf_qualified_name`` on every mappable node.

    Args:
        G: Assembled NetworkX directed graph.

    Returns:
        List of validation error messages. Empty list means the graph is valid.
    """
    errors: list[str] = []

    # Check 1: SObject node ID prefix convention. Only nodes that ARE SObjects
    # (file_type == "sobject") must follow it — a substring match on "sobject"
    # would wrongly flag Apex named ``SObjectUtil`` / methods like
    # ``getSObjectName`` (real-org false positives).
    for node, data in G.nodes(data=True):
        if data.get("file_type") == "sobject" and not str(node).startswith("sobject_"):
            errors.append(f"Malformed SObject node ID: {node}")

    # Check 2: Order of Execution chain must be a DAG
    ooe_edges = [
        (u, v)
        for u, v, d in G.edges(data=True)
        if d.get("relation") == "order_of_execution"
    ]
    if ooe_edges:
        ooe_graph = nx.DiGraph(ooe_edges)
        if not nx.is_directed_acyclic_graph(ooe_graph):
            cycles = list(nx.simple_cycles(ooe_graph))
            first = " -> ".join(cycles[0]) if cycles else "?"
            errors.append(
                f"OoE chain contains cycles (must be DAG): {first}"
            )

    # Checks 3 & 4: required edge attributes by relation
    for u, v, d in G.edges(data=True):
        relation = d.get("relation")
        spec = EDGE_ATTRIBUTES_BY_RELATION.get(relation)
        if not spec:
            continue
        for attr in spec.get("required", []):
            if attr not in d:
                errors.append(
                    f"{relation} edge missing required '{attr}': {u} -> {v}"
                )

    # Check 5: ontology integrity counters
    from graphify.salesforce.quality import graph_summary, integrity_errors

    errors.extend(f"Integrity: {e}" for e in integrity_errors(graph_summary(G)))

    return errors
