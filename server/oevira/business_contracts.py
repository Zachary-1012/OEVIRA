"""Machine-readable input contracts for implemented Business operations.

This module contains protocol descriptions only. Numerical and domain semantics
remain owned by the existing operation evaluators and their versioned Artifact
consumer; a contract never supplies or fabricates business observations.
"""
from __future__ import annotations

from copy import deepcopy


CONTRACT_VERSION = "oevira-business-input-contracts-v1"


def _string(description: str, *, enum=None, format=None, nullable=False) -> dict:
    value = {"type": "string", "description": description}
    if enum is not None:
        value["enum"] = list(enum)
    if format is not None:
        value["format"] = format
    if nullable:
        return {"oneOf": [value, {"type": "null"}], "description": description}
    return value


def _number(description: str, *, minimum=None, exclusive_minimum=None, maximum=None, nullable=False) -> dict:
    value = {"type": "number", "description": description}
    if minimum is not None:
        value["minimum"] = minimum
    if exclusive_minimum is not None:
        value["exclusiveMinimum"] = exclusive_minimum
    if maximum is not None:
        value["maximum"] = maximum
    if nullable:
        return {"oneOf": [value, {"type": "null"}], "description": description}
    return value


def _integer(description: str, *, minimum=None, maximum=None, nullable=False) -> dict:
    value = {"type": "integer", "description": description}
    if minimum is not None:
        value["minimum"] = minimum
    if maximum is not None:
        value["maximum"] = maximum
    if nullable:
        return {"oneOf": [value, {"type": "null"}], "description": description}
    return value


def _boolean(description: str) -> dict:
    return {"type": "boolean", "description": description}


def _array(items: dict, description: str, *, minimum=0, maximum=None) -> dict:
    value = {"type": "array", "items": items, "description": description}
    if minimum:
        value["minItems"] = minimum
    if maximum is not None:
        value["maxItems"] = maximum
    return value


def _object(properties: dict, description: str, *, required=(), additional=False) -> dict:
    return {
        "type": "object",
        "properties": properties,
        "required": list(required),
        "additionalProperties": additional,
        "description": description,
    }


_TEXT = _string("Nonempty text unless the operation says otherwise.")
_REF = _object(
    {
        "material_id": _string("Exact saved Material identity."),
        "segment_id": _string("Saved Material segment identity."),
        "quote": _string("Exact saved source excerpt."),
        "artifact_id": _string("Exact saved Artifact identity."),
        "artifact_version": _integer("Exact positive Artifact version.", minimum=1),
        "label": _string("User-declared reference label."),
    },
    "A source reference. Saved Material and Artifact identities are checked by the Business API envelope; label-only entries remain unverified text.",
    additional=True,
)


def _contract(operation: str, family: str, schema: dict, *, qualifications=(), table_targets=(), sections=(), result_notes=()) -> dict:
    return {
        "contract_version": CONTRACT_VERSION,
        "contract_status": "complete",
        "operation": operation,
        "family": family,
        "input_schema": schema,
        "table_targets": list(table_targets),
        "qualifications": list(qualifications),
        "stable_sections": list(sections),
        "result_notes": list(result_notes),
    }


_TIMESTAMP = _string("ISO-8601 timestamp with explicit timezone.", format="date-time")
_SOURCE_REFS = _array(_REF, "Source references retained with the observation; actual Material access is checked by the API envelope.")

_OBSERVATION = _object(
    {
        "platform": _TEXT,
        "native_id": _TEXT,
        "text": _TEXT,
        "captured_at": _TIMESTAMP,
        "kind": _string("Original content role.", enum=("original", "repost", "root_comment", "reply", "correction", "unknown")),
        "information_origin": _string("Declared shared information origin or source family."),
        "source_refs": _SOURCE_REFS,
    },
    "One captured native content snapshot. Repeated native IDs keep the latest prior-to-cutoff snapshot; conflicting content at the same capture time is rejected.",
    required=("platform", "native_id", "text", "captured_at"),
    additional=True,
)

_CLAIM = _object(
    {
        "id": _TEXT,
        "target": _TEXT,
        "text": _TEXT,
        "status": _string("Claim review state.", enum=("unverified", "reviewed_supported", "disputed", "retracted", "opinion")),
        "severity": _string("Severity is separate from confidence and volume.", enum=("unknown", "routine", "material", "critical")),
        "support": _array(_REF, "Supporting references; presence does not independently verify the claim."),
        "against": _array(_REF, "Counterevidence references."),
        "reviewed_by": _string("User-recorded reviewer; required for reviewed_supported."),
        "review_basis": _string("User-recorded review basis; required for reviewed_supported."),
        "public_use_allowed": _boolean("Whether the user states that this fact may appear in the public response."),
    },
    "One target-specific claim; reviewed_supported requires at least one support reference, reviewer and basis.",
    required=("id", "target", "text"),
    additional=True,
)

_COVERAGE = _object(
    {"source": _TEXT, "status": _string("Observed source-check state.", enum=("complete", "partial", "not_checked", "unavailable", "failed"))},
    "User-recorded coverage by source; non-complete states never imply absence of risk.",
    required=("source", "status"), additional=True,
)

_TIMELINE = _object(
    {"kind": _string("Distinguishes occurrence from discovery and action.", enum=("occurred", "observable", "discovered", "verified", "approved", "acted")),
     "at": _TIMESTAMP, "description": _TEXT},
    "A user-supplied event or response timeline entry.", required=("kind", "at", "description"), additional=True,
)

_IMPACT = _object(
    {"audience": _string("Affected audience.", enum=("customer", "member", "tenant", "partner", "employee", "public")),
     "claim_type": _string("This operation does not establish causality.", enum=("observed", "association", "conditional_scenario", "unknown")),
     "description": _string("User-supplied impact note."), "source_refs": _SOURCE_REFS},
    "Observed impact or explicitly conditional/unknown assessment; causality is not inferred.",
    required=("audience",), additional=True,
)

_COMMITMENT = _object(
    {"id": _TEXT, "deliverable": _TEXT, "owner": _TEXT, "fulfilment_basis": _TEXT,
     "due_at": _TIMESTAMP, "approved_for_response": _boolean("User-declared approval for inclusion in a draft response."),
     "source_refs": _SOURCE_REFS},
    "A proposed service commitment; the calculation does not send it or establish that it was fulfilled.",
    required=("id", "deliverable", "owner", "fulfilment_basis"), additional=True,
)


_ARMS = _array(
    _object({"id": _TEXT, "role": _string("User-declared arm role."),
             "expected_share": _number("Prespecified allocation share in [0,1].", minimum=0, maximum=1, nullable=True),
             "artifact_version": {"description": "Optional exact internal version reference; does not prove external exposure."}},
            "One experiment arm.", required=("id",), additional=False),
    "Experiment arms. Unique IDs are required; expected shares, when all supplied, must sum to one.",
)

_METRIC = _object(
    {"kind": _string("Primary outcome scale.", enum=("binary", "continuous")),
     "name": _string("Metric name."), "unit": _string("Unit of measure."),
     "denominator": _string("Eligible analysis denominator definition."),
     "success_definition": _string("Binary success definition when applicable.")},
    "The primary outcome definition. Missing denominator or units remain unknown.",
    required=("kind",), additional=False,
)

_WINDOW = {
    "oneOf": [
        {"type": "null"},
        _object({"start": _TIMESTAMP, "end": _TIMESTAMP, "timezone": _string("Declared timezone label.")},
                "Observation window; start and end must occur together and start before end.", additional=True),
    ],
    "description": "Optional fixed observation window.",
}

_EXPERIMENT_DESIGN_FIELDS = {
    "title": _string("Optional result title."),
    "hypothesis": _string("Prespecified hypothesis."),
    "arms": _ARMS,
    "primary_metric": _METRIC,
    "window": _WINDOW,
    "control_arm": _string("ID of the declared comparison arm."),
    "assignment_unit": _string("Unit assigned to an arm."),
    "analysis_unit": _string("Independent unit used for the outcome estimate."),
    "allocation_method": _string("Declared allocation method; not verified by this operation."),
    "randomization_method": _string("Declared randomization method."),
    "randomization_record": _string("Reference or description of the assignment record."),
    "exposure_definition": _string("Prespecified exposure eligibility definition."),
    "eligibility_definition": _string("Prespecified inclusion and eligibility rule."),
    "sample_size_target": _integer("Optional target assigned-unit count.", minimum=1),
    "power_plan": _object(
        {"control_rate": _number("Expected binary control rate in [0,1].", minimum=0, maximum=1),
         "treatment_rate": _number("Expected binary treatment rate in [0,1].", minimum=0, maximum=1),
         "standard_deviation": _number("Expected continuous-metric standard deviation; same metric unit."),
         "minimum_effect": _number("Prespecified absolute minimum detectable effect; same metric unit.", exclusive_minimum=0),
         "target_power": _number("Two-sided normal-approximation target power (default 0.8).", exclusive_minimum=0.5, maximum=1),
         "alpha": _number("Two-sided normal-approximation alpha (default 0.05).", exclusive_minimum=0, maximum=1)},
        "Binary metrics require control_rate and treatment_rate; continuous metrics require standard_deviation and minimum_effect. Optional target_power/alpha use the shown defaults.", additional=False),
    "stopping_rule": {"description": "User-declared stopping rule; retained, not executed."},
    "contamination_exclusions": {"description": "User-declared contamination and exclusion rules."},
    "missing_and_withdrawal_rules": {"description": "User-declared missingness, maturity and withdrawal rules."},
    "cost_and_guardrail_metrics": {"description": "User-declared cost and guardrail metric definitions."},
    "success_definition": {"description": "User-declared success criteria."},
}

_ANALYSIS_OBSERVATION = _object(
    {"unit_id": _TEXT, "arm_id": _TEXT, "outcome": {"oneOf": [{"type": "integer", "enum": [0, 1]}, _number("Finite continuous outcome.")]},
     "outcome_mature": _boolean("Whether the outcome window is mature; defaults to whether outcome is non-null."),
     "missing_reason": _string("Reason the outcome is missing or immature."),
     "withdrawal_reason": _string("Reason the unit withdrew."),
     "assigned_at": _TIMESTAMP, "outcome_at": _TIMESTAMP, "cluster_id": _string("Independent cluster identity."),
     "external_exposure": _object({"version_id": _TEXT, "observed_at": _TIMESTAMP, "source_ref": _TEXT},
                                   "Saved external-exposure evidence; required fields are checked if supplied.",
                                   required=("version_id", "observed_at", "source_ref"), additional=False)},
    "One assignment-unit observation. Repeated rows and shared clusters are diagnosed, not counted as independent people.",
    required=("unit_id", "arm_id", "outcome"), additional=False,
)

_JOURNEY_TOUCH = _object(
    {"channel": _TEXT, "touchpoint_id": _string("Optional stable touch identity."), "occurred_at": _TIMESTAMP,
     "source": _object({}, "Optional saved-source metadata.", additional=True)},
    "One same-journey channel touch; timestamps are required for time-window eligibility and Markov ordering.",
    required=("channel",), additional=True,
)
_JOURNEY = _object(
    {"journey_id": _TEXT, "person_key": _string("Optional person identity supplied by the caller; not cross-platform identity proof."),
     "converted": {"oneOf": [{"type": "boolean"}, {"type": "null"}]},
     "conversion_status": _string("Explicitly mapped conversion state.", enum=("converted", "not_converted", "unknown")),
     "conversion_at": _TIMESTAMP, "observation_end": _TIMESTAMP,
     "conversion_value": _object({"amount": _number("Nonnegative value in the declared currency.", minimum=0),
                                  "currency": _TEXT, "status": _string("Actual and estimated values stay separate.", enum=("actual", "estimated"))},
                                 "Optional conversion value; no currency conversion is performed.", required=("amount", "currency", "status"), additional=False),
     "touches": _array(_JOURNEY_TOUCH, "Ordered same-journey touchpoints.")},
    "One observed journey. Duplicate journey IDs are rejected; unknown conversion status stays unknown.",
    required=("journey_id", "touches"), additional=True,
)

_REFUND = _object(
    {"refund_id": _TEXT, "journey_id": _TEXT, "amount": _number("Positive refund amount in the original currency.", exclusive_minimum=0),
     "currency": _TEXT, "status": _string("Refund status.", enum=("actual", "pending", "estimated", "unknown")),
     "occurred_at": _TIMESTAMP},
    "A refund notice linked to one journey; duplicates by refund_id must have identical content.",
    required=("refund_id", "journey_id", "amount", "currency"), additional=True,
)

_DID_OBSERVATION = _object(
    {"unit_id": _TEXT, "group": _string("Treatment assignment group.", enum=("treatment", "control")),
     "period": _TEXT, "value": _number("Observed value in metric_unit."),
     "cluster_id": _string("Optional cluster identity; repeated or cross-arm clusters are not independent."),
     "source_refs": _SOURCE_REFS},
    "One unit-period outcome observation.", required=("unit_id", "group", "period", "value"), additional=True,
)

_SYNTHETIC_OBSERVATION = _object(
    {"unit_id": _TEXT, "unit_label": _string("Optional display label."),
     "role": _string("Unit role.", enum=("treated", "donor")), "period": _TEXT,
     "value": _number("Observed outcome in metric_unit."),
     "untreated_declared": _boolean("Donor untreated-status declaration; required for donors."),
     "source": _object({}, "Saved source metadata for the observation.", additional=True)},
    "One balanced-panel unit-period outcome observation.", required=("unit_id", "role", "period", "value"), additional=False,
)

_CALIBRATION_ROW = _object(
    {"id": _TEXT, "partition": _string("Disjoint data partition.", enum=("training", "calibration", "test")),
     "score": _number("Raw prediction score in [0,1].", minimum=0, maximum=1, nullable=True),
     "label": _integer("Binary label 0/1 or null in training records.", minimum=0, maximum=1, nullable=True),
     "source": _object({}, "Saved source metadata.", additional=True)},
    "Unique scored observation. Calibration and test rows require finite scores and integer 0/1 labels.",
    required=("id", "partition"), additional=False,
)

_CONFORMAL_ROW = _object(
    {"id": _TEXT, "partition": _string("Disjoint data partition.", enum=("training", "calibration", "test")),
     "prediction": _number("Finite prediction in the declared unit; nullable only for training rows.", nullable=True),
     "observed": _number("Finite observed value in the same unit; nullable only for training rows.", nullable=True),
     "source": _object({}, "Saved source metadata.", additional=True)},
    "Unique prediction observation. Calibration/test rows require finite prediction and observed value.",
    required=("id", "partition"), additional=False,
)


# --- Operation registry, read API and declared consumers -----------------

_SOURCE_FORMATS = (
    "sec-companyfacts", "sec-companyconcept", "xbrl-instance", "inline-xbrl",
    "disclosure-extract", "regional-statement", "opendart-json", "macro-release",
)
_VINTAGE = ("filed", "workspace_capture")
_DATA_KIND = ("observed", "synthetic", "user_recorded")
_ATTRIBUTION_MODELS = ("first_touch", "last_touch", "linear", "markov_shapley")

_BRIEF = _object(
    {
        "goal": _string("Campaign or proposal goal."),
        "audience": _string("Intended audience."),
        "market": _string("Market or geography label."),
        "timezone": _string("IANA timezone used to interpret brief dates."),
        "client_title": _string("Optional client-facing title."),
        "currency": _string("ISO-4217 currency, three uppercase letters."),
        "start_date": _string("Inclusive start date."),
        "end_date": _string("Inclusive end date."),
        "constraints": _array(_string("A declared constraint."), "User-declared constraints."),
        "forbidden_claims": _array(_string("A claim that must not appear."), "Forbidden claims."),
        "customer_requests": _array(_string("A customer request."), "Customer requests."),
        "facts": _array(_object({}, "A brief fact retained as supplied.", additional=True), "User-declared facts."),
    },
    "Shared campaign brief. Currency and timezone, when present, are validated; evaluation does not invent a brief.",
    additional=True,
)
_QUOTES = _array(
    _object(
        {
            "label": _string("Quote label."),
            "amount": _number("Quoted amount.", nullable=True),
            "currency": _string("ISO-4217 currency."),
            "status": _string("Quote lifecycle.", enum=("quote", "estimate", "committed", "actual", "refund")),
            "valid_until": _string("Optional validity timestamp."),
            "source_refs": _SOURCE_REFS,
            "evidence_refs": _SOURCE_REFS,
        },
        "One quote or cost evidence row.",
        additional=True,
    ),
    "Quotes indexed for budget and plan calculations.",
)
_IDENTIFIED = _array(
    _object({"id": _TEXT}, "A row with a stable unique identity.", required=("id",), additional=True),
    "Identified rows; duplicate IDs are rejected.",
)
_QUALIFICATION = _object(
    {},
    "Caller-declared qualification evidence for event-analysis conditions. Missing required qualifications become blockers, they are not inferred.",
    additional=True,
)
_TITLE = _string("Optional result title.")
_QUESTION = _string("Optional analysis question.")


def _economics_operating() -> dict:
    return _contract(
        "operating_analysis", "economics",
        _object(
            {
                "title": _TITLE, "question": _QUESTION,
                "observations": _array(_object({}, "One operating observation with a unique id.", additional=True), "Unique-id operating observations; empty is allowed and produces an empty comparison."),
                "comparisons": _array(
                    _object(
                        {
                            "baseline_id": _TEXT, "current_id": _TEXT,
                            "kind": _string("Comparison kind.", enum=("same_period", "period_change")),
                        },
                        "Directed comparison of two different observations.",
                        required=("baseline_id", "current_id", "kind"), additional=False,
                    ),
                    "Optional explicit comparisons. When omitted, adjacent periods and same-window groups are compared.",
                ),
                "market_sales": _array(_object({}, "A market sales row.", additional=True), "Optional market sales evidence."),
                "voice_samples": _array(_object({}, "A voice-of-market sample.", additional=True), "Optional voice samples."),
                "market_boundary": _object({}, "Optional market-boundary declaration.", additional=True),
            },
            "Operating analysis inputs. Unsupported top-level fields are rejected.",
            additional=False,
        ),
        result_notes=("Comparability gaps stay visible; the operation does not bridge missing periods or invent observations.",),
    )


def _economics_consumer() -> dict:
    return _contract(
        "consumer_analysis", "economics",
        _object(
            {
                "title": _TITLE, "question": _QUESTION,
                "recommendation_survey": _object({}, "Recommendation or NPS-style survey summary.", additional=True),
                "sentiment_counts": _object({}, "Sentiment counts as supplied.", additional=True),
                "purchase_cohort": _object({}, "Purchase-cohort observations.", additional=True),
                "qualitative_research": _object({}, "Explicitly coded qualitative research. Codes are not invented.", additional=True),
            },
            "Consumer analysis inputs. Unsupported top-level fields are rejected.",
            additional=False,
        ),
        result_notes=("Qualitative codes and survey totals stay as supplied; the operation does not auto-code transcripts.",),
    )


def _economics_scenario() -> dict:
    return _contract(
        "financial_scenario", "economics",
        _object(
            {
                "title": _TITLE, "question": _QUESTION,
                "period": _object({}, "Scenario comparison period.", additional=True),
                "budget": _object(
                    {
                        "currency": _TEXT, "basis": _TEXT,
                        "period": _object({}, "Budget period.", additional=True),
                        "hard_limit": _number("Hard spending limit."),
                        "target": _number("Budget target."),
                        "reserve": _number("Reserved amount."),
                    },
                    "Required budget context.",
                    required=("currency", "basis", "hard_limit", "target", "reserve"), additional=True,
                ),
                "actuals": _array(_object({}, "An actual cost or income line.", additional=True), "Actuals lines."),
                "actuals_coverage": _object({}, "Coverage declaration for actuals.", additional=True),
                "scenarios": _array(_object({}, "One named scenario with lines and optional resource uses.", additional=True), "Alternative scenarios."),
            },
            "Budget and scenario comparison. Unsupported top-level fields are rejected.",
            required=("budget",), additional=False,
        ),
        result_notes=("Alternative estimates with the same cost key belong in separate scenarios; the operation does not merge them.",),
    )


def _economics_source() -> dict:
    return _contract(
        "financial_source_analysis", "economics",
        _object(
            {
                "source_format": _string("Exact parser selected for the saved original.", enum=_SOURCE_FORMATS),
                "raw_source": {"description": "Saved original bytes or text. The Business envelope supplies this from the selected Material; callers should not fabricate it."},
                "source": _object({}, "Provenance of the saved original.", additional=True),
                "filing_id": _string("Required for XBRL, inline XBRL and regional statements."),
                "filed": _string("Optional filing date.", format="date"),
                "form": _string("Optional form label."),
                "annotations": _array(_object({}, "A disclosure annotation.", additional=True), "Used by disclosure-extract."),
                "locators": _array(_object({}, "A regional-statement locator.", additional=True), "Used by regional-statement."),
                "exchange": _string("Optional exchange label for regional statements."),
                "fs_div": _string("OpenDART financial-statement division; default CFS."),
                "source_url": _string("Optional source URL claimed for a macro release."),
            },
            "Parse a saved financial original. The selected source_format must match the parser that can read that original.",
            required=("source_format",), additional=False,
        ),
        result_notes=("The operation reads the saved original; it does not fetch a live filing or invent facts.",),
    )


def _economics_corrections() -> dict:
    return _contract(
        "financial_fact_corrections", "economics",
        _object(
            {
                "facts": {"description": "Indexed financial fact observations from a prior source analysis.", "type": "object", "additionalProperties": True},
                "corrections": _array(_object({}, "An edge from an old observation to its replacing observation, with a reason and source_ref.", additional=True), "Correction edges."),
                "vintage_basis": _string("Vintage used to apply the as_of cutoff.", enum=_VINTAGE),
                "as_of": _string("Cutoff consistent with vintage_basis.", format="date-time"),
            },
            "Apply user-declared fact corrections onto an indexed fact set. Unsupported fields are rejected.",
            required=("facts", "corrections", "vintage_basis", "as_of"), additional=False,
        ),
        result_notes=("Conflicts are reported; the operation does not silently pick a winner.",),
    )


def _economics_quarterly() -> dict:
    return _contract(
        "financial_quarterly_series", "economics",
        _object(
            {
                "facts": {"description": "Indexed financial fact observations.", "type": "object", "additionalProperties": True},
                "corrections": _array(_object({}, "Optional correction edges.", additional=True), "Optional corrections applied before the series is built."),
                "metric": _TEXT, "unit": _TEXT,
                "filing_entity": _object({}, "Filing entity identity.", additional=True),
                "disclosure_scope": _string("Disclosure scope; default filing_entity."),
                "dimensions": _object({}, "Additional series dimensions.", additional=True),
                "statement_basis": _TEXT,
                "vintage_basis": _string("Vintage for the as_of cutoff.", enum=_VINTAGE),
                "as_of": _string("Cutoff consistent with vintage_basis."),
                "periods": _array(_object({}, "An explicit period request.", additional=True), "Optional explicit periods; otherwise periods come from the facts."),
            },
            "Build a quarterly series from indexed facts. Unsupported fields are rejected.",
            required=("facts", "metric", "unit", "filing_entity", "statement_basis", "vintage_basis", "as_of"),
            additional=False,
        ),
    )


def _economics_mmm() -> dict:
    return _contract(
        "financial_mmm_analysis", "economics",
        _object(
            {
                "periods": _array(_object({}, "One period's spend and outcome observation.", additional=True), "Nonempty observation periods.", minimum=1),
                "currency": _TEXT, "outcome_unit": _TEXT,
                "filing_entity": _object({}, "Filing entity identity.", additional=True),
                "disclosure_scope": _string("Optional disclosure scope."),
                "spend_basis": _string("Declared spend basis."),
                "budget": _object({}, "Optional budget context.", additional=True),
                "region": _string("Optional region label."),
                "observation_frequency": _string("Declared observation frequency."),
                "original_observation_count": _integer("Count of original observations before any transformation.", minimum=0),
                "transformation": _object({}, "Declared transformation, if any.", additional=True),
                "vintage_basis": _string("Vintage for the as_of cutoff.", enum=_VINTAGE),
                "as_of": _string("Cutoff consistent with vintage_basis."),
                "source_refs": _object({}, "Source references keyed for this analysis.", additional=True),
                "channel_labels": _object({}, "Display labels for spend channels.", additional=True),
            },
            "Marketing-mix analysis of supplied period observations. Envelope-resolved macro controls are not a user-authored field.",
            required=("periods", "currency", "outcome_unit", "filing_entity", "as_of"),
            additional=False,
        ),
        result_notes=("The operation does not treat fitted channel effects as proven causality.",),
    )


def _economics_timeseries() -> dict:
    return _contract(
        "operating_time_series", "economics",
        _object(
            {
                "title": _TITLE, "question": _QUESTION,
                "observations": _array(_object({}, "A time-ordered observation.", additional=True), "Required observation series."),
                "seasonal_cycle_periods": _integer("Seasonal cycle length in periods.", minimum=1),
                "forecast_horizon": _integer("Periods to forecast.", minimum=1),
                "holdout_periods": _integer("Holdout length; defaults to the forecast horizon.", minimum=1),
                "event_labels": _array(_object({}, "An event label on the series.", additional=True), "Optional event labels."),
                "forecast_method": _string("Forecast method; default arima.", enum=("arima",)),
            },
            "Operating time-series evaluation. Unsupported fields are rejected.",
            required=("observations",), additional=False,
        ),
        result_notes=("Gaps stay visible; the forecast does not invent missing history.",),
    )


def _campaign_shared(extra: dict, description: str, required=(), notes=()) -> dict:
    properties = {"brief": _BRIEF, "quotes": _QUOTES, **extra}
    return _object(properties, description, required=required, additional=False), notes


def _campaign_strategy() -> dict:
    schema, notes = _campaign_shared(
        {
            "candidates": _IDENTIFIED,
            "deliverables": _IDENTIFIED,
            "resources": _IDENTIFIED,
        },
        "Strategy proposal. At least one concrete candidate is required. Revision-only fields such as candidate_removals cannot be supplied as inputs.",
        required=("candidates",),
        notes=("Evaluation reports gaps and conflicts; it does not contact partners or publish a proposal.",),
    )
    return _contract("strategy_proposal", "campaign", schema, result_notes=notes)


def _campaign_media() -> dict:
    schema, notes = _campaign_shared(
        {
            "candidates": _IDENTIFIED,
            "media_items": _array(_object({}, "A media line used when candidates are omitted.", additional=True), "Direct media lines; used when candidates is omitted."),
            "deliverables": _IDENTIFIED,
        },
        "Media plan. Supply candidates, or media_items as a single implicit plan. At least one plan is required.",
        notes=("The plan does not book inventory or spend.",),
    )
    return _contract("media_plan", "campaign", schema, result_notes=notes)


def _campaign_collaboration() -> dict:
    schema, notes = _campaign_shared(
        {
            "collaborators": _IDENTIFIED,
            "collaborations": _IDENTIFIED,
        },
        "Collaboration plan. collaborators, or the collaborations alias, must contain at least one project-bound collaborator.",
        notes=("Nicknames and follower counts are not used to infer creator identity or effect. The plan does not contact creators, publish, or pay.",),
    )
    return _contract("collaboration_plan", "campaign", schema, result_notes=notes)


def _campaign_schedule() -> dict:
    schema, notes = _campaign_shared(
        {
            "tasks": _IDENTIFIED,
            "resources": _IDENTIFIED,
            "campaigns": _IDENTIFIED,
            "topics": _IDENTIFIED,
            "series": _IDENTIFIED,
            "posts": _IDENTIFIED,
            "calendar_nodes": _IDENTIFIED,
            "member_touchpoints": _IDENTIFIED,
        },
        "Campaign and content schedule. Commitment release and execution correction are revision controls, not inputs.",
        notes=("The schedule does not send messages, publish posts, or release commitments.",),
    )
    return _contract("campaign_schedule", "campaign", schema, result_notes=notes)


def _measurement_design() -> dict:
    return _contract(
        "experiment_design", "measurement",
        _object(_EXPERIMENT_DESIGN_FIELDS, "Prespecified experiment design. Missing denominator, units or a power plan remain unknown rather than assumed.", additional=False),
        sections=("design",),
        result_notes=("A power plan is computed only from declared rates or effects; the operation does not invent a baseline.",),
    )


def _measurement_analysis() -> dict:
    return _contract(
        "experiment_analysis", "measurement",
        _object(
            {
                "title": _TITLE,
                "arms": _ARMS,
                "metric": _METRIC,
                "control_arm": _TEXT,
                "window": _WINDOW,
                "confidence_level": _number("Two-sided confidence level; default 0.95.", exclusive_minimum=0, maximum=1),
                "srm_alpha": _number("Optional SRM test alpha.", exclusive_minimum=0, maximum=1),
                "allocation_method": _string("Declared allocation method."),
                "randomization_record": _string("Assignment record reference."),
                "assignment_unit": _string("Unit assigned to an arm."),
                "analysis_unit": _string("Independent analysis unit."),
                "sequential_plan": {"description": "User-declared sequential plan; retained, not executed as an automatic stopper."},
                "request_next_observation_suggestion": _boolean("Opt-in for a next-observation suggestion."),
                "observations": _array(_ANALYSIS_OBSERVATION, "Assignment-unit observations.", minimum=1),
            },
            "Experiment analysis of supplied assignment observations. At least two arms are required and control_arm must identify one of them.",
            required=("arms", "metric", "control_arm", "observations"), additional=True,
        ),
        result_notes=("Repeated rows and shared clusters are diagnosed, not counted as independent people. Adaptive allocation disables fixed-share SRM.",),
    )


def _measurement_attribution() -> dict:
    return _contract(
        "attribution_analysis", "measurement",
        _object(
            {
                "title": _TITLE,
                "model": _string("Attribution model.", enum=_ATTRIBUTION_MODELS),
                "lookback_days": _number("Optional positive lookback window in days.", exclusive_minimum=0),
                "journeys": _array(_JOURNEY, "Observed journeys. Journey IDs must be unique.", minimum=1),
                "touchpoints": _array(
                    _object(
                        {
                            "journey_id": _TEXT, "channel": _TEXT,
                            "touchpoint_id": _string("Optional stable touch identity."),
                            "occurred_at": _TIMESTAMP,
                            "source": _object({}, "Optional saved-source metadata.", additional=True),
                        },
                        "A touchpoint that will be attached to the named journey. Orphan journey_ids are counted, not silently created.",
                        required=("journey_id",), additional=True,
                    ),
                    "Optional flat touchpoints, merged onto journeys by journey_id.",
                ),
                "refunds": _array(_REFUND, "Refund notices linked to journeys."),
            },
            "Journey attribution. model and journeys are required.",
            required=("model", "journeys"), additional=True,
        ),
        result_notes=("Unknown conversion status stays unknown. No currency conversion is performed. Attribution is a credit rule, not a causal claim.",),
    )


def _measurement_incrementality() -> dict:
    return _contract(
        "incrementality_analysis", "measurement",
        _object(
            {
                "title": _TITLE,
                "estimator": _string("Estimator; default did.", enum=("did", "synthetic_control")),
                "pre_periods": _array(_TEXT, "Required unique pre periods for difference-in-differences."),
                "post_periods": _array(_TEXT, "Required unique post periods, disjoint from pre_periods."),
                "period_positions": _object({}, "Optional numeric positions for every declared period, strictly increasing in declared order.", additional=True),
                "pretrend_equivalence_margin": _number("Optional positive equivalence margin for a pretrend check.", exclusive_minimum=0),
                "unit_of_analysis": _string("Unit of analysis label."),
                "metric_unit": _string("Unit of the outcome."),
                "confidence_level": _number("Two-sided confidence level; default 0.95.", exclusive_minimum=0, maximum=1),
                "observations": _array(
                    {"oneOf": [_DID_OBSERVATION, _SYNTHETIC_OBSERVATION]},
                    "Unit-period observations. Difference-in-differences uses treatment/control groups; synthetic control uses treated/donor roles.",
                    minimum=1,
                ),
            },
            "Incrementality analysis. Difference-in-differences requires nonempty disjoint pre and post periods.",
            required=("observations",), additional=True,
        ),
        result_notes=("The operation does not treat a significant estimate as proven causality. Shared or crossing clusters are not treated as independent.",),
    )


def _measurement_calibration() -> dict:
    return _contract(
        "calibration_analysis", "measurement",
        _object(
            {
                "title": _TITLE,
                "method": _string("Calibration method; default isotonic.", enum=("isotonic",)),
                "rows": _array(_CALIBRATION_ROW, "Unique scored observations with disjoint partitions.", minimum=1),
                "model_provenance": _object({}, "Optional model provenance for the scores.", additional=True),
            },
            "Score calibration. Calibration and test rows require finite scores and 0/1 labels.",
            required=("rows",), additional=True,
        ),
        result_notes=("Training rows are not used as if they were a held-out calibration set.",),
    )


def _measurement_conformal() -> dict:
    return _contract(
        "conformal_analysis", "measurement",
        _object(
            {
                "title": _TITLE,
                "alpha": _number("Miscoverage level; default 0.1.", exclusive_minimum=0, maximum=1),
                "rows": _array(_CONFORMAL_ROW, "Unique prediction observations with disjoint partitions.", minimum=1),
                "model_provenance": _object({}, "Optional model provenance.", additional=True),
            },
            "Conformal prediction intervals. Calibration and test rows require finite prediction and observed values.",
            required=("rows",), additional=True,
        ),
        result_notes=("Intervals are conditional on the supplied exchangeability assumptions; they are not a causal claim.",),
    )


_SERVICE_SCHEMA = _object(
    {
        "title": _TITLE,
        "goal": _TEXT,
        "as_of": _TIMESTAMP,
        "observations": _array(_OBSERVATION, "Native content snapshots captured at or before as_of."),
        "claims": _array(_CLAIM, "Target-specific claims."),
        "coverage": _array(_COVERAGE, "User-recorded source-check coverage."),
        "timeline": _array(_TIMELINE, "Event and response timeline."),
        "impacts": _array(_IMPACT, "Observed or explicitly conditional impact notes."),
        "commitments": _array(_COMMITMENT, "Proposed service commitments; approved_for_response controls inclusion in the draft response."),
    },
    "Incident or service-response inputs. goal and as_of are required. Snapshots later than as_of are excluded, not rewritten.",
    required=("goal", "as_of"), additional=True,
)


def _service_incident() -> dict:
    return _contract(
        "incident_brief", "service", _SERVICE_SCHEMA,
        result_notes=("Claims are not independently verified. reviewed_supported requires support, reviewer and basis.",),
    )


def _service_response() -> dict:
    return _contract(
        "service_response", "service", _SERVICE_SCHEMA,
        result_notes=("The calculation does not send a response or establish that a commitment was fulfilled.",),
    )


def _events_network() -> dict:
    return _contract(
        "propagation_network", "events",
        _object(
            {
                "title": _TITLE,
                "data_kind": _string("Provenance of the supplied observations; default user_recorded.", enum=_DATA_KIND),
                "qualification": _QUALIFICATION,
                "nodes": _array(_object({"id": _TEXT}, "A network node.", required=("id",), additional=True), "Network nodes."),
                "edges": _array(_object({}, "A directed or undirected observed edge with source references.", additional=True), "Observed edges."),
                "relation": _string("Declared relation represented by the edges."),
            },
            "Propagation network from supplied nodes and edges.",
            required=("nodes", "edges"), additional=True,
        ),
        result_notes=("Missing qualification becomes a blocker. The network is not a causal influence map.",),
    )


def _events_hawkes() -> dict:
    return _contract(
        "propagation_hawkes", "events",
        _object(
            {
                "title": _TITLE,
                "data_kind": _string("Provenance of the supplied observations; default user_recorded.", enum=_DATA_KIND),
                "qualification": _QUALIFICATION,
                "events": _array(
                    _object(
                        {"id": _TEXT, "time": _number("Event time inside the observation window.", minimum=0), "source_refs": _SOURCE_REFS},
                        "One event. Missing time or source_refs leaves the event unplaced rather than interpolating a timestamp.",
                        required=("id",), additional=True,
                    ),
                    "Observed event times.",
                ),
                "observation_end": _number("Positive end of the observation window.", exclusive_minimum=0),
                "time_unit": _TEXT,
            },
            "Hawkes point-process fit on supplied event times.",
            required=("events", "observation_end", "time_unit"), additional=True,
        ),
        result_notes=("Tied timestamps are not jittered to fake precision. Conditions below the method's threshold stay insufficient rather than being forced into a fit.",),
    )


def _events_adoption() -> dict:
    return _contract(
        "adoption_diffusion", "events",
        _object(
            {
                "title": _TITLE,
                "data_kind": _string("Provenance of the supplied observations; default user_recorded.", enum=_DATA_KIND),
                "qualification": _QUALIFICATION,
                "observations": _array(_object({}, "A first-adoption cumulative observation.", additional=True), "First-adoption observations."),
                "time_unit": _TEXT,
            },
            "Bass-style first-adoption diffusion on supplied cumulative observations.",
            required=("observations", "time_unit"), additional=True,
        ),
        result_notes=("Cumulative engagement or repeat purchases are not treated as first-adoption counts. The fit does not invent a market potential when conditions are insufficient.",),
    )


def _decision_comparison() -> dict:
    return _contract(
        "decision_comparison", "decision",
        _object(
            {
                "title": _TITLE,
                "question": _TEXT,
                "information_cutoff": _string("ISO date or timezone-aware timestamp. Day-only cutoffs are accepted as date precision."),
                "criteria": _array(
                    _object(
                        {
                            "id": _TEXT, "name": _TEXT,
                            "direction": _string("Optimization direction.", enum=("maximize", "minimize")),
                            "unit": _TEXT, "definition": _TEXT,
                            "weight": _number("Optional declared weight.", minimum=0),
                        },
                        "One comparison criterion.",
                        required=("id", "name", "direction", "unit", "definition"), additional=False,
                    ),
                    "Nonempty unique criteria.",
                    minimum=1,
                ),
                "constraints": _array(
                    _object(
                        {
                            "id": _TEXT, "description": _TEXT,
                            "criterion_id": _string("Optional criterion this numeric constraint applies to."),
                            "operator": _string("Numeric comparison operator."),
                            "value": _number("Numeric limit."),
                        },
                        "A hard constraint with a stable identity and its actual condition.",
                        required=("id", "description"), additional=True,
                    ),
                    "Optional hard constraints.",
                ),
                "alternatives": _array(
                    _object({"id": _TEXT, "title": _string("Option title."), "values": _object({}, "Declared criterion values.", additional=True)}, "One alternative option.", required=("id",), additional=True),
                    "Options being compared.",
                ),
                "pairwise_preferences": {"description": "Optional pairwise criterion preferences used to derive weights."},
                "candidate_additions": _array(_object({}, "A sensitivity addition, not a base option.", additional=True), "Optional sensitivity additions."),
            },
            "Multi-criteria option comparison. question and a nonempty criteria list are required. Unsupported fields are rejected.",
            required=("question", "criteria"), additional=False,
        ),
        result_notes=("Infeasible or incomplete options stay in those states. The ranking is not a recommendation to act.",),
    )


def _build_contracts() -> dict[str, dict]:
    builders = (
        _economics_operating, _economics_consumer, _economics_scenario,
        _economics_source, _economics_corrections, _economics_quarterly,
        _economics_mmm, _economics_timeseries,
        _campaign_strategy, _campaign_media, _campaign_collaboration, _campaign_schedule,
        _measurement_design, _measurement_analysis, _measurement_attribution,
        _measurement_incrementality, _measurement_calibration, _measurement_conformal,
        _service_incident, _service_response,
        _events_network, _events_hawkes, _events_adoption,
        _decision_comparison,
    )
    contracts = {}
    for builder in builders:
        contract = builder()
        name = contract["operation"]
        if name in contracts:
            raise RuntimeError(f"duplicate business contract for {name}")
        if contract.get("contract_status") != "complete":
            raise RuntimeError(f"incomplete business contract for {name}")
        contracts[name] = contract
    return contracts


CONTRACTS = _build_contracts()


class BusinessContractError(Exception):
    """Raised when a caller asks for an operation that has no contract."""

    def __init__(self, code: str, status: int = 404, message: str | None = None):
        super().__init__(message or code)
        self.code = code
        self.status = status


def implemented_operations() -> frozenset[str]:
    """Operations currently implemented by the Business evaluators."""
    from .business_execution import (
        CAMPAIGN_OPERATIONS,
        DECISION_OPERATIONS,
        ECONOMICS_OPERATIONS,
        EVENT_OPERATIONS,
        MEASUREMENT_OPERATIONS,
        SERVICE_OPERATIONS,
    )
    return frozenset(
        ECONOMICS_OPERATIONS | CAMPAIGN_OPERATIONS | MEASUREMENT_OPERATIONS
        | SERVICE_OPERATIONS | EVENT_OPERATIONS | DECISION_OPERATIONS
    )


def assert_contracts_cover_implemented() -> None:
    """Fail loudly if the registry drifts from implemented operations."""
    implemented = implemented_operations()
    registered = set(CONTRACTS)
    missing = implemented - registered
    extra = registered - implemented
    incomplete = [
        name for name, contract in CONTRACTS.items()
        if contract.get("contract_status") != "complete"
        or not isinstance(contract.get("input_schema"), dict)
        or contract.get("input_schema", {}).get("type") != "object"
    ]
    if missing or extra or incomplete:
        raise RuntimeError(
            "Business contracts drifted from implemented operations: "
            f"missing={sorted(missing)} extra={sorted(extra)} incomplete={incomplete}"
        )


def get_contract(operation: str) -> dict:
    """Return a deep copy of the machine-readable contract for one operation."""
    if not isinstance(operation, str) or operation not in CONTRACTS:
        raise BusinessContractError(
            "UNKNOWN_BUSINESS_OPERATION", 404,
            "This business operation is not supported",
        )
    return deepcopy(CONTRACTS[operation])


def list_operation_contracts() -> list[dict]:
    """Return every implemented operation contract, sorted by operation name."""
    return [deepcopy(CONTRACTS[name]) for name in sorted(CONTRACTS)]
