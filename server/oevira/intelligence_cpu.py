"""Bounded CPU candidate fitting; no implicit admission, downloads or promotion."""
from __future__ import annotations

from array import array
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
import ctypes
import hashlib
import json
import math
import os
import random
import statistics
import time
import unicodedata
from typing import Callable

from .auth import utc_now
from .intelligence_base import IntelligenceError, identity_scope


CPU_RECIPE_VERSION = "oevira-cpu-recipes-20261030-r2"
CPU_FEATURE_REVISION = "oevira-cpu-features-20261030-r2"
CPU_HASH_DIMENSIONS = 1 << 16
CPU_SEED = 20261030
CPU_BATCH_SIZE = 16
CPU_L2_CANDIDATES = (0.0001, 0.001, 0.01)
CPU_EPOCH_CANDIDATES = (3, 10)
CPU_MAX_CONFIGURATIONS = 6
CPU_MAX_SECONDS = 1800
CPU_MAX_MEMORY_BYTES = 2 * 1024**3
CPU_MAX_EXAMPLES = 50_000
CPU_MAX_INPUT_BYTES = 64 * 1024**2
CPU_MAX_FEATURE_VALUE_CHARS = 200_000
_CANDIDATE_PACKAGE_SCHEMA = "oevira.cpu_candidate.v1"
_DATASET_ADMISSION_SEAL = object()
_EVALUATION_ADMISSION_SEAL = object()
_CANDIDATE_SEAL = object()
_FEATURE_MISSING = object()


TASK_SCHEMAS = {
    "evidence_ranking": {
        "model": "linear_pairwise_ranker",
        "text": ("query.text", "passage.text", "passage.title"),
        "categorical": ("pair.entity_match", "pair.period_match", "passage.source_kind", "passage.version_state"),
        "numeric": ("bm25", "character_jaccard", "entity_overlap", "period_overlap", "passage_char_length"),
        "binary": ("stable_entity_equal", "exact_passage_locator_present", "source_version_current"),
        "labels": (),
    },
    "identity_candidates": {
        "model": "multiclass_logistic",
        "text": ("left.name", "right.name", "left.alias", "right.alias", "left.city", "right.city", "left.address", "right.address"),
        "categorical": ("pair.relation_type", "pair.valid_period_state"),
        "numeric": ("name_similarity", "alias_similarity", "address_similarity", "valid_period_overlap"),
        "binary": ("stable_identifier_equal", "city_equal", "verified_relation_present"),
        "labels": ("same_identity", "related_only", "different_identity", "insufficient_evidence"),
    },
    "event_relationship": {
        "model": "multihead_logistic_and_pairwise_ranker",
        "text": ("anchor.text", "candidate.text"),
        "categorical": ("pair.entity_match", "pair.native_relation", "candidate.claim_type", "monitor.goal", "monitor.required_response_scope", "candidate.deadline_state", "candidate.impact_scope"),
        "numeric": ("text_similarity", "entity_overlap", "event_time_delta_seconds", "new_number_count", "known_deadline_seconds_remaining", "reviewed_impact_scope_count"),
        "binary": ("native_parent_equal", "native_reference_present", "native_correction_present", "source_check_completed", "current_authorization_valid"),
        "labels": (),
    },
    "professional_action_suggestion": {
        "model": "one_vs_rest_logistic",
        "text": ("request.text",),
        "categorical": ("object.kind", "selection.state", "action.semantic_token", "action.availability_state", "action.required_parameter_state"),
        "numeric": ("selection_object_count", "required_parameter_count", "missing_required_parameter_count"),
        "binary": ("explicit_user_action", "selection_present", "parent_version_current", "action_available"),
        "labels": (),
    },
}

EVENT_HEAD_LABELS = {
    "event_relation": ("same_event", "different_event", "explicitly_unresolved"),
    "change_attributes": ("new_fact", "correction", "retraction", "response_or_followup", "deadline_change", "material_number_change"),
    "severity": ("no_supported_harm", "limited_impact", "material_impact", "critical_impact", "explicitly_undetermined"),
    "credibility": ("supported", "contested", "unsupported", "insufficient_evidence"),
    "need_for_action": ("verification_needed", "review_options", "follow_up_needed"),
    "handling_value": (),
}


class CPUTrainingBlocked(Exception):
    def __init__(self, code: str, details: dict | None = None):
        super().__init__(code)
        self.code = code
        self.details = details or {}


@dataclass(frozen=True)
class TrainingUseDecision:
    allowed: bool
    workspace_id: str
    user_id: str
    purpose: str
    source_ref_sha256: str
    policy_version: str
    policy_sha256: str
    decision_id: str
    valid_until: str


@dataclass(frozen=True)
class LabelReviewDecision:
    approved: bool
    sample_id: str
    task: str
    label_schema_revision: str
    label_sha256: str
    reviewer_id: str
    review_id: str


@dataclass(frozen=True)
class TrainingExample:
    sample_id: str
    group_id: str
    partition: str
    features: dict
    label: object
    source_refs: tuple[dict, ...]
    label_schema_revision: str
    label_review_id: str
    source_use_decision_id: str


@dataclass(frozen=True)
class AdmittedTrainingDataset:
    task: str
    head: str | None
    label_space: tuple[str, ...]
    training_examples: tuple[TrainingExample, ...]
    development_examples: tuple[TrainingExample, ...]
    manifest_sha256: str
    recipe_version: str
    feature_revision: str
    _issuer: object
    _revalidate: Callable[[], None]


@dataclass(frozen=True)
class AdmittedEvaluationDataset:
    task: str
    head: str | None
    label_space: tuple[str, ...]
    examples: tuple[TrainingExample, ...]
    manifest_sha256: str
    training_manifest_sha256: str
    development_manifest_sha256: str
    _issuer: object
    _revalidate: Callable[[], None]


@dataclass(frozen=True)
class CPUResourceBudget:
    workers: int
    threads: int
    peak_bytes: int
    available_physical_bytes: int
    logical_processors: int
    physical_cores: int | None
    observed_at: str
    max_seconds: int = CPU_MAX_SECONDS


@dataclass(frozen=True)
class NumericFeatureStats:
    mean: float | None
    scale: float | None
    available: bool


@dataclass(frozen=True)
class CPUModelCandidate:
    task: str
    head: str | None
    model: str
    labels: tuple[str, ...]
    recipe_version: str
    feature_revision: str
    weights: tuple[tuple[tuple[int, float], ...], ...]
    intercepts: tuple[float, ...]
    numeric_stats: tuple[tuple[str, float | None, float | None, bool], ...]
    selected_config: tuple[float, int]
    decision_thresholds: tuple[float, ...]
    development_metrics: dict
    training_lineage: tuple[dict, ...]
    development_lineage: tuple[dict, ...]
    training_manifest_sha256: str
    development_manifest_sha256: str
    package_sha256: str
    candidate_state: str = "DEVELOPMENT_SELECTED_NOT_PROMOTED"
    evaluation_state: str = "NOT_RUN"
    promotion_state: str = "NOT_PROMOTED"
    _issuer: object = field(default=None, repr=False, compare=False)


def read_cpu_resource_budget() -> CPUResourceBudget:
    """Read a fresh local CPU/RAM budget without stopping or inspecting user processes."""
    logical = os.cpu_count() or 1
    available = None
    if os.name == "nt":
        class MemoryStatus(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        status = MemoryStatus()
        status.dwLength = ctypes.sizeof(MemoryStatus)
        try:
            kernel32 = ctypes.WinDLL("Kernel32", use_last_error=True)
            if kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                available = int(status.ullAvailPhys)
        except (AttributeError, OSError):
            available = None
    elif hasattr(os, "sysconf"):
        try:
            available = int(os.sysconf("SC_AVPHYS_PAGES") * os.sysconf("SC_PAGE_SIZE"))
        except (OSError, ValueError, TypeError):
            available = None
    if type(available) is not int or available <= 0:
        raise CPUTrainingBlocked("CPU_RESOURCE_READBACK_UNAVAILABLE")
    # One worker and one thread are a conservative subset of the reviewed
    # recipe even where Python cannot independently verify physical-core count.
    peak_bytes = min(CPU_MAX_MEMORY_BYTES, available // 4)
    if peak_bytes < 64 * 1024**2:
        raise CPUTrainingBlocked("CPU_RESOURCE_BUDGET_INSUFFICIENT", {"peak_bytes": peak_bytes})
    return CPUResourceBudget(
        workers=1, threads=1, peak_bytes=peak_bytes,
        available_physical_bytes=available, logical_processors=logical,
        physical_cores=None, observed_at=datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
    )


def cpu_candidate_status() -> dict:
    """Report code/data/runtime status without probing, loading, fitting or selecting a model."""
    try:
        budget = read_cpu_resource_budget()
        budget_status = {
            "state": "OBSERVED", "available_physical_bytes": budget.available_physical_bytes,
            "peak_bytes": budget.peak_bytes, "workers": budget.workers, "threads": budget.threads,
            "observed_at": budget.observed_at,
        }
    except CPUTrainingBlocked as exc:
        budget_status = {"state": "UNAVAILABLE", "reason": exc.code}
    return {
        "recipe_version": CPU_RECIPE_VERSION,
        "feature_revision": CPU_FEATURE_REVISION,
        "trainer_state": "IMPLEMENTED_NOT_RUN",
        "data_admission_state": "NOT_READY",
        "fitting_performed": False,
        "model_package": None,
        "promotion_state": "NOT_PROMOTED",
        "fallback": "existing lexical/owner operations remain available",
        "resource_budget": budget_status,
    }


def _canonical_json(value) -> bytes:
    try:
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise CPUTrainingBlocked("CPU_TRAINING_INPUT_NOT_SERIALIZABLE") from exc


def _sha256(value) -> str:
    return hashlib.sha256(value).hexdigest()


def _example_label_digest(label) -> str:
    return _sha256(_canonical_json(label))


def _purpose_for_partition(partition: str) -> str:
    return {
        "train": "parameter_training",
        "development": "development_calibration",
        "final": "independent_evaluation",
    }[partition]


def _validate_source_reference(connection, workspace_id: str, reference: dict) -> None:
    if not isinstance(reference, dict) or not isinstance(reference.get("kind"), str):
        raise CPUTrainingBlocked("TRAINING_SOURCE_REFERENCE_INVALID")
    if reference["kind"] == "material_segment":
        allowed = {"kind", "material_id", "material_sha256", "segment_id", "quote"}
        if set(reference) != allowed:
            raise CPUTrainingBlocked("TRAINING_SOURCE_REFERENCE_INVALID")
        material_id = reference.get("material_id")
        segment_id = reference.get("segment_id")
        quote = reference.get("quote")
        if (not isinstance(material_id, str) or not material_id
                or not isinstance(segment_id, str) or not segment_id
                or not re_full_sha256(reference.get("material_sha256"))):
            raise CPUTrainingBlocked("TRAINING_SOURCE_REFERENCE_INVALID")
        material = connection.execute(
            "SELECT sha256, status FROM materials WHERE id = ? AND workspace_id = ?",
            (material_id, workspace_id),
        ).fetchone()
        segment = connection.execute(
            "SELECT text FROM material_segments WHERE material_id = ? AND id = ? AND workspace_id = ?",
            (material_id, segment_id, workspace_id),
        ).fetchone()
        if (material is None or segment is None or material["sha256"] != reference.get("material_sha256")
                or material["status"] not in {"ready", "partial"}
                or not isinstance(quote, str) or not quote or quote not in segment["text"]):
            raise CPUTrainingBlocked("TRAINING_SOURCE_VERSION_INVALID")
        return
    if reference["kind"] == "artifact_version":
        allowed = {"kind", "artifact_id", "version", "body_sha256", "quote"}
        if set(reference) != allowed:
            raise CPUTrainingBlocked("TRAINING_SOURCE_REFERENCE_INVALID")
        artifact_id, version = reference.get("artifact_id"), reference.get("version")
        quote = reference.get("quote")
        if (not isinstance(artifact_id, str) or not artifact_id or isinstance(version, bool)
                or not isinstance(version, int) or version < 1):
            raise CPUTrainingBlocked("TRAINING_SOURCE_REFERENCE_INVALID")
        if not re_full_sha256(reference.get("body_sha256")):
            raise CPUTrainingBlocked("TRAINING_SOURCE_REFERENCE_INVALID")
        row = connection.execute(
            "SELECT v.body FROM artifacts a JOIN artifact_versions v ON v.artifact_id = a.id "
            "AND v.workspace_id = a.workspace_id WHERE a.id = ? AND a.workspace_id = ? AND v.version = ?",
            (artifact_id, workspace_id, version),
        ).fetchone()
        if row is None or not isinstance(quote, str) or not quote or quote not in row["body"]:
            raise CPUTrainingBlocked("TRAINING_SOURCE_VERSION_INVALID")
        if _sha256(row["body"].encode("utf-8")) != reference.get("body_sha256"):
            raise CPUTrainingBlocked("TRAINING_SOURCE_VERSION_INVALID")
        return
    raise CPUTrainingBlocked("TRAINING_SOURCE_REFERENCE_KIND_UNSUPPORTED")


def _validate_label_shape(task: str, head: str | None, label) -> None:
    if task == "evidence_ranking" or (task == "event_relationship" and head == "handling_value"):
        if (not isinstance(label, dict) or set(label) != {"preferred_candidate", "other_candidate"}
                or any(not isinstance(label[key], str) or not label[key] for key in label)):
            raise CPUTrainingBlocked("TRAINING_PREFERENCE_LABEL_INVALID")
        if label["preferred_candidate"] == label["other_candidate"]:
            raise CPUTrainingBlocked("TRAINING_PREFERENCE_PAIR_INVALID")
        return
    if task == "identity_candidates":
        if label not in TASK_SCHEMAS[task]["labels"]:
            raise CPUTrainingBlocked("TRAINING_IDENTITY_LABEL_INVALID")
        return
    if task == "event_relationship":
        if head == "event_relation" and label not in EVENT_HEAD_LABELS[head]:
            raise CPUTrainingBlocked("TRAINING_EVENT_RELATION_LABEL_INVALID")
        if head == "severity" and label not in EVENT_HEAD_LABELS[head]:
            raise CPUTrainingBlocked("TRAINING_EVENT_SEVERITY_LABEL_INVALID")
        if head == "credibility" and label not in EVENT_HEAD_LABELS[head]:
            raise CPUTrainingBlocked("TRAINING_EVENT_CREDIBILITY_LABEL_INVALID")
        if head in {"change_attributes", "need_for_action"}:
            if not isinstance(label, dict) or any(
                key not in EVENT_HEAD_LABELS[head]
                or (value is not None and type(value) is not bool)
                for key, value in label.items()
            ):
                raise CPUTrainingBlocked("TRAINING_EVENT_MULTILABEL_INVALID")
            return
        if head != "handling_value":
            if head not in EVENT_HEAD_LABELS:
                raise CPUTrainingBlocked("TRAINING_EVENT_HEAD_INVALID")
            if label not in EVENT_HEAD_LABELS[head]:
                raise CPUTrainingBlocked("TRAINING_EVENT_LABEL_INVALID")
        return
    if task == "professional_action_suggestion":
        if not isinstance(label, dict) or not label or any(
            not isinstance(action_id, str) or not action_id
            or (value is not None and type(value) is not bool)
            for action_id, value in label.items()
        ):
            raise CPUTrainingBlocked("TRAINING_ACTION_LABEL_INVALID")
        return
    raise CPUTrainingBlocked("CPU_TRAINING_TASK_UNSUPPORTED")


class TrainingDataAdmission:
    """Build a fitting view only from current SourceUse and independent review decisions."""

    def __init__(self, store, *, source_use_resolver=None, label_review_resolver=None,
                 available_action_ids: set[str] | None = None):
        self.store = store
        self.source_use_resolver = source_use_resolver
        self.label_review_resolver = label_review_resolver
        self.available_action_ids = frozenset(available_action_ids or ())

    def admit(self, identity: dict, task: str, examples: list[dict], *, head: str | None = None,
              evaluation_candidate: CPUModelCandidate | None = None,
              is_cancelled: Callable[[], bool] | None = None
              ) -> AdmittedTrainingDataset | AdmittedEvaluationDataset:
        evaluation_only = evaluation_candidate is not None
        if not isinstance(task, str) or task not in TASK_SCHEMAS:
            raise CPUTrainingBlocked("CPU_TRAINING_TASK_UNSUPPORTED")
        if task == "event_relationship" and (not isinstance(head, str) or head not in EVENT_HEAD_LABELS):
            raise CPUTrainingBlocked("TRAINING_EVENT_HEAD_INVALID")
        if task != "event_relationship" and head is not None:
            raise CPUTrainingBlocked("CPU_TRAINING_HEAD_INVALID")
        if evaluation_only:
            _verify_candidate_for_evaluation(evaluation_candidate, task=task, head=head)
        if not isinstance(examples, list) or not examples or len(examples) > CPU_MAX_EXAMPLES:
            raise CPUTrainingBlocked("TRAINING_DATASET_INVALID")
        if not callable(self.source_use_resolver):
            raise CPUTrainingBlocked("TRAINING_SOURCE_USE_OWNER_UNAVAILABLE")
        if not callable(self.label_review_resolver):
            raise CPUTrainingBlocked("TRAINING_LABEL_REVIEW_OWNER_UNAVAILABLE")
        if task == "professional_action_suggestion" and not self.available_action_ids:
            raise CPUTrainingBlocked("TRAINING_ACTION_CONTRACT_UNAVAILABLE")
        if task == "identity_candidates":
            label_space = TASK_SCHEMAS[task]["labels"]
        elif task == "event_relationship":
            label_space = EVENT_HEAD_LABELS[head]
        elif task == "professional_action_suggestion":
            label_space = tuple(sorted(self.available_action_ids))
        else:
            label_space = ()
        if is_cancelled is not None and not callable(is_cancelled):
            raise CPUTrainingBlocked("CPU_TRAINING_CANCEL_CHECK_INVALID")

        workspace_id, user_id, session_id = identity_scope(identity)
        admitted: list[TrainingExample] = []
        groups: dict[str, set[str]] = defaultdict(set)
        total_input_bytes = 0
        decisions: list[tuple[str, str, tuple[dict, ...], object, str,
                              TrainingUseDecision, LabelReviewDecision]] = []
        seen_ids: set[str] = set()
        with self.store.transaction(immediate=False) as connection:
            member = connection.execute(
                "SELECT active, role FROM memberships WHERE workspace_id = ? AND user_id = ?",
                (workspace_id, user_id),
            ).fetchone()
            if member is None or member["active"] != 1:
                raise CPUTrainingBlocked("TRAINING_ACTOR_ACCESS_REVOKED")
            if member["role"] not in {"owner", "editor"}:
                raise CPUTrainingBlocked("TRAINING_WRITE_ACCESS_DENIED")
            if session_id is not None:
                from .auth import utc_now
                active_session = connection.execute(
                    "SELECT revoked_at, expires_at FROM sessions WHERE id = ? AND workspace_id = ? AND user_id = ?",
                    (session_id, workspace_id, user_id),
                ).fetchone()
                if (active_session is None or active_session["revoked_at"] is not None
                        or active_session["expires_at"] <= utc_now()):
                    raise CPUTrainingBlocked("TRAINING_ACTOR_SESSION_REVOKED")

            for index, example in enumerate(examples):
                if index % 32 == 0 and is_cancelled is not None:
                    try:
                        cancelled = is_cancelled()
                    except Exception as exc:
                        raise CPUTrainingBlocked("CPU_TRAINING_CANCEL_CHECK_FAILED") from exc
                    if type(cancelled) is not bool:
                        raise CPUTrainingBlocked("CPU_TRAINING_CANCEL_CHECK_INVALID")
                    if cancelled:
                        raise CPUTrainingBlocked("CPU_TRAINING_CANCELLED")
                if not isinstance(example, dict) or set(example) != {
                    "sample_id", "group_id", "partition", "features", "label",
                    "source_refs", "label_schema_revision", "created_by", "synthetic",
                }:
                    raise CPUTrainingBlocked("TRAINING_EXAMPLE_SCHEMA_INVALID")
                sample_id = example.get("sample_id")
                group_id = example.get("group_id")
                partition = example.get("partition")
                created_by = example.get("created_by")
                if (not isinstance(sample_id, str) or not sample_id or len(sample_id) > 256
                        or sample_id in seen_ids or not isinstance(group_id, str) or not group_id
                        or len(group_id) > 512 or not isinstance(partition, str)
                        or partition not in {"train", "development", "final"}
                        or not isinstance(created_by, str) or not created_by
                        or type(example.get("synthetic")) is not bool):
                    raise CPUTrainingBlocked("TRAINING_EXAMPLE_IDENTITY_INVALID")
                if evaluation_only and partition != "final":
                    raise CPUTrainingBlocked("EVALUATION_FINAL_PARTITION_REQUIRED")
                if not evaluation_only and partition == "final":
                    raise CPUTrainingBlocked("TRAINING_FINAL_SET_MUST_BE_ADMITTED_SEPARATELY")
                if example["synthetic"]:
                    raise CPUTrainingBlocked("TRAINING_SYNTHETIC_SAMPLE_NOT_ADMITTED")
                if not isinstance(example["label_schema_revision"], str) or not example["label_schema_revision"]:
                    raise CPUTrainingBlocked("TRAINING_LABEL_SCHEMA_REQUIRED")
                features = example.get("features")
                if not isinstance(features, dict):
                    raise CPUTrainingBlocked("TRAINING_FEATURES_INVALID")
                _validate_task_feature_fields(task, features, head=head)
                _validate_label_shape(task, head, example["label"])
                if task == "professional_action_suggestion" and not set(example["label"]).issubset(self.available_action_ids):
                    raise CPUTrainingBlocked("TRAINING_ACTION_CONTRACT_MISMATCH")
                if task == "identity_candidates" and example["label"] not in label_space:
                    raise CPUTrainingBlocked("TRAINING_IDENTITY_LABEL_INVALID")
                if task == "event_relationship" and head in {"event_relation", "severity", "credibility"} and example["label"] not in label_space:
                    raise CPUTrainingBlocked("TRAINING_EVENT_LABEL_INVALID")
                if task == "event_relationship" and head in {"change_attributes", "need_for_action"} and not set(example["label"]).issubset(label_space):
                    raise CPUTrainingBlocked("TRAINING_EVENT_MULTILABEL_INVALID")
                refs = example.get("source_refs")
                if not isinstance(refs, list) or not refs or len(refs) > 256:
                    raise CPUTrainingBlocked("TRAINING_SOURCE_REFERENCE_REQUIRED")
                for reference in refs:
                    _validate_source_reference(connection, workspace_id, reference)

                source_digest = _sha256(_canonical_json(refs))
                label_digest = _example_label_digest(example["label"])
                try:
                    source_decision = self.source_use_resolver(
                        identity, tuple(refs), purpose=_purpose_for_partition(partition), task=task,
                    )
                    review = self.label_review_resolver(
                        identity, sample_id=sample_id, task=task, head=head,
                        label=example["label"], label_schema_revision=example["label_schema_revision"],
                        label_sha256=label_digest, source_ref_sha256=source_digest,
                    )
                except Exception as exc:
                    raise CPUTrainingBlocked("TRAINING_ADMISSION_UNAVAILABLE") from exc
                if (type(source_decision) is not TrainingUseDecision or source_decision.allowed is not True
                        or source_decision.workspace_id != workspace_id or source_decision.user_id != user_id
                        or source_decision.purpose != _purpose_for_partition(partition)
                        or source_decision.source_ref_sha256 != source_digest
                        or not source_decision.decision_id or not source_decision.policy_version
                        or not re_full_sha256(source_decision.policy_sha256)
                        or _expires_in_future(source_decision.valid_until) is not True):
                    raise CPUTrainingBlocked("TRAINING_USE_NOT_ADMITTED", {"sample_id": sample_id})
                if (type(review) is not LabelReviewDecision or review.approved is not True
                        or review.sample_id != sample_id or review.task != task
                        or review.label_schema_revision != example["label_schema_revision"]
                        or review.label_sha256 != label_digest or not review.review_id
                        or not isinstance(review.reviewer_id, str) or review.reviewer_id == created_by):
                    raise CPUTrainingBlocked("TRAINING_LABEL_NOT_INDEPENDENTLY_REVIEWED", {"sample_id": sample_id})
                safe_features = json.loads(_canonical_json(features))
                safe_label = json.loads(_canonical_json(example["label"]))
                safe_refs = tuple(json.loads(_canonical_json(refs)))
                total_input_bytes += len(_canonical_json({
                    "features": safe_features, "label": safe_label, "source_refs": safe_refs,
                }))
                if total_input_bytes > CPU_MAX_INPUT_BYTES:
                    raise CPUTrainingBlocked("TRAINING_DATASET_TOO_LARGE")
                seen_ids.add(sample_id)
                groups[group_id].add(partition)
                admitted.append(TrainingExample(
                    sample_id=sample_id, group_id=group_id, partition=partition,
                    features=safe_features, label=safe_label,
                    source_refs=safe_refs,
                    label_schema_revision=example["label_schema_revision"],
                    label_review_id=review.review_id, source_use_decision_id=source_decision.decision_id,
                ))
                decisions.append((
                    sample_id, partition, safe_refs, safe_label,
                    example["label_schema_revision"], source_decision, review,
                ))

        cross_split_groups = [group for group, partitions in groups.items() if len(partitions) > 1]
        if cross_split_groups:
            code = "EVALUATION_GROUP_DUPLICATE" if evaluation_only else "TRAINING_GROUP_LEAKAGE"
            raise CPUTrainingBlocked(code, {"group_count": len(cross_split_groups)})
        if evaluation_only:
            final_examples = tuple(sample for sample in admitted if sample.partition == "final")
            if not final_examples:
                raise CPUTrainingBlocked("EVALUATION_FINAL_VIEW_EMPTY")
            candidate_ids = {
                item["sample_id"] for item in (
                    evaluation_candidate.training_lineage + evaluation_candidate.development_lineage
                )
            }
            candidate_groups = {
                item["group_id"] for item in (
                    evaluation_candidate.training_lineage + evaluation_candidate.development_lineage
                )
            }
            final_ids = {sample.sample_id for sample in final_examples}
            final_groups = {sample.group_id for sample in final_examples}
            if final_ids & candidate_ids or final_groups & candidate_groups:
                raise CPUTrainingBlocked("EVALUATION_FINAL_SET_LEAKAGE")
            manifest_sha = _admitted_manifest_digest(task, head, final_examples)

            def revalidate_evaluation() -> None:
                _revalidate_training_admission(
                    store=self.store, identity=identity, workspace_id=workspace_id, user_id=user_id,
                    session_id=session_id, decisions=decisions, source_use_resolver=self.source_use_resolver,
                    label_review_resolver=self.label_review_resolver, task=task, head=head,
                )
                _verify_candidate_for_evaluation(evaluation_candidate, task=task, head=head)

            return AdmittedEvaluationDataset(
                task=task, head=head, label_space=label_space, examples=final_examples,
                manifest_sha256=manifest_sha,
                training_manifest_sha256=evaluation_candidate.training_manifest_sha256,
                development_manifest_sha256=evaluation_candidate.development_manifest_sha256,
                _issuer=_EVALUATION_ADMISSION_SEAL, _revalidate=revalidate_evaluation,
            )
        if not any(example.partition == "train" for example in admitted):
            raise CPUTrainingBlocked("TRAINING_VIEW_EMPTY")
        if not any(example.partition == "development" for example in admitted):
            raise CPUTrainingBlocked("TRAINING_DEVELOPMENT_VIEW_REQUIRED")

        training_examples = tuple(sample for sample in admitted if sample.partition == "train")
        development_examples = tuple(sample for sample in admitted if sample.partition == "development")
        if not training_examples or not development_examples:
            raise CPUTrainingBlocked("TRAINING_REQUIRED_VIEW_MISSING")
        admitted_examples = training_examples + development_examples
        manifest_sha = _admitted_manifest_digest(task, head, admitted_examples)

        def revalidate() -> None:
            _revalidate_training_admission(
                store=self.store, identity=identity, workspace_id=workspace_id, user_id=user_id,
                session_id=session_id, decisions=decisions, source_use_resolver=self.source_use_resolver,
                label_review_resolver=self.label_review_resolver, task=task, head=head,
            )

        return AdmittedTrainingDataset(
            task=task, head=head, label_space=label_space,
            training_examples=training_examples, development_examples=development_examples,
            manifest_sha256=manifest_sha,
            recipe_version=CPU_RECIPE_VERSION, feature_revision=CPU_FEATURE_REVISION,
            _issuer=_DATASET_ADMISSION_SEAL, _revalidate=revalidate,
        )


def _revalidate_training_admission(*, store, identity, workspace_id, user_id, session_id,
                                   decisions, source_use_resolver, label_review_resolver,
                                   task, head) -> None:
    if not callable(source_use_resolver) or not callable(label_review_resolver):
        raise CPUTrainingBlocked("TRAINING_ADMISSION_OWNER_UNAVAILABLE")
    with store.transaction(immediate=False) as connection:
        member = connection.execute(
            "SELECT active, role FROM memberships WHERE workspace_id = ? AND user_id = ?",
            (workspace_id, user_id),
        ).fetchone()
        if member is None or member["active"] != 1 or member["role"] not in {"owner", "editor"}:
            raise CPUTrainingBlocked("TRAINING_ACTOR_ACCESS_REVOKED")
        if session_id is not None:
            from .auth import utc_now
            active_session = connection.execute(
                "SELECT revoked_at, expires_at FROM sessions WHERE id = ? AND workspace_id = ? AND user_id = ?",
                (session_id, workspace_id, user_id),
            ).fetchone()
            if (active_session is None or active_session["revoked_at"] is not None
                    or active_session["expires_at"] <= utc_now()):
                raise CPUTrainingBlocked("TRAINING_ACTOR_SESSION_REVOKED")
        for _sample_id, _partition, refs, _label, _label_revision, _source_decision, _review in decisions:
            for reference in refs:
                _validate_source_reference(connection, workspace_id, reference)
    for sample_id, partition, refs, label, label_revision, prior_source_decision, prior_review in decisions:
        source_digest = _sha256(_canonical_json(refs))
        new_source_decision = source_use_resolver(
            identity, refs, purpose=_purpose_for_partition(partition), task=task,
        )
        if (type(new_source_decision) is not TrainingUseDecision
                or new_source_decision.allowed is not True
                or new_source_decision.decision_id != prior_source_decision.decision_id
                or new_source_decision.workspace_id != workspace_id
                or new_source_decision.user_id != user_id
                or new_source_decision.purpose != _purpose_for_partition(partition)
                or new_source_decision.source_ref_sha256 != source_digest
                or new_source_decision.policy_version != prior_source_decision.policy_version
                or new_source_decision.policy_sha256 != prior_source_decision.policy_sha256
                or _expires_in_future(new_source_decision.valid_until) is not True):
            raise CPUTrainingBlocked("TRAINING_USE_REVOKED")
        label_sha = _example_label_digest(label)
        new_review = label_review_resolver(
            identity, sample_id=sample_id, task=task, head=head,
            label=label, label_schema_revision=label_revision,
            label_sha256=label_sha, source_ref_sha256=source_digest,
        )
        if (type(new_review) is not LabelReviewDecision or new_review.approved is not True
                or new_review.review_id != prior_review.review_id):
            raise CPUTrainingBlocked("TRAINING_LABEL_REVIEW_REVOKED")


def re_full_sha256(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def _expires_in_future(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed > datetime.now(timezone.utc)


def _validate_task_feature_fields(task: str, features: dict, *, head: str | None = None) -> None:
    schema = TASK_SCHEMAS[task]
    allowed = set(schema["text"]) | set(schema["categorical"]) | set(schema["numeric"]) | set(schema["binary"])
    pairwise = task == "evidence_ranking" or (task == "event_relationship" and head == "handling_value")
    candidates = ("preferred", "other") if pairwise else (None,)
    if not isinstance(features, dict):
        raise CPUTrainingBlocked("TRAINING_FEATURES_INVALID")
    if pairwise and set(features) != {"preferred", "other"}:
        raise CPUTrainingBlocked("TRAINING_PREFERENCE_PAIR_INVALID")
    for candidate in candidates:
        value = features.get(candidate) if candidate is not None else features
        if not isinstance(value, dict) or set(value) - allowed:
            raise CPUTrainingBlocked("TRAINING_FEATURE_SCHEMA_INVALID")
        if candidate is not None and candidate not in features:
            raise CPUTrainingBlocked("TRAINING_PREFERENCE_PAIR_INVALID")
        for key in schema["text"] + schema["categorical"]:
            item = value.get(key, _FEATURE_MISSING)
            if item is not _FEATURE_MISSING and item is not None and not isinstance(item, (str, bool)):
                raise CPUTrainingBlocked("TRAINING_TEXT_FEATURE_INVALID")
            if isinstance(item, str) and len(item) > CPU_MAX_FEATURE_VALUE_CHARS:
                raise CPUTrainingBlocked("TRAINING_FEATURE_VALUE_TOO_LARGE")
        for key in schema["numeric"]:
            item = value.get(key, _FEATURE_MISSING)
            if item is _FEATURE_MISSING or item is None:
                continue
            if type(item) not in (int, float) or not math.isfinite(item):
                raise CPUTrainingBlocked("TRAINING_NUMERIC_FEATURE_INVALID")
        for key in schema["binary"]:
            item = value.get(key, _FEATURE_MISSING)
            if item is not _FEATURE_MISSING and item is not None and type(item) is not bool:
                raise CPUTrainingBlocked("TRAINING_BINARY_FEATURE_INVALID")


def _feature_dimension(task: str) -> int:
    schema = TASK_SCHEMAS[task]
    return CPU_HASH_DIMENSIONS + 2 * len(schema["numeric"]) + 3 * len(schema["binary"])


def _feature_bucket(task: str, field: str, token_type: str, token: str) -> int:
    payload = _canonical_json([CPU_FEATURE_REVISION, task, field, token_type, token])
    return int.from_bytes(hashlib.sha256(payload).digest()[:8], "little") % CPU_HASH_DIMENSIONS


def _character_ngrams(value: str) -> Counter[str]:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    counts: Counter[str] = Counter()
    for size in (2, 3, 4):
        counts.update(normalized[index:index + size] for index in range(max(0, len(normalized) - size + 1)))
    return counts


def _numeric_stats(task: str, training_examples: list[TrainingExample], *, head: str | None) -> dict[str, NumericFeatureStats]:
    schema = TASK_SCHEMAS[task]
    values: dict[str, list[float]] = {name: [] for name in schema["numeric"]}
    for sample in training_examples:
        feature_sets = (sample.features["preferred"], sample.features["other"]) if (
            task == "evidence_ranking" or (task == "event_relationship" and head == "handling_value")
        ) else (sample.features,)
        for feature_set in feature_sets:
            for name in schema["numeric"]:
                value = feature_set.get(name)
                if value is not None:
                    if type(value) not in (int, float) or not math.isfinite(value):
                        raise CPUTrainingBlocked("TRAINING_NUMERIC_FEATURE_INVALID")
                    values[name].append(float(value))
    result = {}
    for name, observed in values.items():
        if not observed:
            result[name] = NumericFeatureStats(None, None, False)
            continue
        mean = statistics.fmean(observed)
        variance = math.fsum((value - mean) ** 2 for value in observed) / len(observed)
        scale = math.sqrt(variance) or 1.0
        if not math.isfinite(mean) or not math.isfinite(scale):
            raise CPUTrainingBlocked("TRAINING_NUMERIC_STATS_INVALID")
        result[name] = NumericFeatureStats(mean, scale, True)
    return result


def _one_text_field(task: str, field: str, value, output: dict[int, float]) -> int:
    if value is _FEATURE_MISSING or value is None:
        output[_feature_bucket(task, field, "missing", "<MISSING>")] += 1.0
        return 0
    text = str(value) if isinstance(value, bool) else value
    if not text:
        output[_feature_bucket(task, field, "empty", "<EMPTY>")] += 1.0
        return 0
    counts = _character_ngrams(text)
    block: dict[int, float] = defaultdict(float)
    unique_buckets: set[int] = set()
    for token, count in counts.items():
        bucket = _feature_bucket(task, field, "ngram", token)
        unique_buckets.add(bucket)
        block[bucket] += 1.0 + math.log(count)
    norm = math.sqrt(math.fsum(value * value for value in block.values()))
    if norm:
        for bucket, value in block.items():
            output[bucket] += value / norm
    return max(0, len(counts) - len(unique_buckets))


def _encode_single_features(
    task: str,
    values: dict,
    stats: dict[str, NumericFeatureStats],
    *,
    collision_counter: list[int] | None = None,
) -> dict[int, float]:
    schema = TASK_SCHEMAS[task]
    dimension = _feature_dimension(task)
    result: dict[int, float] = defaultdict(float)
    for field in schema["text"]:
        collisions = _one_text_field(task, field, values.get(field, _FEATURE_MISSING), result)
        if collision_counter is not None:
            collision_counter[0] += collisions
    for field in schema["categorical"]:
        value = values.get(field, _FEATURE_MISSING)
        if value is _FEATURE_MISSING or value is None:
            token_type, token = "missing", "<MISSING>"
        elif isinstance(value, bool):
            token_type, token = "categorical", "true" if value else "false"
        elif isinstance(value, str):
            token_type, token = ("empty", "<EMPTY>") if not value else ("categorical", value)
        else:
            raise CPUTrainingBlocked("TRAINING_CATEGORICAL_FEATURE_INVALID")
        result[_feature_bucket(task, field, token_type, token)] += 1.0

    numeric_offset = CPU_HASH_DIMENSIONS
    numeric_fields = schema["numeric"]
    for index, field in enumerate(numeric_fields):
        value = values.get(field, _FEATURE_MISSING)
        stat = stats.get(field)
        if stat is None or not stat.available or stat.mean is None or stat.scale is None:
            if value is not _FEATURE_MISSING and value is not None:
                raise CPUTrainingBlocked("TRAINING_NUMERIC_FEATURE_NOT_AVAILABLE", {"field": field})
            normalized = 0.0
            missing = 1.0
        elif value is _FEATURE_MISSING or value is None:
            normalized = 0.0
            missing = 1.0
        else:
            if type(value) not in (int, float) or not math.isfinite(value):
                raise CPUTrainingBlocked("TRAINING_NUMERIC_FEATURE_INVALID", {"field": field})
            normalized = (float(value) - stat.mean) / stat.scale
            if not math.isfinite(normalized):
                raise CPUTrainingBlocked("TRAINING_NUMERIC_FEATURE_INVALID", {"field": field})
            missing = 0.0
        if normalized:
            result[numeric_offset + index * 2] = normalized
        if missing:
            result[numeric_offset + index * 2 + 1] = 1.0

    binary_offset = numeric_offset + 2 * len(numeric_fields)
    for index, field in enumerate(schema["binary"]):
        value = values.get(field, _FEATURE_MISSING)
        base = binary_offset + index * 3
        if value is _FEATURE_MISSING or value is None:
            result[base + 2] = 1.0
        else:
            if type(value) is not bool:
                raise CPUTrainingBlocked("TRAINING_BINARY_FEATURE_INVALID", {"field": field})
            result[base + (1 if value else 0)] = 1.0
    return {index: value for index, value in result.items() if value}


def encode_task_features(
    task: str, values: dict, stats: dict[str, NumericFeatureStats] | None = None,
    *, head: str | None = None, collision_counter: list[int] | None = None,
):
    """Encode the pinned sparse feature contract; IDs never become feature tokens."""
    if not isinstance(task, str) or task not in TASK_SCHEMAS:
        raise CPUTrainingBlocked("CPU_TRAINING_TASK_UNSUPPORTED")
    _validate_task_feature_fields(task, values, head=head)
    if stats is None:
        stats = {name: NumericFeatureStats(None, None, False) for name in TASK_SCHEMAS[task]["numeric"]}
    pairwise = task == "evidence_ranking" or (task == "event_relationship" and head == "handling_value")
    if pairwise:
        return (
            _encode_single_features(task, values["preferred"], stats, collision_counter=collision_counter),
            _encode_single_features(task, values["other"], stats, collision_counter=collision_counter),
        )
    return _encode_single_features(task, values, stats, collision_counter=collision_counter)


def _numeric_stats_for_examples(task: str, examples: list[TrainingExample], *, head: str | None):
    return _numeric_stats(task, examples, head=head)


def _feature_sets(task: str, sample: TrainingExample, *, head: str | None):
    if task == "evidence_ranking" or (task == "event_relationship" and head == "handling_value"):
        return (sample.features["preferred"], sample.features["other"])
    return (sample.features,)


def _sample_digest(samples: list[TrainingExample], partition: str) -> str:
    data = []
    for sample in samples:
        if sample.partition != partition:
            continue
        data.append({
            "sample_id": sample.sample_id,
            "group_id": sample.group_id,
            "feature_sha256": _sha256(_canonical_json(sample.features)),
            "label_sha256": _example_label_digest(sample.label),
            "source_ref_sha256": _sha256(_canonical_json(sample.source_refs)),
            "label_schema_revision": sample.label_schema_revision,
        })
    return _sha256(_canonical_json(data))


def _admitted_manifest_digest(task: str, head: str | None, examples: tuple[TrainingExample, ...] | list[TrainingExample]) -> str:
    payload = {
        "recipe_version": CPU_RECIPE_VERSION,
        "feature_revision": CPU_FEATURE_REVISION,
        "task": task,
        "head": head,
        "examples": [
            {
                "sample_id": sample.sample_id,
                "group_id": sample.group_id,
                "partition": sample.partition,
                "feature_sha256": _sha256(_canonical_json(sample.features)),
                "label_sha256": _example_label_digest(sample.label),
                "source_ref_sha256": _sha256(_canonical_json(sample.source_refs)),
                "label_schema_revision": sample.label_schema_revision,
                "label_review_id": sample.label_review_id,
                "source_use_decision_id": sample.source_use_decision_id,
            }
            for sample in sorted(examples, key=lambda item: item.sample_id)
        ],
    }
    return _sha256(_canonical_json(payload))


def _sample_group_weights(samples: list[TrainingExample]) -> dict[str, float]:
    sizes = Counter(sample.group_id for sample in samples)
    return {sample.sample_id: 1.0 / sizes[sample.group_id] for sample in samples}


def _stable_group_batches(samples: list[TrainingExample], *, epoch: int, batch_size: int = CPU_BATCH_SIZE):
    grouped: dict[str, list[TrainingExample]] = defaultdict(list)
    for sample in samples:
        grouped[sample.group_id].append(sample)
    groups = sorted(grouped)
    seed_material = f"{CPU_SEED}:{epoch}".encode("utf-8")
    seed = int.from_bytes(hashlib.sha256(seed_material).digest()[:8], "little")
    random.Random(seed).shuffle(groups)
    for index in range(0, len(groups), batch_size):
        batch_groups = groups[index:index + batch_size]
        yield [sample for group in batch_groups for sample in sorted(grouped[group], key=lambda item: item.sample_id)], len(batch_groups)


def _dot(weights: array, scale: float, features: dict[int, float]) -> float:
    return math.fsum(weights[index] * scale * value for index, value in features.items())


def _sigmoid(value: float) -> float:
    if value >= 0:
        z = math.exp(-min(value, 709.0))
        return 1.0 / (1.0 + z)
    z = math.exp(max(value, -709.0))
    return z / (1.0 + z)


def _softmax(scores: list[float]) -> list[float]:
    if not scores:
        raise CPUTrainingBlocked("CPU_TRAINING_OUTPUT_HEAD_EMPTY")
    maximum = max(scores)
    values = [math.exp(max(-709.0, score - maximum)) for score in scores]
    total = math.fsum(values)
    if not math.isfinite(total) or total <= 0:
        raise CPUTrainingBlocked("CPU_TRAINING_NUMERICAL_ERROR")
    return [value / total for value in values]


def _apply_sparse_update(weights: array, gradients: dict[int, float], *, scale: float,
                         learning_rate: float, l2: float) -> float:
    shrink = 1.0 - learning_rate * l2
    if shrink <= 0 or not math.isfinite(shrink):
        raise CPUTrainingBlocked("CPU_TRAINING_OPTIMIZER_INVALID")
    new_scale = scale * shrink
    if new_scale < 1e-9:
        for index in range(len(weights)):
            weights[index] *= scale
        scale = 1.0
        new_scale = shrink
    for index, gradient in gradients.items():
        weights[index] -= learning_rate * gradient / new_scale
        if not math.isfinite(weights[index]):
            raise CPUTrainingBlocked("CPU_TRAINING_NUMERICAL_ERROR")
    return new_scale


def _softmax_epoch(
    examples: list[TrainingExample], vectors: dict[str, dict[int, float]], labels: tuple[str, ...],
    *, l2: float, epoch_count: int, dimension: int,
    is_cancelled: Callable[[], bool] | None, deadline: float,
):
    label_index = {label: index for index, label in enumerate(labels)}
    observed = {sample.label for sample in examples}
    if not observed.issubset(label_index):
        raise CPUTrainingBlocked("TRAINING_CLASS_LABEL_INVALID")
    missing = set(labels) - observed
    if missing:
        raise CPUTrainingBlocked("TRAINING_REQUIRED_CLASS_UNOBSERVED", {"class_count": len(missing)})
    group_sizes = Counter(sample.group_id for sample in examples)
    class_mass: Counter[str] = Counter()
    for sample in examples:
        class_mass[sample.label] += 1.0 / group_sizes[sample.group_id]
    represented = len(class_mass)
    total_mass = math.fsum(class_mass.values())
    class_weights = {label: total_mass / (represented * mass) for label, mass in class_mass.items()}
    weights = [array("d", [0.0]) * dimension for _ in labels]
    scales = [1.0] * len(labels)
    intercepts = [0.0] * len(labels)
    update_index = 0
    for epoch in range(epoch_count):
        for batch, batch_group_count in _stable_group_batches(examples, epoch=epoch):
            if time.monotonic() >= deadline:
                raise CPUTrainingBlocked("CPU_TRAINING_TIME_BUDGET_EXCEEDED")
            if is_cancelled is not None and is_cancelled():
                raise CPUTrainingBlocked("CPU_TRAINING_CANCELLED")
            gradients = [defaultdict(float) for _ in labels]
            bias_gradients = [0.0] * len(labels)
            for sample in batch:
                vector = vectors[sample.sample_id]
                sample_weight = class_weights[sample.label] / group_sizes[sample.group_id] / batch_group_count
                scores = [intercepts[index] + _dot(weights[index], scales[index], vector) for index in range(len(labels))]
                probabilities = _softmax(scores)
                target = label_index[sample.label]
                for index, probability in enumerate(probabilities):
                    gradient = sample_weight * (probability - (1.0 if index == target else 0.0))
                    bias_gradients[index] += gradient
                    for feature_index, value in vector.items():
                        gradients[index][feature_index] += gradient * value
            learning_rate = 0.05 / math.sqrt(1 + update_index / 1000)
            for index in range(len(labels)):
                scales[index] = _apply_sparse_update(
                    weights[index], gradients[index], scale=scales[index],
                    learning_rate=learning_rate, l2=l2,
                )
                intercepts[index] -= learning_rate * bias_gradients[index]
            update_index += 1
    for index in range(len(labels)):
        if scales[index] != 1.0:
            for feature_index in range(len(weights[index])):
                weights[index][feature_index] *= scales[index]
    return weights, intercepts


def _binary_epoch(
    examples: list[TrainingExample], vectors: dict[str, dict[int, float]], labels: tuple[str, ...],
    *, task: str, l2: float, epoch_count: int,
    is_cancelled: Callable[[], bool] | None, deadline: float,
):
    dimension = _feature_dimension(task)
    trained_labels = []
    models = []
    unfit = []
    for label in labels:
        labelled_examples = []
        group_known_counts: Counter[str] = Counter()
        for sample in examples:
            value = sample.label.get(label) if isinstance(sample.label, dict) else None
            if value is None:
                continue
            if type(value) is not bool:
                raise CPUTrainingBlocked("TRAINING_MULTILABEL_VALUE_INVALID")
            labelled_examples.append(sample)
            group_known_counts[sample.group_id] += 1
        class_mass: Counter[str] = Counter()
        for sample in labelled_examples:
            value = sample.label[label]
            class_mass["positive" if value else "negative"] += 1.0 / group_known_counts[sample.group_id]
        positive, negative = class_mass["positive"], class_mass["negative"]
        if positive <= 0 or negative <= 0:
            unfit.append(label)
            continue
        total = positive + negative
        positive_weight = total / (2 * positive)
        negative_weight = total / (2 * negative)
        weights = array("d", [0.0]) * dimension
        scale = 1.0
        intercept = 0.0
        update_index = 0
        for epoch in range(epoch_count):
            for batch, batch_group_count in _stable_group_batches(labelled_examples, epoch=epoch):
                if time.monotonic() >= deadline:
                    raise CPUTrainingBlocked("CPU_TRAINING_TIME_BUDGET_EXCEEDED")
                if is_cancelled is not None and is_cancelled():
                    raise CPUTrainingBlocked("CPU_TRAINING_CANCELLED")
                gradients: dict[int, float] = defaultdict(float)
                bias_gradient = 0.0
                for sample in batch:
                    value = (sample.label or {}).get(label)
                    if value is None:
                        continue
                    group_count = group_known_counts[sample.group_id][label]
                    class_weight = positive_weight if value else negative_weight
                    sample_weight = class_weight / group_count / batch_group_count
                    vector = vectors[sample.sample_id]
                    probability = _sigmoid(intercept + _dot(weights, scale, vector))
                    gradient = sample_weight * (probability - (1.0 if value else 0.0))
                    bias_gradient += gradient
                    for feature_index, feature_value in vector.items():
                        gradients[feature_index] += gradient * feature_value
                learning_rate = 0.05 / math.sqrt(1 + update_index / 1000)
                scale = _apply_sparse_update(
                    weights, gradients, scale=scale, learning_rate=learning_rate, l2=l2,
                )
                intercept -= learning_rate * bias_gradient
                update_index += 1
        if scale != 1.0:
            for feature_index in range(len(weights)):
                weights[feature_index] *= scale
        trained_labels.append(label)
        models.append((weights, intercept))
    return trained_labels, models, unfit


def _pairwise_epoch(
    examples: list[TrainingExample], vectors: dict[str, tuple[dict[int, float], dict[int, float]]],
    *, task: str, l2: float, epoch_count: int,
    is_cancelled: Callable[[], bool] | None, deadline: float,
):
    dimension = _feature_dimension(task)
    weights = array("d", [0.0]) * dimension
    scale = 1.0
    update_index = 0
    group_sizes = Counter(sample.group_id for sample in examples)
    for epoch in range(epoch_count):
        for batch, batch_group_count in _stable_group_batches(examples, epoch=epoch):
            if time.monotonic() >= deadline:
                raise CPUTrainingBlocked("CPU_TRAINING_TIME_BUDGET_EXCEEDED")
            if is_cancelled is not None and is_cancelled():
                raise CPUTrainingBlocked("CPU_TRAINING_CANCELLED")
            gradients: dict[int, float] = defaultdict(float)
            for sample in batch:
                preferred, other = vectors[sample.sample_id]
                sample_weight = 1.0 / group_sizes[sample.group_id] / batch_group_count
                difference: dict[int, float] = defaultdict(float)
                for feature_index, value in preferred.items():
                    difference[feature_index] += value
                for feature_index, value in other.items():
                    difference[feature_index] -= value
                margin = _dot(weights, scale, difference)
                gradient = sample_weight * (_sigmoid(margin) - 1.0)
                for feature_index, value in difference.items():
                    gradients[feature_index] += gradient * value
            learning_rate = 0.05 / math.sqrt(1 + update_index / 1000)
            scale = _apply_sparse_update(
                weights, gradients, scale=scale, learning_rate=learning_rate, l2=l2,
            )
            update_index += 1
    if scale != 1.0:
        for feature_index in range(len(weights)):
            weights[feature_index] *= scale
    return weights


def _sample_partition_digest(samples: list[TrainingExample], partition: str) -> str:
    return _sample_digest(samples, partition)


def _candidate_features(candidate: CPUModelCandidate) -> tuple[int, dict[str, NumericFeatureStats]]:
    if candidate.task not in TASK_SCHEMAS or candidate.feature_revision != CPU_FEATURE_REVISION:
        raise CPUTrainingBlocked("CPU_MODEL_FEATURE_SCHEMA_UNSUPPORTED")
    stats = {
        name: NumericFeatureStats(mean, scale, available)
        for name, mean, scale, available in candidate.numeric_stats
    }
    if set(stats) != set(TASK_SCHEMAS[candidate.task]["numeric"]):
        raise CPUTrainingBlocked("CPU_MODEL_NUMERIC_SCHEMA_INVALID")
    return _feature_dimension(candidate.task), stats


def score_cpu_candidate(candidate: CPUModelCandidate, features: dict, *, head: str | None = None) -> dict:
    """Score one candidate for evaluation; raw scores are not probabilities or product facts."""
    if (type(candidate) is not CPUModelCandidate or candidate._issuer is not _CANDIDATE_SEAL
            or candidate.promotion_state != "NOT_PROMOTED"
            or _sha256(_canonical_json(_candidate_payload(candidate))) != candidate.package_sha256):
        raise CPUTrainingBlocked("CPU_CANDIDATE_NOT_EVALUATION_ONLY")
    if head != candidate.head:
        raise CPUTrainingBlocked("CPU_CANDIDATE_HEAD_MISMATCH")
    dimension, stats = _candidate_features(candidate)
    encoded = encode_task_features(candidate.task, features, stats, head=head)
    if candidate.task == "evidence_ranking" or (candidate.task == "event_relationship" and head == "handling_value"):
        preferred, other = encoded
        weights = array("d", [0.0]) * dimension
        for index, value in candidate.weights[0]:
            weights[index] = value
        margin = _dot(weights, 1.0, preferred) - _dot(weights, 1.0, other)
        return {"score_margin": margin, "score_state": "RAW_UNCALIBRATED_PAIRWISE_CANDIDATE"}
    scores = []
    for weight_items, intercept in zip(candidate.weights, candidate.intercepts):
        weights = array("d", [0.0]) * dimension
        for index, value in weight_items:
            weights[index] = value
        scores.append(intercept + _dot(weights, 1.0, encoded))
    if candidate.model == "one_vs_rest_logistic":
        return {
            "scores": dict(zip(candidate.labels, scores)),
            "thresholds": dict(zip(candidate.labels, candidate.decision_thresholds)),
            "score_state": "RAW_UNCALIBRATED_ONE_VS_REST_CANDIDATE",
            "unfit_labels": candidate.development_metrics.get("unfit_labels", []),
        }
    return {
        "scores": dict(zip(candidate.labels, scores)),
        "score_state": "RAW_UNCALIBRATED_MULTICLASS_CANDIDATE",
    }


def _macro_f1(labels: tuple[str, ...], expected: list[str], predicted: list[str]) -> float:
    if not expected:
        return 0.0
    values = []
    for label in labels:
        tp = sum(actual == label and guess == label for actual, guess in zip(expected, predicted))
        fp = sum(actual != label and guess == label for actual, guess in zip(expected, predicted))
        fn = sum(actual == label and guess != label for actual, guess in zip(expected, predicted))
        denominator = 2 * tp + fp + fn
        values.append((2 * tp / denominator) if denominator else 0.0)
    return statistics.fmean(values) if values else 0.0


def _group_metrics(samples: list[TrainingExample], labels: tuple[str, ...], predictions: list[str]) -> dict:
    expected = [sample.label for sample in samples]
    if not expected:
        raise CPUTrainingBlocked("TRAINING_DEVELOPMENT_VIEW_EMPTY")
    accuracy = sum(left == right for left, right in zip(expected, predictions)) / len(expected)
    return {
        "reviewed_examples": len(expected),
        "reviewed_groups": len({sample.group_id for sample in samples}),
        "accuracy": accuracy,
        "macro_f1": _macro_f1(labels, expected, predictions),
    }


def _multiclass_predictions(samples, vectors, weights, intercepts, labels) -> list[str]:
    predictions = []
    for sample in samples:
        scores = [
            intercepts[class_index] + _dot(weights[class_index], 1.0, vectors[sample.sample_id])
            for class_index in range(len(labels))
        ]
        predictions.append(labels[max(range(len(scores)), key=lambda index: (scores[index], -index))])
    return predictions


def _choose_binary_threshold(samples, vectors, weights, intercept, label: str) -> tuple[float, dict]:
    scored = []
    for sample in samples:
        value = sample.label.get(label) if isinstance(sample.label, dict) else None
        if value is None:
            continue
        scored.append((_sigmoid(intercept + _dot(weights, 1.0, vectors[sample.sample_id])), value))
    if not scored or not any(value for _, value in scored) or all(value for _, value in scored):
        raise CPUTrainingBlocked("TRAINING_BINARY_DEVELOPMENT_LABELS_INSUFFICIENT", {"label": label})
    thresholds = sorted({0.5, *(score for score, _ in scored)})
    best = None
    for threshold in thresholds:
        tp = sum(score >= threshold and value for score, value in scored)
        fp = sum(score >= threshold and not value for score, value in scored)
        fn = sum(score < threshold and value for score, value in scored)
        denominator = 2 * tp + fp + fn
        f1 = (2 * tp / denominator) if denominator else 0.0
        precision = tp / (tp + fp) if tp + fp else 0.0
        recall = tp / (tp + fn) if tp + fn else 0.0
        candidate = (f1, precision, -threshold, threshold, tp, fp, fn)
        if best is None or candidate > best:
            best = candidate
    assert best is not None
    recall_denominator = sum(value for _, value in scored)
    recall = best[4] / recall_denominator if recall_denominator else 0.0
    return best[3], {"f1": best[0], "precision": best[1], "recall": recall,
                     "true_positive": best[4], "false_positive": best[5],
                     "false_negative": best[6], "reviewed_examples": len(scored)}


def _pairwise_development_score(samples, vectors, weights) -> dict:
    correct = 0
    groups = set()
    for sample in samples:
        preferred, other = vectors[sample.sample_id]
        margin = _dot(weights, 1.0, preferred) - _dot(weights, 1.0, other)
        correct += margin > 0
        groups.add(sample.group_id)
    if not samples:
        raise CPUTrainingBlocked("TRAINING_DEVELOPMENT_VIEW_EMPTY")
    return {
        "reviewed_pairs": len(samples), "reviewed_queries": len(groups),
        "pairwise_accuracy": correct / len(samples),
    }


def _candidate_payload(candidate: CPUModelCandidate) -> dict:
    return {
        "schema": _CANDIDATE_PACKAGE_SCHEMA,
        "task": candidate.task, "head": candidate.head, "model": candidate.model,
        "labels": list(candidate.labels), "recipe_version": candidate.recipe_version,
        "feature_revision": candidate.feature_revision,
        "weights": [[[index, value] for index, value in row] for row in candidate.weights],
        "intercepts": list(candidate.intercepts),
        "numeric_stats": [list(value) for value in candidate.numeric_stats],
        "selected_config": list(candidate.selected_config),
        "decision_thresholds": list(candidate.decision_thresholds),
        "development_metrics": candidate.development_metrics,
        "training_lineage": list(candidate.training_lineage),
        "development_lineage": list(candidate.development_lineage),
        "training_manifest_sha256": candidate.training_manifest_sha256,
        "development_manifest_sha256": candidate.development_manifest_sha256,
        "candidate_state": candidate.candidate_state,
        "evaluation_state": candidate.evaluation_state,
        "promotion_state": candidate.promotion_state,
    }


def serialize_cpu_candidate(candidate: CPUModelCandidate) -> bytes:
    """Serialize finite parameters as data-only JSON; never serialize executable objects."""
    if (type(candidate) is not CPUModelCandidate or candidate._issuer is not _CANDIDATE_SEAL
            or candidate.promotion_state != "NOT_PROMOTED"
            or _sha256(_canonical_json(_candidate_payload(candidate))) != candidate.package_sha256):
        raise CPUTrainingBlocked("CPU_CANDIDATE_PACKAGE_INVALID")
    payload = _candidate_payload(candidate)
    package_hash = _sha256(_canonical_json(payload))
    return _canonical_json({**payload, "package_sha256": package_hash})


def inspect_cpu_candidate_package(content: bytes) -> dict:
    """Validate package identity and bounded numeric schema without activating it."""
    if not isinstance(content, bytes) or not content or len(content) > 64 * 1024**2:
        raise CPUTrainingBlocked("CPU_CANDIDATE_PACKAGE_SIZE_INVALID")
    try:
        package = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise CPUTrainingBlocked("CPU_CANDIDATE_PACKAGE_INVALID") from exc
    if not isinstance(package, dict) or package.get("schema") != _CANDIDATE_PACKAGE_SCHEMA:
        raise CPUTrainingBlocked("CPU_CANDIDATE_PACKAGE_SCHEMA_UNSUPPORTED")
    package_hash = package.pop("package_sha256", None)
    if not re_full_sha256(package_hash) or _sha256(_canonical_json(package)) != package_hash:
        raise CPUTrainingBlocked("CPU_CANDIDATE_PACKAGE_HASH_MISMATCH")
    expected_keys = {
        "schema", "task", "head", "model", "labels", "recipe_version", "feature_revision",
        "weights", "intercepts", "numeric_stats", "selected_config", "decision_thresholds",
        "development_metrics", "training_lineage", "development_lineage",
        "training_manifest_sha256", "development_manifest_sha256", "candidate_state",
        "evaluation_state", "promotion_state",
    }
    if set(package) != expected_keys:
        raise CPUTrainingBlocked("CPU_CANDIDATE_PACKAGE_SCHEMA_INVALID")
    task = package.get("task")
    head = package.get("head")
    if (not isinstance(task, str) or task not in TASK_SCHEMAS
            or package.get("recipe_version") != CPU_RECIPE_VERSION
            or package.get("feature_revision") != CPU_FEATURE_REVISION):
        raise CPUTrainingBlocked("CPU_CANDIDATE_PACKAGE_FEATURE_MISMATCH")
    labels = package.get("labels")
    weights = package.get("weights")
    intercepts = package.get("intercepts")
    pairwise = task == "evidence_ranking" or task == "event_relationship" and head == "handling_value"
    multiclass = task == "identity_candidates" or (
        task == "event_relationship" and head in {"event_relation", "severity", "credibility"}
    )
    if ((task == "event_relationship" and head not in EVENT_HEAD_LABELS)
            or (task != "event_relationship" and head is not None)):
        raise CPUTrainingBlocked("CPU_CANDIDATE_PACKAGE_HEAD_INVALID")
    expected_model = ("linear_pairwise_ranker" if pairwise else
                      "multiclass_logistic" if multiclass else "one_vs_rest_logistic")
    if (not isinstance(labels, list) or not labels or any(not isinstance(label, str) or not label for label in labels)
            or len(set(labels)) != len(labels) or package.get("model") != expected_model
            or not isinstance(weights, list) or len(weights) != len(labels)
            or not isinstance(intercepts, list) or len(intercepts) != len(labels)):
        raise CPUTrainingBlocked("CPU_CANDIDATE_PACKAGE_PARAMETERS_INVALID")
    if pairwise and (labels != ["preferred"] or package.get("decision_thresholds") != []):
        raise CPUTrainingBlocked("CPU_CANDIDATE_PACKAGE_PARAMETERS_INVALID")
    if multiclass:
        known_labels = list(TASK_SCHEMAS[task]["labels"] if task == "identity_candidates" else EVENT_HEAD_LABELS[head])
        if labels != known_labels or package.get("decision_thresholds") != []:
            raise CPUTrainingBlocked("CPU_CANDIDATE_PACKAGE_LABELS_INVALID")
    if not (pairwise or multiclass):
        known_labels = (
            list(EVENT_HEAD_LABELS[head]) if task == "event_relationship"
            else None
        )
        if task == "professional_action_suggestion":
            if not isinstance(labels, list) or not labels:
                raise CPUTrainingBlocked("CPU_CANDIDATE_PACKAGE_LABELS_INVALID")
        elif not set(labels).issubset(set(known_labels or [])):
            raise CPUTrainingBlocked("CPU_CANDIDATE_PACKAGE_LABELS_INVALID")
    dimension = _feature_dimension(task)
    for row in weights:
        if not isinstance(row, list):
            raise CPUTrainingBlocked("CPU_CANDIDATE_PACKAGE_PARAMETERS_INVALID")
        previous = -1
        for item in row:
            if (not isinstance(item, list) or len(item) != 2
                    or isinstance(item[0], bool) or not isinstance(item[0], int)
                    or not 0 <= item[0] < dimension or item[0] <= previous
                    or type(item[1]) not in (int, float) or not math.isfinite(item[1])):
                raise CPUTrainingBlocked("CPU_CANDIDATE_PACKAGE_PARAMETERS_INVALID")
            previous = item[0]
    if any(type(value) not in (int, float) or not math.isfinite(value) for value in intercepts):
        raise CPUTrainingBlocked("CPU_CANDIDATE_PACKAGE_PARAMETERS_INVALID")
    numeric_stats = package.get("numeric_stats")
    expected_numeric = set(TASK_SCHEMAS[task]["numeric"])
    if (not isinstance(numeric_stats, list) or len(numeric_stats) != len(expected_numeric)
            or any(not isinstance(item, list) or len(item) != 4 for item in numeric_stats)
            or {item[0] for item in numeric_stats if isinstance(item, list) and item} != expected_numeric):
        raise CPUTrainingBlocked("CPU_CANDIDATE_PACKAGE_NUMERIC_SCHEMA_INVALID")
    for name, mean, scale, available in numeric_stats:
        if (type(available) is not bool or available is not True
                or type(mean) not in (int, float) or not math.isfinite(mean)
                or type(scale) not in (int, float) or not math.isfinite(scale) or scale <= 0):
            raise CPUTrainingBlocked("CPU_CANDIDATE_PACKAGE_NUMERIC_SCHEMA_INVALID")
    selected_config = package.get("selected_config")
    thresholds = package.get("decision_thresholds")
    if (not isinstance(selected_config, list) or len(selected_config) != 2
            or type(selected_config[0]) not in (int, float) or selected_config[0] not in CPU_L2_CANDIDATES
            or type(selected_config[1]) is not int or selected_config[1] not in CPU_EPOCH_CANDIDATES
            or not isinstance(thresholds, list)
            or (not pairwise and not multiclass and len(thresholds) != len(labels))
            or any(type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1
                   for value in thresholds)):
        raise CPUTrainingBlocked("CPU_CANDIDATE_PACKAGE_CONFIGURATION_INVALID")
    metrics = package.get("development_metrics")
    if not isinstance(metrics, dict) or not _finite_json_values(metrics):
        raise CPUTrainingBlocked("CPU_CANDIDATE_PACKAGE_METRICS_INVALID")
    training_lineage = package.get("training_lineage")
    development_lineage = package.get("development_lineage")
    if not _valid_lineage(training_lineage) or not _valid_lineage(development_lineage):
        raise CPUTrainingBlocked("CPU_CANDIDATE_PACKAGE_LINEAGE_INVALID")
    train_ids = {item["sample_id"] for item in training_lineage}
    dev_ids = {item["sample_id"] for item in development_lineage}
    train_groups = {item["group_id"] for item in training_lineage}
    dev_groups = {item["group_id"] for item in development_lineage}
    if train_ids & dev_ids or train_groups & dev_groups:
        raise CPUTrainingBlocked("CPU_CANDIDATE_PACKAGE_SPLIT_LEAKAGE")
    if (not re_full_sha256(package.get("training_manifest_sha256"))
            or not re_full_sha256(package.get("development_manifest_sha256"))):
        raise CPUTrainingBlocked("CPU_CANDIDATE_PACKAGE_LINEAGE_INVALID")
    if package.get("evaluation_state") != "NOT_RUN":
        raise CPUTrainingBlocked("CPU_CANDIDATE_PACKAGE_EVALUATION_STATE_INVALID")
    if package.get("promotion_state") != "NOT_PROMOTED" or package.get("candidate_state") not in {
        "DEVELOPMENT_SELECTED_NOT_PROMOTED", "PARTIAL_NOT_PROMOTED",
    }:
        raise CPUTrainingBlocked("CPU_CANDIDATE_NOT_PRODUCTION_APPROVED")
    return {
        "schema": package["schema"], "task": task, "head": package.get("head"),
        "model": package.get("model"), "feature_revision": package["feature_revision"],
        "labels": labels, "package_sha256": package_hash,
        "candidate_state": package["candidate_state"],
        "evaluation_state": package.get("evaluation_state"),
        "promotion_state": package["promotion_state"],
        "weights": weights, "intercepts": intercepts,
        "numeric_stats": numeric_stats,
        "decision_thresholds": thresholds,
    }


def _finite_json_values(value) -> bool:
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return True
    if type(value) in (int, float):
        return math.isfinite(value)
    if isinstance(value, list):
        return all(_finite_json_values(item) for item in value)
    if isinstance(value, dict):
        return all(isinstance(key, str) and _finite_json_values(item) for key, item in value.items())
    return False


def _valid_lineage(value) -> bool:
    required = {"sample_id", "group_id", "source_ref_sha256", "label_review_id", "source_use_decision_id"}
    if not isinstance(value, list) or not value:
        return False
    seen = set()
    for item in value:
        if (not isinstance(item, dict) or set(item) != required
                or any(not isinstance(item.get(key), str) or not item.get(key)
                       for key in ("sample_id", "group_id", "label_review_id", "source_use_decision_id"))
                or not re_full_sha256(item.get("source_ref_sha256"))
                or item["sample_id"] in seen):
            return False
        seen.add(item["sample_id"])
    return True


def load_cpu_candidate_package(content: bytes) -> CPUModelCandidate:
    """Reload a hashed, development-only package for evaluation; never activate it."""
    inspect_cpu_candidate_package(content)
    package = json.loads(content.decode("utf-8"))
    package_hash = package.pop("package_sha256")
    candidate = CPUModelCandidate(
        task=package["task"], head=package["head"], model=package["model"],
        labels=tuple(package["labels"]), recipe_version=package["recipe_version"],
        feature_revision=package["feature_revision"],
        weights=tuple(tuple((item[0], float(item[1])) for item in row) for row in package["weights"]),
        intercepts=tuple(float(value) for value in package["intercepts"]),
        numeric_stats=tuple((item[0], item[1], item[2], item[3]) for item in package["numeric_stats"]),
        selected_config=(float(package["selected_config"][0]), package["selected_config"][1]),
        decision_thresholds=tuple(float(value) for value in package["decision_thresholds"]),
        development_metrics=package["development_metrics"],
        training_lineage=tuple(package["training_lineage"]),
        development_lineage=tuple(package["development_lineage"]),
        training_manifest_sha256=package["training_manifest_sha256"],
        development_manifest_sha256=package["development_manifest_sha256"],
        package_sha256=package_hash, candidate_state=package["candidate_state"],
        evaluation_state=package["evaluation_state"], promotion_state=package["promotion_state"],
        _issuer=_CANDIDATE_SEAL,
    )
    if _sha256(_canonical_json(_candidate_payload(candidate))) != package_hash:
        raise CPUTrainingBlocked("CPU_CANDIDATE_PACKAGE_HASH_MISMATCH")
    return candidate


def _verify_candidate_for_evaluation(candidate: CPUModelCandidate, *, task: str, head: str | None) -> None:
    if (type(candidate) is not CPUModelCandidate or candidate._issuer is not _CANDIDATE_SEAL
            or candidate.task != task or candidate.head != head
            or candidate.promotion_state != "NOT_PROMOTED"
            or candidate.evaluation_state != "NOT_RUN"
            or candidate.recipe_version != CPU_RECIPE_VERSION
            or candidate.feature_revision != CPU_FEATURE_REVISION
            or not re_full_sha256(candidate.package_sha256)
            or _sha256(_canonical_json(_candidate_payload(candidate))) != candidate.package_sha256):
        raise CPUTrainingBlocked("CPU_CANDIDATE_NOT_EVALUATION_ONLY")


def evaluate_cpu_candidate(candidate: CPUModelCandidate,
                           dataset: AdmittedEvaluationDataset) -> dict:
    """Evaluate a reloaded/development candidate on a separately admitted final set.

    Thresholds and parameters are frozen before this function. The final set is
    never used to refit, retune, select or promote the candidate.
    """
    if (type(dataset) is not AdmittedEvaluationDataset
            or dataset._issuer is not _EVALUATION_ADMISSION_SEAL
            or not callable(dataset._revalidate)):
        raise CPUTrainingBlocked("INDEPENDENT_EVALUATION_ADMISSION_REQUIRED")
    _verify_candidate_for_evaluation(candidate, task=dataset.task, head=dataset.head)
    examples = list(dataset.examples)
    if (not examples or any(sample.partition != "final" for sample in examples)
            or _admitted_manifest_digest(dataset.task, dataset.head, tuple(examples)) != dataset.manifest_sha256):
        raise CPUTrainingBlocked("EVALUATION_FINAL_DATASET_CHANGED")
    training_ids = {item["sample_id"] for item in candidate.training_lineage}
    development_ids = {item["sample_id"] for item in candidate.development_lineage}
    training_groups = {item["group_id"] for item in candidate.training_lineage}
    development_groups = {item["group_id"] for item in candidate.development_lineage}
    final_ids = {sample.sample_id for sample in examples}
    final_groups = {sample.group_id for sample in examples}
    if (final_ids & (training_ids | development_ids)
            or final_groups & (training_groups | development_groups)):
        raise CPUTrainingBlocked("EVALUATION_FINAL_SET_LEAKAGE")
    for sample in examples:
        _validate_task_feature_fields(dataset.task, sample.features, head=dataset.head)
        _validate_label_shape(dataset.task, dataset.head, sample.label)
    try:
        dataset._revalidate()
    except CPUTrainingBlocked:
        raise
    except Exception as exc:
        raise CPUTrainingBlocked("EVALUATION_ADMISSION_RECHECK_FAILED") from exc

    numeric_stats = {
        name: NumericFeatureStats(mean, scale, available)
        for name, mean, scale, available in candidate.numeric_stats
    }
    vectors, collisions = _encoded_partition(dataset.task, dataset.head, examples, numeric_stats)
    pairwise = dataset.task == "evidence_ranking" or (
        dataset.task == "event_relationship" and dataset.head == "handling_value"
    )
    multiclass = dataset.task == "identity_candidates" or (
        dataset.task == "event_relationship"
        and dataset.head in {"event_relation", "severity", "credibility"}
    )
    if pairwise:
        weights = array("d", [0.0]) * _feature_dimension(dataset.task)
        for index, value in candidate.weights[0]:
            weights[index] = value
        metrics = _pairwise_development_score(examples, vectors, weights)
        primary_metric = metrics["pairwise_accuracy"]
    elif multiclass:
        weights = []
        for row in candidate.weights:
            dense = array("d", [0.0]) * _feature_dimension(dataset.task)
            for index, value in row:
                dense[index] = value
            weights.append(dense)
        predictions = _multiclass_predictions(
            examples, vectors, weights, candidate.intercepts, candidate.labels,
        )
        metrics = _group_metrics(examples, candidate.labels, predictions)
        primary_metric = metrics["macro_f1"]
    else:
        models = []
        for row, intercept in zip(candidate.weights, candidate.intercepts):
            dense = array("d", [0.0]) * _feature_dimension(dataset.task)
            for index, value in row:
                dense[index] = value
            models.append((dense, intercept))
        primary_metric, metrics = _macro_multilabel_f1(
            examples, vectors, candidate.labels, models, candidate.decision_thresholds,
        )
    try:
        dataset._revalidate()
        _verify_candidate_for_evaluation(candidate, task=dataset.task, head=dataset.head)
    except CPUTrainingBlocked:
        raise
    except Exception as exc:
        raise CPUTrainingBlocked("EVALUATION_ADMISSION_RECHECK_FAILED") from exc
    return {
        "status": "evaluated",
        "candidate_package_sha256": candidate.package_sha256,
        "task": dataset.task, "head": dataset.head,
        "final_dataset_sha256": dataset.manifest_sha256,
        "training_manifest_sha256": candidate.training_manifest_sha256,
        "development_manifest_sha256": candidate.development_manifest_sha256,
        "reviewed_examples": len(examples),
        "reviewed_groups": len(final_groups),
        "metric": primary_metric,
        "metrics": metrics,
        "hashed_feature_collision_count": collisions,
        "evaluation_state": "INDEPENDENT_FINAL_HELDOUT_EVALUATED",
        "candidate_state": candidate.candidate_state,
        "promotion_state": "NOT_PROMOTED",
        "fitting_or_threshold_selection_performed": False,
    }


def _validate_budget(budget: CPUResourceBudget) -> None:
    if type(budget) is not CPUResourceBudget:
        raise CPUTrainingBlocked("CPU_RESOURCE_READBACK_REQUIRED")
    if (budget.workers != 1 or budget.threads != 1
            or type(budget.available_physical_bytes) is not int or budget.available_physical_bytes <= 0
            or type(budget.peak_bytes) is not int or budget.peak_bytes <= 0
            or budget.peak_bytes > min(CPU_MAX_MEMORY_BYTES, budget.available_physical_bytes // 4)
            or type(budget.max_seconds) is not int or not 1 <= budget.max_seconds <= CPU_MAX_SECONDS):
        raise CPUTrainingBlocked("CPU_RESOURCE_BUDGET_INVALID")
    try:
        observed_at = datetime.fromisoformat(budget.observed_at.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise CPUTrainingBlocked("CPU_RESOURCE_READBACK_INVALID") from exc
    age = (datetime.now(timezone.utc) - observed_at).total_seconds()
    if observed_at.tzinfo is None or age < -5 or age > 60:
        raise CPUTrainingBlocked("CPU_RESOURCE_READBACK_STALE")


def _fit_estimated_bytes(task: str, labels: tuple[str, ...], examples: list[TrainingExample]) -> int:
    dimension = _feature_dimension(task)
    feature_bytes = sum(len(_canonical_json(sample.features)) for sample in examples)
    # Keep a gradient and optimizer allowance beside the float64 parameter set.
    parameter_bytes = dimension * max(1, len(labels)) * 8 * 3
    return parameter_bytes + feature_bytes * 3 + 16 * 1024**2


def _encoded_partition(task: str, head: str | None, examples: list[TrainingExample], stats):
    vectors = {}
    collisions = [0]
    for sample in examples:
        vectors[sample.sample_id] = encode_task_features(
            task, sample.features, stats, head=head, collision_counter=collisions,
        )
    return vectors, collisions[0]


def _macro_multilabel_f1(samples, vectors, labels, models, thresholds) -> tuple[float, dict]:
    per_label = {}
    f1_values = []
    for label, (weights, intercept), threshold in zip(labels, models, thresholds):
        stats = []
        for sample in samples:
            target = sample.label.get(label) if isinstance(sample.label, dict) else None
            if target is None:
                continue
            score = _sigmoid(intercept + _dot(weights, 1.0, vectors[sample.sample_id]))
            stats.append((score, target))
        if not stats:
            continue
        tp = sum(score >= threshold and target for score, target in stats)
        fp = sum(score >= threshold and not target for score, target in stats)
        fn = sum(score < threshold and target for score, target in stats)
        denominator = 2 * tp + fp + fn
        f1 = 2 * tp / denominator if denominator else 0.0
        f1_values.append(f1)
        per_label[label] = {
            "f1": f1,
            "reviewed_examples": len(stats),
            "true_positive": tp,
            "false_positive": fp,
            "false_negative": fn,
        }
    if not f1_values:
        raise CPUTrainingBlocked("TRAINING_DEVELOPMENT_LABELS_INSUFFICIENT")
    return statistics.fmean(f1_values), {"heads": per_label, "macro_f1": statistics.fmean(f1_values)}


def _metrics_and_thresholds(task, head, dev, vectors, trained_labels, models):
    if task == "evidence_ranking" or (task == "event_relationship" and head == "handling_value"):
        metric = _pairwise_development_score(dev, vectors, models[0])
        return metric["pairwise_accuracy"], metric, ()
    if task == "identity_candidates" or (
        task == "event_relationship" and head in {"event_relation", "severity", "credibility"}
    ):
        labels = tuple(TASK_SCHEMAS[task]["labels"] if task == "identity_candidates" else EVENT_HEAD_LABELS[head])
        predictions = _multiclass_predictions(dev, vectors, models[0], models[1], labels)
        metric = _group_metrics(dev, labels, predictions)
        return metric["macro_f1"], metric, ()
    thresholds = []
    retained_labels = []
    retained_models = []
    threshold_metrics = {}
    unfit = []
    for label, (weights, intercept) in zip(trained_labels, models):
        try:
            threshold, metric = _choose_binary_threshold(dev, vectors, weights, intercept, label)
        except CPUTrainingBlocked as exc:
            if exc.code != "TRAINING_BINARY_DEVELOPMENT_LABELS_INSUFFICIENT":
                raise
            unfit.append(label)
            continue
        thresholds.append(threshold)
        threshold_metrics[label] = metric
        retained_labels.append(label)
        retained_models.append((weights, intercept))
    if not retained_labels:
        raise CPUTrainingBlocked("TRAINING_DEVELOPMENT_LABELS_INSUFFICIENT")
    score, metrics = _macro_multilabel_f1(dev, vectors, retained_labels, retained_models, thresholds)
    metrics["development_thresholds"] = dict(zip(retained_labels, thresholds))
    metrics["threshold_review"] = threshold_metrics
    return score, metrics, tuple(thresholds), tuple(retained_labels), tuple(retained_models), unfit


def _fit_one_configuration(dataset, train, dev, numeric_stats, *, l2, epochs,
                           is_cancelled, deadline):
    task, head = dataset.task, dataset.head
    train_vectors, collisions = _encoded_partition(task, head, train, numeric_stats)
    dev_vectors, dev_collisions = _encoded_partition(task, head, dev, numeric_stats)
    if task == "evidence_ranking" or (task == "event_relationship" and head == "handling_value"):
        weights = _pairwise_epoch(
            train, train_vectors, task=task, l2=l2, epoch_count=epochs,
            is_cancelled=is_cancelled, deadline=deadline,
        )
        metric = _pairwise_development_score(dev, dev_vectors, weights)
        return ("linear_pairwise_ranker", ("preferred",), (weights,), (0.0,), (),
                metric["pairwise_accuracy"], metric, collisions + dev_collisions, [])

    if task == "identity_candidates" or (
        task == "event_relationship" and head in {"event_relation", "severity", "credibility"}
    ):
        labels = tuple(dataset.label_space)
        if not labels:
            raise CPUTrainingBlocked("TRAINING_LABEL_SPACE_REQUIRED")
        weights, intercepts = _softmax_epoch(
            train, train_vectors, labels, l2=l2, epoch_count=epochs,
            dimension=_feature_dimension(task), is_cancelled=is_cancelled, deadline=deadline,
        )
        predictions = _multiclass_predictions(dev, dev_vectors, weights, intercepts, labels)
        metric = _group_metrics(dev, labels, predictions)
        return ("multiclass_logistic", labels, tuple(weights), tuple(intercepts), (),
                metric["macro_f1"], metric, collisions + dev_collisions, [])

    labels = tuple(dataset.label_space)
    if not labels:
        raise CPUTrainingBlocked("TRAINING_LABEL_SPACE_REQUIRED")
    fitted_labels, fitted_models, unfit = _binary_epoch(
        train, train_vectors, labels, task=task, l2=l2, epoch_count=epochs,
        is_cancelled=is_cancelled, deadline=deadline,
    )
    if not fitted_labels:
        raise CPUTrainingBlocked("TRAINING_NO_COMPLETE_BINARY_HEADS")
    metric_score, metric, thresholds, trained_labels, fitted_models, unfit_dev = _metrics_and_thresholds(
        task, head, dev, dev_vectors, fitted_labels, fitted_models,
    )
    model_name = "one_vs_rest_logistic"
    return (model_name, tuple(trained_labels), tuple(item[0] for item in fitted_models),
            tuple(item[1] for item in fitted_models), thresholds,
            metric_score, {**metric, "unfit_labels": sorted(set(unfit) | set(unfit_dev))},
            collisions + dev_collisions, sorted(set(unfit) | set(unfit_dev)))


def fit_cpu_candidate(dataset: AdmittedTrainingDataset, *, budget: CPUResourceBudget | None = None,
                      is_cancelled: Callable[[], bool] | None = None) -> CPUModelCandidate:
    """Fit at most six CPU candidates from a sealed, source-authorized dataset.

    The returned object is a development-selected candidate only. This function
    never promotes, deploys, writes a model file, or reads the final partition.
    """
    if (type(dataset) is not AdmittedTrainingDataset or dataset._issuer is not _DATASET_ADMISSION_SEAL
            or not callable(dataset._revalidate)):
        raise CPUTrainingBlocked("TRAINING_DATA_ADMISSION_REQUIRED")
    if dataset.task not in TASK_SCHEMAS:
        raise CPUTrainingBlocked("CPU_TRAINING_TASK_UNSUPPORTED")
    if is_cancelled is not None and not callable(is_cancelled):
        raise CPUTrainingBlocked("CPU_TRAINING_CANCEL_CHECK_INVALID")
    if budget is None:
        budget = read_cpu_resource_budget()
    _validate_budget(budget)
    admitted_examples = dataset.training_examples + dataset.development_examples
    if _admitted_manifest_digest(dataset.task, dataset.head, admitted_examples) != dataset.manifest_sha256:
        raise CPUTrainingBlocked("TRAINING_DATASET_CHANGED_AFTER_ADMISSION")
    train = list(dataset.training_examples)
    dev = list(dataset.development_examples)
    if not train or not dev:
        raise CPUTrainingBlocked("TRAINING_REQUIRED_VIEW_MISSING")
    train_groups = {sample.group_id for sample in train}
    dev_groups = {sample.group_id for sample in dev}
    if not train_groups or not dev_groups or train_groups & dev_groups:
        raise CPUTrainingBlocked("TRAINING_GROUP_SPLIT_INVALID")
    if dataset.task == "evidence_ranking" or (dataset.task == "event_relationship" and dataset.head == "handling_value"):
        labels = ("preferred",)
    elif dataset.task == "identity_candidates" or (
        dataset.task == "event_relationship" and dataset.head in {"event_relation", "severity", "credibility"}
    ):
        labels = tuple(dataset.label_space)
        represented = {sample.label for sample in train}
        if set(labels) - represented:
            raise CPUTrainingBlocked("TRAINING_REQUIRED_CLASS_UNOBSERVED", {"class_count": len(set(labels) - represented)})
    else:
        labels = tuple(dataset.label_space)
    _validate_budget(budget)
    numeric_stats = _numeric_stats_for_examples(dataset.task, train, head=dataset.head)
    unavailable_required = [name for name, value in numeric_stats.items() if not value.available]
    if unavailable_required:
        raise CPUTrainingBlocked("TRAINING_REQUIRED_NUMERIC_FEATURE_UNAVAILABLE", {"field_count": len(unavailable_required)})
    estimated_bytes = _fit_estimated_bytes(dataset.task, labels, train + dev)
    if estimated_bytes > budget.peak_bytes:
        raise CPUTrainingBlocked("CPU_TRAINING_MEMORY_BUDGET_EXCEEDED", {
            "estimated_bytes": estimated_bytes, "budget_bytes": budget.peak_bytes,
        })
    try:
        dataset._revalidate()
    except CPUTrainingBlocked:
        raise
    except Exception as exc:
        raise CPUTrainingBlocked("TRAINING_ADMISSION_RECHECK_FAILED") from exc

    deadline = time.monotonic() + budget.max_seconds
    configurations = [(l2, epochs) for l2 in CPU_L2_CANDIDATES for epochs in CPU_EPOCH_CANDIDATES]
    if len(configurations) > CPU_MAX_CONFIGURATIONS:
        raise CPUTrainingBlocked("CPU_RECIPE_CONFIGURATION_BUDGET_INVALID")
    best = None
    trials = []
    for l2, epochs in configurations:
        if time.monotonic() >= deadline:
            raise CPUTrainingBlocked("CPU_TRAINING_TIME_BUDGET_EXCEEDED")
        if is_cancelled is not None:
            try:
                cancelled = is_cancelled()
            except Exception as exc:
                raise CPUTrainingBlocked("CPU_TRAINING_CANCEL_CHECK_FAILED") from exc
            if type(cancelled) is not bool:
                raise CPUTrainingBlocked("CPU_TRAINING_CANCEL_CHECK_INVALID")
            if cancelled:
                raise CPUTrainingBlocked("CPU_TRAINING_CANCELLED")
        try:
            dataset._revalidate()
        except CPUTrainingBlocked:
            raise
        except Exception as exc:
            raise CPUTrainingBlocked("TRAINING_ADMISSION_RECHECK_FAILED") from exc
        started = time.monotonic()
        fitted = _fit_one_configuration(
            dataset, train, dev, numeric_stats, l2=l2, epochs=epochs,
            is_cancelled=is_cancelled, deadline=deadline,
        )
        elapsed = time.monotonic() - started
        model_name, model_labels, weights, intercepts, thresholds, score, metrics, collisions, unfit = fitted
        trials.append({"l2": l2, "epochs": epochs, "development_score": score,
                       "elapsed_seconds": elapsed, "hashed_feature_collisions": collisions,
                       "unfit_label_count": len(unfit)})
        rank = (score, -elapsed, l2, -epochs)
        if best is None or rank > best[0]:
            best = (rank, model_name, model_labels, weights, intercepts, thresholds,
                    score, metrics, collisions, unfit, l2, epochs)
    if best is None:
        raise CPUTrainingBlocked("CPU_TRAINING_NO_CANDIDATE")
    try:
        dataset._revalidate()
    except CPUTrainingBlocked:
        raise
    except Exception as exc:
        raise CPUTrainingBlocked("TRAINING_ADMISSION_RECHECK_FAILED") from exc

    (_, model_name, model_labels, weights, intercepts, thresholds,
     score, metrics, collisions, unfit, selected_l2, selected_epochs) = best
    training_lineage = tuple({
        "sample_id": sample.sample_id, "group_id": sample.group_id,
        "source_ref_sha256": _sha256(_canonical_json(sample.source_refs)),
        "label_review_id": sample.label_review_id,
        "source_use_decision_id": sample.source_use_decision_id,
    } for sample in train)
    development_lineage = tuple({
        "sample_id": sample.sample_id, "group_id": sample.group_id,
        "source_ref_sha256": _sha256(_canonical_json(sample.source_refs)),
        "label_review_id": sample.label_review_id,
        "source_use_decision_id": sample.source_use_decision_id,
    } for sample in dev)
    candidate_state = "PARTIAL_NOT_PROMOTED" if unfit else "DEVELOPMENT_SELECTED_NOT_PROMOTED"
    numeric_serialized = tuple(
        (name, stats.mean, stats.scale, stats.available)
        for name, stats in numeric_stats.items()
    )
    candidate = CPUModelCandidate(
        task=dataset.task, head=dataset.head, model=model_name,
        labels=tuple(model_labels),
        recipe_version=CPU_RECIPE_VERSION,
        feature_revision=CPU_FEATURE_REVISION,
        weights=tuple(tuple((index, float(value)) for index, value in enumerate(weight) if value)
                      for weight in weights),
        intercepts=tuple(float(value) for value in intercepts),
        numeric_stats=numeric_serialized,
        selected_config=(selected_l2, selected_epochs),
        decision_thresholds=tuple(float(value) for value in thresholds),
        development_metrics={
            **metrics, "selected_development_score": score,
            "candidate_trials": trials,
            "hash_collision_count": collisions,
            "development_set_sha256": _sample_partition_digest(admitted_examples, "development"),
            "final_evaluation_state": "NOT_RUN",
        },
        training_lineage=training_lineage,
        development_lineage=development_lineage,
        training_manifest_sha256=_sample_partition_digest(admitted_examples, "train"),
        development_manifest_sha256=_sample_partition_digest(admitted_examples, "development"),
        package_sha256="0" * 64,
        candidate_state=candidate_state,
        _issuer=_CANDIDATE_SEAL,
    )
    package_sha = _sha256(_canonical_json(_candidate_payload(candidate)))
    return CPUModelCandidate(**{
        **candidate.__dict__, "package_sha256": package_sha,
    })
