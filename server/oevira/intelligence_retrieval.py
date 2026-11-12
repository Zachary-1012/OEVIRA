"""Scoped lexical retrieval over existing Materials and exact parser spans."""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from functools import lru_cache
import hashlib
import heapq
import json
import math
from pathlib import Path, PurePosixPath
import re
import unicodedata
from urllib.parse import urlsplit

from .auth import utc_now
from .intelligence_base import IntelligenceError, identity_scope


MAX_QUERY_CHARACTERS = 512
MAX_MATERIAL_IDS = 500
MAX_RESULTS = 50
MAX_SCOPE_MATERIALS = 5_000
MAX_SCOPE_SEGMENTS = 100_000
MAX_SCOPE_CHARACTERS = 12_000_000
MAX_PASSAGE_CHARACTERS = 1_200
PASSAGE_OVERLAP_CHARACTERS = 160
MAX_LEGACY_CANDIDATE_SETS = 5_000
MAX_LEGACY_CANDIDATE_FILE_BYTES = 4 * 1024 * 1024
MAX_LEGACY_CANDIDATE_BYTES = 64 * 1024 * 1024
LEGACY_CANDIDATE_OWNER = (
    "Knowledge/Research owner: candidate sets, query scope, provenance and review history"
)
LEGACY_CANDIDATE_SOURCE_TABLE = "lumenis_shared_artifacts"
_LEXICAL_SPAN = re.compile(r"[\u3400-\u9fff]+|[\uac00-\ud7a3]+|[^\W_]+(?:[-'][^\W_]+)*", re.UNICODE)


@dataclass(frozen=True)
class _Passage:
    material_id: str
    material_title: str
    filename: str
    material_sha256: str
    source_url: str | None
    source_metadata: dict
    segment_id: str
    segment_label: str
    segment_kind: str
    segment_text: str
    material_text: str
    segment_start: int
    segment_end: int
    window_start: int
    window_end: int
    text: str

    @property
    def start_offset(self) -> int:
        return self.segment_start + self.window_start

    @property
    def end_offset(self) -> int:
        return self.segment_start + self.window_end


@lru_cache(maxsize=4096)
def _read_preserved_candidate_payload(
    store_root: str,
    workspace_id: str,
    resource_id: str,
    relative_path: str,
    expected_sha256: str,
    actual_sha256: str,
    metadata_sha256: str,
    byte_size: int,
    modified_ns: int,
    created_ns: int,
) -> dict:
    """Read a workspace-local preserved candidate only after exact path/hash checks."""
    root = Path(store_root).resolve(strict=True)
    expected_relative = f"objects/{workspace_id}/legacy-preserved/{resource_id}/original.bin"
    if (relative_path != expected_relative or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256)
            or actual_sha256 != expected_sha256 or metadata_sha256 != expected_sha256
            or type(byte_size) is not int or not 1 <= byte_size <= MAX_LEGACY_CANDIDATE_FILE_BYTES):
        raise ValueError("preserved_candidate_metadata_invalid")
    relative = PurePosixPath(relative_path)
    if relative.is_absolute() or ".." in relative.parts or "\\" in relative_path:
        raise ValueError("preserved_candidate_path_invalid")
    path = root.joinpath(*relative.parts)
    cursor = root
    for part in relative.parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise ValueError("preserved_candidate_path_invalid")
    resolved = path.resolve(strict=True)
    resolved.relative_to(root)
    if path.is_symlink() or not resolved.is_file():
        raise ValueError("preserved_candidate_file_invalid")
    current = resolved.stat()
    if (current.st_size != byte_size or current.st_mtime_ns != modified_ns
            or current.st_ctime_ns != created_ns):
        raise ValueError("preserved_candidate_file_changed")
    raw = resolved.read_bytes()
    after = resolved.stat()
    if (len(raw) != byte_size or after.st_size != byte_size
            or after.st_mtime_ns != modified_ns or after.st_ctime_ns != created_ns
            or hashlib.sha256(raw).hexdigest() != expected_sha256):
        raise ValueError("preserved_candidate_hash_mismatch")
    payload = json.loads(raw.decode("utf-8"))
    if (not isinstance(payload, dict) or not isinstance(payload.get("source_inputs"), dict)
            or not isinstance(payload.get("source_inputs", {}).get("source_rights"), dict)
            or not isinstance(payload.get("candidates"), list)):
        raise ValueError("preserved_candidate_schema_invalid")
    return payload


def _candidate_research_permission(payload: dict) -> tuple[bool, str]:
    source_inputs = payload.get("source_inputs")
    rights = source_inputs.get("source_rights") if isinstance(source_inputs, dict) else None
    if not isinstance(rights, dict):
        return False, "SOURCE_RIGHTS_UNAVAILABLE"
    uses = rights.get("allowed_uses")
    if (rights.get("status") != "APPROVED" or not isinstance(uses, list)
            or not any(value in {"research", "knowledge_retrieval", "internal_research"} for value in uses)
            or rights.get("third_party_rights_granted") is not True):
        return False, "SOURCE_USE_NOT_ADMITTED"
    return True, "SOURCE_USE_ADMITTED"


def _score_candidate_terms(query_terms: list[str], candidate_texts: list[str]) -> list[tuple[int, float]]:
    if not candidate_texts:
        return []
    query_frequency = Counter(query_terms)
    term_frequencies = [Counter(_tokens(text)) for text in candidate_texts]
    lengths = [sum(value.values()) for value in term_frequencies]
    total_length = sum(lengths)
    average_length = total_length / len(candidate_texts) if total_length else 1.0
    document_frequency: Counter[str] = Counter()
    for frequencies in term_frequencies:
        document_frequency.update(frequencies.keys() & query_frequency.keys())
    scored = []
    for index, frequencies in enumerate(term_frequencies):
        score = 0.0
        for term, query_count in query_frequency.items():
            frequency = frequencies.get(term, 0)
            if not frequency:
                continue
            count = document_frequency[term]
            inverse_frequency = math.log1p((len(candidate_texts) - count + 0.5) / (count + 0.5))
            denominator = frequency + 1.2 * (1 - 0.75 + 0.75 * lengths[index] / average_length)
            score += query_count * inverse_frequency * (frequency * 2.2 / denominator)
        if score > 0:
            scored.append((index, score))
    scored.sort(key=lambda item: (-item[1], item[0]))
    return scored


def _derive_material_query_candidates(materials: list, query_terms: list[str], limit: int) -> dict:
    """Extract bounded, source-located query leads from already-scoped Material text.

    This adopts the old producer's distinction between literal hashtags and
    deterministic body-derived terms. It does not mark a term as a native
    platform topic, fact, preference, or training example.
    """
    candidates = []
    for material in materials:
        if material["status"] not in {"ready", "partial"}:
            continue
        body = material["text"]
        if not isinstance(body, str) or not body:
            continue
        by_term: dict[str, dict] = {}

        def add(term: str, kind: str, spans: list[tuple[int, int]]) -> None:
            if not term or not spans:
                return
            current = by_term.get(term)
            if current is None or kind == "VERBATIM_BODY_HASHTAG_LITERAL":
                by_term[term] = {"term": term, "kind": kind, "spans": spans}

        hashtags: dict[str, list[tuple[int, int]]] = {}
        for match in re.finditer(r"(?<!#)#[\w]+", body, flags=re.UNICODE):
            hashtags.setdefault(match.group(0), []).append(match.span())
        for term, spans in hashtags.items():
            add(term, "VERBATIM_BODY_HASHTAG_LITERAL", spans)

        cjk_counts = Counter(re.findall(r"[\u4e00-\u9fa5]{2,4}", body))
        for term, count in cjk_counts.items():
            if count >= 2:
                spans = [match.span() for match in re.finditer(re.escape(term), body)]
                add(term, "DERIVED_TEXT_KEYWORD", spans)

        english_pattern = re.compile(r"(?<![A-Za-z0-9])([A-Za-z][A-Za-z0-9]*)(?![A-Za-z0-9])")
        english_spans: dict[str, list[tuple[int, int]]] = {}
        for match in english_pattern.finditer(body):
            term = match.group(1)
            if sum(character.isascii() and character.isalpha() for character in term) >= 2:
                english_spans.setdefault(term, []).append(match.span(1))
        for term, spans in english_spans.items():
            if len(spans) >= 2:
                add(term, "DERIVED_TEXT_KEYWORD", spans)

        ranked_local = sorted(
            by_term.values(),
            key=lambda item: (-len(item["spans"]), item["spans"][0][0], item["term"].casefold()),
        )[:20]
        text_sha256 = hashlib.sha256(body.encode("utf-8")).hexdigest()
        for candidate in ranked_local:
            spans = candidate["spans"]
            candidates.append({
                "material_id": material["id"],
                "material_title": material["title"],
                "material_sha256": material["sha256"],
                "text_sha256": text_sha256,
                "source_status": material["status"],
                "term": candidate["term"],
                "kind": candidate["kind"],
                "occurrence_count": len(spans),
                "body_locators": [
                    {"unit": "material-body", "json_path": "$.text", "start": start,
                     "end": end, "offset_unit": "unicode-codepoint"}
                    for start, end in spans[:5]
                ],
                "first_quote_sha256": hashlib.sha256(body[spans[0][0]:spans[0][1]].encode("utf-8")).hexdigest(),
                "admitted": False,
                "automatic_adoption": False,
                "owned_inference": "NOT_RUN",
                "training_use": "NOT_GRANTED_BY_RETRIEVAL",
            })
    scored = _score_candidate_terms(
        query_terms,
        [candidate["term"].lstrip("#") for candidate in candidates],
    )
    results = []
    for rank, (index, score) in enumerate(scored[:limit], 1):
        candidate = candidates[index]
        locator = candidate["body_locators"][0]
        material_id = candidate["material_id"]
        results.append({
            "rank": rank, "lexical_score": score,
            "term": candidate["term"], "candidate_kind": candidate["kind"],
            "occurrence_count": candidate["occurrence_count"],
            "source": {
                "kind": "material_body_candidate", "material_id": material_id,
                "material_sha256": candidate["material_sha256"],
                "text_sha256": candidate["text_sha256"],
                "source_status": candidate["source_status"],
                "start_offset": locator["start"], "end_offset": locator["end"],
                "offset_unit": locator["offset_unit"],
                "quote_sha256": candidate["first_quote_sha256"],
            },
            "source_title": candidate["material_title"],
            "evidence_state": "UNVERIFIED_RESEARCH_HINT_NOT_FACT",
            "training_use": "NOT_GRANTED_BY_RETRIEVAL",
        })
    return {
        "status": "complete" if results else "no_matches",
        "results": results,
        "coverage": {
            "eligible_materials": len(materials),
            "derived_candidates": len(candidates),
            "matched_candidates": len(scored),
            "returned_candidates": len(results),
            "source_text_modified": False,
        },
        "limitations": [
            "These terms were derived from exact text in the authorized Work's Materials; they are discovery hints, not native platform topics or verified facts.",
            "Each result points to the original Material text and must be checked in its current source context before factual use.",
        ],
    }


def _search_preserved_research_candidates(connection, store, workspace_id: str, query_terms: list[str],
                                          limit: int, *, is_cancelled=None) -> dict:
    """Search legacy candidate hints only when the preserved SourceUse permits research."""
    tables = {row[0] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('legacy_import_items','legacy_import_files')",
    )}
    if tables != {"legacy_import_items", "legacy_import_files"}:
        return {"status": "unavailable", "results": [], "coverage": {"complete_scope_scanned": False},
                "limitations": ["The preserved research candidate adapter is not present in this workspace."]}
    rows = connection.execute(
        "SELECT i.source_identity, i.source_key, i.resource_id, i.input_sha256, i.metadata_json, "
        "f.private_relative_path, f.expected_sha256, f.actual_sha256, f.size_bytes "
        "FROM legacy_import_items i JOIN legacy_import_files f "
        "ON f.workspace_id=i.workspace_id AND f.source_identity=i.source_identity "
        "AND f.source_table=i.source_table AND f.source_key=i.source_key "
        "WHERE i.workspace_id=? AND i.source_table=? AND i.resource_type='preserved_private_file' "
        "AND i.owner_adapter=? AND i.disposition='owner_adapter_pending' "
        "ORDER BY i.source_identity, i.source_key LIMIT ?",
        (workspace_id, LEGACY_CANDIDATE_SOURCE_TABLE, LEGACY_CANDIDATE_OWNER, MAX_LEGACY_CANDIDATE_SETS + 1),
    ).fetchall()
    if len(rows) > MAX_LEGACY_CANDIDATE_SETS:
        return {"status": "scope_too_large", "results": [], "coverage": {
            "preserved_candidate_sets": len(rows), "complete_scope_scanned": False,
        }, "limitations": ["The preserved candidate scope exceeds the bounded query limit."]}
    total_bytes = sum(row["size_bytes"] or 0 for row in rows)
    if total_bytes > MAX_LEGACY_CANDIDATE_BYTES:
        return {"status": "scope_too_large", "results": [], "coverage": {
            "preserved_candidate_sets": len(rows), "preserved_bytes": total_bytes,
            "complete_scope_scanned": False,
        }, "limitations": ["The preserved candidate scope exceeds the bounded read budget."]}
    payloads = []
    source_use_blocked = 0
    hash_verified = 0
    for row in rows:
        if is_cancelled is not None:
            try:
                cancelled = is_cancelled()
            except Exception as exc:
                raise IntelligenceError("SEARCH_CANCEL_CHECK_FAILED", 503) from exc
            if type(cancelled) is not bool:
                raise IntelligenceError("SEARCH_CANCEL_CHECK_INVALID", 503)
            if cancelled:
                raise IntelligenceError("SEARCH_CANCELLED", 409)
        try:
            metadata = json.loads(row["metadata_json"] or "{}")
            if not isinstance(metadata, dict):
                raise ValueError("preserved_candidate_metadata_invalid")
            expected = row["expected_sha256"]
            if (row["resource_id"] is None or row["source_key"] is None
                    or not isinstance(row["source_identity"], str)
                    or not row["source_identity"].startswith("lumenis:")
                    or row["source_identity"][len("lumenis:"):]
                    != row["source_identity"][len("lumenis:"):].casefold()
                    or not re.fullmatch(r"[0-9a-f]{64}", row["input_sha256"] or "")
                    or metadata.get("expected_sha256") != expected
                    or metadata.get("actual_sha256") != row["actual_sha256"]
                    or metadata.get("hash_matches") is not True
                    or metadata.get("private_relative_path") != row["private_relative_path"]
                    or row["private_relative_path"]
                    != f"objects/{workspace_id}/legacy-preserved/{row['resource_id']}/original.bin"):
                raise ValueError("preserved_candidate_mapping_invalid")
            path = Path(store.root).resolve(strict=True).joinpath(*PurePosixPath(row["private_relative_path"]).parts)
            stat = path.stat()
            payload = _read_preserved_candidate_payload(
                str(Path(store.root).resolve(strict=True)), workspace_id, row["resource_id"],
                row["private_relative_path"], expected, row["actual_sha256"], metadata.get("actual_sha256"),
                row["size_bytes"], stat.st_mtime_ns, stat.st_ctime_ns,
            )
        except (OSError, RuntimeError, ValueError, TypeError, json.JSONDecodeError):
            return {"status": "integrity_blocked", "results": [], "coverage": {
                "preserved_candidate_sets": len(rows), "hash_verified_candidate_sets": hash_verified,
                "complete_scope_scanned": False,
            }, "limitations": ["A preserved research candidate failed exact workspace path or hash validation; no candidate hints were returned."]}
        hash_verified += 1
        permitted, reason = _candidate_research_permission(payload)
        if not permitted:
            source_use_blocked += 1
            continue
        inputs = payload.get("source_inputs", {})
        rights = inputs.get("source_rights", {}) if isinstance(inputs, dict) else {}
        for index, candidate in enumerate(payload["candidates"]):
            if not isinstance(candidate, dict) or not isinstance(candidate.get("term"), str):
                continue
            term = candidate["term"].strip()
            if not term or len(term) > 512:
                continue
            source_scope = inputs.get("scope") if isinstance(inputs, dict) else None
            source_refs = {
                "kind": "legacy_research_candidate",
                "source_identity": row["source_identity"],
                "source_table": LEGACY_CANDIDATE_SOURCE_TABLE,
                "source_key": row["source_key"],
                "resource_id": row["resource_id"],
                "source_row_sha256": row["input_sha256"],
                "content_sha256": expected,
                "candidate_index": index,
                "candidate_kind": candidate.get("kind"),
                "candidate_origin": candidate.get("candidate_origin"),
                "source_scope": source_scope if isinstance(source_scope, dict) else {},
                "source_evidence_ref": inputs.get("source_evidence_ref"),
                "source_artifact_ref": inputs.get("source_artifact_ref"),
                "source_rights_status": rights.get("status"),
                "source_allowed_uses": rights.get("allowed_uses", []),
                "historical_body_locator_count": len(candidate.get("body_locators", []))
                if isinstance(candidate.get("body_locators"), list) else 0,
                "factual_state": "NOT_FACT",
                "preference_state": "NOT_PREFERENCE",
                "training_use": "NOT_GRANTED_BY_RETRIEVAL",
                "source_use_decision": reason,
            }
            payloads.append({
                "term": term,
                "kind": candidate.get("kind") if isinstance(candidate.get("kind"), str) else "unknown",
                "occurrence_count": candidate.get("occurrence_count")
                if type(candidate.get("occurrence_count")) is int and candidate["occurrence_count"] >= 0 else None,
                "source": source_refs,
                "text": " ".join((term, str(candidate.get("kind", "")), str(candidate.get("candidate_origin", "")))),
            })
    scored = _score_candidate_terms(query_terms, [item["text"] for item in payloads])
    results = []
    for rank, (index, score) in enumerate(scored[:limit], 1):
        candidate = payloads[index]
        results.append({
            "rank": rank, "lexical_score": score, "term": candidate["term"],
            "candidate_kind": candidate["kind"], "occurrence_count": candidate["occurrence_count"],
            "source": candidate["source"], "evidence_state": "UNVERIFIED_RESEARCH_CANDIDATE_NOT_FACT",
        })
    status = "complete" if results else ("source_use_blocked" if source_use_blocked else "no_matches")
    limitations = [
        "Candidate terms are internal research leads only. They are not verified facts, user preferences, or training examples.",
        "Historical source locators are retained as provenance and require current source resolution before factual citation.",
    ]
    if source_use_blocked:
        limitations.append("Preserved candidate records with unreviewed or missing current research-use rights were not searched or returned.")
    return {
        "status": status,
        "results": results,
        "coverage": {
            "preserved_candidate_sets": len(rows),
            "hash_verified_candidate_sets": hash_verified,
            "source_use_blocked_candidate_sets": source_use_blocked,
            "source_use_eligible_candidates": len(payloads),
            "matched_candidates": len(scored),
            "returned_candidates": len(results),
            "complete_scope_scanned": True,
        },
        "limitations": limitations,
    }


def revalidate_preserved_research_candidate_ref(connection, store, identity: dict,
                                                reference: dict) -> None:
    """Recheck a candidate's exact workspace mapping, private hash and research-use decision."""
    workspace_id, _user_id, _session_id = _verify_read_scope(connection, identity)
    if (not isinstance(reference, dict)
            or reference.get("kind") != "legacy_research_candidate"
            or reference.get("source_table") != LEGACY_CANDIDATE_SOURCE_TABLE):
        raise IntelligenceError("LEGACY_RESEARCH_REFERENCE_INVALID", 409)
    row = connection.execute(
        "SELECT i.source_identity,i.source_key,i.resource_id,i.input_sha256,i.metadata_json, "
        "f.private_relative_path,f.expected_sha256,f.actual_sha256,f.size_bytes "
        "FROM legacy_import_items i JOIN legacy_import_files f "
        "ON f.workspace_id=i.workspace_id AND f.source_identity=i.source_identity "
        "AND f.source_table=i.source_table AND f.source_key=i.source_key "
        "WHERE i.workspace_id=? AND i.source_identity=? AND i.source_table=? AND i.source_key=? "
        "AND i.resource_type='preserved_private_file' AND i.owner_adapter=? "
        "AND i.disposition='owner_adapter_pending'",
        (workspace_id, reference.get("source_identity"), LEGACY_CANDIDATE_SOURCE_TABLE,
         reference.get("source_key"), LEGACY_CANDIDATE_OWNER),
    ).fetchone()
    if row is None or row["resource_id"] != reference.get("resource_id"):
        raise IntelligenceError("LEGACY_RESEARCH_SOURCE_REVOKED", 409)
    try:
        metadata = json.loads(row["metadata_json"] or "{}")
        if (not isinstance(metadata, dict)
                or row["input_sha256"] != reference.get("source_row_sha256")
                or metadata.get("expected_sha256") != row["expected_sha256"]
                or metadata.get("actual_sha256") != row["actual_sha256"]
                or metadata.get("hash_matches") is not True
                or metadata.get("private_relative_path") != row["private_relative_path"]):
            raise ValueError("preserved_candidate_mapping_invalid")
        path = Path(store.root).resolve(strict=True).joinpath(*PurePosixPath(row["private_relative_path"]).parts)
        stat = path.stat()
        payload = _read_preserved_candidate_payload(
            str(Path(store.root).resolve(strict=True)), workspace_id, row["resource_id"],
            row["private_relative_path"], row["expected_sha256"], row["actual_sha256"],
            metadata.get("actual_sha256") if isinstance(metadata, dict) else None,
            row["size_bytes"], stat.st_mtime_ns, stat.st_ctime_ns,
        )
    except (OSError, RuntimeError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise IntelligenceError("LEGACY_RESEARCH_SOURCE_CHANGED", 409) from exc
    permitted, _reason = _candidate_research_permission(payload)
    if not permitted or row["expected_sha256"] != reference.get("content_sha256"):
        raise IntelligenceError("LEGACY_RESEARCH_SOURCE_USE_NOT_ADMITTED", 403)


def _tokens(text: str) -> list[str]:
    """Lexical Unicode tokens; CJK and Hangul keep overlapping bigrams."""
    normalized = unicodedata.normalize("NFKC", text).casefold()
    result: list[str] = []
    for match in _LEXICAL_SPAN.finditer(normalized):
        token = match.group(0)
        if any("\u3400" <= character <= "\u9fff" for character in token):
            result.extend(token)
            result.extend(token[index:index + 2] for index in range(len(token) - 1))
        elif any("\uac00" <= character <= "\ud7a3" for character in token):
            result.append(token)
            if len(token) > 2:
                result.extend(token[index:index + 2] for index in range(len(token) - 1))
        else:
            result.append(token)
    return result


def _passage_ranges(text: str) -> list[tuple[int, int]]:
    if not text:
        return []
    stride = MAX_PASSAGE_CHARACTERS - PASSAGE_OVERLAP_CHARACTERS
    last = max(0, len(text) - MAX_PASSAGE_CHARACTERS)
    ranges = [(start, min(len(text), start + MAX_PASSAGE_CHARACTERS))
              for start in range(0, last + 1, stride)]
    if not ranges or ranges[-1][0] != last:
        ranges.append((last, len(text)))
    return ranges


def _safe_source_metadata(row) -> tuple[str | None, dict]:
    try:
        raw = json.loads(row["source_json"] or "{}")
    except (TypeError, ValueError):
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    allowed = (
        "kind", "actual_access_class", "authentication_state", "source_admission",
        "content_trust", "capture_mode", "captured_at", "published_at",
        "published_at_origin", "coverage", "page_revision", "scope",
        "redacted_query_keys",
    )
    metadata = {key: raw[key] for key in allowed if key in raw}
    source_url = row["source_url"]
    if not isinstance(source_url, str) or not source_url.strip():
        source_url = raw.get("source_url")
    if not isinstance(source_url, str) or not source_url.strip():
        return None, metadata
    try:
        # Browser captures already use this shared redactor. Reapply it here
        # so an older imported Material cannot leak URL query credentials.
        from .browser import browser_safe_url
        safe_url, redacted_keys = browser_safe_url(source_url)
    except Exception:
        return None, metadata
    try:
        parsed = urlsplit(safe_url)
        if parsed.username or parsed.password:
            return None, metadata
    except ValueError:
        return None, metadata
    existing_keys = metadata.get("redacted_query_keys", [])
    if not isinstance(existing_keys, list) or any(not isinstance(key, str) for key in existing_keys):
        existing_keys = []
    if redacted_keys:
        metadata["redacted_query_keys"] = sorted(set(existing_keys) | set(redacted_keys))
    return safe_url, metadata


def _verify_read_scope(connection, identity: dict, *, write: bool = False) -> tuple[str, str, str | None]:
    workspace_id, user_id, session_id = identity_scope(identity)
    member = connection.execute(
        "SELECT active, role FROM memberships WHERE workspace_id = ? AND user_id = ?",
        (workspace_id, user_id),
    ).fetchone()
    if member is None or member["active"] != 1:
        raise IntelligenceError("ACCESS_REVOKED", 403)
    if write and member["role"] not in {"owner", "editor"}:
        raise IntelligenceError("WRITE_ACCESS_DENIED", 403)
    if session_id is not None:
        auth_session = connection.execute(
            "SELECT revoked_at, expires_at FROM sessions WHERE id = ? AND workspace_id = ? AND user_id = ?",
            (session_id, workspace_id, user_id),
        ).fetchone()
        if auth_session is None or auth_session["revoked_at"] is not None or auth_session["expires_at"] <= utc_now():
            raise IntelligenceError("UNAUTHENTICATED", 401)
    return workspace_id, user_id, session_id


def _load_scope_materials(connection, workspace_id: str, *, work_id: str | None,
                          material_ids: list[str] | None) -> tuple[list, dict | None]:
    work = None
    if work_id is not None:
        if not isinstance(work_id, str) or not work_id:
            raise IntelligenceError("WORK_SCOPE_INVALID", 400)
        work = connection.execute(
            "SELECT id, title, goal, brief_json, status, version FROM works WHERE id = ? AND workspace_id = ?",
            (work_id, workspace_id),
        ).fetchone()
        if work is None:
            raise IntelligenceError("SOURCE_SCOPE_NOT_FOUND", 404)
    if material_ids is not None:
        if (not isinstance(material_ids, list) or not material_ids
                or len(material_ids) > MAX_MATERIAL_IDS
                or any(not isinstance(value, str) or not value for value in material_ids)
                or len(set(material_ids)) != len(material_ids)):
            raise IntelligenceError("MATERIAL_SCOPE_INVALID", 400)
    elif work_id is None:
        raise IntelligenceError("SOURCE_SCOPE_REQUIRED", 400)

    if work_id is not None:
        sql = (
            "SELECT m.* FROM work_materials wm JOIN materials m "
            "ON m.id = wm.material_id AND m.workspace_id = wm.workspace_id "
            "WHERE wm.workspace_id = ? AND wm.work_id = ?"
        )
        parameters: list[object] = [workspace_id, work_id]
        if material_ids is not None:
            placeholders = ",".join("?" for _ in material_ids)
            sql += f" AND m.id IN ({placeholders})"
            parameters.extend(material_ids)
        sql += " ORDER BY wm.created_at, m.id"
    else:
        placeholders = ",".join("?" for _ in material_ids or [])
        sql = f"SELECT m.* FROM materials m WHERE m.workspace_id = ? AND m.id IN ({placeholders}) ORDER BY m.created_at, m.id"
        parameters = [workspace_id, *(material_ids or [])]
    rows = connection.execute(sql, parameters).fetchall()
    if material_ids is not None and {row["id"] for row in rows} != set(material_ids):
        raise IntelligenceError("SOURCE_SCOPE_NOT_FOUND", 404)
    if len(rows) > MAX_SCOPE_MATERIALS:
        raise IntelligenceError("SEARCH_SCOPE_TOO_LARGE", 413, {"material_count": len(rows), "limit": MAX_SCOPE_MATERIALS})
    return rows, dict(work) if work is not None else None


def _score_passages(query_terms: list[str], passages: list[_Passage]) -> list[tuple[int, float]]:
    if not passages:
        return []
    query_frequency = Counter(query_terms)
    document_frequency: Counter[str] = Counter()
    document_lengths: list[int] = []
    term_frequencies: list[Counter[str]] = []
    total_length = 0
    for passage in passages:
        frequencies = Counter(_tokens(passage.text))
        length = sum(frequencies.values())
        term_frequencies.append(frequencies)
        document_lengths.append(length)
        total_length += length
        document_frequency.update(frequencies.keys() & query_frequency.keys())
    average_length = total_length / len(passages) if total_length else 1.0
    scores: list[tuple[int, float]] = []
    for index, frequencies in enumerate(term_frequencies):
        score = 0.0
        for term, query_count in query_frequency.items():
            frequency = frequencies.get(term, 0)
            if not frequency:
                continue
            document_count = document_frequency[term]
            inverse_frequency = math.log1p(
                (len(passages) - document_count + 0.5) / (document_count + 0.5)
            )
            normalizer = 1.2 * (
                1 - 0.75 + 0.75 * document_lengths[index] / average_length
            )
            score += query_count * inverse_frequency * frequency * 2.2 / (frequency + normalizer)
        if score > 0 and math.isfinite(score):
            scores.append((index, score))
    scores.sort(key=lambda item: (-item[1], passages[item[0]].material_id,
                                 passages[item[0]].segment_id, passages[item[0]].start_offset))
    return scores


def search_material_evidence(store, identity: dict, query: str, *, work_id: str | None = None,
                             material_ids: list[str] | None = None, limit: int = 12,
                             is_cancelled=None) -> dict:
    """Rank all authorized parser spans before limiting results; never label facts."""
    if (not isinstance(query, str) or not query.strip() or len(query) > MAX_QUERY_CHARACTERS
            or any(ord(character) < 32 and character not in "\t\n\r" for character in query)):
        raise IntelligenceError("SEARCH_QUERY_INVALID", 400)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_RESULTS:
        raise IntelligenceError("SEARCH_LIMIT_INVALID", 400)
    query_terms = list(dict.fromkeys(_tokens(query)))
    if not query_terms:
        raise IntelligenceError("SEARCH_QUERY_NO_TERMS", 400)
    if is_cancelled is not None and not callable(is_cancelled):
        raise IntelligenceError("SEARCH_CANCEL_CHECK_INVALID", 400)

    try:
        with store.transaction(immediate=False) as connection:
            workspace_id, _user_id, _session_id = _verify_read_scope(connection, identity)
            materials, work = _load_scope_materials(
                connection, workspace_id, work_id=work_id, material_ids=material_ids,
            )
            from .collaboration import require_work_access, require_material_access
            from .api import ApiError
            try:
                if work_id:
                    require_work_access(connection, identity, work_id)
                for material in materials:
                    require_material_access(connection, identity, material["id"], target_work_id=work_id)
            except ApiError as error:
                raise IntelligenceError("SOURCE_SCOPE_NOT_FOUND", 404) from error
            derived_candidates = _derive_material_query_candidates(materials, query_terms, limit)
            if work_id is not None and material_ids is None:
                preserved_candidates = _search_preserved_research_candidates(
                    connection, store, workspace_id, query_terms, limit, is_cancelled=is_cancelled,
                )
            else:
                preserved_candidates = {
                    "status": "not_in_scope", "results": [],
                    "coverage": {"complete_scope_scanned": False},
                    "limitations": ["Preserved historical candidate sets are searched only in the full current Work scope."],
                }
            has_candidate_results = bool(derived_candidates["results"] or preserved_candidates["results"])
            research_status = ("complete" if has_candidate_results else
                               "source_use_blocked" if preserved_candidates.get("status") == "source_use_blocked" else
                               "no_matches")
            research_candidates = {
                "status": research_status,
                "derived_from_current_materials": derived_candidates,
                "preserved_legacy": preserved_candidates,
            }
            if not materials:
                return {
                    "status": "no_data", "query": query, "work": work,
                    "scope": {"work_id": work_id, "material_ids": material_ids or []},
                    "coverage": {"authorized_materials": 0, "scanned_materials": 0,
                                 "scanned_segments": 0, "scored_passages": 0,
                                 "matched_passages": 0, "returned_passages": 0,
                                 "complete_scope_scanned": True},
                    "evidence": [], "citations": [], "research_candidates": research_candidates,
                    "limitations": ["No Materials are attached to this authorized scope."],
                }
            total_characters = sum(len(row["text"] or "") for row in materials)
            if total_characters > MAX_SCOPE_CHARACTERS:
                raise IntelligenceError(
                    "SEARCH_SCOPE_TOO_LARGE", 413,
                    {"material_count": len(materials), "character_count": total_characters,
                     "character_limit": MAX_SCOPE_CHARACTERS,
                     "partial_results_returned": False},
                )
            material_by_id = {row["id"]: row for row in materials}
            placeholders = ",".join("?" for _ in material_by_id)
            segment_rows = connection.execute(
                "SELECT material_id, id, label, text, start_offset, end_offset, kind "
                f"FROM material_segments WHERE workspace_id = ? AND material_id IN ({placeholders}) "
                "ORDER BY material_id, start_offset, id",
                [workspace_id, *material_by_id],
            ).fetchall()
            if len(segment_rows) > MAX_SCOPE_SEGMENTS:
                raise IntelligenceError(
                    "SEARCH_SCOPE_TOO_LARGE", 413,
                    {"material_count": len(materials), "segment_count": len(segment_rows),
                     "segment_limit": MAX_SCOPE_SEGMENTS,
                     "partial_results_returned": False},
                )

            source_summaries = []
            partial_materials = [row["id"] for row in materials if row["status"] == "partial"]
            unavailable_materials = [
                row["id"] for row in materials
                if row["status"] not in {"ready", "partial"}
                or not isinstance(row["text"], str) or not row["text"].strip()
            ]
            for row in materials:
                safe_url, source_meta = _safe_source_metadata(row)
                source_summaries.append({
                    "material_id": row["id"], "title": row["title"], "filename": row["filename"],
                    "format": row["format"], "status": row["status"], "reason": row["reason"],
                    "material_sha256": row["sha256"], "source_url": safe_url,
                    "source_metadata": source_meta,
                    "text_characters": len(row["text"] or ""),
                })

            passages: list[_Passage] = []
            locator_errors = 0
            skipped_statuses: Counter[str] = Counter()
            segments_by_material: Counter[str] = Counter()
            for index, segment in enumerate(segment_rows):
                if index % 256 == 0 and is_cancelled is not None:
                    try:
                        cancelled = is_cancelled()
                    except Exception as exc:
                        raise IntelligenceError("SEARCH_CANCEL_CHECK_FAILED", 503) from exc
                    if type(cancelled) is not bool:
                        raise IntelligenceError("SEARCH_CANCEL_CHECK_INVALID", 503)
                    if cancelled:
                        raise IntelligenceError("SEARCH_CANCELLED", 409)
                material = material_by_id[segment["material_id"]]
                status = material["status"]
                segments_by_material[segment["material_id"]] += 1
                if status not in {"ready", "partial"}:
                    skipped_statuses[status or "unknown"] += 1
                    continue
                body = material["text"]
                text = segment["text"]
                start = segment["start_offset"]
                end = segment["end_offset"]
                if (not isinstance(body, str) or not isinstance(text, str)
                        or isinstance(start, bool) or not isinstance(start, int)
                        or isinstance(end, bool) or not isinstance(end, int)
                        or start < 0 or end < start or end > len(body)
                        or body[start:end] != text):
                    locator_errors += 1
                    continue
                safe_url, source_meta = _safe_source_metadata(material)
                for local_start, local_end in _passage_ranges(text):
                    passages.append(_Passage(
                        material_id=material["id"], material_title=material["title"],
                        filename=material["filename"], material_sha256=material["sha256"],
                        source_url=safe_url, source_metadata=source_meta,
                        segment_id=segment["id"], segment_label=segment["label"],
                        segment_kind=segment["kind"], segment_text=text, material_text=body,
                        segment_start=start, segment_end=end,
                        window_start=local_start, window_end=local_end,
                        text=text[local_start:local_end],
                    ))
                    if len(passages) > MAX_SCOPE_SEGMENTS:
                        raise IntelligenceError(
                            "SEARCH_SCOPE_TOO_LARGE", 413,
                            {"material_count": len(materials), "passage_count": len(passages),
                             "passage_limit": MAX_SCOPE_SEGMENTS,
                             "partial_results_returned": False},
                        )

            scored = _score_passages(query_terms, passages)
            selected = scored[:limit]
            evidence = []
            citations = []
            for rank, (passage_index, score) in enumerate(selected, 1):
                passage = passages[passage_index]
                quote = passage.segment_text[passage.window_start:passage.window_end]
                evidence.append({
                    "rank": rank, "lexical_score": score,
                    "material_id": passage.material_id, "material_sha256": passage.material_sha256,
                    "title": passage.material_title, "filename": passage.filename,
                    "source_url": passage.source_url,
                    "source_metadata": passage.source_metadata,
                    "segment_id": passage.segment_id, "segment_label": passage.segment_label,
                    "segment_kind": passage.segment_kind,
                    "start_offset": passage.start_offset, "end_offset": passage.end_offset,
                    "offset_unit": "unicode_codepoint", "quote": quote,
                    "quote_sha256": hashlib.sha256(quote.encode("utf-8")).hexdigest(),
                    "evidence_state": "UNVERIFIED_SOURCE_TEXT",
                })
                citations.append({"material_id": passage.material_id,
                                  "segment_id": passage.segment_id, "quote": quote})

            for row in materials:
                if row["status"] in {"ready", "partial"} and (row["text"] or "") and not segments_by_material[row["id"]]:
                    locator_errors += 1
            complete_scan = not locator_errors and not unavailable_materials and not skipped_statuses
            limitations = [
                "Ranking is lexical BM25 over the full authorized scope; no embedding or reranker ran.",
                "A lexical match is a source passage, not a verified fact, claim verdict, or proof of support.",
                "Returned passages are limited by the requested result limit; all in-scope passages were scored first.",
            ]
            if partial_materials:
                limitations.append("Some Materials are only partially parsed; their passages do not establish complete source coverage.")
            if unavailable_materials:
                limitations.append("Some scoped Materials were unavailable and were not searched.")
            if locator_errors:
                limitations.append("Some saved parser segments failed exact offset readback and were excluded from evidence results.")
            if len(scored) > len(selected):
                limitations.append(f"{len(scored) - len(selected)} matching passages remain outside this response limit; request another scoped search to review them.")
            if research_candidates.get("status") == "source_use_blocked":
                limitations.append("Preserved historical keyword candidates exist, but their current research-use rights are unreviewed; none were returned.")
            if derived_candidates.get("results"):
                limitations.extend(derived_candidates.get("limitations", []))
            if preserved_candidates.get("status") in {"complete", "source_use_blocked", "integrity_blocked", "scope_too_large"}:
                limitations.extend(preserved_candidates.get("limitations", []))
            status = "complete" if complete_scan and not partial_materials else "partial"
            if not scored and complete_scan and not partial_materials:
                status = "no_matches"
            return {
                "status": status, "query": query, "work": work,
                "scope": {"work_id": work_id, "material_ids": material_ids or [row["id"] for row in materials]},
                "sources": source_summaries,
                "coverage": {
                    "authorized_materials": len(materials),
                    "scanned_materials": sum(
                        row["status"] in {"ready", "partial"}
                        and isinstance(row["text"], str) and bool(row["text"].strip())
                        for row in materials
                    ),
                    "scanned_segments": len(segment_rows) - sum(skipped_statuses.values()),
                    "locator_errors": locator_errors,
                    "scored_passages": len(passages),
                    "matched_passages": len(scored),
                    "returned_passages": len(evidence),
                    "complete_scope_scanned": complete_scan,
                    "unavailable_by_status": dict(skipped_statuses),
                },
                "evidence": evidence,
                "citations": citations,
                "research_candidates": research_candidates,
                "retrieval": {"method": "BM25", "semantic_retrieval_performed": False,
                              "reranking_performed": False, "cache_used": False},
                "limitations": limitations,
            }
    except IntelligenceError:
        raise
    except Exception as exc:
        raise IntelligenceError("INTELLIGENCE_SEARCH_UNAVAILABLE", 503) from exc
