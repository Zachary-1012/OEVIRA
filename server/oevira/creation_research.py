"""Source-bound research work, revisions, and Artifact persistence.

Research excerpts and numeric/wording comparisons remain candidates for review.
This module never promotes a match into a verified claim or conclusion.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import uuid

from flask import g, jsonify, request

from .api import (
    ApiError,
    _artifact_data,
    _body,
    _input_snapshot,
    _job_data,
    _not_found,
    _optional_string,
    _raw_text,
    _request_fingerprint,
    _resource_path,
    _string,
    _success,
    _validate_citations,
    _work_row,
)
from .auth import utc_now
from .collaboration import require_artifact_access, require_material_access, require_work_access
from .comparison import build_research_evidence_draft


_JOB_KIND = "material_research"
_RESEARCH_SCHEMA = "oevira-research/1"
_REFERENCE_FIELDS = {"material_id", "segment_id", "quote", "source_start", "source_end"}
_PROPOSITION_FIELDS = {
    "statement", "kind", "scope", "reasoning", "assumptions",
    "supporting_refs", "counterevidence_refs",
}
_PROPOSITION_KINDS = {"source_statement", "interpretation", "hypothesis", "conclusion"}
_SCOPE_FIELDS = {"period", "geography", "industry", "population", "unit", "entity"}
_MAX_MATERIALS = 100
_MAX_PROPOSITIONS = 20
_MAX_REFERENCES_PER_PROPOSITION = 100
_MAX_RESEARCH_TEXT = 80_000
_MAX_ANALYSIS_SEGMENT_PASSES = 500_000
_MAX_VISIBLE_CANDIDATES = 300
_MAX_RESEARCH_BODY_CHARS = 200_000


def install_research_schema(store) -> None:
    """Install idempotent research revision receipts without owning versions."""
    with store.transaction() as connection:
        connection.execute(
            """CREATE TABLE IF NOT EXISTS research_revision_receipts (
                workspace_id TEXT NOT NULL REFERENCES workspaces(id),
                operation_key TEXT NOT NULL,
                artifact_id TEXT NOT NULL REFERENCES artifacts(id),
                request_hash TEXT NOT NULL,
                result_version INTEGER NOT NULL,
                result_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY(workspace_id, operation_key)
            )"""
        )


def _research_error(code: str, message: str, status: int = 400, details: dict | None = None) -> ApiError:
    return ApiError(code, message, status, details or {})


def _clean_material_ids(value) -> list[str]:
    if value is None:
        return []
    if (
        not isinstance(value, list)
        or len(value) > _MAX_MATERIALS
        or any(not isinstance(item, str) or not item.strip() or len(item) > 80 for item in value)
    ):
        raise _research_error("INVALID_INPUT", "material_ids must contain at most 100 distinct material IDs")
    result = [item.strip() for item in value]
    if len(set(result)) != len(result):
        raise _research_error("INVALID_INPUT", "material_ids must contain at most 100 distinct material IDs")
    return result


def _clean_scope(value, field: str = "scope") -> dict:
    if value is None:
        return {}
    if not isinstance(value, dict) or set(value) - _SCOPE_FIELDS:
        raise _research_error("INVALID_INPUT", f"{field} may contain period, geography, industry, population, unit and entity")
    return {
        key: _optional_string(value.get(key), f"{field}.{key}", maximum=1000)
        for key in _SCOPE_FIELDS
        if key in value and value.get(key) is not None
    }


def _text_list(value, field: str, *, maximum_items: int, maximum_chars: int) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > maximum_items:
        raise _research_error("INVALID_INPUT", f"{field} must be an array of at most {maximum_items} strings")
    return [_string(item, f"{field}[]", maximum=maximum_chars) for item in value]


def _reference_occurrence(segment_text: str, quote: str, start: int | None, end: int | None) -> tuple[int, int]:
    if (start is None) != (end is None):
        raise _research_error("INVALID_RESEARCH_REFERENCE", "source_start and source_end must be supplied together")
    if start is not None:
        if start < 0 or end <= start or end > len(segment_text) or segment_text[start:end] != quote:
            raise _research_error("INVALID_RESEARCH_REFERENCE", "The selected quote does not match its exact source position")
        return start, end

    offsets = []
    offset = 0
    while True:
        found = segment_text.find(quote, offset)
        if found < 0:
            break
        offsets.append((found, found + len(quote)))
        offset = found + 1
    if len(offsets) != 1:
        reason = "The quote does not match this source segment" if not offsets else "The quote appears more than once; provide source_start and source_end"
        raise _research_error("INVALID_RESEARCH_REFERENCE", reason)
    return offsets[0]


def _normalise_reference(
    connection,
    identity: dict,
    work_id: str,
    raw,
    *,
    selected_material_ids: set[str] | None = None,
) -> dict:
    if not isinstance(raw, dict) or set(raw) - _REFERENCE_FIELDS or not {"material_id", "segment_id", "quote"} <= set(raw):
        raise _research_error("INVALID_RESEARCH_REFERENCE", "Each research reference needs material_id, segment_id and quote")
    material_id = _string(raw["material_id"], "material_id", maximum=80)
    segment_id = _string(raw["segment_id"], "segment_id", maximum=200)
    quote = _raw_text(raw["quote"], "quote", maximum=10_000)
    if selected_material_ids is not None and material_id not in selected_material_ids:
        raise _research_error("INVALID_RESEARCH_REFERENCE", "Every proposition source must be selected for this research operation")
    supplied_start = raw.get("source_start")
    supplied_end = raw.get("source_end")
    if supplied_start is not None and (isinstance(supplied_start, bool) or not isinstance(supplied_start, int)):
        raise _research_error("INVALID_RESEARCH_REFERENCE", "source_start must be an integer")
    if supplied_end is not None and (isinstance(supplied_end, bool) or not isinstance(supplied_end, int)):
        raise _research_error("INVALID_RESEARCH_REFERENCE", "source_end must be an integer")

    material = require_material_access(connection, identity, material_id, target_work_id=work_id)
    segment = connection.execute(
        "SELECT id, label, text, start_offset, end_offset, kind FROM material_segments "
        "WHERE workspace_id = ? AND material_id = ? AND id = ?",
        (identity["workspace"]["id"], material_id, segment_id),
    ).fetchone()
    material_text = material["text"] or ""
    if segment is None or segment["text"] != material_text[segment["start_offset"]:segment["end_offset"]]:
        raise _research_error("INVALID_RESEARCH_REFERENCE", "The selected source position is unavailable or inconsistent")
    if supplied_start is not None:
        if supplied_start < segment["start_offset"] or supplied_end > segment["end_offset"]:
            raise _research_error("INVALID_RESEARCH_REFERENCE", "The source range must remain inside its selected segment")
        supplied_start -= segment["start_offset"]
        supplied_end -= segment["start_offset"]
    relative_start, relative_end = _reference_occurrence(segment["text"], quote, supplied_start, supplied_end)
    absolute_start = segment["start_offset"] + relative_start
    absolute_end = segment["start_offset"] + relative_end
    return {
        "material_id": material_id,
        "segment_id": segment_id,
        "label": segment["label"],
        "kind": segment["kind"],
        "quote": quote,
        "source_start": absolute_start,
        "source_end": absolute_end,
        "source_sha256": material["sha256"],
        "parsed_text_sha256": hashlib.sha256(material_text.encode("utf-8")).hexdigest(),
        "relation_state": "author_labeled_unverified",
        "source_state": "pending_integrity_check",
    }


def _normalise_reference_list(
    connection,
    identity: dict,
    work_id: str,
    value,
    field: str,
    *,
    selected_material_ids: set[str] | None = None,
) -> list[dict]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > _MAX_REFERENCES_PER_PROPOSITION:
        raise _research_error("INVALID_INPUT", f"{field} must contain at most 100 exact source references")
    references = [
        _normalise_reference(
            connection, identity, work_id, raw,
            selected_material_ids=selected_material_ids,
        )
        for raw in value
    ]
    keys = [(item["material_id"], item["segment_id"], item["source_start"], item["source_end"]) for item in references]
    if len(keys) != len(set(keys)):
        raise _research_error("INVALID_INPUT", f"{field} must not repeat the same source range")
    return references


def _normalise_proposition(
    connection,
    identity: dict,
    work_id: str,
    raw,
    *,
    proposition_id: str | None = None,
    selected_material_ids: set[str] | None = None,
    partial: bool = False,
    origin: str = "user_supplied",
) -> dict:
    if not isinstance(raw, dict):
        raise _research_error("INVALID_INPUT", "Each proposition must be an object")
    allowed = _PROPOSITION_FIELDS | ({"proposition_id"} if partial else set())
    if set(raw) - allowed:
        raise _research_error("INVALID_INPUT", "A proposition contains an unsupported field")
    if partial and (not isinstance(raw.get("proposition_id"), str) or not raw["proposition_id"].strip()):
        raise _research_error("INVALID_INPUT", "Each update needs a proposition_id")
    if not partial and "statement" not in raw:
        raise _research_error("INVALID_INPUT", "Each proposition needs a statement")

    result = {"proposition_id": proposition_id or raw.get("proposition_id")}
    if "statement" in raw:
        result["statement"] = _raw_text(raw["statement"], "proposition.statement", maximum=2000)
    if "kind" in raw:
        kind = raw["kind"]
        if kind not in _PROPOSITION_KINDS:
            raise _research_error("INVALID_INPUT", "proposition.kind must be source_statement, interpretation, hypothesis or conclusion")
        result["kind"] = kind
    elif not partial:
        result["kind"] = "hypothesis"
    if "scope" in raw:
        result["scope"] = _clean_scope(raw["scope"], "proposition.scope")
    elif not partial:
        result["scope"] = {}
    if "reasoning" in raw:
        result["reasoning"] = _raw_text(raw["reasoning"], "proposition.reasoning", maximum=20_000, allow_empty=True)
    elif not partial:
        result["reasoning"] = ""
    if "assumptions" in raw:
        result["assumptions"] = _text_list(raw["assumptions"], "proposition.assumptions", maximum_items=50, maximum_chars=2000)
    elif not partial:
        result["assumptions"] = []
    for field in ("supporting_refs", "counterevidence_refs"):
        if field in raw:
            result[field] = _normalise_reference_list(
                connection, identity, work_id, raw[field], f"proposition.{field}",
                selected_material_ids=selected_material_ids,
            )
        elif not partial:
            result[field] = []
    if not partial:
        if result["kind"] == "source_statement" and not result["supporting_refs"]:
            raise _research_error("SOURCE_REFERENCE_REQUIRED", "A source statement needs at least one exact supporting reference")
        if origin not in {"user_supplied", "assistant_candidate"}:
            raise _research_error("INVALID_INPUT", "Unsupported proposition origin")
        result.update({"assessment_state": "unreviewed", "origin": origin})
    return result


def _normalise_research_request(
    connection, identity: dict, work_id: str, data: dict, *, proposition_origin: str = "user_supplied",
) -> dict:
    allowed = {"question", "material_ids", "purpose", "scope", "propositions"}
    if set(data) - allowed:
        raise _research_error("INVALID_INPUT", "Research request contains an unsupported field")
    question = _raw_text(data.get("question"), "question", maximum=2000)
    material_ids = _clean_material_ids(data.get("material_ids", []))
    purpose = _optional_string(data.get("purpose"), "purpose", maximum=2000)
    scope = _clean_scope(data.get("scope"))
    raw_propositions = data.get("propositions", [])
    if not isinstance(raw_propositions, list) or len(raw_propositions) > _MAX_PROPOSITIONS:
        raise _research_error("INVALID_INPUT", "propositions must contain at most 20 items")
    selected = set(material_ids)
    propositions = [
        _normalise_proposition(
            connection, identity, work_id, raw,
            proposition_id=str(uuid.uuid4()),
            selected_material_ids=selected,
            origin=proposition_origin,
        )
        for raw in raw_propositions
    ]
    total_text = len(question) + len(purpose or "") + sum(
        len(item["statement"]) + len(item["reasoning"]) + sum(map(len, item["assumptions"]))
        for item in propositions
    )
    if total_text > _MAX_RESEARCH_TEXT:
        raise _research_error("RESEARCH_INPUT_TOO_LARGE", "Research statements exceed the current 80000-character limit", 413)
    return {
        "question": question,
        "material_ids": material_ids,
        "purpose": purpose,
        "scope": scope,
        "propositions": propositions,
    }


def _segment_digest(segment_rows) -> str:
    entries = [
        {
            "id": row["id"], "label": row["label"], "text": row["text"],
            "start_offset": row["start_offset"], "end_offset": row["end_offset"], "kind": row["kind"],
        }
        for row in segment_rows
    ]
    entries.sort(key=lambda item: (item["start_offset"], item["id"]))
    canonical = json.dumps(entries, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _stored_segment_digest(connection, workspace_id: str, material_id: str) -> str:
    rows = connection.execute(
        "SELECT id, label, text, start_offset, end_offset, kind FROM material_segments "
        "WHERE material_id = ? AND workspace_id = ? ORDER BY start_offset, id",
        (material_id, workspace_id),
    ).fetchall()
    return _segment_digest(rows)


def _parsed_segment_digest(segments: list[dict]) -> str:
    return _segment_digest([
        {
            "id": item["id"], "label": item["label"], "text": item["text"],
            "start_offset": item["start"], "end_offset": item["end"], "kind": item["kind"],
        }
        for item in segments
    ])


def _create_research_job(
    store, identity: dict, work_id: str, data: dict, idempotency_key: str, *,
    proposition_origin: str = "user_supplied",
) -> tuple[dict, bool]:
    """Create or replay one source-bound Job without depending on HTTP state."""
    if not isinstance(data, dict):
        raise _research_error("INVALID_INPUT", "Research request must be an object")
    if proposition_origin not in {"user_supplied", "assistant_candidate"}:
        raise _research_error("INVALID_INPUT", "Unsupported proposition origin")
    workspace_id = identity["workspace"]["id"]
    key = idempotency_key.strip() if isinstance(idempotency_key, str) else ""
    if not 1 <= len(key) <= 200:
        raise _research_error("IDEMPOTENCY_KEY_REQUIRED", "Idempotency-Key is required for research")

    job_id = str(uuid.uuid4())
    now = utc_now()
    with store.transaction() as connection:
        work = require_work_access(connection, identity, work_id, write=True)
        if work["status"] != "active":
            raise _research_error("WORK_ARCHIVED", "Reopen this work before starting research", 409)
        normalized = _normalise_research_request(
            connection, identity, work_id, data, proposition_origin=proposition_origin,
        )
        fingerprint_value = {
            "work_id": work_id,
            "question": normalized["question"],
            "material_ids": normalized["material_ids"],
            "purpose": normalized["purpose"],
            "scope": normalized["scope"],
            "proposition_origin": proposition_origin,
            "propositions": [
                {key: value for key, value in item.items() if key not in {"proposition_id", "assessment_state", "origin"}}
                for item in normalized["propositions"]
            ],
        }
        fingerprint = _request_fingerprint(fingerprint_value)
        existing = connection.execute(
            "SELECT * FROM jobs WHERE workspace_id = ? AND kind = ? AND idempotency_key = ?",
            (workspace_id, _JOB_KIND, key),
        ).fetchone()
        if existing:
            if existing["request_hash"] != fingerprint:
                raise _research_error("IDEMPOTENCY_CONFLICT", "This key was already used for a different research request", 409)
            return _job_data(existing), False

        by_id = {}
        for material_id in normalized["material_ids"]:
            material = require_material_access(connection, identity, material_id, target_work_id=work_id)
            by_id[material_id] = material
        if len(by_id) != len(normalized["material_ids"]):
            raise _not_found()
        material_snapshot = []
        for material_id in normalized["material_ids"]:
            material = by_id[material_id]
            material_snapshot.append({
                "id": material_id,
                "sha256": material["sha256"],
                "parsed_text_sha256": hashlib.sha256((material["text"] or "").encode("utf-8")).hexdigest(),
                "parsed_segments_sha256": _stored_segment_digest(connection, workspace_id, material_id),
                "title": material["title"], "filename": material["filename"],
                "format": material["format"], "status": material["status"], "reason": material["reason"],
            })
        connection.executemany(
            "INSERT OR IGNORE INTO work_materials(work_id, material_id, workspace_id, created_at) VALUES(?, ?, ?, ?)",
            [(work_id, material_id, workspace_id, now) for material_id in normalized["material_ids"]],
        )
        snapshot = {
            "kind": "research",
            "work": {
                "id": work["id"], "title": work["title"], "version": work["version"],
                "goal": work["goal"], "brief": json.loads(work["brief_json"]),
            },
            "materials": material_snapshot,
            "research_request": normalized,
        }
        payload = {
            "work_id": work_id,
            "material_ids": normalized["material_ids"],
            "input_snapshot": snapshot,
            "research_request": normalized,
        }
        connection.execute(
            "INSERT INTO jobs(id, workspace_id, user_id, work_id, kind, status, payload_json, request_hash, idempotency_key, progress_total, progress_label, created_at, updated_at) "
            "VALUES(?, ?, ?, ?, ?, 'queued', ?, ?, ?, ?, 'Waiting for source-bound research', ?, ?)",
            (
                job_id, workspace_id, identity["user"]["id"], work_id, _JOB_KIND,
                json.dumps(payload, ensure_ascii=False), fingerprint, key,
                max(1, len(normalized["material_ids"]) + len(normalized["propositions"]) + 1), now, now,
            ),
        )
        job = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return _job_data(job), True


def create_research_job(
    store, identity: dict, work_id: str, data: dict, idempotency_key: str, *,
    proposition_origin: str = "user_supplied",
) -> dict:
    """Natural-language/owner-tool seam; returns the existing durable Job object."""
    job, _created = _create_research_job(
        store, identity, work_id, data, idempotency_key,
        proposition_origin=proposition_origin,
    )
    return job


def enqueue_research(store, identity: dict, work_id: str, data: dict, idempotency_key: str | None = None):
    """HTTP adapter for the domain Job operation."""
    key = idempotency_key
    if key is None:
        key = request.headers.get("Idempotency-Key", "")
    job, created = _create_research_job(store, identity, work_id, data, key)
    return jsonify({"data": job}), 202 if created else 200


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _source_record_from_input(snapshot: dict, material: dict, parsed: dict, integrity: str, reason: str | None) -> dict:
    return {
        "material_id": material["id"],
        "title": material["title"],
        "filename": material["filename"],
        "format": material["format"],
        "sha256": material["sha256"],
        "parsed_text_sha256": snapshot.get("parsed_text_sha256"),
        "parsed_segments_sha256": snapshot.get("parsed_segments_sha256"),
        "status": parsed["status"],
        "reason": reason or parsed.get("reason"),
        "integrity": integrity,
        "matched_segment_count": 0,
    }


def _load_material_inputs(store, identity: dict, work_id: str, source_snapshots: list[dict]) -> tuple[list[dict], list[dict], dict[str, str]]:
    material_inputs = []
    source_records = []
    verified_originals = {}
    workspace_id = identity["workspace"]["id"]
    with store.transaction(immediate=False) as connection:
        require_work_access(connection, identity, work_id, write=True)
        source_rows = {}
        for expected in source_snapshots:
            row = require_material_access(connection, identity, expected["id"], target_work_id=work_id)
            source_rows[expected["id"]] = row

    for expected in source_snapshots:
        material = source_rows[expected["id"]]
        reason = None
        integrity = "verified"
        parsed_text = material["text"] or ""
        segments = store.rows(
            "SELECT id, label, text, start_offset, end_offset, kind FROM material_segments "
            "WHERE material_id = ? AND workspace_id = ? ORDER BY start_offset, id",
            (expected["id"], workspace_id),
        )
        if material["sha256"] != expected["sha256"]:
            reason = "原始资料的SHA-256与研究任务快照不同。"
        elif hashlib.sha256(parsed_text.encode("utf-8")).hexdigest() != expected["parsed_text_sha256"]:
            reason = "已保存解析文本与研究任务快照不同。"
        elif expected.get("parsed_segments_sha256") and _segment_digest(segments) != expected["parsed_segments_sha256"]:
            reason = "已保存解析分段与研究任务快照不同。"
        else:
            try:
                original_path = _resource_path(store, material["relative_path"])
                if not original_path.is_file() or _sha256_file(original_path) != expected["sha256"]:
                    reason = "原始文件缺失或未通过SHA-256完整性核对；已保存解析文本不作替代。"
            except (ApiError, OSError):
                reason = "原始文件路径不可核验；已保存解析文本不作替代。"
        if reason:
            integrity = "unavailable"
            parsed = {"status": "unavailable", "reason": reason, "format": material["format"], "text": "", "segments": []}
        else:
            verified_originals[expected["id"]] = expected["sha256"]
            parsed = {
                "status": material["status"], "reason": material["reason"], "format": material["format"],
                "text": parsed_text,
                "segments": [
                    {"id": part["id"], "label": part["label"], "text": part["text"],
                     "start": part["start_offset"], "end": part["end_offset"], "kind": part["kind"]}
                    for part in segments
                ],
            }
            if material["status"] not in {"ready", "partial"} or not parsed_text.strip():
                integrity = "unavailable"
                reason = material["reason"] or "资料没有可读取的解析文本。"
                parsed = {**parsed, "status": "unavailable", "reason": reason, "text": "", "segments": []}
                verified_originals.pop(expected["id"], None)
        material_inputs.append({"material_id": expected["id"], "parsed": parsed})
        source_records.append(_source_record_from_input(expected, material, parsed, integrity, reason))
    return material_inputs, source_records, verified_originals


def _mark_reference_integrity(propositions: list[dict], source_records: list[dict]) -> list[dict]:
    states = {item["material_id"]: item["integrity"] for item in source_records}
    result = []
    for proposition in propositions:
        copied = dict(proposition)
        for field in ("supporting_refs", "counterevidence_refs"):
            copied[field] = [
                {**reference, "source_state": states.get(reference["material_id"], "unavailable")}
                for reference in proposition.get(field, [])
            ]
        result.append(copied)
    return result


def _candidate_limits(draft: dict, budget: int) -> tuple[list[dict], int]:
    evidence = draft.get("evidence", [])
    selected = evidence[:max(0, budget)]
    return selected, max(0, len(evidence) - len(selected))


def _build_research(materials: list[dict], request_data: dict, source_records: list[dict]) -> dict:
    question_draft = build_research_evidence_draft(materials, request_data["question"])
    budget = _MAX_VISIBLE_CANDIDATES
    question_evidence, omitted = _candidate_limits(question_draft, budget)
    budget -= len(question_evidence)
    source_counts = {item["material_id"]: item["matched_segment_count"] for item in question_draft["sources"]}
    sources = []
    for record in source_records:
        sources.append({**record, "matched_segment_count": source_counts.get(record["material_id"], 0)})

    propositions = []
    limitations = list(question_draft["limitations"])
    for proposition in request_data["propositions"]:
        draft = build_research_evidence_draft(materials, proposition["statement"])
        candidates, skipped = _candidate_limits(draft, min(8, budget))
        budget -= len(candidates)
        if skipped:
            limitations.append(f"命题“{proposition['statement'][:80]}”另有 {skipped} 个词面片段未展开。")
        if not budget and draft.get("evidence"):
            limitations.append("本次研究达到词面候选展开上限；未展开项不表示不存在相关证据。")
        source_counts_for_prop = {item["material_id"]: item["matched_segment_count"] for item in draft["sources"]}
        propositions.append({
            **proposition,
            "assessment_state": "unreviewed",
            "literal_evidence_candidates": candidates,
            "candidate_numeric_differences": draft.get("numeric_difference_candidates", [])[:20],
            "candidate_opposing_language": draft.get("conflict_candidates", [])[:20],
            "source_match_counts": source_counts_for_prop,
            "omitted_candidate_count": skipped,
        })
        limitations.extend(draft.get("limitations", []))

    if any(item["integrity"] != "verified" for item in sources):
        limitations.extend(
            f"{item['filename']}：{item['reason'] or '原件完整性尚未核验。'}"
            for item in sources if item["integrity"] != "verified"
        )
    if omitted:
        limitations.append(f"问题本身另有 {omitted} 个词面候选未展开；调整问题或资料范围后可继续检查。")
    limitations.append("词面命中、数字差异及相反用词均是复核线索，不构成语义支持、事实核实或已确认反证。")
    limitations.append("命题关系、推理与结论尚未独立评估；AI增强如需使用，应沿当前智能工具和同一研究版本。")
    research = {
        "schema": _RESEARCH_SCHEMA,
        "question": request_data["question"],
        "purpose": request_data.get("purpose"),
        "scope": request_data.get("scope", {}),
        "source_records": sources,
        "question_evidence_candidates": question_evidence,
        "question_numeric_difference_candidates": question_draft.get("numeric_difference_candidates", [])[:50],
        "question_opposing_language_candidates": question_draft.get("conflict_candidates", [])[:50],
        "propositions": propositions,
        "limitations": list(dict.fromkeys(limitations)),
        "completion_state": "evidence_review_required",
        "analysis_complete": False,
        "judgment_state": "not_independently_assessed",
    }
    return research


def build_research_analysis(
    materials: list[dict],
    request_data: dict,
    source_records: list[dict],
    *,
    allowed_material_ids,
    proposition_origin: str = "assistant_candidate",
) -> dict:
    """Pure research-builder seam for trusted Intelligence tools.

    The caller supplies only parsed materials it has already authorized for the
    current work/tool binding. This function independently rejects any selected
    source or proposition citation outside that explicit allowlist.
    """
    if not isinstance(request_data, dict) or not isinstance(materials, list) or not isinstance(source_records, list):
        raise _research_error("INVALID_INPUT", "Research analysis inputs must be structured values")
    if not isinstance(allowed_material_ids, (set, frozenset, list, tuple)):
        raise _research_error("SOURCE_SCOPE_REQUIRED", "allowed_material_ids must be an explicit source-ID collection")
    if any(not isinstance(item, str) or not item for item in allowed_material_ids):
        raise _research_error("SOURCE_SCOPE_REQUIRED", "allowed_material_ids must contain only material IDs")
    allowed = set(allowed_material_ids)
    selected_ids = request_data.get("material_ids", [])
    if (
        not isinstance(selected_ids, list)
        or any(not isinstance(item, str) or not item for item in selected_ids)
        or len(selected_ids) != len(set(selected_ids))
    ):
        raise _research_error("INVALID_INPUT", "Research material_ids must be a distinct list")
    if not set(selected_ids) <= allowed:
        raise _research_error("SOURCE_SCOPE_MISMATCH", "The research request includes a source outside the current allowed material scope", 403)
    input_by_id = {}
    for item in materials:
        if not isinstance(item, dict) or not isinstance(item.get("material_id"), str) or item["material_id"] in input_by_id:
            raise _research_error("INVALID_INPUT", "Research material inputs need unique material IDs")
        if item["material_id"] not in allowed or item["material_id"] not in selected_ids:
            raise _research_error("SOURCE_SCOPE_MISMATCH", "Parsed source input falls outside the selected allowed materials", 403)
        input_by_id[item["material_id"]] = item
    if set(input_by_id) != set(selected_ids):
        raise _research_error("SOURCE_SCOPE_MISMATCH", "Every selected material must have an explicit parsed-source state", 403)
    source_by_id = {}
    for source in source_records:
        if not isinstance(source, dict) or not isinstance(source.get("material_id"), str):
            raise _research_error("INVALID_INPUT", "Research source records need a material_id")
        material_id = source["material_id"]
        if material_id not in allowed or material_id not in selected_ids:
            raise _research_error("SOURCE_SCOPE_MISMATCH", "Research source metadata falls outside the selected allowed materials", 403)
        if material_id in source_by_id:
            raise _research_error("INVALID_INPUT", "Research source records must have unique material IDs")
        source_by_id[material_id] = source
    if set(source_by_id) != set(selected_ids):
        raise _research_error("SOURCE_SCOPE_MISMATCH", "Each selected material needs a source record", 403)

    propositions = request_data.get("propositions", [])
    if not isinstance(propositions, list):
        raise _research_error("INVALID_INPUT", "Research propositions must be an array")
    for proposition in propositions:
        if not isinstance(proposition, dict):
            raise _research_error("INVALID_INPUT", "Each proposition must be an object")
        for field in ("supporting_refs", "counterevidence_refs"):
            for reference in proposition.get(field, []):
                material_id = reference.get("material_id") if isinstance(reference, dict) else None
                item = input_by_id.get(material_id)
                source = source_by_id.get(material_id)
                if item is None or source is None or material_id not in allowed:
                    raise _research_error("SOURCE_SCOPE_MISMATCH", "A proposition cites a source outside the current allowed material scope", 403)
                parsed = item.get("parsed")
                if not isinstance(parsed, dict) or source.get("integrity") != "verified":
                    raise _research_error("INVALID_RESEARCH_REFERENCE", "A proposition cannot cite an unavailable or unverified source", 409)
                source_text = parsed.get("text")
                start, end = reference.get("source_start"), reference.get("source_end")
                segment = next((row for row in parsed.get("segments", []) if row.get("id") == reference.get("segment_id")), None)
                if (
                    not isinstance(source_text, str) or segment is None
                    or type(start) is not int or type(end) is not int
                    or start < segment.get("start", -1) or end > segment.get("end", -1)
                    or source_text[start:end] != reference.get("quote")
                ):
                    raise _research_error("INVALID_RESEARCH_REFERENCE", "A proposition citation does not match its exact parsed source range")
                expected_hash = source.get("sha256")
                if reference.get("source_sha256") != expected_hash:
                    raise _research_error("SOURCE_VERSION_CHANGED", "A proposition citation does not match the selected source version", 409)
                if hashlib.sha256(source_text.encode("utf-8")).hexdigest() != source.get("parsed_text_sha256"):
                    raise _research_error("SOURCE_VERSION_CHANGED", "A proposition citation does not match the selected parsed text version", 409)
                if source.get("parsed_segments_sha256") and _parsed_segment_digest(parsed.get("segments", [])) != source["parsed_segments_sha256"]:
                    raise _research_error("SOURCE_VERSION_CHANGED", "A proposition citation does not match the selected parsed segment version", 409)
    if proposition_origin not in {"user_supplied", "assistant_candidate"}:
        raise _research_error("INVALID_INPUT", "Unsupported proposition origin")
    normalized_request = {
        **request_data,
        "propositions": [
            {**item, "origin": proposition_origin, "assessment_state": "unreviewed"}
            for item in propositions
        ],
    }
    return _build_research(materials, normalized_request, source_records)


def _source_lookup(research: dict) -> dict[str, dict]:
    return {item["material_id"]: item for item in research.get("source_records", [])}


def _citation_rows(research: dict) -> list[dict]:
    rows = []
    seen = set()

    def add(material_id, segment_id, quote, *, source_state="verified"):
        if source_state != "verified" or not all(isinstance(value, str) and value for value in (material_id, segment_id, quote)):
            return
        key = (material_id, segment_id, quote)
        if key not in seen:
            seen.add(key)
            rows.append({"material_id": material_id, "segment_id": segment_id, "quote": quote})

    for item in research.get("question_evidence_candidates", []):
        source = _source_lookup(research).get(item["material_id"], {})
        add(item["material_id"], item["segment_id"], item["quote"], source_state=source.get("integrity"))
    for proposition in research.get("propositions", []):
        for field in ("supporting_refs", "counterevidence_refs"):
            for item in proposition.get(field, []):
                source = _source_lookup(research).get(item["material_id"], {})
                add(item["material_id"], item["segment_id"], item["quote"], source_state=source.get("integrity"))
        for item in proposition.get("literal_evidence_candidates", []):
            source = _source_lookup(research).get(item["material_id"], {})
            add(item["material_id"], item["segment_id"], item["quote"], source_state=source.get("integrity"))
    for field in ("question_numeric_difference_candidates", "question_opposing_language_candidates"):
        for candidate in research.get(field, []):
            for item in candidate.get("evidence", []):
                source = _source_lookup(research).get(item["material_id"], {})
                add(item["material_id"], item["segment_id"], item["quote"], source_state=source.get("integrity"))
    return rows


def _safe_markdown(value) -> str:
    return str(value or "").replace("|", "\\|").replace("\r", " ").replace("\n", " ").replace("`", "\\`")


def _reference_label(reference: dict, sources: dict[str, dict]) -> str:
    source = sources.get(reference.get("material_id"), {})
    file_name = _safe_markdown(source.get("filename") or "资料")
    label = _safe_markdown(reference.get("label") or "解析位置未提供")
    start = reference.get("source_start")
    end = reference.get("source_end")
    position = f"，解析文本字符 {start}–{end}" if isinstance(start, int) and isinstance(end, int) else ""
    return f"{file_name} · {label}{position}"


def _render_research_body(research: dict) -> str:
    sources = _source_lookup(research)
    lines = [
        "# 研究草稿",
        "",
        "## 研究问题",
        research["question"],
        "",
        "## 用途与范围",
        f"用途：{research.get('purpose') or '未指定'}",
    ]
    scope = research.get("scope") or {}
    lines.extend([f"- {key}：{value}" for key, value in scope.items()] or ["- 时间、地域、行业、总体及单位均未补充；未从文件时间或列表顺序推定。"])
    lines.extend(["", "## 来源覆盖"])
    if research.get("source_records"):
        for source in research["source_records"]:
            state = source.get("integrity")
            match_count = source.get("matched_segment_count", 0)
            reason = _safe_markdown(source.get("reason"))
            lines.append(
                f"- **{_safe_markdown(source.get('filename'))}**：解析状态 `{source.get('status')}`；" +
                f"原件完整性 `{state}`；问题词面候选 {match_count} 个。" + (f" {reason}" if reason else "")
            )
    else:
        lines.append("- 当前未选入资料；已保存问题，不代表已完成检索。")

    lines.extend(["", "## 待核原文候选"])
    candidates = research.get("question_evidence_candidates", [])
    if candidates:
        for item in candidates[:25]:
            source = sources.get(item["material_id"], {})
            state = source.get("integrity")
            if state == "verified":
                location = _reference_label(item, sources)
                lines.append(f"- “{_safe_markdown(item['quote'])}” — {location}；匹配 `{item['match_type']}`，仅为字面候选。")
            else:
                lines.append(f"- {_safe_markdown(source.get('filename'))} 的候选原文无法通过原件完整性核对；不作为引用。")
        if len(candidates) > 25:
            lines.append(f"- 另有 {len(candidates) - 25} 个候选保存在研究数据中，未在正文展开。")
    else:
        lines.append("- 当前没有词面匹配；这不证明资料中不存在相关内容。")

    lines.extend(["", "## 命题、论证与反证"])
    propositions = research.get("propositions", [])
    if not propositions:
        lines.append("- 尚无明确命题。可通过研究修订加入来源主张、解释、假设或结论，并绑定支持和反证。")
    for index, proposition in enumerate(propositions, start=1):
        provenance = " · 智能工具候选，未独立核实" if proposition.get("origin") == "assistant_candidate" else ""
        if proposition.get("last_revision_origin") == "assistant_candidate":
            provenance += " · 智能工具曾提出修订候选"
        lines.extend(["", f"### 命题 {index} · {proposition['kind']}{provenance}", _safe_markdown(proposition["statement"])])
        if proposition.get("reasoning"):
            lines.append(f"- 推理：{_safe_markdown(proposition['reasoning'])}")
        if proposition.get("scope"):
            lines.extend(f"- 命题范围 · {key}：{_safe_markdown(value)}" for key, value in proposition["scope"].items())
        for field, label in (("supporting_refs", "作者标记为支持的来源"), ("counterevidence_refs", "作者标记为反证的来源")):
            references = proposition.get(field, [])
            if not references:
                continue
            relation_state = "requires_reassessment" if any(item.get("relation_state") == "requires_reassessment" for item in references) else "author_labeled_unverified"
            relation_label = "命题修改后关系需重新核对" if relation_state == "requires_reassessment" else "关系尚未独立核实"
            lines.append(f"- {label}（{relation_label}）：")
            for reference in references[:10]:
                if reference.get("source_state") == "verified":
                    lines.append(f"  - “{_safe_markdown(reference['quote'])}” — {_reference_label(reference, sources)}")
                else:
                    lines.append(f"  - 原件不可用；来源定位保留为待核记录。")
        if proposition.get("assumptions"):
            lines.extend(["- 假设：", *[f"  - {_safe_markdown(item)}" for item in proposition["assumptions"]]])
        literal = proposition.get("literal_evidence_candidates", [])
        if literal:
            lines.append("- 与命题词面重叠的待核候选（不代表支持或反对）：")
            for item in literal[:3]:
                source = sources.get(item["material_id"], {})
                if source.get("integrity") == "verified":
                    lines.append(f"  - “{_safe_markdown(item['quote'])}” — {_reference_label(item, sources)}")
                else:
                    lines.append("  - 候选资料原件完整性未通过，不作为引用。")

    lines.extend(["", "## 数值与相反用词候选"])
    numeric = research.get("question_numeric_difference_candidates", [])
    conflicts = research.get("question_opposing_language_candidates", [])
    if not numeric and not conflicts:
        lines.append("- 没有发现可列出的数值或相反用词候选；未检出不代表不存在差异。")
    for candidate in numeric[:20] + conflicts[:20]:
        kind = candidate.get("type", "candidate")
        lines.append(f"- `{kind}`：{_safe_markdown(candidate.get('note'))}")
        for evidence in candidate.get("evidence", [])[:4]:
            source = sources.get(evidence.get("material_id"), {})
            if source.get("integrity") == "verified":
                lines.append(f"  - “{_safe_markdown(evidence.get('quote'))}” — {_reference_label(evidence, sources)}")
    lines.extend(["", "## 结论状态", "尚未独立评估命题关系、方法适用性或结论范围；当前结果是可修订的证据工作底稿。", "", "## 局限与待办"])
    lines.extend(f"- {_safe_markdown(item)}" for item in research.get("limitations", []))
    body = "\n".join(lines)
    if len(body) > _MAX_RESEARCH_BODY_CHARS:
        raise _research_error("RESEARCH_RESULT_TOO_LARGE", "The research body exceeds the current 200000-character result limit", 413)
    return body


def _source_title(question: str) -> str:
    title = f"研究：{question.strip()}"
    return title if len(title) <= 180 else title[:177] + "…"


def _fail_job(store, job_id: str, generation: int, code: str, message: str) -> None:
    from .worker import _set_job_error

    _set_job_error(store, job_id, generation, code, message)


def _advance_job(store, job_id: str, generation: int, completed: int, total: int, label: str) -> bool:
    from .worker import _advance

    return _advance(store, job_id, generation, completed, total, label)


def _process_research_job(app, claim: dict) -> None:
    store = app.extensions["oevira.store"]
    job_id = claim["id"]
    generation = claim["generation"]
    job = store.row("SELECT * FROM jobs WHERE id = ? AND workspace_id = ?", (job_id, claim["workspace_id"]))
    if job is None or job["status"] != "running" or job["lease_generation"] != generation:
        return
    payload = json.loads(job["payload_json"])
    snapshot = payload["input_snapshot"]
    request_data = payload["research_request"]
    work_id = claim["work_id"]
    identity = {"workspace": {"id": claim["workspace_id"]}, "user": {"id": claim["user_id"]}}
    material_ids = payload.get("material_ids", [])

    try:
        with store.transaction(immediate=False) as connection:
            validate_research_resume(connection, identity, job, {})
    except ApiError as exc:
        _fail_job(store, job_id, generation, exc.code, exc.message)
        return

    if not _advance_job(store, job_id, generation, 0, max(1, len(material_ids) + len(request_data["propositions"]) + 1), "Checking selected source originals"):
        return
    material_snapshots = snapshot.get("materials", [])
    try:
        material_inputs, source_records, verified_originals = _load_material_inputs(store, identity, work_id, material_snapshots)
    except ApiError:
        _fail_job(store, job_id, generation, "ACCESS_REVOKED", "Research source access changed before reading.")
        return
    segment_count = sum(len(item["parsed"].get("segments", [])) for item in material_inputs)
    if segment_count * (1 + len(request_data["propositions"])) > _MAX_ANALYSIS_SEGMENT_PASSES:
        _fail_job(store, job_id, generation, "RESEARCH_SCOPE_TOO_LARGE", "The selected sources and propositions exceed the current evidence review budget; narrow the research scope and retry.")
        return
    total_text = sum(len(item["parsed"].get("text", "")) for item in material_inputs)
    if total_text * (1 + len(request_data["propositions"])) > 200_000_000:
        _fail_job(store, job_id, generation, "RESEARCH_SCOPE_TOO_LARGE", "The selected source text across propositions exceeds the current research processing budget.")
        return

    if not _advance_job(store, job_id, generation, len(material_ids), max(1, len(material_ids) + len(request_data["propositions"]) + 1), "Reviewing source evidence candidates"):
        return
    request_for_build = {**request_data, "propositions": _mark_reference_integrity(request_data["propositions"], source_records)}
    try:
        research = _build_research(material_inputs, request_for_build, source_records)
        body = _render_research_body(research)
        citations = _validate_research_citations(store, claim["workspace_id"], _citation_rows(research))
        title = _source_title(request_data["question"])
    except ApiError as exc:
        _fail_job(store, job_id, generation, exc.code, exc.message)
        return
    except Exception:
        _fail_job(store, job_id, generation, "RESEARCH_FAILED", "The source-bound research draft could not be completed; inputs and source records remain attached to the job.")
        return

    try:
        with store.transaction() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE id = ? AND workspace_id = ?", (job_id, claim["workspace_id"])).fetchone()
            if row is None or row["lease_generation"] != generation or row["status"] != "running":
                return
            if row["cancel_requested"]:
                connection.execute("UPDATE jobs SET status='cancelled',updated_at=? WHERE id=? AND lease_generation=?", (utc_now(), job_id, generation))
                return
            try:
                validate_research_resume(connection, identity, row, {})
            except ApiError as exc:
                connection.execute(
                    "UPDATE jobs SET status='failed',error_code=?,error_message=?,updated_at=? WHERE id=? AND lease_generation=?",
                    (exc.code, exc.message, utc_now(), job_id, generation),
                )
                return
            for material_id, expected_hash in verified_originals.items():
                source = connection.execute(
                    "SELECT relative_path, sha256, text FROM materials WHERE id = ? AND workspace_id = ?",
                    (material_id, claim["workspace_id"]),
                ).fetchone()
                try:
                    path = _resource_path(store, source["relative_path"]) if source else None
                    still_valid = (
                        source is not None and path is not None and path.is_file()
                        and source["sha256"] == expected_hash
                        and hashlib.sha256((source["text"] or "").encode("utf-8")).hexdigest()
                        == next(item["parsed_text_sha256"] for item in material_snapshots if item["id"] == material_id)
                        and _stored_segment_digest(connection, claim["workspace_id"], material_id)
                        == next(item["parsed_segments_sha256"] for item in material_snapshots if item["id"] == material_id)
                        and _sha256_file(path) == expected_hash
                    )
                except (ApiError, OSError, StopIteration):
                    still_valid = False
                if not still_valid:
                    connection.execute(
                        "UPDATE jobs SET status='failed',error_code='SOURCE_FILE_INTEGRITY_ERROR',error_message='A research source changed while the draft was being prepared; no Artifact was published.',updated_at=? WHERE id=? AND lease_generation=?",
                        (utc_now(), job_id, generation),
                    )
                    return

            citations = _validate_citations(connection, claim["workspace_id"], citations)
            now = utc_now()
            artifact_id = str(uuid.uuid4())
            version_snapshot = dict(snapshot)
            version_snapshot["research"] = research
            connection.execute(
                "INSERT INTO artifacts(id, workspace_id, work_id, owner_user_id, title, current_version, created_at, updated_at) VALUES(?, ?, ?, ?, ?, 1, ?, ?)",
                (artifact_id, claim["workspace_id"], work_id, row["user_id"], title, now, now),
            )
            connection.execute(
                "INSERT INTO artifact_versions(artifact_id, workspace_id, version, title, body, citations_json, input_snapshot_json, revision_note, created_at, created_by, parent_version) VALUES(?, ?, 1, ?, ?, ?, ?, 'Source-bound research evidence draft', ?, ?, NULL)",
                (artifact_id, claim["workspace_id"], title, body, json.dumps(citations, ensure_ascii=False), json.dumps(version_snapshot, ensure_ascii=False), now, row["user_id"]),
            )
            connection.execute(
                "INSERT OR IGNORE INTO work_artifacts(work_id, artifact_id, workspace_id, created_at) VALUES(?, ?, ?, ?)",
                (work_id, artifact_id, claim["workspace_id"], now),
            )
            partial = any(
                item["integrity"] != "verified" or item["status"] != "ready"
                for item in source_records
            )
            connection.execute(
                "UPDATE jobs SET status=?,progress_completed=?,progress_total=?,progress_label=?,artifact_id=?,error_code=?,error_message=?,updated_at=? WHERE id=? AND status='running' AND lease_generation=?",
                (
                    "partial" if partial else "completed",
                    max(1, len(material_ids) + len(request_data["propositions"]) + 1),
                    max(1, len(material_ids) + len(request_data["propositions"]) + 1),
                    "Research draft saved with source positions and review state",
                    artifact_id,
                    "SOME_MATERIALS_UNAVAILABLE" if partial else None,
                    "Some selected originals or parsed text were unavailable; see the saved source states." if partial else None,
                    now, job_id, generation,
                ),
            )
    except ApiError as exc:
        _fail_job(store, job_id, generation, exc.code, exc.message)
    except Exception:
        _fail_job(store, job_id, generation, "RESULT_SAVE_FAILED", "The research Artifact could not be saved; verify the job before retrying.")


def _validate_research_citations(store, workspace_id: str, citations: list[dict]) -> list[dict]:
    with store.transaction(immediate=False) as connection:
        return _validate_citations(connection, workspace_id, citations)


def process_research_job(app, claim: dict) -> None:
    """Worker entry point for source-bound research; respects lease and pause state."""
    _process_research_job(app, claim)


def _clean_revision_request(data: dict) -> dict:
    allowed = {"base_version", "updates", "additions", "remove_ids", "material_ids", "revision_note"}
    if not isinstance(data, dict) or set(data) - allowed:
        raise _research_error("INVALID_INPUT", "Research revision contains an unsupported field")
    base_version = data.get("base_version")
    if isinstance(base_version, bool) or not isinstance(base_version, int) or base_version < 1:
        raise _research_error("INVALID_INPUT", "base_version must be a positive integer")
    updates = data.get("updates", [])
    additions = data.get("additions", [])
    remove_ids = data.get("remove_ids", [])
    if not isinstance(updates, list) or len(updates) > _MAX_PROPOSITIONS:
        raise _research_error("INVALID_INPUT", "updates must contain at most 20 propositions")
    if not isinstance(additions, list) or len(additions) > _MAX_PROPOSITIONS:
        raise _research_error("INVALID_INPUT", "additions must contain at most 20 propositions")
    if not isinstance(remove_ids, list) or len(remove_ids) > _MAX_PROPOSITIONS or any(not isinstance(item, str) or not item.strip() for item in remove_ids):
        raise _research_error("INVALID_INPUT", "remove_ids must contain at most 20 proposition IDs")
    if len(set(remove_ids)) != len(remove_ids):
        raise _research_error("INVALID_INPUT", "remove_ids must be unique")
    update_ids = [item.get("proposition_id") for item in updates if isinstance(item, dict)]
    if len(update_ids) != len(updates) or len(update_ids) != len(set(update_ids)):
        raise _research_error("INVALID_INPUT", "updates must contain one unique proposition_id per item")
    if set(remove_ids) & set(update_ids):
        raise _research_error("INVALID_INPUT", "A proposition cannot be updated and removed in one revision")
    material_ids = _clean_material_ids(data.get("material_ids", []))
    revision_note = _optional_string(data.get("revision_note"), "revision_note", maximum=2000) or "研究命题修订"
    if not updates and not additions and not remove_ids:
        raise _research_error("NO_RESEARCH_CHANGES", "Add, update or remove at least one proposition")
    return {
        "base_version": base_version,
        "updates": updates,
        "additions": additions,
        "remove_ids": remove_ids,
        "material_ids": material_ids,
        "revision_note": revision_note,
    }


def _apply_research_changes(
    connection, identity: dict, work_id: str, current: dict, changes: dict, *,
    proposition_origin: str = "user_supplied",
) -> dict:
    research = current.get("research")
    if not isinstance(research, dict) or research.get("schema") != _RESEARCH_SCHEMA:
        raise _research_error("RESEARCH_REVISION_REQUIRED", "This Artifact is not a versioned research draft", 409)
    propositions = [dict(item) for item in research.get("propositions", [])]
    by_id = {item.get("proposition_id"): item for item in propositions}
    if len(by_id) != len(propositions) or any(not isinstance(key, str) for key in by_id):
        raise _research_error("RESEARCH_STATE_INVALID", "The saved research propositions are inconsistent", 409)
    selected_sources = {item.get("id") for item in current.get("materials", []) if isinstance(item, dict)}
    selected_sources.update(changes["material_ids"])

    for raw in changes["updates"]:
        proposition_id = raw.get("proposition_id")
        if proposition_id not in by_id:
            raise _research_error("PROPOSITION_NOT_FOUND", "One or more selected propositions are not in this version", 404)
        mutable = {key: value for key, value in raw.items() if key != "proposition_id"}
        if not mutable:
            raise _research_error("NO_RESEARCH_CHANGES", "Each update must change at least one proposition field")
        normalized = _normalise_proposition(
            connection, identity, work_id, raw, proposition_id=proposition_id,
            selected_material_ids=selected_sources, partial=True,
        )
        statement_changed = (
            "statement" in mutable
            and mutable["statement"] != by_id[proposition_id].get("statement")
        )
        by_id[proposition_id].update({key: value for key, value in normalized.items() if key != "proposition_id"})
        if statement_changed:
            for field in ("supporting_refs", "counterevidence_refs"):
                if field not in mutable:
                    by_id[proposition_id][field] = [
                        {**reference, "relation_state": "requires_reassessment"}
                        for reference in by_id[proposition_id].get(field, [])
                    ]
        if by_id[proposition_id].get("kind") == "source_statement" and not by_id[proposition_id].get("supporting_refs"):
            raise _research_error("SOURCE_REFERENCE_REQUIRED", "A source statement needs at least one exact supporting reference")
        by_id[proposition_id]["assessment_state"] = "unreviewed"
        by_id[proposition_id]["last_revision_origin"] = proposition_origin

    removed = set(changes["remove_ids"])
    if removed - set(by_id):
        raise _research_error("PROPOSITION_NOT_FOUND", "One or more selected propositions are not in this version", 404)
    for proposition_id in removed:
        by_id.pop(proposition_id)

    additions = []
    for raw in changes["additions"]:
        additions.append(_normalise_proposition(
            connection, identity, work_id, raw,
            proposition_id=str(uuid.uuid4()), selected_material_ids=selected_sources,
            origin=proposition_origin,
        ))
    new_items = propositions.copy()
    new_items = [by_id[item["proposition_id"]] for item in propositions if item["proposition_id"] in by_id]
    new_items.extend(additions)
    if len(new_items) > _MAX_PROPOSITIONS:
        raise _research_error("RESEARCH_INPUT_TOO_LARGE", "A research version may contain at most 20 propositions", 413)
    total_text = sum(
        len(item.get("statement", "")) + len(item.get("reasoning", "")) + sum(map(len, item.get("assumptions", [])))
        for item in new_items
    )
    if total_text > _MAX_RESEARCH_TEXT:
        raise _research_error("RESEARCH_INPUT_TOO_LARGE", "Research statements exceed the current 80000-character limit", 413)
    return {**research, "propositions": new_items, "analysis_complete": False, "judgment_state": "not_independently_assessed"}


def _reference_identity(proposition_id: str, field: str, reference: dict) -> tuple:
    return (
        proposition_id, field, reference.get("material_id"), reference.get("segment_id"),
        reference.get("source_start"), reference.get("source_end"), reference.get("quote"),
    )


def _merge_material_snapshots(prior: list[dict], additions: list[dict]) -> list[dict]:
    merged = {item["id"]: item for item in prior if isinstance(item, dict) and isinstance(item.get("id"), str)}
    merged.update((item["id"], item) for item in additions)
    return list(merged.values())


def _load_revision_materials(store, identity: dict, work_id: str, materials: list[dict]) -> tuple[list[dict], list[dict], dict[str, str]]:
    return _load_material_inputs(store, identity, work_id, materials)


def revise_research_artifact(
    store, identity: dict, artifact_id: str, raw_data: dict, idempotency_key: str, *,
    proposition_origin: str = "user_supplied",
) -> dict:
    """Structured proposition revision seam for natural-language and direct callers."""
    changes = _clean_revision_request(raw_data)
    workspace_id = identity["workspace"]["id"]
    key = idempotency_key.strip() if isinstance(idempotency_key, str) else ""
    if proposition_origin not in {"user_supplied", "assistant_candidate"}:
        raise _research_error("INVALID_INPUT", "Unsupported proposition origin")
    if not 1 <= len(key) <= 200:
        raise _research_error("IDEMPOTENCY_KEY_REQUIRED", "Idempotency-Key is required for a research revision")
    fingerprint = _request_fingerprint({"artifact_id": artifact_id, "changes": changes, "proposition_origin": proposition_origin})
    now = utc_now()

    with store.transaction(immediate=False) as connection:
        artifact = require_artifact_access(connection, identity, artifact_id, write=True)
        receipt = connection.execute(
            "SELECT * FROM research_revision_receipts WHERE workspace_id = ? AND operation_key = ?",
            (workspace_id, key),
        ).fetchone()
        if receipt is not None:
            if receipt["artifact_id"] != artifact_id or receipt["request_hash"] != fingerprint:
                raise _research_error("IDEMPOTENCY_CONFLICT", "This key was already used for a different research revision", 409)
            return json.loads(receipt["result_json"])
        if artifact["current_version"] != changes["base_version"]:
            raise _research_error("VERSION_CONFLICT", "Research has changed since it was opened", 409, {"current": _artifact_data(connection, artifact)})
        current = connection.execute(
            "SELECT * FROM artifact_versions WHERE artifact_id = ? AND workspace_id = ? AND version = ?",
            (artifact_id, workspace_id, changes["base_version"]),
        ).fetchone()
        if current is None:
            raise _not_found()
        prior_snapshot = json.loads(current["input_snapshot_json"] or "{}")
        if prior_snapshot.get("kind") != "research" or prior_snapshot.get("research", {}).get("schema") != _RESEARCH_SCHEMA:
            raise _research_error("RESEARCH_REVISION_REQUIRED", "This Artifact is not a versioned research draft", 409)
        work_id = artifact["work_id"]
        if not work_id:
            raise _research_error("RESEARCH_WORK_REQUIRED", "Research revisions require their original work context", 409)
        old_research = prior_snapshot["research"]
        old_reference_ids = {
            _reference_identity(proposition["proposition_id"], field, reference)
            for proposition in old_research.get("propositions", [])
            for field in ("supporting_refs", "counterevidence_refs")
            for reference in proposition.get(field, [])
        }
        old_proposition_ids = {item["proposition_id"] for item in old_research.get("propositions", [])}
        next_research = _apply_research_changes(
            connection, identity, work_id, prior_snapshot, changes,
            proposition_origin=proposition_origin,
        )
        new_refs = [
            reference
            for proposition in next_research.get("propositions", [])
            for field in ("supporting_refs", "counterevidence_refs")
            for reference in proposition.get(field, [])
            if _reference_identity(proposition["proposition_id"], field, reference) not in old_reference_ids
        ]
        prior_source_ids = {item.get("id") for item in prior_snapshot.get("materials", []) if isinstance(item, dict)}
        added_material_ids = set(changes["material_ids"]) - prior_source_ids
        for reference in new_refs:
            previous_source = next((item for item in prior_snapshot.get("materials", []) if item.get("id") == reference["material_id"]), None)
            if previous_source is None:
                added_material_ids.add(reference["material_id"])
            elif previous_source.get("sha256") != reference.get("source_sha256"):
                raise _research_error("SOURCE_VERSION_CHANGED", "A selected Material changed; add the newer source as a separate Material before citing it", 409)
        new_material_snapshots = []
        if added_material_ids:
            for material_id in added_material_ids:
                material = require_material_access(connection, identity, material_id, target_work_id=work_id)
                new_material_snapshots.append({
                    "id": material_id,
                    "sha256": material["sha256"],
                    "parsed_text_sha256": hashlib.sha256((material["text"] or "").encode("utf-8")).hexdigest(),
                    "parsed_segments_sha256": _stored_segment_digest(connection, workspace_id, material_id),
                    "title": material["title"], "filename": material["filename"], "format": material["format"],
                    "status": material["status"], "reason": material["reason"],
                })
                connection.execute(
                    "INSERT OR IGNORE INTO work_materials(work_id, material_id, workspace_id, created_at) VALUES(?, ?, ?, ?)",
                    (work_id, material_id, workspace_id, now),
                )
        material_snapshots = _merge_material_snapshots(prior_snapshot.get("materials", []), new_material_snapshots)
        request_data = {
            "question": next_research["question"], "purpose": next_research.get("purpose"),
            "scope": next_research.get("scope", {}), "propositions": next_research.get("propositions", []),
        }

    try:
        with store.transaction(immediate=False) as connection:
            require_work_access(connection, identity, work_id, write=True)
            for source in material_snapshots:
                require_material_access(connection, identity, source["id"], target_work_id=work_id)
        material_inputs, source_records, verified_originals = _load_revision_materials(store, identity, work_id, material_snapshots)
        missing_ids = {item["id"] for item in material_snapshots} - set(verified_originals)
        if any(reference["material_id"] in missing_ids for reference in new_refs):
            raise _research_error("SOURCE_FILE_INTEGRITY_ERROR", "A newly selected research source failed integrity verification", 409)
        segment_count = sum(len(item["parsed"].get("segments", [])) for item in material_inputs)
        if segment_count * (1 + len(request_data["propositions"])) > _MAX_ANALYSIS_SEGMENT_PASSES:
            raise _research_error("RESEARCH_SCOPE_TOO_LARGE", "The updated source and proposition set exceeds the current evidence review budget", 413)
        request_for_build = {**request_data, "propositions": _mark_reference_integrity(request_data["propositions"], source_records)}
        research = _build_research(material_inputs, request_for_build, source_records)
        research["revision_parent_version"] = changes["base_version"]
        body = _render_research_body(research)
        citations = _citation_rows(research)
        title = _source_title(research["question"])
    except ApiError:
        raise
    except Exception as exc:
        if isinstance(exc, ValueError):
            raise _research_error("RESEARCH_FAILED", "The revised research evidence could not be validated", 409) from exc
        raise _research_error("RESEARCH_FAILED", "The revised research Artifact could not be prepared", 409) from exc

    with store.transaction() as connection:
        artifact = require_artifact_access(connection, identity, artifact_id, write=True)
        existing_receipt = connection.execute(
            "SELECT * FROM research_revision_receipts WHERE workspace_id = ? AND operation_key = ?",
            (workspace_id, key),
        ).fetchone()
        if existing_receipt is not None:
            if existing_receipt["artifact_id"] != artifact_id or existing_receipt["request_hash"] != fingerprint:
                raise _research_error("IDEMPOTENCY_CONFLICT", "This key was already used for a different research revision", 409)
            return json.loads(existing_receipt["result_json"])
        if artifact["current_version"] != changes["base_version"]:
            raise _research_error("VERSION_CONFLICT", "Research has changed while this revision was prepared", 409, {"current": _artifact_data(connection, artifact)})
        require_work_access(connection, identity, work_id, write=True)
        for material_id in verified_originals:
            require_material_access(connection, identity, material_id, target_work_id=work_id)
            source = connection.execute(
                "SELECT relative_path, sha256, text FROM materials WHERE id = ? AND workspace_id = ?",
                (material_id, workspace_id),
            ).fetchone()
            try:
                path = _resource_path(store, source["relative_path"]) if source else None
                valid = (
                    source is not None and path is not None and path.is_file()
                    and source["sha256"] == verified_originals[material_id]
                    and hashlib.sha256((source["text"] or "").encode("utf-8")).hexdigest()
                    == next(item["parsed_text_sha256"] for item in material_snapshots if item["id"] == material_id)
                    and _stored_segment_digest(connection, workspace_id, material_id)
                    == next(item["parsed_segments_sha256"] for item in material_snapshots if item["id"] == material_id)
                    and _sha256_file(path) == verified_originals[material_id]
                )
            except (ApiError, OSError):
                valid = False
            if not valid:
                raise _research_error("SOURCE_FILE_INTEGRITY_ERROR", "A research source changed while the revision was prepared", 409)
        citations = _validate_citations(connection, workspace_id, citations)
        snapshot = dict(prior_snapshot)
        snapshot["materials"] = _merge_material_snapshots(prior_snapshot.get("materials", []), new_material_snapshots)
        snapshot["research"] = research
        snapshot = _input_snapshot(connection, workspace_id, work_id, citations, snapshot)
        preserved_sources = {
            item["id"]: item
            for item in _merge_material_snapshots(prior_snapshot.get("materials", []), new_material_snapshots)
        }
        snapshot["materials"] = [
            {**item, **preserved_sources.get(item["id"], {})}
            for item in snapshot.get("materials", [])
        ]
        version = changes["base_version"] + 1
        connection.execute(
            "INSERT INTO artifact_versions(artifact_id, workspace_id, version, title, body, citations_json, input_snapshot_json, revision_note, created_at, created_by, parent_version) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (artifact_id, workspace_id, version, title, body, json.dumps(citations, ensure_ascii=False), json.dumps(snapshot, ensure_ascii=False), changes["revision_note"], now, identity["user"]["id"], changes["base_version"]),
        )
        connection.execute(
            "UPDATE artifacts SET title = ?, current_version = ?, updated_at = ? WHERE id = ? AND workspace_id = ? AND current_version = ?",
            (title, version, now, artifact_id, workspace_id, changes["base_version"]),
        )
        updated = connection.execute("SELECT * FROM artifacts WHERE id = ? AND workspace_id = ?", (artifact_id, workspace_id)).fetchone()
        result = _artifact_data(connection, updated)
        after_proposition_ids = {item["proposition_id"] for item in research.get("propositions", [])}
        updated_proposition_ids = {item["proposition_id"] for item in changes["updates"]}
        added_proposition_ids = after_proposition_ids - old_proposition_ids
        changed_proposition_ids = updated_proposition_ids | added_proposition_ids
        result["research_revision"] = {
            "parent_version": changes["base_version"],
            "changed_proposition_ids": sorted(changed_proposition_ids),
            "removed_proposition_ids": changes["remove_ids"],
            "preserved_proposition_ids": [
                item["proposition_id"] for item in research.get("propositions", [])
                if item["proposition_id"] not in changed_proposition_ids
            ],
            "source_integrity": {item["material_id"]: item["integrity"] for item in source_records},
        }
        connection.execute(
            "INSERT INTO research_revision_receipts(workspace_id, operation_key, artifact_id, request_hash, result_version, result_json, created_at) VALUES(?, ?, ?, ?, ?, ?, ?)",
            (workspace_id, key, artifact_id, fingerprint, version, json.dumps(result, ensure_ascii=False), now),
        )
    return result


def register_research_routes(app, store) -> None:
    """Register structured research revisions on the existing Artifact owner."""
    install_research_schema(store)
    app.extensions.setdefault("oevira.job_resume_validators", {})[_JOB_KIND] = validate_research_resume

    @app.post("/api/artifacts/<artifact_id>/research-revisions")
    def post_research_revision(artifact_id: str):
        key = request.headers.get("Idempotency-Key", "")
        return _success(revise_research_artifact(store, g.identity, artifact_id, _body(), key))


def validate_research_resume(connection, identity: dict, row, resume_context: dict) -> None:
    """Resume only the exact frozen work, original bytes, parser text and segments."""
    if resume_context:
        raise _research_error("INVALID_INPUT", "Continue the original research request; changed inputs require a new research job")
    payload = json.loads(row["payload_json"])
    snapshot = payload.get("input_snapshot")
    request_data = payload.get("research_request")
    if (
        not isinstance(snapshot, dict) or snapshot.get("kind") != "research"
        or not isinstance(request_data, dict)
        or not isinstance(snapshot.get("work"), dict)
        or snapshot["work"].get("id") != row["work_id"]
    ):
        raise _research_error("JOB_INPUT_VERSION_UNAVAILABLE", "This research job has no complete frozen request; create a new job", 409)
    work = require_work_access(connection, identity, row["work_id"], write=True)
    if work["version"] != snapshot["work"].get("version"):
        raise _research_error("WORK_VERSION_CONFLICT", "The work changed after research was queued; start a new research job", 409)
    materials = snapshot.get("materials")
    if not isinstance(materials, list):
        raise _research_error("JOB_INPUT_VERSION_UNAVAILABLE", "This research job has no complete source snapshot; create a new job", 409)
    material_ids = [item.get("id") for item in materials if isinstance(item, dict)]
    if len(material_ids) != len(materials) or material_ids != payload.get("material_ids") or material_ids != request_data.get("material_ids"):
        raise _research_error("JOB_INPUT_VERSION_UNAVAILABLE", "The research source selection no longer matches its frozen request", 409)
    workspace_id = identity["workspace"]["id"]
    source_by_id = {}
    for source in materials:
        if not all(isinstance(source.get(field), str) and source[field] for field in ("id", "sha256", "parsed_text_sha256", "parsed_segments_sha256")):
            raise _research_error("JOB_INPUT_VERSION_UNAVAILABLE", "A research source lacks its frozen content identity; start a new job", 409)
        material = require_material_access(connection, identity, source["id"], target_work_id=row["work_id"])
        if material["sha256"] != source["sha256"]:
            raise _research_error("JOB_INPUT_CHANGED", "A selected original changed while research was stopped; start a new research job", 409)
        if hashlib.sha256((material["text"] or "").encode("utf-8")).hexdigest() != source["parsed_text_sha256"]:
            raise _research_error("JOB_INPUT_CHANGED", "A selected parsed source changed while research was stopped; start a new research job", 409)
        if _stored_segment_digest(connection, workspace_id, source["id"]) != source["parsed_segments_sha256"]:
            raise _research_error("JOB_INPUT_CHANGED", "A selected source segment map changed while research was stopped; start a new research job", 409)
        source_by_id[source["id"]] = material

    for proposition in request_data.get("propositions", []):
        if not isinstance(proposition, dict):
            raise _research_error("JOB_INPUT_VERSION_UNAVAILABLE", "The saved proposition input is invalid", 409)
        for field in ("supporting_refs", "counterevidence_refs"):
            for reference in proposition.get(field, []):
                material = source_by_id.get(reference.get("material_id"))
                if material is None:
                    raise _research_error("JOB_INPUT_VERSION_UNAVAILABLE", "A proposition cites a source outside the frozen selection", 409)
                if reference.get("source_sha256") != material["sha256"]:
                    raise _research_error("JOB_INPUT_CHANGED", "A proposition source version changed while research was stopped", 409)
                segment = connection.execute(
                    "SELECT text, start_offset, end_offset FROM material_segments WHERE workspace_id = ? AND material_id = ? AND id = ?",
                    (workspace_id, reference["material_id"], reference["segment_id"]),
                ).fetchone()
                start, end = reference.get("source_start"), reference.get("source_end")
                if (
                    segment is None or type(start) is not int or type(end) is not int
                    or start < segment["start_offset"] or end > segment["end_offset"]
                    or (material["text"] or "")[start:end] != reference.get("quote")
                ):
                    raise _research_error("JOB_INPUT_CHANGED", "A proposition source position changed while research was stopped", 409)
