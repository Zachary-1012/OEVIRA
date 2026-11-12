"""Resolve exact saved financial Artifact versions for business calculations.

This is an adapter over the existing Artifact, Material, Work and commission
owners. It does not persist a second financial-source store. The caller keeps
``_resolved_financial_sources`` out of user inputs and stores the manifest on
the resulting business Artifact version for commit rechecks and cold reopen.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
from typing import Any

from .api import ApiError, _not_found
from .business_analysis_economics import _check_cancelled
from .collaboration import require_artifact_access, require_material_access, work_commission


_FACT_OPERATIONS = {"financial_fact_corrections", "financial_quarterly_series"}


def _version(value: Any, field: str) -> int:
    if type(value) is not int or value < 1:
        raise ApiError("INVALID_FINANCIAL_SOURCE", f"{field} must be an exact positive Artifact version", 400)
    return value


def _canonical_hash(value: Any) -> str:
    try:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as error:
        raise ApiError("FINANCIAL_SOURCE_UNREADABLE", "The selected saved result is not valid finite JSON", 409) from error
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _positive_reference(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {"artifact_id", "artifact_version"}:
        raise ApiError("INVALID_FINANCIAL_SOURCE", f"{field} needs artifact_id and artifact_version", 400)
    artifact_id = value["artifact_id"]
    if not isinstance(artifact_id, str) or not artifact_id.strip():
        raise ApiError("INVALID_FINANCIAL_SOURCE", f"{field}.artifact_id is required", 400)
    return {"artifact_id": artifact_id, "artifact_version": _version(value["artifact_version"], f"{field}.artifact_version")}


def _artifact_reference_list(value: Any, field: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value:
        raise ApiError("SAVED_FINANCIAL_SOURCE_REQUIRED", f"Select at least one exact saved source Artifact for {field}", 400)
    references = [_positive_reference(item, f"{field}[{index}]") for index, item in enumerate(value)]
    identities = [(item["artifact_id"], item["artifact_version"]) for item in references]
    if len(identities) != len(set(identities)):
        raise ApiError("INVALID_FINANCIAL_SOURCE", f"{field} cannot repeat the same Artifact version", 400)
    return references


def _source_reference_list(inputs: dict[str, Any], field: str) -> list[dict[str, Any]]:
    plural = inputs.get("source_artifacts")
    singular = inputs.get("source_artifact")
    if plural is not None and singular is not None:
        raise ApiError("INVALID_FINANCIAL_SOURCE", "Use one source_artifact field or source_artifacts list", 400)
    if plural is None and singular is not None:
        plural = [singular]
    return _artifact_reference_list(plural, field)


def _artifact_version(store, connection, identity, work_id: str, reference: dict[str, Any]):
    artifact = require_artifact_access(connection, identity, reference["artifact_id"])
    artifact_work_id = artifact["work_id"]
    if artifact_work_id != work_id:
        linked = connection.execute(
            "SELECT 1 FROM work_artifacts WHERE work_id=? AND artifact_id=? AND workspace_id=?",
            (work_id, artifact["id"], identity["workspace"]["id"]),
        ).fetchone()
        if linked is None:
            raise ApiError("FINANCIAL_SOURCE_WORK_MISMATCH", "Adopt this exact financial result into the current work before using it", 403)
    source_scope = work_commission(connection, artifact_work_id)
    target_scope = work_commission(connection, work_id)
    if source_scope and source_scope != target_scope:
        raise ApiError("CLIENT_SCOPE_MISMATCH", "Private financial work cannot cross client commissions", 403)
    version = reference["artifact_version"]
    saved = connection.execute(
        "SELECT * FROM artifact_versions WHERE artifact_id=? AND workspace_id=? AND version=?",
        (artifact["id"], identity["workspace"]["id"], version),
    ).fetchone()
    if saved is None:
        raise _not_found()
    snapshot = json.loads(saved["input_snapshot_json"])
    business = snapshot.get("business") if isinstance(snapshot, dict) else None
    if not isinstance(snapshot, dict) or snapshot.get("kind") != "business_analysis" or not isinstance(business, dict):
        raise ApiError("FINANCIAL_SOURCE_NOT_PARSED", "The selected Artifact is not a saved business analysis", 409)
    manifest = {
        "artifact_id": artifact["id"],
        "artifact_version": version,
        "content_sha256": _canonical_hash(dict(saved)),
        "operation": business.get("operation"),
    }
    return artifact, saved, snapshot, business, manifest


def _read_and_verify_material(store, connection, identity, work_id: str, material_ref: dict[str, Any], *, is_cancelled=None) -> dict[str, Any]:
    _check_cancelled(is_cancelled)
    material_id = material_ref.get("material_id")
    expected = material_ref.get("sha256", material_ref.get("material_sha256"))
    if not isinstance(material_id, str) or not material_id or not isinstance(expected, str) or len(expected) != 64:
        raise ApiError("FINANCIAL_FACT_SOURCE_UNAVAILABLE", "A parsed financial fact has no exact saved Material version", 409)
    material = require_material_access(connection, identity, material_id, target_work_id=work_id)
    if material["sha256"] != expected:
        raise ApiError("SOURCE_CHANGED", "A financial source Material changed after parsing", 409)
    relative = material["relative_path"]
    if not isinstance(relative, str) or not relative:
        raise ApiError("FINANCIAL_FACT_SOURCE_UNAVAILABLE", "The saved financial source has no retained original path", 409)
    original_path = store.root / relative
    path = original_path.resolve()
    root = Path(store.root).resolve()
    if original_path.is_symlink() or not path.is_relative_to(root) or not path.is_file():
        raise ApiError("FINANCIAL_FACT_SOURCE_UNAVAILABLE", "The saved financial original is unavailable", 409)
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            while True:
                _check_cancelled(is_cancelled)
                chunk = stream.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
    except OSError as error:
        raise ApiError("FINANCIAL_FACT_SOURCE_UNAVAILABLE", "The saved financial original cannot be read", 409) from error
    if digest.hexdigest() != expected:
        raise ApiError("SOURCE_CHANGED", "The financial original no longer matches its saved hash", 409)
    return {
        "material_id": material["id"],
        "sha256": material["sha256"],
        "filename": material["filename"],
        "title": material["title"],
        "format": material["format"],
    }


def _saved_materials(snapshot: dict) -> dict[str, dict[str, Any]]:
    rows = snapshot.get("materials")
    if not isinstance(rows, list):
        raise ApiError("FINANCIAL_SOURCE_UNREADABLE", "The saved result has no frozen source manifest", 409)
    output = {}
    for row in rows:
        if isinstance(row, dict) and isinstance(row.get("id"), str):
            output[row["id"]] = row
    return output


def _fact_sources(store, connection, identity, work_id: str, snapshot: dict, facts: list[dict], *, is_cancelled=None) -> list[dict[str, Any]]:
    frozen = _saved_materials(snapshot)
    found: dict[str, dict[str, Any]] = {}
    for index, fact in enumerate(facts):
        _check_cancelled(is_cancelled)
        if not isinstance(fact, dict):
            raise ApiError("FINANCIAL_SOURCE_UNREADABLE", f"financial fact {index} is malformed", 409)
        source = fact.get("source_ref")
        if not isinstance(source, dict):
            raise ApiError("FINANCIAL_FACT_SOURCE_UNAVAILABLE", f"financial fact {index} has no source reference", 409)
        material_id = source.get("material_id")
        material_sha = source.get("material_sha256", source.get("material_version"))
        manifest = frozen.get(material_id) if isinstance(material_id, str) else None
        if manifest is None or manifest.get("sha256") != material_sha:
            raise ApiError("FINANCIAL_FACT_SOURCE_UNAVAILABLE", f"financial fact {index} is not tied to the exact Material frozen in its saved Artifact", 409)
        if material_id not in found:
            found[material_id] = _read_and_verify_material(
                store, connection, identity, work_id,
                {"material_id": material_id, "sha256": material_sha},
                is_cancelled=is_cancelled,
            )
    return [found[key] for key in sorted(found)]


def _source_facts(store, connection, identity, work_id: str, reference: dict[str, Any], *, is_cancelled=None) -> tuple[list[dict], dict[str, Any]]:
    _check_cancelled(is_cancelled)
    artifact, _version_row, snapshot, business, version_manifest = _artifact_version(
        store, connection, identity, work_id, reference,
    )
    if business.get("operation") != "financial_source_analysis":
        raise ApiError("FINANCIAL_SOURCE_NOT_PARSED", "Select an exact saved financial source analysis result", 409)
    source_format = business.get("inputs", {}).get("source_format") if isinstance(business.get("inputs"), dict) else None
    if source_format == "macro-release":
        raise ApiError("FINANCIAL_SOURCE_NOT_CORPORATE_FACTS", "A macroeconomic release cannot be used as corporate filing facts", 409)
    sections = [
        item for item in business.get("generated_sections", [])
        if isinstance(item, dict) and item.get("id") == "financial_source.facts"
    ]
    if len(sections) != 1 or not isinstance(sections[0].get("data"), dict):
        raise ApiError("FINANCIAL_SOURCE_NOT_FOUND", "The exact saved financial result has no fact section", 409)
    facts = sections[0]["data"].get("facts")
    if not isinstance(facts, list) or not facts:
        raise ApiError("FINANCIAL_FACTS_UNAVAILABLE", "The selected filing contains no usable numeric facts", 409)
    materials = _fact_sources(store, connection, identity, work_id, snapshot, facts, is_cancelled=is_cancelled)
    version_manifest.update(source_format=source_format, material_ids=[item["material_id"] for item in materials])
    return deepcopy(facts), {"artifact": version_manifest, "materials": materials, "source_work_id": artifact["work_id"]}


def _resolved_fact_sources(store, connection, identity, work_id: str, references: list[dict[str, Any]], *, is_cancelled=None) -> tuple[list[dict], dict[str, Any]]:
    facts: list[dict] = []
    manifests = []
    materials: dict[str, dict] = {}
    by_id: dict[str, dict] = {}
    for reference in references:
        _check_cancelled(is_cancelled)
        selected, manifest = _source_facts(
            store, connection, identity, work_id, reference, is_cancelled=is_cancelled,
        )
        manifests.append(manifest["artifact"])
        for material in manifest["materials"]:
            materials[material["material_id"]] = material
        for fact in selected:
            identity_id = fact.get("observation_id")
            if not isinstance(identity_id, str) or not identity_id:
                raise ApiError("FINANCIAL_SOURCE_UNREADABLE", "A saved financial fact has no stable identity", 409)
            previous = by_id.get(identity_id)
            if previous is None:
                by_id[identity_id] = fact
                facts.append(fact)
                continue
            comparable_previous, comparable_fact = dict(previous), dict(fact)
            comparable_previous.pop("source_ref", None)
            comparable_fact.pop("source_ref", None)
            comparable_previous.pop("source_ref_alternates", None)
            comparable_fact.pop("source_ref_alternates", None)
            comparable_previous.pop("captured_at", None)
            comparable_fact.pop("captured_at", None)
            if comparable_previous != comparable_fact:
                raise ApiError("FINANCIAL_FACT_ID_COLLISION", "Different saved facts share one observation identity", 409)
            references_for_fact = list(previous.get("source_ref_alternates", []))
            primary = previous.get("source_ref")
            if isinstance(primary, dict):
                references_for_fact.insert(0, primary)
            alternate = fact.get("source_ref")
            if isinstance(alternate, dict) and alternate not in references_for_fact:
                references_for_fact.append(alternate)
            for alternate in fact.get("source_ref_alternates", []):
                if isinstance(alternate, dict) and alternate not in references_for_fact:
                    references_for_fact.append(alternate)
            previous["source_ref_alternates"] = references_for_fact[1:]
            captures = [
                value for value in (previous.get("captured_at"), fact.get("captured_at"))
                if isinstance(value, str) and value
            ]
            if captures:
                previous["captured_at"] = min(captures)
    return facts, {
        "resolved_source_versions": sorted(manifests, key=lambda item: (item["artifact_id"], item["artifact_version"])),
        "source_materials": [materials[key] for key in sorted(materials)],
    }


def _source_corrections(inputs: dict[str, Any], facts: list[dict]) -> dict[str, Any]:
    original = inputs.get("corrections", [])
    if not isinstance(original, list):
        raise ApiError("INVALID_FINANCIAL_CORRECTION", "corrections must be a list", 400)
    index = {fact["observation_id"]: fact for fact in facts}
    normalized = []
    for number, raw in enumerate(original):
        if not isinstance(raw, dict):
            raise ApiError("INVALID_FINANCIAL_CORRECTION", f"corrections[{number}] must be an object", 400)
        if set(raw) - {"old_observation_id", "new_observation_id", "reason", "source_ref"}:
            raise ApiError("INVALID_FINANCIAL_CORRECTION", f"corrections[{number}] contains unsupported fields", 400)
        old_id, new_id = raw.get("old_observation_id"), raw.get("new_observation_id")
        if not isinstance(old_id, str) or not isinstance(new_id, str) or old_id not in index or new_id not in index:
            raise ApiError("FINANCIAL_FACT_NOT_SELECTED", f"corrections[{number}] must refer to facts from the selected exact source versions", 409)
        new_fact = index[new_id]
        valid_refs = [new_fact.get("source_ref"), *new_fact.get("source_ref_alternates", [])]
        valid_refs = [ref for ref in valid_refs if isinstance(ref, dict)]
        supplied = raw.get("source_ref")
        if supplied is not None and supplied not in valid_refs:
            raise ApiError("FINANCIAL_CORRECTION_SOURCE_MISMATCH", f"corrections[{number}] source reference must be the correcting fact's retained evidence", 409)
        normalized.append({
            "old_observation_id": old_id,
            "new_observation_id": new_id,
            "reason": raw.get("reason"),
            "source_ref": deepcopy(supplied if supplied is not None else valid_refs[0]) if valid_refs else {},
            "captured_at": new_fact.get("captured_at"),
        })
    return {**inputs, "facts": facts, "corrections": normalized}


def _extract_quarterly_series(store, connection, identity, work_id: str, reference: dict[str, Any], *, is_cancelled=None) -> tuple[dict, dict]:
    _check_cancelled(is_cancelled)
    _artifact, _version_row, snapshot, business, manifest = _artifact_version(
        store, connection, identity, work_id, reference,
    )
    if business.get("operation") != "financial_quarterly_series":
        raise ApiError("FINANCIAL_OUTCOME_ARTIFACT_INVALID", "MMM outcome must come from a saved quarterly financial analysis", 409)
    saved_inputs = business.get("inputs")
    if not isinstance(saved_inputs, dict) or "facts" in saved_inputs:
        raise ApiError("FINANCIAL_OUTCOME_ARTIFACT_UNVERIFIED", "The selected quarterly result was not resolved from exact saved financial sources", 409)
    source_refs = saved_inputs.get("source_artifacts")
    references = _artifact_reference_list(source_refs, "quarterly_result.source_artifacts")
    facts, upstream = _resolved_fact_sources(store, connection, identity, work_id, references, is_cancelled=is_cancelled)
    declared_manifest = business.get("resolved_financial_sources")
    if not isinstance(declared_manifest, dict):
        raise ApiError("FINANCIAL_OUTCOME_ARTIFACT_UNVERIFIED", "The saved quarterly result has no resolved financial-source manifest", 409)
    if (
        declared_manifest.get("resolved_source_versions") != upstream.get("resolved_source_versions")
        or declared_manifest.get("source_materials") != upstream.get("source_materials")
    ):
        raise ApiError("FINANCIAL_OUTCOME_SOURCE_CHANGED", "The quarterly result's saved source versions no longer match its derived manifest", 409)
    sections = [
        row for row in business.get("generated_sections", [])
        if isinstance(row, dict) and row.get("id") == "financial_quarters.series"
    ]
    if len(sections) != 1 or not isinstance(sections[0].get("data"), dict):
        raise ApiError("FINANCIAL_OUTCOME_ARTIFACT_INVALID", "The selected quarterly Artifact has no saved series section", 409)
    series = deepcopy(sections[0]["data"])
    periods = series.get("periods")
    if not isinstance(periods, list):
        raise ApiError("FINANCIAL_OUTCOME_ARTIFACT_INVALID", "The selected quarterly series is unreadable", 409)
    fact_index = {row["observation_id"]: row for row in facts}
    quarterly_materials = _saved_materials(snapshot)
    for index, period in enumerate(periods):
        _check_cancelled(is_cancelled)
        if not isinstance(period, dict):
            raise ApiError("FINANCIAL_OUTCOME_ARTIFACT_INVALID", f"quarterly series row {index} is malformed", 409)
        ids = period.get("observation_ids", [])
        refs = period.get("source_refs", [])
        if not isinstance(ids, list) or not isinstance(refs, list):
            raise ApiError("FINANCIAL_OUTCOME_ARTIFACT_INVALID", f"quarterly series row {index} has malformed fact references", 409)
        for identity_id in ids:
            if identity_id not in fact_index:
                raise ApiError("FINANCIAL_OUTCOME_FACT_NOT_SELECTED", f"quarterly series row {index} contains a fact outside its saved source Artifact versions", 409)
        for source_ref in refs:
            if not isinstance(source_ref, dict):
                raise ApiError("FINANCIAL_OUTCOME_ARTIFACT_INVALID", f"quarterly series row {index} has a malformed source reference", 409)
            material_id = source_ref.get("material_id")
            material_sha = source_ref.get("material_sha256", source_ref.get("material_version"))
            frozen = quarterly_materials.get(material_id) if isinstance(material_id, str) else None
            if frozen is None or frozen.get("sha256") != material_sha:
                raise ApiError("FINANCIAL_OUTCOME_SOURCE_CHANGED", f"quarterly series row {index} no longer matches its frozen Material source", 409)
            if material_id not in {item["material_id"] for item in upstream["source_materials"]}:
                raise ApiError("FINANCIAL_OUTCOME_FACT_NOT_SELECTED", f"quarterly series row {index} source is outside its selected parsed Artifacts", 409)
    manifest["operation"] = business.get("operation")
    manifest["source_materials"] = upstream["source_materials"]
    manifest["upstream_source_versions"] = upstream["resolved_source_versions"]
    return series, {"artifact": manifest, "resolved_source_versions": upstream["resolved_source_versions"], "source_materials": upstream["source_materials"]}


def _merge_quarterly_outcomes(inputs: dict[str, Any], series: dict[str, Any], artifact_manifest: dict[str, Any]) -> dict[str, Any]:
    periods = inputs.get("periods")
    if not isinstance(periods, list):
        raise ApiError("INVALID_FINANCIAL_MMM_PERIODS", "MMM channel-spend observations must be a table", 400)
    series_by_period = {
        row.get("period"): row for row in series["periods"] if isinstance(row, dict) and isinstance(row.get("period"), str)
    }
    requested_vintage = inputs.get("vintage_basis")
    requested_as_of = inputs.get("as_of")
    if series.get("vintage_basis") != requested_vintage or series.get("as_of") != requested_as_of:
        raise ApiError("FINANCIAL_OUTCOME_VINTAGE_MISMATCH", "The outcome series and MMM must use the same information cutoff", 409)
    requested_entity = inputs.get("filing_entity")
    actual_entity = series.get("filing_entity")
    if not isinstance(requested_entity, dict) or not isinstance(actual_entity, dict) or any(
        requested_entity.get(field) != actual_entity.get(field) for field in ("scheme", "identifier")
    ):
        raise ApiError("FINANCIAL_OUTCOME_ENTITY_MISMATCH", "The outcome series entity does not match this MMM request", 409)
    if inputs.get("disclosure_scope") != series.get("disclosure_scope"):
        raise ApiError("FINANCIAL_OUTCOME_SCOPE_MISMATCH", "The outcome series disclosure scope does not match this MMM request", 409)
    if inputs.get("outcome_unit") != series.get("unit"):
        raise ApiError("FINANCIAL_OUTCOME_UNIT_MISMATCH", "The outcome series unit does not match this MMM request", 409)
    merged = []
    for index, raw in enumerate(periods):
        if not isinstance(raw, dict):
            raise ApiError("INVALID_FINANCIAL_MMM_PERIODS", f"periods[{index}] must be an object", 400)
        if raw.get("outcome") is not None:
            raise ApiError("FINANCIAL_OUTCOME_OVERRIDE_FORBIDDEN", "MMM outcomes must come from the selected saved quarterly Artifact", 409)
        period = raw.get("period")
        selected = series_by_period.get(period)
        if selected is None:
            outcome = None
            source_refs = []
        else:
            outcome = selected.get("value") if selected.get("state") == "AVAILABLE" else None
            source_refs = selected.get("source_refs", [])
        row = {key: deepcopy(value) for key, value in raw.items() if key != "outcome"}
        row["outcome"] = outcome
        existing_refs = row.get("source_refs", {})
        if not isinstance(existing_refs, dict):
            raise ApiError("INVALID_FINANCIAL_MMM_PERIODS", f"periods[{index}].source_refs must be an object", 400)
        outcome_refs = [
            {**deepcopy(reference),
             "artifact_id": artifact_manifest["artifact"]["artifact_id"],
             "artifact_version": artifact_manifest["artifact"]["artifact_version"],
             "artifact_sha256": artifact_manifest["artifact"]["content_sha256"]}
            for reference in source_refs if isinstance(reference, dict)
        ]
        if not outcome_refs and selected is not None and selected.get("state") != "AVAILABLE":
            outcome_refs = [{
                "artifact_id": artifact_manifest["artifact"]["artifact_id"],
                "artifact_version": artifact_manifest["artifact"]["artifact_version"],
                "artifact_sha256": artifact_manifest["artifact"]["content_sha256"],
                "period": period,
                "state": selected.get("state"),
                "reason": selected.get("reason"),
            }]
        row["source_refs"] = {**existing_refs, "outcome": outcome_refs}
        merged.append(row)
    return {**inputs, "periods": merged}


def resolve_saved_financial_inputs(store, connection, identity, work_id, operation, inputs, *, is_cancelled=None):
    """Resolve immutable filing facts or quarter outcomes from exact saved Artifacts.

    The returned evaluation inputs contain server-resolved facts/period values
    plus an internal ``_resolved_financial_sources`` manifest. Callers must pop
    that internal key before the pure evaluator, persist it next to the result,
    and pass the saved inputs back through this resolver before revisions.
    """
    _check_cancelled(is_cancelled)
    if not isinstance(inputs, dict):
        raise ApiError("INVALID_INPUT", "Financial business inputs must be an object", 400)
    if operation in _FACT_OPERATIONS:
        if "facts" in inputs:
            raise ApiError("CLIENT_FINANCIAL_FACTS_FORBIDDEN", "Financial facts must be read from exact saved source Artifact versions", 409)
        if "_resolved_financial_sources" in inputs:
            raise ApiError("CLIENT_FINANCIAL_SOURCE_OVERRIDE", "Financial source manifests are server-generated", 400)
        references = _source_reference_list(inputs, "source_artifacts")
        facts, manifest = _resolved_fact_sources(store, connection, identity, work_id, references, is_cancelled=is_cancelled)
        saved_inputs = deepcopy(inputs)
        saved_inputs["source_artifacts"] = references
        saved_inputs.pop("source_artifact", None)
        saved_inputs.pop("facts", None)
        if operation == "financial_fact_corrections":
            resolved_corrections = _source_corrections(saved_inputs, facts)
            saved_inputs["corrections"] = [{key: value for key, value in correction.items() if key != "captured_at"} for correction in resolved_corrections["corrections"]]
            evaluation_inputs = {**resolved_corrections, "_resolved_financial_sources": manifest}
        else:
            evaluation_inputs = {**saved_inputs, "facts": facts, "_resolved_financial_sources": manifest}
            if "corrections" in saved_inputs:
                evaluation_inputs["corrections"] = _source_corrections(saved_inputs, facts)["corrections"]
        evaluation_inputs.pop("source_artifacts", None)
        evaluation_inputs.pop("source_artifact", None)
        return evaluation_inputs, saved_inputs

    if operation == "financial_mmm_analysis" and inputs.get("outcome_artifact") is not None:
        if "_resolved_financial_sources" in inputs:
            raise ApiError("CLIENT_FINANCIAL_SOURCE_OVERRIDE", "Financial source manifests are server-generated", 400)
        if "facts" in inputs:
            raise ApiError("CLIENT_FINANCIAL_FACTS_FORBIDDEN", "Financial facts must be read from exact saved source Artifact versions", 409)
        reference = _positive_reference(inputs["outcome_artifact"], "outcome_artifact")
        series, artifact_manifest = _extract_quarterly_series(
            store, connection, identity, work_id, reference, is_cancelled=is_cancelled,
        )
        saved_inputs = deepcopy(inputs)
        saved_inputs["outcome_artifact"] = reference
        raw_periods = saved_inputs.get("periods")
        if isinstance(raw_periods, list):
            if any(isinstance(row, dict) and row.get("outcome") is not None for row in raw_periods):
                raise ApiError("FINANCIAL_OUTCOME_OVERRIDE_FORBIDDEN", "MMM outcomes must come from the selected saved quarterly Artifact", 409)
            saved_inputs["periods"] = [
                {key: deepcopy(value) for key, value in row.items() if key != "outcome"}
                if isinstance(row, dict) else row
                for row in raw_periods
            ]
        evaluation_inputs = _merge_quarterly_outcomes(saved_inputs, series, artifact_manifest)
        evaluation_inputs.pop("outcome_artifact", None)
        evaluation_inputs["_resolved_financial_sources"] = {
            "resolved_source_versions": [artifact_manifest["artifact"], *artifact_manifest["resolved_source_versions"]],
            "source_materials": artifact_manifest["source_materials"],
        }
        return evaluation_inputs, saved_inputs

    # Keep the existing source-table MMM path available when outcome facts are
    # supplied as an authorized original table rather than a saved filing series.
    if operation == "financial_mmm_analysis":
        if "_resolved_financial_sources" in inputs or "facts" in inputs:
            raise ApiError("CLIENT_FINANCIAL_SOURCE_OVERRIDE", "Financial source manifests and fact arrays are server-generated", 400)
        saved_inputs = deepcopy(inputs)
        saved_inputs.pop("outcome_artifact", None)
        return deepcopy(saved_inputs), saved_inputs

    raise ValueError(f"Unsupported financial source resolution operation: {operation}")
