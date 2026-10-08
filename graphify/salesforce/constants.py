"""
graphify-sf: Salesforce domain constants and helpers.

This module is the *single source of truth* for:
    - SObject node ID generation (`sobject_nid`) — ADR-002, and the other
      API-name-based IDs (`field_nid`, `apex_nid`, `apex_method_nid`,
      `validation_rule_nid`)
    - Governor Limits values + staleness metadata — ADR-006, ADR-026
    - Salesforce Order of Execution (OoE) step definitions
    - CPQ (SBQQ__) object / interface constants

CRITICAL (ADR-002): `sobject_nid()` is the ONLY function allowed to build
SObject node IDs anywhere in graphify-sf. Every parser (Apex, Flow, Object XML,
LWC) must call it so that `build_graph()` merges the same SObject into one node.
Do not hand-assemble "sobject_..." strings elsewhere.

CRITICAL (ADR-006): Governor Limits are hardcoded here and synced via
`release_sync.sync_release_notes()`. Do not redefine these values anywhere else.
"""

from __future__ import annotations

import re
import unicodedata

# ---------------------------------------------------------------------------
# 1. SObject node ID (ADR-002 — single source of truth)
# ---------------------------------------------------------------------------

#: Strip everything except word chars (incl. underscore), hyphen, and dot.
#: `\w` keeps double underscores ("__") and the "__c" custom suffix intact.
_SOBJECT_ID_DISALLOWED = re.compile(r"[^\w\-.]")


def sobject_nid(api_name: str) -> str:
    """Convert a Salesforce API name to a normalized graph node ID.

    Examples:
        sobject_nid("Account")        -> "sobject_account"
        sobject_nid("SBQQ__Quote__c") -> "sobject_sbqq__quote__c"
        sobject_nid("Contact__c")     -> "sobject_contact__c"
        sobject_nid("account")        -> "sobject_account"   (case-insensitive)

    Rules:
        - Unicode NFKC normalization, then lowercase (Salesforce is
          case-insensitive on API names).
        - Surrounding whitespace stripped; internal disallowed chars
          (incl. whitespace) collapsed to underscore.
        - Double underscore ("__") preserved (e.g. SBQQ__Quote__c).
        - "__c" / "__e" custom/event suffixes preserved.

    Returns:
        Node ID in the form ``"sobject_{normalized_api_name}"``.
    """
    return f"sobject_{_normalize_api(api_name)}"


def sobject_stub(api_name: str, source_file) -> dict:
    """A referenced-but-not-yet-defined SObject node (``sf_stub=True``).

    Parsers emit stubs so their edges never dangle; ``extract_sf`` lets the
    object's own definition replace the stub, and a stub that survives means the
    object has no metadata in the repo (standard / managed-package object).
    """
    return {
        "id": sobject_nid(api_name),
        "label": api_name,
        "file_type": "sobject",
        "source_file": str(source_file),
        "sf_api_name": api_name,
        "sf_qualified_name": api_name,
        "sf_object_type": "custom" if "__" in api_name else "standard",
        "sf_stub": True,
    }


def _normalize_api(api_name: str) -> str:
    normalized = unicodedata.normalize("NFKC", api_name.strip()).lower()
    return _SOBJECT_ID_DISALLOWED.sub("_", normalized)


#: Object segment of a field ID whose owning object could not be determined
#: (e.g. a CPQ condition naming only a field). ``?`` never survives
#: ``_normalize_api``, so it cannot collide with a real object.
UNRESOLVED_OBJECT = "?"


def field_nid(object_api: str | None, field_api: str) -> str:
    """Node ID for a field, qualified by its owning object.

    Examples:
        field_nid("Account", "CLOUD_Miles__c") -> "field_account.cloud_miles__c"
        field_nid(None, "Discount__c")         -> "field_?.discount__c"

    The object is part of the ID because field API names repeat across objects
    (``Name``, ``OwnerId``, ``Status__c`` …); ``__c`` is kept so a custom field
    never merges with a same-named standard field.
    """
    obj = _normalize_api(object_api) if object_api else UNRESOLVED_OBJECT
    return f"field_{obj}.{_normalize_api(field_api)}"


def apex_nid(class_name: str) -> str:
    """Node ID for an Apex class / trigger: ``apex_<name>``."""
    return f"apex_{_normalize_api(class_name)}"


def apex_method_nid(class_name: str, method_name: str) -> str:
    """Node ID for an Apex method: ``apex_<class>.<method>``.

    The ``.`` separator keeps ``Foo_Bar.baz`` and ``Foo.bar_baz`` distinct.
    Overloads share one node (callers such as LWC imports cannot tell them apart).
    """
    return f"apex_{_normalize_api(class_name)}.{_normalize_api(method_name)}"


def validation_rule_nid(object_api: str, rule_name: str) -> str:
    """Node ID for a Validation Rule: ``validation_<object>.<rule>``."""
    return f"validation_{_normalize_api(object_api)}.{_normalize_api(rule_name)}"


# ---------------------------------------------------------------------------
# 2. Governor Limits (ADR-006, ADR-026 — single source of truth)
# ---------------------------------------------------------------------------

GOVERNOR_LIMITS: dict[str, int] = {
    "soql_queries_per_transaction": 100,
    "dml_statements_per_transaction": 150,
    "heap_size_bytes_sync": 6_000_000,
    "heap_size_bytes_async": 12_000_000,
    "cpu_time_ms_sync": 10_000,
    "cpu_time_ms_batch": 60_000,
    "callouts_per_transaction": 100,
}

#: Release these limits were last verified against (see ADR-026).
GOVERNOR_LIMITS_VERSION = "spring-2025"
#: ISO date of the last `sync-release-notes` run. Staleness is measured from here.
GOVERNOR_LIMITS_SYNCED_AT = "2025-03-01"


# ---------------------------------------------------------------------------
# 3. Order of Execution (18 steps)
# ---------------------------------------------------------------------------

#: Salesforce Order of Execution as (step_number, description, trigger_type).
#: Used by `order_of_execution.ooe_analysis_pass()` to build the OoE chain for
#: SObjects that have a real trigger / Before-Save Flow / Validation Rule
#: (ADR-005, ADR-017).
SALESFORCE_OOE_STEPS: list[tuple[int, str, str]] = [
    (1, "System Validation", "system"),
    (2, "Before Trigger", "apex"),
    (3, "Custom Validation", "validation_rule"),
    (4, "Before-Save Flow", "flow"),
    (5, "CPQ Calc Engine", "cpq"),
    (6, "Duplicate Rules", "duplicate_rule"),
    (7, "Database Save (Before Commit)", "system"),
    (8, "After Trigger", "apex"),
    (9, "Assignment Rules", "assignment_rule"),
    (10, "Auto-Response Rules", "auto_response_rule"),
    (11, "Workflow Rules", "workflow_rule"),
    (12, "Process Builder", "process_builder"),
    (13, "After-Save Flow", "flow"),
    (14, "Escalation Rules", "escalation_rule"),
    (15, "Roll-Up Summary", "rollup_summary"),
    (16, "Criteria-Based Sharing", "sharing"),
    (17, "Commit DML", "system"),
    (18, "Post-Commit Logic", "post_commit"),
]


# ---------------------------------------------------------------------------
# 4. CPQ (SBQQ__) constants
# ---------------------------------------------------------------------------

#: Prefix that identifies Salesforce CPQ (SteelBrick) managed-package objects.
CPQ_RULE_PREFIX = "SBQQ__"

#: Quote Calculator Plugin interface implemented by custom QCP classes.
CPQ_QCP_INTERFACE = "SBQQ.QuoteCalculatorPlugin"

#: QCP callback methods, in CPQ Calc Engine execution order (ADR-025).
CPQ_QCP_METHODS = [
    "onBeforePriceRules",
    "onAfterPriceRules",
    "onBeforeCalculate",
    "calculate",  # legacy single-method form
    "onAfterCalculate",
]

#: Standard CPQ objects. `SBQQ__Calculation__c` is an ephemeral calc-time record
#: (excluded from OoE chains via `sf_cpq_ephemeral`, see PRD).
CPQ_OBJECTS = [
    "SBQQ__Quote__c",
    "SBQQ__QuoteLine__c",
    "SBQQ__QuoteLineGroup__c",
    "SBQQ__ProductOption__c",
    "SBQQ__ConfigurationAttribute__c",
    "SBQQ__ProductRule__c",
    "SBQQ__PriceRule__c",
    "SBQQ__PriceCondition__c",
    "SBQQ__PriceAction__c",
    "SBQQ__ErrorCondition__c",
    "SBQQ__DiscountSchedule__c",
    "SBQQ__Subscription__c",
    "SBQQ__Calculation__c",
]
