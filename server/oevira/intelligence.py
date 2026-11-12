"""One Intelligence owner over existing Work, Material, Artifact and domain owners."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from typing import Callable

from . import api as _api
from .intelligence_base import IntelligenceError, INTELLIGENCE_OUTPUT_FORMATS, identity_scope
from .intelligence_cpu import (
    CPUTrainingBlocked,
    TrainingDataAdmission,
    cpu_candidate_status,
    evaluate_cpu_candidate as _evaluate_cpu_candidate,
    fit_cpu_candidate,
    load_cpu_candidate_package as _load_cpu_candidate_package,
    read_cpu_resource_budget,
    serialize_cpu_candidate as _serialize_cpu_candidate,
)
from .intelligence_codex import CodexAppServerGateway, CodexAppServerRuntimeResolver, CodexRuntimePaths
from .intelligence_memory import CorrectableMemory, install_intelligence_memory_schema
from .intelligence_provider import (
    CHAT_COMPLETIONS_PROTOCOL,
    RESPONSES_PROTOCOL,
    UserConfiguredChatGateway,
)
from .intelligence_skills import IntelligenceSkillStore, SkillLifecycleError, install_intelligence_skill_schema
from .intelligence_models import IntelligenceModelSettings, ModelSettingsError, install_intelligence_model_schema
from .intelligence_retrieval import _safe_source_metadata, search_material_evidence
from .intelligence_tools import IntelligenceToolRouter, OwnerToolBinding, ToolExecutionContext
from .collaboration import require_work_access, require_material_access, require_artifact_access


@dataclass(frozen=True)
class ExplicitModelSelection:
    """A non-secret selection resolved by the user's existing model-settings owner."""

    route: str
    selection_id: str
    revision: str
    model_id: str
    recipient_id: str
    selected_by_user: bool
    protocol: str = CHAT_COMPLETIONS_PROTOCOL


@dataclass(frozen=True)
class ModelInputAuthorization:
    """A current, exact SourceUse/Permission decision from the existing shared owner."""

    allowed: bool
    workspace_id: str
    user_id: str
    selection_id: str
    selection_revision: str
    route: str
    model_id: str
    source_ref_sha256: str
    purpose: str
    recipient: str
    authorization_id: str
    expires_at: str


@dataclass(frozen=True)
class ExternalModelApproval:
    """One exact, single-use Effect-Fence decision for a user-configured route."""

    allowed: bool
    workspace_id: str
    user_id: str
    selection_id: str
    selection_revision: str
    recipient: str
    request_sha256: str
    approval_id: str
    expires_at: str


_PREPARED_CONTEXT_SEAL = object()


@dataclass(frozen=True)
class PreparedIntelligenceContext:
    """A server-read, version-pinned task context; clients cannot assemble model inputs."""

    identity: dict
    goal: str
    query: str
    work: dict | None
    artifact: dict | None
    media_project: dict | None
    evidence: dict
    memory: dict
    tool_results: tuple[dict, ...]
    conversation_history: tuple[dict, ...]
    skills: tuple[dict, ...]
    skill_snapshots: tuple[dict, ...]
    allowed_tool_names: tuple[str, ...] | None
    source_refs: tuple[dict, ...]
    source_ref_sha256: str
    citations: tuple[dict, ...]
    payload_sha256: str
    requested_output_formats: tuple[str, ...]
    artifact_write_intent: bool
    _issuer: object

    def payload(self) -> dict:
        return json.loads(_canonical({
            "goal": self.goal,
            "query": self.query,
            "work": self.work,
            "artifact": self.artifact,
            "media_project": self.media_project,
            "evidence": self.evidence,
            "memory": self.memory,
            "requested_output_formats": list(self.requested_output_formats),
            "tool_results": list(self.tool_results),
            "conversation_history": list(self.conversation_history),
            "skills": list(self.skills),
        }))


def _canonical(value) -> bytes:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise IntelligenceError("INTELLIGENCE_INPUT_INVALID", 400) from exc


def _digest(value) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _media_project_digest(project: dict) -> str:
    return _digest({key: value for key, value in project.items() if key != "current_version"})


def install_intelligence_schema(store) -> None:
    """Install Intelligence-owned tables into the shared product Store."""
    install_intelligence_memory_schema(store)
    from .intelligence_conversation import install_intelligence_conversation_schema
    install_intelligence_conversation_schema(store)
    install_intelligence_skill_schema(store)
    install_intelligence_model_schema(store)


class IntelligenceService:
    """Compose real source retrieval, editable context, owner tools and gated inference."""

    def __init__(self, store, *, model_gateway: Callable | None = None,
                 model_selection_resolver: Callable | None = None,
                 source_use_authorizer: Callable | None = None,
                 external_effect_authorizer: Callable | None = None,
                 owned_inference_runner: Callable | None = None,
                 user_model_connection_resolver: Callable | None = None,
                 codex_runtime_resolver: Callable | None = None,
                 codex_transport_for_test: Callable | None = None,
                 training_use_authorizer: Callable | None = None,
                 label_review_resolver: Callable | None = None,
                 available_action_ids: set[str] | None = None,
                 owner_tool_effect_authorizer: Callable | None = None,
                 owner_tool_bindings: tuple[OwnerToolBinding, ...] = (),
                 ):
        self.store = store
        self.model_settings = IntelligenceModelSettings(store)
        model_selection_resolver = model_selection_resolver or self.model_settings.resolve_selection
        user_model_connection_resolver = user_model_connection_resolver or self.model_settings.resolve_connection
        source_use_authorizer = source_use_authorizer or self.model_settings.authorize_model_input
        external_effect_authorizer = external_effect_authorizer or self.model_settings.authorize_provider_request
        codex_runtime_resolver = codex_runtime_resolver or CodexAppServerRuntimeResolver(store)
        self.codex_runtime_resolver = codex_runtime_resolver
        self.model_gateway = model_gateway or (
            UserConfiguredChatGateway(user_model_connection_resolver)
            if user_model_connection_resolver is not None else None
        )
        self.codex_gateway = (
            CodexAppServerGateway(
                self, connection_resolver=user_model_connection_resolver,
                runtime_resolver=codex_runtime_resolver,
                transport_for_test=codex_transport_for_test,
            )
            if user_model_connection_resolver is not None and codex_runtime_resolver is not None else None
        )
        self.model_selection_resolver = model_selection_resolver
        self.source_use_authorizer = source_use_authorizer
        self.training_use_authorizer = training_use_authorizer
        self.external_effect_authorizer = external_effect_authorizer
        self.owned_inference_runner = owned_inference_runner
        self.memory = CorrectableMemory(store)
        self.skills = IntelligenceSkillStore(store)
        self.training_admission = TrainingDataAdmission(
            store,
            source_use_resolver=training_use_authorizer,
            label_review_resolver=label_review_resolver,
            available_action_ids=available_action_ids,
        )
        self.tools = IntelligenceToolRouter(
            self, owner_bindings=owner_tool_bindings,
            external_effect_authorizer=owner_tool_effect_authorizer,
        )

    def status(self) -> dict:
        """Report only actual bindings; no provider selection, model probing or fallback."""
        return {
            "owner": "Intelligence",
            "base_operations": "AVAILABLE",
            "lexical_evidence_retrieval": "AVAILABLE",
            "user_correctable_memory": "AVAILABLE",
            "github_skill_store": "BOUND_PUBLIC_AND_USER_AUTHORIZED_PRIVATE_READ_ONLY",
            "github_skill_runtime": "DECLARATIVE_SKILLS_RESTRICTED_SCRIPTS_AND_MCP_HTTP_READ_TOOLS",
            "github_skill_discovery": "USER_INITIATED_NOT_RUN",
            "github_credential_protection": self.skills.credential_protection_status(),
            "mcp_http_runtime": "MCP_2026_07_28_AND_LEGACY_2025_11_25_BOUND",
            "mcp_local_stdio_runtime": "NOT_BOUND_OS_SANDBOX_REQUIRED",
            "skill_script_runtime": "RESTRICTED_PYTHON_AST_TOOL_SUBSET_BOUND",
            "skill_script_dependencies": "NOT_SUPPORTED_NO_DEPENDENCY_INSTALLER",
            "semantic_retrieval": "NOT_BOUND",
            "cross_encoder_reranking": "NOT_BOUND",
            "owned_inference": "BOUND" if self.owned_inference_runner is not None else "NOT_BOUND",
            "user_configured_model_route": "SETTINGS_OWNER_BOUND",
            "model_selection_state": "RESOLVED_PER_AUTHENTICATED_USER",
            "provider_credentials": self.model_settings.credential_protection_status(),
            "user_configured_model_protocols": [
                protocol for protocol, bound in (
                    (CHAT_COMPLETIONS_PROTOCOL, self.model_gateway is not None),
                    (RESPONSES_PROTOCOL, self.codex_gateway is not None),
                ) if bound
            ],
            "codex_app_server_harness": {
                **(self.codex_runtime_resolver.status() if hasattr(self.codex_runtime_resolver, "status")
                   else {"state": "BOUND_NOT_PROBED" if self.codex_gateway is not None else "NOT_BOUND"}),
                "dynamic_product_tools": self.codex_gateway is not None,
                "built_in_os_tools": "DENIED_BY_LOCAL_RESPONSES_GATE",
                "model_execution": "NOT_RUN",
            },
            "fallback_allowed": False,
            "tools": self.tools.catalog(),
            "cpu_candidate": cpu_candidate_status(),
        }

    def read_work(self, identity: dict, work_id: str) -> dict:
        workspace_id, _user_id, _session_id = identity_scope(identity)
        if not isinstance(work_id, str) or not work_id:
            raise IntelligenceError("WORK_SCOPE_INVALID", 400)
        try:
            with self.store.transaction(immediate=False) as connection:
                _verify_actor(connection, identity)
                row = require_work_access(connection, identity, work_id)
                if row is None:
                    raise IntelligenceError("SOURCE_SCOPE_NOT_FOUND", 404)
                return _api._work_data(connection, row)
        except IntelligenceError:
            raise
        except _api.ApiError as exc:
            code = "SOURCE_SCOPE_NOT_FOUND" if exc.code == "NOT_FOUND" else exc.code
            raise IntelligenceError(code, exc.status, exc.details) from exc
        except Exception as exc:
            raise IntelligenceError("WORK_READ_UNAVAILABLE", 503) from exc

    def read_material(self, identity: dict, material_id: str) -> dict:
        workspace_id, _user_id, _session_id = identity_scope(identity)
        if not isinstance(material_id, str) or not material_id:
            raise IntelligenceError("MATERIAL_ID_INVALID", 400)
        try:
            with self.store.transaction(immediate=False) as connection:
                _verify_actor(connection, identity)
                row = require_material_access(connection, identity, material_id)
                if row is None:
                    raise IntelligenceError("SOURCE_SCOPE_NOT_FOUND", 404)
                material = _api._material_data(connection, row)
                safe_url, source_metadata = _safe_source_metadata(row)
                # Material source_json is imported or captured metadata, not a
                # credential-safe model contract. Expose only the shared
                # provenance allowlist and never forward source_text (which can
                # contain raw HTML or access-bearing page state).
                material.pop("source_text", None)
                material["source_url"] = safe_url
                material["source"] = source_metadata or None
                return material
        except IntelligenceError:
            raise
        except _api.ApiError as exc:
            code = "SOURCE_SCOPE_NOT_FOUND" if exc.code == "NOT_FOUND" else exc.code
            raise IntelligenceError(code, exc.status, exc.details) from exc
        except Exception as exc:
            raise IntelligenceError("MATERIAL_READ_UNAVAILABLE", 503) from exc

    def read_media_project(self, identity: dict, media_project_id: str, *, work_id: str,
                           version: int | None = None) -> dict:
        """Read one versioned Media project explicitly selected in this Work."""
        workspace_id, _user_id, _session_id = identity_scope(identity)
        if (not isinstance(media_project_id, str) or not media_project_id
                or work_id is not None and (not isinstance(work_id, str) or not work_id)):
            raise IntelligenceError("MEDIA_PROJECT_SCOPE_INVALID", 400)
        try:
            with self.store.transaction(immediate=False) as connection:
                _verify_actor(connection, identity)
                if work_id is not None:
                    require_work_access(connection, identity, work_id)
                row = connection.execute(
                    "SELECT id, work_id, current_version FROM media_projects WHERE id=? AND workspace_id=?",
                    (media_project_id, workspace_id),
                ).fetchone()
                if (row is None or (work_id is None and row["work_id"] is not None)
                        or (work_id is not None and row["work_id"] not in {None, work_id})):
                    raise IntelligenceError("SOURCE_SCOPE_NOT_FOUND", 404)
                selected_version = row["current_version"] if version is None else version
                if type(selected_version) is not int or selected_version < 1:
                    raise IntelligenceError("MEDIA_PROJECT_VERSION_INVALID", 400)
            from .media_api import _project_view
            view = _project_view(self.store, workspace_id, media_project_id, selected_version)
            project_work_id = view.get("work_id")
            if ((project_work_id is not None and project_work_id != work_id)
                    or (work_id is None and project_work_id is not None)
                    or view.get("version") != selected_version
                    or version is None and view.get("current_version") != row["current_version"]):
                raise IntelligenceError("INTELLIGENCE_MEDIA_PROJECT_CHANGED", 409)
            with self.store.transaction(immediate=False) as connection:
                _verify_actor(connection, identity)
                if work_id is not None:
                    require_work_access(connection, identity, work_id)
                for source in view.get("sources", []):
                    material_id = source.get("source_material_id") if isinstance(source, dict) else None
                    if isinstance(material_id, str) and material_id:
                        require_material_access(connection, identity, material_id)
            return {key: view.get(key) for key in (
                "id", "title", "kind", "work_id", "version", "current_version", "clock",
                "sources", "tracks", "audio_buses", "video_color_interpretations",
                "transcript_sources", "transitions", "overlays", "chapters", "format_ratio", "duration",
            )}
        except IntelligenceError:
            raise
        except _api.ApiError as exc:
            code = "SOURCE_SCOPE_NOT_FOUND" if exc.code == "NOT_FOUND" else exc.code
            raise IntelligenceError(code, exc.status, exc.details) from exc
        except Exception as exc:
            raise IntelligenceError("MEDIA_PROJECT_READ_UNAVAILABLE", 503) from exc

    def read_artifact(self, identity: dict, artifact_id: str, *, version: int | None = None) -> dict:
        workspace_id, _user_id, _session_id = identity_scope(identity)
        if not isinstance(artifact_id, str) or not artifact_id:
            raise IntelligenceError("ARTIFACT_ID_INVALID", 400)
        if version is not None and (isinstance(version, bool) or not isinstance(version, int) or version < 1):
            raise IntelligenceError("ARTIFACT_VERSION_INVALID", 400)
        try:
            with self.store.transaction(immediate=False) as connection:
                _verify_actor(connection, identity)
                row = require_artifact_access(connection, identity, artifact_id)
                if row is None:
                    raise IntelligenceError("SOURCE_SCOPE_NOT_FOUND", 404)
                result = _api._artifact_data(connection, row, version)
                result["current_version"] = row["current_version"]
                try:
                    result["validated_citations"] = _api._validate_citations(
                        connection, workspace_id, result["citations"],
                    )
                    result["citation_state"] = "current"
                except Exception:
                    result["validated_citations"] = []
                    result["citation_state"] = "stale_or_unavailable"
                return result
        except IntelligenceError:
            raise
        except _api.ApiError as exc:
            code = "SOURCE_SCOPE_NOT_FOUND" if exc.code == "NOT_FOUND" else exc.code
            raise IntelligenceError(code, exc.status, exc.details) from exc
        except Exception as exc:
            raise IntelligenceError("ARTIFACT_READ_UNAVAILABLE", 503) from exc

    def search_materials(self, identity: dict, query: str, *, work_id: str | None = None,
                         material_ids: list[str] | None = None, limit: int = 12,
                         is_cancelled: Callable[[], bool] | None = None) -> dict:
        return search_material_evidence(
            self.store, identity, query, work_id=work_id, material_ids=material_ids,
            limit=limit, is_cancelled=is_cancelled,
        )

    def prepare_context(self, identity: dict, goal: str, *, work_id: str | None = None,
                        material_ids: list[str] | None = None, query: str | None = None,
                        artifact_id: str | None = None, artifact_version: int | None = None,
                        media_project_id: str | None = None,
                        include_memory: bool = True,
                        requested_output_formats: tuple[str, ...] = (),
                        artifact_write_intent: bool = True) -> PreparedIntelligenceContext:
        """Read the actual task objects and return locatable evidence without claiming inference."""
        if not isinstance(goal, str) or not goal.strip() or len(goal) > 5_000:
            raise IntelligenceError("INTELLIGENCE_GOAL_INVALID", 400)
        if (type(include_memory) is not bool or type(artifact_write_intent) is not bool
                or media_project_id is not None and (not isinstance(media_project_id, str) or not media_project_id)
                or not isinstance(requested_output_formats, tuple)
                or any(value not in INTELLIGENCE_OUTPUT_FORMATS for value in requested_output_formats)):
            raise IntelligenceError("INTELLIGENCE_CONTEXT_OPTIONS_INVALID", 400)
        work = self.read_work(identity, work_id) if work_id is not None else None
        if work is not None and work["status"] == "archived":
            raise IntelligenceError("WORK_ARCHIVED", 409)
        media_project = (
            self.read_media_project(identity, media_project_id, work_id=work_id)
            if media_project_id is not None else None
        )
        artifact = None
        artifact_citations = []
        artifact_citation_state = "none"
        if artifact_id is not None:
            artifact = self.read_artifact(identity, artifact_id, version=artifact_version)
            if work_id is not None and artifact.get("work_id") != work_id:
                raise IntelligenceError("SOURCE_SCOPE_NOT_FOUND", 404)
            if artifact.get("citations"):
                workspace_id, _user_id, _session_id = identity_scope(identity)
                try:
                    with self.store.transaction(immediate=False) as connection:
                        _verify_actor(connection, identity)
                        artifact_citations = _api._validate_citations(
                            connection, workspace_id, artifact["citations"],
                        )
                        artifact_citation_state = "current"
                except Exception:
                    artifact_citations = []
                    artifact_citation_state = "stale_or_unavailable"
            artifact = {**artifact, "citation_state": artifact_citation_state}
        selected_query = query if query is not None else goal
        media_source_ids = sorted({
            source.get("source_material_id")
            for source in media_project.get("sources", [])
            if isinstance(source, dict) and isinstance(source.get("source_material_id"), str)
        }) if media_project is not None else []
        if media_project is not None:
            scoped_material_ids = set(media_source_ids)
            if work is not None:
                scoped_material_ids.update(
                    item for item in work.get("material_ids", []) if isinstance(item, str)
                )
            if material_ids is not None:
                scoped_material_ids.update(material_ids)
            evidence = self.search_materials(
                identity, selected_query, material_ids=sorted(scoped_material_ids),
            ) if scoped_material_ids else {
                "status": "no_data", "query": selected_query,
                "scope": {"work_id": work_id, "material_ids": []},
                "coverage": {"complete_scope_scanned": True},
                "sources": [], "evidence": [], "citations": [],
                "limitations": ["No authorized Materials are attached to this Media project or Work."],
                "research_candidates": {"status": "no_matches", "results": []},
            }
        elif material_ids is not None or work_id is not None:
            evidence = self.search_materials(
                identity, selected_query, work_id=work_id, material_ids=material_ids,
            )
        else:
            evidence = {
                "status": "no_scope", "query": selected_query,
                "scope": {"work_id": None, "material_ids": []},
                "coverage": {"complete_scope_scanned": False},
                "sources": [], "evidence": [], "citations": [],
                "limitations": ["Choose an authorized Work or specific Materials to search."],
            }
        memory = self.memory.context(identity, work_id=work_id) if include_memory else {
            "items": [], "excluded_count": 0, "status": "not_requested",
            "canonical_state": "NOT_CANONICAL", "parameter_training": "NOT_GRANTED",
        }
        source_refs = [{
            "kind": "user_input", "goal_sha256": hashlib.sha256(goal.encode("utf-8")).hexdigest(),
            "query_sha256": hashlib.sha256(selected_query.encode("utf-8")).hexdigest(),
        }]
        if work is not None:
            work_payload = {"id": work["id"], "version": work["version"], "title": work["title"],
                            "goal": work["goal"], "brief": work["brief"]}
            source_refs.append({
                "kind": "work_version", "work_id": work["id"], "version": work["version"],
                "content_sha256": _digest(work_payload),
            })
        if media_project is not None:
            source_refs.append({
                "kind": "media_project_version", "media_project_id": media_project["id"],
                "work_id": media_project["work_id"], "version": media_project["current_version"],
                "content_sha256": _media_project_digest(media_project),
            })
            for source in media_project.get("sources", []):
                material_id = source.get("source_material_id") if isinstance(source, dict) else None
                if not isinstance(material_id, str) or not material_id:
                    continue
                material = self.read_material(identity, material_id)
                if material.get("sha256") != source.get("sha256"):
                    raise IntelligenceError("INTELLIGENCE_MEDIA_PROJECT_SOURCE_CHANGED", 409)
                source_refs.append({
                    "kind": "media_project_source", "media_project_id": media_project["id"],
                    "media_project_version": media_project["current_version"],
                    "material_id": material_id, "material_sha256": material["sha256"],
                    "source_status": material["status"],
                })
        if artifact is not None:
            artifact_snapshot = artifact.get("input_snapshot") if isinstance(artifact.get("input_snapshot"), dict) else {}
            artifact_material_versions = {
                item.get("id"): item.get("sha256")
                for item in artifact_snapshot.get("materials", [])
                if isinstance(item, dict) and isinstance(item.get("id"), str)
            }
            artifact_payload = {
                "id": artifact["id"], "version": artifact["version"], "title": artifact["title"],
                "body": artifact["body"], "citations": artifact["citations"],
                "input_snapshot": artifact["input_snapshot"],
            }
            source_refs.append({
                "kind": "artifact_version", "artifact_id": artifact["id"], "version": artifact["version"],
                "content_sha256": _digest(artifact_payload),
                "citation_refs_sha256": _digest(artifact["citations"]),
                "citation_state": artifact_citation_state,
            })
            for citation in artifact.get("citations", []):
                if not isinstance(citation, dict):
                    continue
                source_refs.append({
                    "kind": "artifact_citation", "artifact_id": artifact["id"],
                    "artifact_version": artifact["version"],
                    "material_id": citation.get("material_id"),
                    "segment_id": citation.get("segment_id"),
                    "material_sha256": artifact_material_versions.get(citation.get("material_id")),
                    "quote_sha256": hashlib.sha256(citation.get("quote", "").encode("utf-8")).hexdigest()
                    if isinstance(citation.get("quote"), str) else None,
                    "citation_state": artifact_citation_state,
                })
        for source in evidence.get("sources", []):
            source_refs.append({
                "kind": "material", "material_id": source["material_id"],
                "material_sha256": source["material_sha256"], "status": source["status"],
            })
        for passage in evidence.get("evidence", []):
            source_refs.append({
                "kind": "material_segment", "material_id": passage["material_id"],
                "material_sha256": passage["material_sha256"], "segment_id": passage["segment_id"],
                "start_offset": passage["start_offset"], "end_offset": passage["end_offset"],
                "quote_sha256": passage["quote_sha256"],
            })
        for item in memory.get("items", []):
            source_refs.append({
                "kind": "user_memory", "memory_id": item["memory_id"], "version": item["version"],
                "content_sha256": hashlib.sha256(item["content"].encode("utf-8")).hexdigest(),
                "work_id": item["work_id"],
                "source_ref_sha256": _digest(item["source_refs"]),
            })
        legacy_candidates = evidence.get("research_candidates")
        if isinstance(legacy_candidates, dict):
            for lane in ("derived_from_current_materials", "preserved_legacy"):
                result = legacy_candidates.get(lane)
                for candidate in result.get("results", []) if isinstance(result, dict) else []:
                    source = candidate.get("source") if isinstance(candidate, dict) else None
                    if isinstance(source, dict) and source.get("kind") in {
                        "material_body_candidate", "legacy_research_candidate",
                    }:
                        source_refs.append(source)
        unique_refs = {_canonical(reference): reference for reference in source_refs}
        ordered_refs = tuple(unique_refs[key] for key in sorted(unique_refs))
        requested_formats = tuple(sorted(set(requested_output_formats)))
        payload_sha256 = _digest({
            "goal": goal, "query": selected_query, "work": work, "artifact": artifact,
            "media_project": media_project,
            "evidence": evidence, "memory": memory,
            "requested_output_formats": list(requested_formats),
            "tool_results": [],
            "conversation_history": [],
            "skills": [],
        })
        return PreparedIntelligenceContext(
            identity=json.loads(_canonical(identity)),
            goal=goal,
            query=selected_query,
            work=work,
            artifact=artifact,
            media_project=media_project,
            evidence=evidence,
            memory=memory,
            tool_results=(),
            conversation_history=(),
            skills=(),
            skill_snapshots=(),
            allowed_tool_names=None,
            source_refs=ordered_refs,
            source_ref_sha256=_digest(list(ordered_refs)),
            citations=tuple(evidence.get("citations", []) + artifact_citations),
            payload_sha256=payload_sha256,
            requested_output_formats=requested_formats,
            artifact_write_intent=artifact_write_intent,
            _issuer=_PREPARED_CONTEXT_SEAL,
        )

    def with_conversation_context(self, prepared: PreparedIntelligenceContext, *,
                                  conversation_id: str, history: list[dict],
                                  prior_source_refs: list[dict],
                                  prior_citations: list[dict],
                                  include_history: bool = True) -> PreparedIntelligenceContext:
        """Bind persisted turns and prior sources to a new authorized model turn."""
        if (type(prepared) is not PreparedIntelligenceContext
                or prepared._issuer is not _PREPARED_CONTEXT_SEAL
                or not isinstance(conversation_id, str) or not conversation_id
                or type(include_history) is not bool
                or not isinstance(history, list) or len(history) > 80
                or not isinstance(prior_source_refs, list) or len(prior_source_refs) > 20_000
                or not isinstance(prior_citations, list) or len(prior_citations) > 512):
            raise IntelligenceError("INTELLIGENCE_CONVERSATION_CONTEXT_INVALID", 400)
        normalized_history = []
        message_refs = []
        history_characters = 0
        for item in history:
            if (not isinstance(item, dict) or set(item) != {"turn_id", "role", "content"}
                    or not isinstance(item.get("turn_id"), str) or not item["turn_id"]
                    or item.get("role") not in {"user", "assistant"}
                    or not isinstance(item.get("content"), str) or not item["content"]):
                raise IntelligenceError("INTELLIGENCE_CONVERSATION_CONTEXT_INVALID", 400)
            history_characters += len(item["content"])
            if history_characters > 250_000:
                raise IntelligenceError("INTELLIGENCE_CONVERSATION_CONTEXT_TOO_LARGE", 413)
            if include_history:
                normalized_history.append({"role": item["role"], "content": item["content"]})
            message_refs.append({
                "kind": "conversation_message", "conversation_id": conversation_id,
                "turn_id": item["turn_id"], "role": item["role"],
                "content_sha256": hashlib.sha256(item["content"].encode("utf-8")).hexdigest(),
            })
        allowed_prior_kinds = {
            "work_version", "material", "material_segment", "artifact_version",
            "artifact_citation", "user_memory", "skill_package", "media_project_version",
            "material_body_candidate", "legacy_research_candidate",
        }
        retained_prior_refs = []
        for item in prior_source_refs:
            if not isinstance(item, dict) or not isinstance(item.get("kind"), str):
                raise IntelligenceError("INTELLIGENCE_CONVERSATION_CONTEXT_INVALID", 400)
            if item["kind"] in {"user_input", "conversation_message"}:
                continue
            if item["kind"] not in allowed_prior_kinds:
                raise IntelligenceError("INTELLIGENCE_CONVERSATION_CONTEXT_INVALID", 400)
            retained_prior_refs.append(item)
        unique_refs = {
            _canonical(item): item
            for item in [*prepared.source_refs, *retained_prior_refs, *message_refs]
        }
        ordered_refs = tuple(unique_refs[key] for key in sorted(unique_refs))
        citations_by_key = {}
        for citation in [*prepared.citations, *prior_citations]:
            if (not isinstance(citation, dict)
                    or set(citation) != {"material_id", "segment_id", "quote"}
                    or not all(isinstance(citation.get(key), str) and citation[key]
                               for key in ("material_id", "segment_id", "quote"))):
                raise IntelligenceError("INTELLIGENCE_CONVERSATION_CITATION_INVALID", 409)
            citations_by_key[_canonical(citation)] = citation
        citations = tuple(citations_by_key[key] for key in sorted(citations_by_key))
        workspace_id, _user_id, _session_id = identity_scope(prepared.identity)
        source_keys = {
            (item.get("material_id"), item.get("segment_id"), item.get("quote"))
            for item in citations
        }
        if prior_citations:
            try:
                with self.store.transaction(immediate=False) as connection:
                    _verify_actor(connection, prepared.identity)
                    validated = _api._validate_citations(connection, workspace_id, list(prior_citations))
                    if len(validated) != len(prior_citations):
                        raise IntelligenceError("INTELLIGENCE_CONVERSATION_CITATION_INVALID", 409)
            except IntelligenceError:
                raise
            except Exception as exc:
                raise IntelligenceError("INTELLIGENCE_CONVERSATION_CITATION_RECHECK_UNAVAILABLE", 503) from exc
            source_material_ids = {
                item.get("material_id") for item in ordered_refs
                if item.get("kind") in {"material", "material_segment", "artifact_citation"}
            }
            if any(citation["material_id"] not in source_material_ids for citation in prior_citations):
                raise IntelligenceError("INTELLIGENCE_CONVERSATION_CITATION_OUTSIDE_SCOPE", 409)
        extended = PreparedIntelligenceContext(
            identity=prepared.identity, goal=prepared.goal, query=prepared.query,
            work=prepared.work, artifact=prepared.artifact, media_project=prepared.media_project,
            evidence=prepared.evidence,
            memory=prepared.memory, tool_results=prepared.tool_results,
            conversation_history=tuple(normalized_history),
            skills=prepared.skills, skill_snapshots=prepared.skill_snapshots,
            allowed_tool_names=prepared.allowed_tool_names,
            source_refs=ordered_refs, source_ref_sha256=_digest(list(ordered_refs)),
            citations=citations,
            payload_sha256=_digest({
                "goal": prepared.goal, "query": prepared.query, "work": prepared.work,
                "artifact": prepared.artifact, "media_project": prepared.media_project,
                "evidence": prepared.evidence,
                "memory": prepared.memory,
                "requested_output_formats": list(prepared.requested_output_formats),
                "tool_results": list(prepared.tool_results),
                "conversation_history": normalized_history,
                "skills": list(prepared.skills),
            }),
            requested_output_formats=prepared.requested_output_formats,
            artifact_write_intent=prepared.artifact_write_intent,
            _issuer=_PREPARED_CONTEXT_SEAL,
        )
        self._revalidate_prepared_context(extended)
        return extended

    def with_skill_context(self, prepared: PreparedIntelligenceContext,
                           snapshots: list[dict]) -> PreparedIntelligenceContext:
        """Bind enabled Skill instructions and exact product-tool grants to one turn."""
        if type(prepared) is not PreparedIntelligenceContext or prepared._issuer is not _PREPARED_CONTEXT_SEAL:
            raise IntelligenceError("INTELLIGENCE_CONTEXT_INVALID", 400)
        try:
            prompts, refs = self.skills.load_turn_context(prepared.identity, snapshots, self)
        except SkillLifecycleError as exc:
            raise IntelligenceError(exc.code, exc.status, exc.details) from exc
        allowed_tools = sorted({name for skill in prompts for name in skill["allowed_tools"]}) if snapshots else None
        source_map = {_canonical(item): item for item in [*prepared.source_refs, *refs]}
        ordered_refs = tuple(source_map[key] for key in sorted(source_map))
        payload_sha = _digest({
            "goal": prepared.goal, "query": prepared.query, "work": prepared.work,
            "artifact": prepared.artifact, "media_project": prepared.media_project,
            "evidence": prepared.evidence, "memory": prepared.memory,
            "requested_output_formats": list(prepared.requested_output_formats),
            "tool_results": list(prepared.tool_results),
            "conversation_history": list(prepared.conversation_history), "skills": prompts,
        })
        context = PreparedIntelligenceContext(
            identity=prepared.identity, goal=prepared.goal, query=prepared.query,
            work=prepared.work, artifact=prepared.artifact, media_project=prepared.media_project,
            evidence=prepared.evidence,
            memory=prepared.memory, tool_results=prepared.tool_results,
            conversation_history=prepared.conversation_history, skills=tuple(prompts),
            skill_snapshots=tuple(json.loads(_canonical(item)) for item in snapshots),
            allowed_tool_names=tuple(allowed_tools) if allowed_tools is not None else None,
            source_refs=ordered_refs, source_ref_sha256=_digest(list(ordered_refs)),
            citations=prepared.citations, payload_sha256=payload_sha,
            requested_output_formats=prepared.requested_output_formats,
            artifact_write_intent=prepared.artifact_write_intent, _issuer=_PREPARED_CONTEXT_SEAL,
        )
        self._revalidate_prepared_context(context)
        return context

    def public_context(self, context: PreparedIntelligenceContext) -> dict:
        """Return the safe display package for the user-facing current task."""
        if type(context) is not PreparedIntelligenceContext or context._issuer is not _PREPARED_CONTEXT_SEAL:
            raise IntelligenceError("INTELLIGENCE_CONTEXT_INVALID", 400)
        return {
            "status": "prepared",
            "goal": context.goal,
            "work": context.work,
            "artifact": context.artifact,
            "media_project": ({
                "id": context.media_project["id"], "title": context.media_project["title"],
                "kind": context.media_project["kind"], "version": context.media_project["current_version"],
            } if context.media_project else None),
            "evidence": context.evidence,
            "memory": context.memory,
            "tool_results": list(context.tool_results),
            "versions": {
                "work_version": context.work.get("version") if context.work else None,
                "artifact_version": context.artifact.get("version") if context.artifact else None,
                "materials": [
                    {"material_id": source["material_id"], "sha256": source["material_sha256"],
                     "status": source["status"]}
                    for source in context.evidence.get("sources", [])
                ],
            },
            "requested_output_formats": list(context.requested_output_formats),
            "skills": [
                {key: skill[key] for key in (
                    "skill_id", "name", "repository", "path", "version", "revision", "content_sha256", "allowed_tools",
                )} for skill in context.skills
            ],
            "trust": {
                "material_content": "UNTRUSTED_SOURCE_CONTENT",
                "artifact_content": "USER_EDITABLE_DRAFT_NOT_FACT",
                "memory_content": "USER_ASSERTED_CONTEXT_NOT_CANONICAL",
            },
            "model_execution": "NOT_RUN",
            "limitations": [
                "Preparation reads existing task data and cited passages; it is not a model answer.",
                "Lexical relevance does not establish fact status, permission, support or contradiction.",
            ],
        }

    def create_tool_context(self, prepared: PreparedIntelligenceContext, *, caller: str = "model",
                            operation_scope: str | None = None,
                            is_cancelled: Callable[[], bool] | None = None) -> ToolExecutionContext:
        if type(prepared) is not PreparedIntelligenceContext or prepared._issuer is not _PREPARED_CONTEXT_SEAL:
            raise IntelligenceError("INTELLIGENCE_CONTEXT_INVALID", 400)
        self._revalidate_prepared_context(prepared)
        return self.tools.create_context(
            prepared, caller=caller, operation_scope=operation_scope, is_cancelled=is_cancelled,
        )

    def extend_context(self, prepared: PreparedIntelligenceContext,
                       tool_context: ToolExecutionContext) -> PreparedIntelligenceContext:
        """Bind actual local tool results and their new source refs to a subsequent model turn."""
        if (type(prepared) is not PreparedIntelligenceContext or prepared._issuer is not _PREPARED_CONTEXT_SEAL
                or not isinstance(tool_context, ToolExecutionContext)
                or tool_context.prepared_context_sha256 != prepared.payload_sha256
                or identity_scope(tool_context.identity)[:2] != identity_scope(prepared.identity)[:2]):
            raise IntelligenceError("INTELLIGENCE_TOOL_CONTEXT_CHANGED", 409)
        references = {_canonical(reference): reference for reference in tool_context.source_refs}
        ordered_refs = tuple(references[key] for key in sorted(references))
        citations = tuple({
            "material_id": material_id, "segment_id": segment_id, "quote": quote,
        } for material_id, segment_id, quote in sorted(tool_context.citations))
        tool_results = tuple(json.loads(_canonical(result)) for result in tool_context.tool_results)
        media_project = prepared.media_project
        for operation in tool_context.tool_results:
            value = operation.get("result") if isinstance(operation, dict) else None
            project = value.get("project") if isinstance(value, dict) else None
            if isinstance(project, dict) and isinstance(project.get("id"), str):
                media_project = project
        formats = tuple(prepared.requested_output_formats)
        payload_sha = _digest({
            "goal": prepared.goal, "query": prepared.query, "work": prepared.work,
            "artifact": prepared.artifact, "media_project": media_project,
            "evidence": prepared.evidence, "memory": prepared.memory,
            "requested_output_formats": list(formats), "tool_results": list(tool_results),
            "conversation_history": list(prepared.conversation_history),
            "skills": list(prepared.skills),
        })
        extended = PreparedIntelligenceContext(
            identity=prepared.identity, goal=prepared.goal, query=prepared.query,
            work=prepared.work, artifact=prepared.artifact, media_project=media_project,
            evidence=prepared.evidence,
            memory=prepared.memory, tool_results=tool_results,
            conversation_history=prepared.conversation_history,
            skills=prepared.skills, skill_snapshots=prepared.skill_snapshots,
            allowed_tool_names=prepared.allowed_tool_names,
            source_refs=ordered_refs, source_ref_sha256=_digest(list(ordered_refs)),
            citations=citations, payload_sha256=payload_sha,
            requested_output_formats=formats, artifact_write_intent=prepared.artifact_write_intent,
            _issuer=_PREPARED_CONTEXT_SEAL,
        )
        tool_context.prepared_context_sha256 = payload_sha
        return extended

    def resume_from_saved_tool_results(self, prepared: PreparedIntelligenceContext, *,
                                       tool_results: list[dict], source_refs: list[dict],
                                       citations: list[dict], operation_scope: str,
                                       is_cancelled: Callable[[], bool] | None = None) -> PreparedIntelligenceContext:
        """Continue after verified tool receipts without reissuing their operations."""
        tool_context = self.create_tool_context(
            prepared, caller="model", operation_scope=operation_scope, is_cancelled=is_cancelled,
        )
        self.tools.restore_saved_tool_results(
            tool_context, tool_results, source_refs=source_refs, citations=citations,
        )
        continued = self.extend_context(prepared, tool_context)
        self._revalidate_prepared_context(continued)
        return continued

    def invoke_tool(self, identity: dict, tool_name: str, arguments: dict,
                    context: ToolExecutionContext) -> dict:
        return self.tools.invoke(identity, tool_name, arguments, context)

    def run_model(self, context: PreparedIntelligenceContext, *, selection_id: str | None,
                  is_cancelled: Callable[[], bool] | None = None, _turn: int = 0,
                  codex_thread_id: str | None = None,
                  conversation_id: str | None = None,
                  operation_scope: str | None = None,
                  on_event: Callable[[dict], None] | None = None) -> dict:
        """Run the current user-selected model only after exact data/effect admission."""
        if type(context) is not PreparedIntelligenceContext or context._issuer is not _PREPARED_CONTEXT_SEAL:
            raise IntelligenceError("INTELLIGENCE_CONTEXT_INVALID", 400)
        if isinstance(_turn, bool) or not isinstance(_turn, int) or not 0 <= _turn <= 8:
            raise IntelligenceError("INTELLIGENCE_TOOL_TURN_INVALID", 400)
        if (operation_scope is not None and (not isinstance(operation_scope, str)
                or not operation_scope or len(operation_scope) > 128)):
            raise IntelligenceError("INTELLIGENCE_TOOL_OPERATION_SCOPE_INVALID", 400)
        if (is_cancelled is not None and not callable(is_cancelled)
                or on_event is not None and not callable(on_event)
                or codex_thread_id is not None and (not isinstance(codex_thread_id, str) or not codex_thread_id)
                or conversation_id is not None and (not isinstance(conversation_id, str) or not conversation_id)):
            raise IntelligenceError("INTELLIGENCE_CANCEL_CHECK_INVALID", 400)
        if self.model_selection_resolver is None:
            return self._model_unavailable("MODEL_SELECTION_OWNER_NOT_BOUND")
        try:
            selection = self.model_selection_resolver(context.identity, selection_id)
        except ModelSettingsError as exc:
            return self._model_unavailable(exc.code, status="blocked")
        except Exception:
            return self._model_unavailable("MODEL_SELECTION_UNAVAILABLE")
        if (selection is None or type(selection) is not ExplicitModelSelection
                or selection.selected_by_user is not True
                or selection.route not in {"owned", "user_configured"}
                or not selection.selection_id or not selection.revision or not selection.model_id
                or not selection.recipient_id
                or selection.protocol not in {CHAT_COMPLETIONS_PROTOCOL, RESPONSES_PROTOCOL}):
            return self._model_unavailable("MODEL_NOT_SELECTED")
        if self.source_use_authorizer is None:
            return self._model_unavailable("SOURCE_USE_OWNER_NOT_BOUND", provider=selection.route,
                                           model_id=selection.model_id)

        try:
            payload = context.payload()
            payload["tools"] = self.tools.catalog(
                set(context.allowed_tool_names) if context.allowed_tool_names is not None else None,
            )["tools"]
            self._revalidate_prepared_context(context)
            authorization = self.source_use_authorizer(
                context.identity, selection, context.source_refs, purpose="inference",
            )
        except ModelSettingsError as exc:
            return self._model_unavailable(exc.code, provider=selection.route,
                                            model_id=selection.model_id, status="blocked")
        except IntelligenceError as exc:
            return self._model_unavailable(exc.code, provider=selection.route,
                                            model_id=selection.model_id, status="blocked")
        except Exception:
            return self._model_unavailable("SOURCE_USE_CHECK_UNAVAILABLE", provider=selection.route,
                                           model_id=selection.model_id)
        workspace_id, user_id, _session_id = identity_scope(context.identity)
        if (type(authorization) is not ModelInputAuthorization or authorization.allowed is not True
                or authorization.workspace_id != workspace_id or authorization.user_id != user_id
                or authorization.selection_id != selection.selection_id
                or authorization.selection_revision != selection.revision
                or authorization.route != selection.route or authorization.model_id != selection.model_id
                or authorization.source_ref_sha256 != context.source_ref_sha256
                or authorization.purpose != "inference" or not authorization.authorization_id
                or authorization.recipient != selection.recipient_id
                or not _future_time(authorization.expires_at)):
            return self._model_unavailable("SOURCE_USE_NOT_AUTHORIZED", provider=selection.route,
                                           model_id=selection.model_id, status="blocked")

        if selection.route == "user_configured" and selection.protocol == RESPONSES_PROTOCOL:
            if self.codex_gateway is None:
                return self._model_unavailable(
                    "CODEX_APP_SERVER_RUNTIME_NOT_BOUND", provider=selection.route,
                    model_id=selection.model_id,
                )
            try:
                result = self.codex_gateway.run_turn(
                    selection, context, is_cancelled=is_cancelled,
                    on_event=on_event, codex_thread_id=codex_thread_id,
                    operation_scope=operation_scope, conversation_id=conversation_id,
                )
            except IntelligenceError as exc:
                return self._model_unavailable(
                    exc.code, provider=selection.route, model_id=selection.model_id,
                    execution="ATTEMPTED", status="error",
                )
            partial_context = result.get("_prepared_context")
            partial_source_refs = (
                list(partial_context.source_refs)
                if type(partial_context) is PreparedIntelligenceContext
                and partial_context._issuer is _PREPARED_CONTEXT_SEAL
                else list(context.source_refs)
            )
            partial_citations = (
                list(partial_context.citations)
                if type(partial_context) is PreparedIntelligenceContext
                and partial_context._issuer is _PREPARED_CONTEXT_SEAL
                else list(context.citations)
            )
            if result.get("status") != "completed":
                return {
                    "status": result.get("status", "unavailable"),
                    "code": result.get("code") or result.get("error_code") or "CODEX_TURN_NOT_COMPLETED",
                    "provider": selection.route, "model_id": selection.model_id,
                    "execution": "ATTEMPTED", "fallback_allowed": False,
                    "tool_results": result.get("tool_results", []),
                    "codex_thread_id": result.get("codex_thread_id"),
                    "_source_refs": partial_source_refs,
                    "_citations": partial_citations,
                    "provider_request_count": result.get("provider_request_count"),
                    "catalog_request_count": result.get("catalog_request_count"),
                }
            final_context = result.get("_prepared_context")
            if (type(final_context) is not PreparedIntelligenceContext
                    or final_context._issuer is not _PREPARED_CONTEXT_SEAL):
                return {
                    "status": "error", "code": "CODEX_CONTEXT_READBACK_INVALID",
                    "provider": selection.route, "model_id": selection.model_id,
                    "execution": "ATTEMPTED", "fallback_allowed": False,
                    "tool_results": result.get("tool_results", []),
                    "codex_thread_id": result.get("codex_thread_id"),
                    "_source_refs": partial_source_refs,
                    "_citations": partial_citations,
                }
            text = result.get("text")
            citations = result.get("citations")
            if (not isinstance(text, str) or not text.strip() or len(text) > 200_000
                    or not isinstance(citations, list)):
                return {
                    "status": "error", "code": "CODEX_FINAL_OUTPUT_SCHEMA_INVALID",
                    "provider": selection.route, "model_id": selection.model_id,
                    "execution": "ATTEMPTED", "fallback_allowed": False,
                    "tool_results": result.get("tool_results", []),
                    "codex_thread_id": result.get("codex_thread_id"),
                    "_source_refs": partial_source_refs,
                    "_citations": partial_citations,
                }
            try:
                self._validate_model_citations(final_context.identity, citations, list(final_context.citations))
                self._revalidate_prepared_context(final_context)
                current_authorization = self.source_use_authorizer(
                    final_context.identity, selection, final_context.source_refs, purpose="inference",
                )
                current_selection = self.model_selection_resolver(final_context.identity, selection_id)
            except IntelligenceError as exc:
                return {
                    "status": "error", "code": exc.code,
                    "provider": selection.route, "model_id": selection.model_id,
                    "execution": "ATTEMPTED", "fallback_allowed": False,
                    "tool_results": result.get("tool_results", []),
                    "codex_thread_id": result.get("codex_thread_id"),
                    "_source_refs": partial_source_refs,
                    "_citations": partial_citations,
                }
            except Exception:
                return {
                    "status": "error", "code": "CODEX_RESULT_RECHECK_UNAVAILABLE",
                    "provider": selection.route, "model_id": selection.model_id,
                    "execution": "ATTEMPTED", "fallback_allowed": False,
                    "tool_results": result.get("tool_results", []),
                    "codex_thread_id": result.get("codex_thread_id"),
                    "_source_refs": partial_source_refs,
                    "_citations": partial_citations,
                }
            if (type(current_authorization) is not ModelInputAuthorization
                    or current_authorization.allowed is not True
                    or current_authorization.workspace_id != workspace_id
                    or current_authorization.user_id != user_id
                    or current_authorization.selection_id != selection.selection_id
                    or current_authorization.selection_revision != selection.revision
                    or current_authorization.route != selection.route
                    or current_authorization.model_id != selection.model_id
                    or current_authorization.source_ref_sha256 != final_context.source_ref_sha256
                    or current_authorization.purpose != "inference"
                    or current_authorization.recipient != selection.recipient_id
                    or not current_authorization.authorization_id
                    or not _future_time(current_authorization.expires_at)
                    or current_selection != selection):
                return {
                    "status": "error", "code": "CODEX_CONTEXT_OR_AUTHORIZATION_CHANGED",
                    "provider": selection.route, "model_id": selection.model_id,
                    "execution": "ATTEMPTED", "fallback_allowed": False,
                    "tool_results": result.get("tool_results", []),
                    "codex_thread_id": result.get("codex_thread_id"),
                    "_source_refs": partial_source_refs,
                    "_citations": partial_citations,
                }
            return {
                "status": "completed", "provider": selection.route,
                "model_id": selection.model_id, "model_revision": selection.revision,
                "text": text, "citations": citations,
                "content_trust": "MODEL_GENERATED_NOT_CANONICAL",
                "source_admission": "UNCHANGED_BY_INFERENCE",
                "evaluation_state": "NOT_INDEPENDENTLY_EVALUATED",
                "fallback_allowed": False, "execution": "COMPLETED",
                "tool_results": result.get("tool_results", []),
                "codex_thread_id": result.get("codex_thread_id"),
                "provider_request_count": result.get("provider_request_count"),
                "tool_call_count": result.get("tool_call_count"),
                "_source_refs": list(final_context.source_refs),
                "_citations": list(final_context.citations),
                "_prepared_context": final_context,
            }

        approval = None
        exact_request_approvals = []
        if selection.route == "user_configured":
            if self.external_effect_authorizer is None:
                return self._model_unavailable("EXTERNAL_EFFECT_AUTHORIZER_NOT_BOUND",
                                               provider=selection.route, model_id=selection.model_id,
                                               status="blocked")
            if (selection.protocol != CHAT_COMPLETIONS_PROTOCOL
                    or not isinstance(self.model_gateway, UserConfiguredChatGateway)):
                return self._model_unavailable("USER_MODEL_ADAPTER_NOT_BOUND",
                                               provider=selection.route, model_id=selection.model_id,
                                               status="blocked")

        runner = self.owned_inference_runner if selection.route == "owned" else self.model_gateway
        if runner is None:
            return self._model_unavailable("SELECTED_MODEL_ROUTE_UNAVAILABLE",
                                           provider=selection.route, model_id=selection.model_id)
        if is_cancelled is not None:
            try:
                cancelled = is_cancelled()
            except Exception:
                return self._model_unavailable("INTELLIGENCE_CANCEL_CHECK_UNAVAILABLE",
                                               provider=selection.route, model_id=selection.model_id)
            if type(cancelled) is not bool:
                return self._model_unavailable("INTELLIGENCE_CANCEL_CHECK_INVALID",
                                               provider=selection.route, model_id=selection.model_id)
            if cancelled:
                return self._model_unavailable("INTELLIGENCE_TASK_CANCELLED",
                                               provider=selection.route, model_id=selection.model_id,
                                               status="cancelled")
        def authorize_exact_provider_request(request_body: dict, request_sha256: str) -> bool:
            if (not isinstance(request_body, dict) or not isinstance(request_sha256, str)
                    or _digest(request_body) != request_sha256):
                raise IntelligenceError("EXTERNAL_MODEL_REQUEST_BINDING_INVALID", 403)
            self._revalidate_prepared_context(context)
            try:
                current_selection = self.model_selection_resolver(context.identity, selection_id)
                current_authorization = self.source_use_authorizer(
                    context.identity, selection, context.source_refs, purpose="inference",
                )
                approval = self.external_effect_authorizer(
                    context.identity, selection, current_authorization, request_body, request_sha256,
                )
            except ModelSettingsError as exc:
                raise IntelligenceError(exc.code, exc.status, exc.details) from exc
            except IntelligenceError:
                raise
            except Exception as exc:
                raise IntelligenceError("EXTERNAL_EFFECT_CHECK_UNAVAILABLE", 503) from exc
            if (current_selection != selection
                    or type(current_authorization) is not ModelInputAuthorization
                    or current_authorization.allowed is not True
                    or current_authorization.workspace_id != workspace_id
                    or current_authorization.user_id != user_id
                    or current_authorization.selection_id != selection.selection_id
                    or current_authorization.selection_revision != selection.revision
                    or current_authorization.route != selection.route
                    or current_authorization.model_id != selection.model_id
                    or current_authorization.source_ref_sha256 != context.source_ref_sha256
                    or current_authorization.purpose != "inference"
                    or current_authorization.recipient != selection.recipient_id
                    or not _future_time(current_authorization.expires_at)):
                raise IntelligenceError("SOURCE_USE_NOT_AUTHORIZED", 403)
            if (type(approval) is not ExternalModelApproval or approval.allowed is not True
                    or approval.workspace_id != workspace_id or approval.user_id != user_id
                    or approval.selection_id != selection.selection_id
                    or approval.selection_revision != selection.revision
                    or approval.recipient != selection.recipient_id
                    or approval.request_sha256 != request_sha256 or not approval.approval_id
                    or not _future_time(approval.expires_at)):
                raise IntelligenceError("EXTERNAL_MODEL_APPROVAL_REQUIRED", 403)
            exact_request_approvals.append(approval)
            return True

        try:
            if selection.route == "user_configured":
                result = runner(
                    selection, payload, is_cancelled=is_cancelled,
                    request_authorizer=authorize_exact_provider_request,
                )
            else:
                result = runner(selection, payload, is_cancelled=is_cancelled)
        except IntelligenceError as exc:
            return self._model_unavailable(
                exc.code, provider=selection.route, model_id=selection.model_id,
                execution="NOT_RUN" if exc.code in {
                    "EXTERNAL_MODEL_APPROVAL_REQUIRED", "SOURCE_USE_NOT_AUTHORIZED",
                    "USER_MODEL_SETTINGS_CHANGED", "INTELLIGENCE_CONTEXT_CHANGED",
                } else "ATTEMPTED",
                status="blocked" if exc.code in {
                    "EXTERNAL_MODEL_APPROVAL_REQUIRED", "SOURCE_USE_NOT_AUTHORIZED",
                    "USER_MODEL_SETTINGS_CHANGED", "INTELLIGENCE_CONTEXT_CHANGED",
                } else "error",
            )
        except Exception:
            return self._model_unavailable("MODEL_EXECUTION_FAILED", provider=selection.route,
                                           model_id=selection.model_id, execution="ATTEMPTED", status="error")
        if exact_request_approvals:
            approval = exact_request_approvals[-1]
        if not isinstance(result, dict) or result.get("status") not in {"completed", "complete"}:
            return self._model_unavailable("MODEL_RESULT_INVALID", provider=selection.route,
                                           model_id=selection.model_id, execution="ATTEMPTED", status="error")
        if "tool_calls" in result:
            calls = result.get("tool_calls")
            if not isinstance(calls, list) or not calls or len(calls) > 8:
                raise IntelligenceError("MODEL_TOOL_CALLS_INVALID", 502)
            self._revalidate_prepared_context(context)
            try:
                current_authorization = self.source_use_authorizer(
                    context.identity, selection, context.source_refs, purpose="inference",
                )
                current_selection = self.model_selection_resolver(context.identity, selection_id)
            except Exception as exc:
                raise IntelligenceError("MODEL_RESULT_RECHECK_UNAVAILABLE", 503) from exc
            if current_authorization != authorization or current_selection != selection:
                raise IntelligenceError("INTELLIGENCE_CONTEXT_OR_AUTHORIZATION_CHANGED", 409)
            if approval is not None and not _future_time(approval.expires_at):
                raise IntelligenceError("EXTERNAL_MODEL_APPROVAL_EXPIRED", 409)
            if is_cancelled is not None:
                try:
                    cancelled = is_cancelled()
                except Exception as exc:
                    raise IntelligenceError("INTELLIGENCE_CANCEL_CHECK_UNAVAILABLE", 503) from exc
                if type(cancelled) is not bool:
                    raise IntelligenceError("INTELLIGENCE_CANCEL_CHECK_INVALID", 503)
                if cancelled:
                    return self._model_unavailable(
                        "INTELLIGENCE_TASK_CANCELLED_AFTER_EXECUTION", provider=selection.route,
                        model_id=selection.model_id, execution="ATTEMPTED", status="cancelled",
                    )
            tool_context = self.create_tool_context(
                context, caller="model", operation_scope=operation_scope, is_cancelled=is_cancelled,
            )
            validated_calls = self._validate_model_tool_calls(calls, tool_context)
            # Run read operations first. A malformed or failed read therefore
            # cannot follow a draft write from the same model turn.
            ordered_calls = sorted(
                validated_calls,
                key=lambda call: (self.tools._bindings[call["name"]].effect != "read", call["name"]),
            )
            if sum(self.tools._bindings[call["name"]].effect == "draft_write" for call in ordered_calls) > 1:
                raise IntelligenceError("MODEL_MULTIPLE_DRAFT_WRITES_NOT_ALLOWED", 409)
            try:
                for call in ordered_calls:
                    self.invoke_tool(context.identity, call["name"], call["arguments"], tool_context)
            except IntelligenceError as exc:
                return {
                    "status": "tool_failed", "code": exc.code,
                    "execution": "PARTIAL" if tool_context.tool_results else "NO_EFFECT",
                    "tool_results": list(tool_context.tool_results),
                    "_source_refs": list(tool_context.source_refs),
                    "_citations": [
                        {"material_id": material_id, "segment_id": segment_id, "quote": quote}
                        for material_id, segment_id, quote in sorted(tool_context.citations)
                    ],
                    "content_trust": "MODEL_GENERATED_NOT_CANONICAL",
                    "fallback_allowed": False,
                }
            extended = self.extend_context(context, tool_context)
            if _turn >= 7:
                return {
                    "status": "tool_turn_limit", "code": "MODEL_TOOL_TURN_LIMIT_REACHED",
                    "tool_results": list(extended.tool_results),
                    "_source_refs": list(extended.source_refs),
                    "_citations": list(extended.citations),
                    "content_trust": "MODEL_GENERATED_NOT_CANONICAL",
                    "fallback_allowed": False,
                }
            completed = self.run_model(
                extended, selection_id=selection_id, is_cancelled=is_cancelled, _turn=_turn + 1,
                operation_scope=operation_scope, conversation_id=conversation_id,
            )
            if isinstance(completed, dict):
                prior_tool_turn = completed.get("_tool_turn", 0)
                if isinstance(prior_tool_turn, int) and not isinstance(prior_tool_turn, bool):
                    completed["_tool_turn"] = max(prior_tool_turn, _turn + 1)
            if completed.get("status") in {"completed", "unavailable", "approval_required", "blocked",
                                            "cancelled", "error", "tool_failed", "tool_turn_limit"}:
                # Preserve readback receipts when a later model round is blocked;
                # never imply that the saved operation was rolled back.
                for field, prior_values in (
                    ("tool_results", extended.tool_results),
                    ("_source_refs", extended.source_refs),
                    ("_citations", extended.citations),
                ):
                    if not isinstance(completed.get(field), list) or not completed[field]:
                        completed[field] = list(prior_values)
            return completed
        text = result.get("text")
        citations = result.get("citations", [])
        if not isinstance(text, str) or not text.strip() or len(text) > 200_000 or not isinstance(citations, list):
            raise IntelligenceError("MODEL_OUTPUT_SCHEMA_INVALID", 502)
        self._validate_model_citations(context.identity, citations, list(context.citations))
        try:
            self._revalidate_prepared_context(context)
            current_authorization = self.source_use_authorizer(
                context.identity, selection, context.source_refs, purpose="inference",
            )
            current_selection = self.model_selection_resolver(context.identity, selection_id)
        except Exception as exc:
            raise IntelligenceError("MODEL_RESULT_RECHECK_UNAVAILABLE", 503) from exc
        if (not _same_model_input_grant(current_authorization, authorization)
                or current_selection != selection or (approval is not None and not _future_time(approval.expires_at))):
            raise IntelligenceError("INTELLIGENCE_CONTEXT_OR_AUTHORIZATION_CHANGED", 409)
        if is_cancelled is not None:
            try:
                if is_cancelled():
                    return self._model_unavailable("INTELLIGENCE_TASK_CANCELLED_AFTER_EXECUTION",
                                                   provider=selection.route, model_id=selection.model_id,
                                                   execution="ATTEMPTED", status="cancelled")
            except Exception as exc:
                raise IntelligenceError("INTELLIGENCE_CANCEL_CHECK_UNAVAILABLE", 503) from exc
        return {
            "status": "completed", "provider": selection.route, "model_id": selection.model_id,
            "model_revision": selection.revision, "text": text, "citations": citations,
            "content_trust": "MODEL_GENERATED_NOT_CANONICAL",
            "source_admission": "UNCHANGED_BY_INFERENCE",
            "evaluation_state": "NOT_INDEPENDENTLY_EVALUATED",
            "tool_results": list(context.tool_results),
            "_source_refs": list(context.source_refs),
            "_citations": list(context.citations),
            "fallback_allowed": False, "execution": "COMPLETED",
        }

    def _validate_model_tool_calls(self, calls: list[dict], context: ToolExecutionContext) -> list[dict]:
        validated = []
        seen = set()
        for call in calls:
            if (not isinstance(call, dict) or set(call) != {"name", "arguments"}
                    or not isinstance(call.get("name"), str)
                    or not isinstance(call.get("arguments"), dict)):
                raise IntelligenceError("MODEL_TOOL_CALLS_INVALID", 502)
            name = call["name"]
            duplicate_key = _digest(call)
            if duplicate_key in seen:
                raise IntelligenceError("MODEL_TOOL_CALLS_INVALID", 502)
            seen.add(duplicate_key)
            binding = self.tools._bindings.get(name)
            if binding is None:
                raise IntelligenceError("INTELLIGENCE_TOOL_UNAVAILABLE", 409)
            if context.allowed_tool_names is not None and name not in context.allowed_tool_names:
                raise IntelligenceError("INTELLIGENCE_SKILL_TOOL_NOT_GRANTED", 403)
            if context.skill_snapshots:
                try:
                    with self.store.transaction(immediate=False) as connection:
                        self.skills.assert_active(connection, context.identity, list(context.skill_snapshots), self)
                except SkillLifecycleError as exc:
                    raise IntelligenceError(exc.code, exc.status, exc.details) from exc
                except IntelligenceError:
                    raise
                except Exception as exc:
                    raise IntelligenceError("INTELLIGENCE_SKILL_PERMISSION_RECHECK_UNAVAILABLE", 503) from exc
            arguments = call["arguments"]
            if (binding.required_fields - arguments.keys()
                    or arguments.keys() - (binding.required_fields | binding.optional_fields)):
                raise IntelligenceError("INTELLIGENCE_TOOL_INPUT_INVALID", 400)
            if binding.effect == "draft_write" and not context.artifact_write_intent:
                raise IntelligenceError("INTELLIGENCE_TOOL_WRITE_NOT_REQUESTED", 409)
            if binding.effect == "external_effect" and self.external_effect_authorizer is None:
                raise IntelligenceError("INTELLIGENCE_TOOL_EFFECT_NOT_AUTHORIZED", 403)
            validated.append({"name": name, "arguments": arguments})
        return validated

    @staticmethod
    def _model_unavailable(code: str, *, provider: str | None = None, model_id: str | None = None,
                           execution: str = "NOT_RUN", status: str = "unavailable") -> dict:
        return {
            "status": status, "code": code, "provider": provider, "model_id": model_id,
            "execution": execution, "fallback_allowed": False,
            "base_operations_available": True,
        }

    def _revalidate_prepared_context(self, context: PreparedIntelligenceContext) -> None:
        if (type(context) is not PreparedIntelligenceContext or context._issuer is not _PREPARED_CONTEXT_SEAL
                or _digest(context.payload()) != context.payload_sha256
                or _digest(list(context.source_refs)) != context.source_ref_sha256):
            raise IntelligenceError("INTELLIGENCE_CONTEXT_CHANGED", 409)
        workspace_id, _user_id, _session_id = identity_scope(context.identity)
        if context.media_project is not None:
            current_media_project = self.read_media_project(
                context.identity, context.media_project["id"], work_id=context.media_project["work_id"],
            )
            if (current_media_project.get("current_version") != context.media_project.get("current_version")
                    or _media_project_digest(current_media_project) != _media_project_digest(context.media_project)):
                raise IntelligenceError("INTELLIGENCE_MEDIA_PROJECT_CHANGED", 409)
        try:
            with self.store.transaction(immediate=False) as connection:
                _verify_actor(connection, context.identity)
                work_id = context.work.get("id") if context.work else None
                if work_id:
                    require_work_access(connection, context.identity, work_id)
                for reference in context.source_refs:
                    kind = reference.get("kind")
                    if reference.get("material_id"):
                        target_work_id = None if kind == "media_project_source" else work_id
                        require_material_access(connection, context.identity, reference["material_id"],
                                                target_work_id=target_work_id)
                    if reference.get("artifact_id"):
                        require_artifact_access(connection, context.identity, reference["artifact_id"])
                    if kind == "user_input":
                        if (reference.get("goal_sha256") != hashlib.sha256(context.goal.encode("utf-8")).hexdigest()
                                or reference.get("query_sha256") != hashlib.sha256(context.query.encode("utf-8")).hexdigest()):
                            raise IntelligenceError("INTELLIGENCE_CONTEXT_CHANGED", 409)
                    elif kind == "conversation_message":
                        turn = connection.execute(
                            "SELECT request_json, assistant_text, status FROM intelligence_turns "
                            "WHERE turn_id = ? AND conversation_id = ? AND workspace_id = ? AND owner_user_id = ?",
                            (reference.get("turn_id"), reference.get("conversation_id"),
                             workspace_id, context.identity["user"]["id"]),
                        ).fetchone()
                        if turn is None or turn["status"] in {"queued", "running"}:
                            raise IntelligenceError("INTELLIGENCE_CONTEXT_CHANGED", 409)
                        if reference.get("role") == "user":
                            request_payload = json.loads(turn["request_json"])
                            message = request_payload.get("message") if isinstance(request_payload, dict) else None
                        elif reference.get("role") == "assistant":
                            if turn["status"] not in {"completed", "partial"}:
                                raise IntelligenceError("INTELLIGENCE_CONTEXT_CHANGED", 409)
                            message = turn["assistant_text"]
                        else:
                            raise IntelligenceError("INTELLIGENCE_CONTEXT_CHANGED", 409)
                        if (not isinstance(message, str)
                                or hashlib.sha256(message.encode("utf-8")).hexdigest()
                                != reference.get("content_sha256")):
                            raise IntelligenceError("INTELLIGENCE_CONTEXT_CHANGED", 409)
                    elif kind == "work_version":
                        row = connection.execute(
                            "SELECT id, version, title, goal, brief_json FROM works WHERE id = ? AND workspace_id = ?",
                            (reference.get("work_id"), workspace_id),
                        ).fetchone()
                        if row is None or row["version"] != reference.get("version"):
                            raise IntelligenceError("INTELLIGENCE_CONTEXT_CHANGED", 409)
                        payload = {"id": row["id"], "version": row["version"], "title": row["title"],
                                   "goal": row["goal"], "brief": json.loads(row["brief_json"])}
                        if _digest(payload) != reference.get("content_sha256"):
                            raise IntelligenceError("INTELLIGENCE_CONTEXT_CHANGED", 409)
                    elif kind == "legacy_research_candidate":
                        from .intelligence_retrieval import revalidate_preserved_research_candidate_ref
                        revalidate_preserved_research_candidate_ref(connection, self.store, context.identity, reference)
                    elif kind == "media_project_version":
                        saved_media_project = self.read_media_project(
                            context.identity, reference.get("media_project_id"),
                            work_id=reference.get("work_id"),
                            version=reference.get("version"),
                        )
                        if _media_project_digest(saved_media_project) != reference.get("content_sha256"):
                            raise IntelligenceError("INTELLIGENCE_MEDIA_PROJECT_CHANGED", 409)
                    elif kind == "material":
                        row = connection.execute(
                            "SELECT sha256, status FROM materials WHERE id = ? AND workspace_id = ?",
                            (reference.get("material_id"), workspace_id),
                        ).fetchone()
                        if (row is None or row["sha256"] != reference.get("material_sha256")
                                or row["status"] != reference.get("status")):
                            raise IntelligenceError("INTELLIGENCE_CONTEXT_CHANGED", 409)
                    elif kind == "media_project_source":
                        row = connection.execute(
                            "SELECT sha256, status FROM materials WHERE id=? AND workspace_id=?",
                            (reference.get("material_id"), workspace_id),
                        ).fetchone()
                        if (row is None or row["sha256"] != reference.get("material_sha256")
                                or row["status"] != reference.get("source_status")):
                            raise IntelligenceError("INTELLIGENCE_CONTEXT_CHANGED", 409)
                    elif kind == "material_segment":
                        row = connection.execute(
                            "SELECT m.sha256, m.status, s.text, s.start_offset, s.end_offset "
                            "FROM materials m JOIN material_segments s ON s.material_id = m.id "
                            "AND s.workspace_id = m.workspace_id WHERE m.id = ? AND m.workspace_id = ? AND s.id = ?",
                            (reference.get("material_id"), workspace_id, reference.get("segment_id")),
                        ).fetchone()
                        if row is None or row["sha256"] != reference.get("material_sha256"):
                            raise IntelligenceError("INTELLIGENCE_CONTEXT_CHANGED", 409)
                        start = reference.get("start_offset") - row["start_offset"]
                        end = reference.get("end_offset") - row["start_offset"]
                        if (row["status"] not in {"ready", "partial"} or start < 0 or end > len(row["text"])
                                or end < start or hashlib.sha256(row["text"][start:end].encode("utf-8")).hexdigest()
                                != reference.get("quote_sha256")):
                            raise IntelligenceError("INTELLIGENCE_CONTEXT_CHANGED", 409)
                    elif kind == "material_body_candidate":
                        row = connection.execute(
                            "SELECT sha256, status, text FROM materials WHERE id = ? AND workspace_id = ?",
                            (reference.get("material_id"), workspace_id),
                        ).fetchone()
                        start = reference.get("start_offset")
                        end = reference.get("end_offset")
                        if (row is None or row["sha256"] != reference.get("material_sha256")
                                or row["status"] != reference.get("source_status")
                                or not isinstance(row["text"], str)
                                or hashlib.sha256(row["text"].encode("utf-8")).hexdigest()
                                != reference.get("text_sha256")
                                or type(start) is not int or type(end) is not int
                                or start < 0 or end <= start or end > len(row["text"])
                                or hashlib.sha256(row["text"][start:end].encode("utf-8")).hexdigest()
                                != reference.get("quote_sha256")):
                            raise IntelligenceError("INTELLIGENCE_CONTEXT_CHANGED", 409)
                    elif kind == "artifact_version":
                        row = connection.execute(
                            "SELECT title, body, citations_json, input_snapshot_json FROM artifact_versions "
                            "WHERE artifact_id = ? AND workspace_id = ? AND version = ?",
                            (reference.get("artifact_id"), workspace_id, reference.get("version")),
                        ).fetchone()
                        if row is None:
                            raise IntelligenceError("INTELLIGENCE_CONTEXT_CHANGED", 409)
                        payload = {
                            "id": reference["artifact_id"], "version": reference["version"],
                            "title": row["title"], "body": row["body"],
                            "citations": json.loads(row["citations_json"] or "[]"),
                            "input_snapshot": json.loads(row["input_snapshot_json"] or "{}"),
                        }
                        if _digest(payload) != reference.get("content_sha256"):
                            raise IntelligenceError("INTELLIGENCE_CONTEXT_CHANGED", 409)
                    elif kind == "user_memory":
                        row = connection.execute(
                            "SELECT content, status, source_refs_json FROM intelligence_memories "
                            "WHERE memory_id = ? AND workspace_id = ? AND owner_user_id = ? AND version = ?",
                            (reference.get("memory_id"), workspace_id, context.identity["user"]["id"], reference.get("version")),
                        ).fetchone()
                        if (row is None or row["status"] != "active" or row["content"] is None
                                or hashlib.sha256(row["content"].encode("utf-8")).hexdigest() != reference.get("content_sha256")
                                or _digest(json.loads(row["source_refs_json"] or "[]")) != reference.get("source_ref_sha256")):
                            raise IntelligenceError("INTELLIGENCE_CONTEXT_CHANGED", 409)
                    elif kind == "artifact_citation":
                        artifact = connection.execute(
                            "SELECT citations_json, input_snapshot_json FROM artifact_versions WHERE artifact_id = ? "
                            "AND workspace_id = ? AND version = ?",
                            (reference.get("artifact_id"), workspace_id, reference.get("artifact_version")),
                        ).fetchone()
                        if artifact is None:
                            raise IntelligenceError("INTELLIGENCE_CONTEXT_CHANGED", 409)
                        citations = json.loads(artifact["citations_json"] or "[]")
                        if not any(
                            item.get("material_id") == reference.get("material_id")
                            and item.get("segment_id") == reference.get("segment_id")
                            and hashlib.sha256(item.get("quote", "").encode("utf-8")).hexdigest() == reference.get("quote_sha256")
                            for item in citations if isinstance(item, dict) and isinstance(item.get("quote"), str)
                        ):
                            raise IntelligenceError("INTELLIGENCE_CONTEXT_CHANGED", 409)
                        material = connection.execute(
                            "SELECT sha256, status FROM materials WHERE id = ? AND workspace_id = ?",
                            (reference.get("material_id"), workspace_id),
                        ).fetchone()
                        segment = connection.execute(
                            "SELECT text FROM material_segments WHERE material_id = ? AND id = ? AND workspace_id = ?",
                            (reference.get("material_id"), reference.get("segment_id"), workspace_id),
                        ).fetchone()
                        matching_quote = next((
                            item.get("quote") for item in citations
                            if isinstance(item, dict)
                            and item.get("material_id") == reference.get("material_id")
                            and item.get("segment_id") == reference.get("segment_id")
                            and isinstance(item.get("quote"), str)
                            and hashlib.sha256(item["quote"].encode("utf-8")).hexdigest() == reference.get("quote_sha256")
                        ), None)
                        if (material is None or segment is None or material["status"] not in {"ready", "partial"}
                                or not reference.get("material_sha256")
                                or material["sha256"] != reference.get("material_sha256")
                                or not isinstance(matching_quote, str) or matching_quote not in segment["text"]):
                            raise IntelligenceError("INTELLIGENCE_CONTEXT_CHANGED", 409)
                    elif kind == "skill_package":
                        matching = next((item for item in context.skill_snapshots
                                         if item.get("skill_id") == reference.get("skill_id")
                                         and item.get("version") == reference.get("version")
                                         and item.get("content_sha256") == reference.get("content_sha256")
                                         and item.get("tool_schema_sha256") == reference.get("tool_schema_sha256")
                                         and item.get("license_sha256") == reference.get("license_sha256")), None)
                        if matching is None:
                            raise IntelligenceError("INTELLIGENCE_CONTEXT_CHANGED", 409)
                        try:
                            self.skills.assert_active(connection, context.identity, [matching], self)
                        except SkillLifecycleError as exc:
                            raise IntelligenceError(exc.code, exc.status, exc.details) from exc
            self.tools.revalidate_prepared_tool_results(context)
        except IntelligenceError:
            raise
        except Exception as exc:
            raise IntelligenceError("INTELLIGENCE_CONTEXT_RECHECK_UNAVAILABLE", 503) from exc

    def _validate_model_citations(self, identity: dict, citations: list[dict], source_refs: list[dict]) -> None:
        if len(citations) > 256:
            raise IntelligenceError("MODEL_CITATIONS_INVALID", 502)
        workspace_id, _user_id, _session_id = identity_scope(identity)
        source_keys = {
            (item.get("material_id"), item.get("segment_id"), item.get("quote"))
            for item in source_refs if isinstance(item, dict)
        }
        for citation in citations:
            if not isinstance(citation, dict):
                raise IntelligenceError("MODEL_CITATIONS_INVALID", 502)
            key = (citation.get("material_id"), citation.get("segment_id"), citation.get("quote"))
            if key not in source_keys:
                raise IntelligenceError("MODEL_CITATION_OUTSIDE_CONTEXT", 502)
        try:
            with self.store.transaction(immediate=False) as connection:
                _verify_actor(connection, identity)
                _api._validate_citations(connection, workspace_id, citations)
        except IntelligenceError:
            raise
        except Exception as exc:
            raise IntelligenceError("MODEL_CITATION_INVALID", 502) from exc

    def training_status(self) -> dict:
        status = cpu_candidate_status()
        status["trainer_state"] = "IMPLEMENTED_NOT_RUN"
        status["data_admission_state"] = (
            "READY_FOR_EXPLICIT_ADMISSION"
            if self.training_use_authorizer is not None and self.training_admission.label_review_resolver is not None
            else "REQUIRES_CURRENT_SOURCE_USE_AND_INDEPENDENT_LABEL_REVIEW"
        )
        status["source_use_owner_bound"] = self.training_use_authorizer is not None
        status["independent_label_review_bound"] = self.training_admission.label_review_resolver is not None
        status["fit_allowed_by_default"] = False
        return status

    def admit_training_dataset(self, identity: dict, task: str, examples: list[dict], *, head: str | None = None,
                               evaluation_candidate=None,
                               is_cancelled: Callable[[], bool] | None = None):
        try:
            return self.training_admission.admit(
                identity, task, examples, head=head,
                evaluation_candidate=evaluation_candidate, is_cancelled=is_cancelled,
            )
        except CPUTrainingBlocked as exc:
            raise IntelligenceError(exc.code, 409, exc.details) from exc

    def fit_cpu_candidate(self, dataset, *, is_cancelled: Callable[[], bool] | None = None):
        try:
            return fit_cpu_candidate(dataset, budget=read_cpu_resource_budget(), is_cancelled=is_cancelled)
        except CPUTrainingBlocked as exc:
            raise IntelligenceError(exc.code, 409, exc.details) from exc

    @staticmethod
    def serialize_cpu_candidate(candidate) -> bytes:
        try:
            return _serialize_cpu_candidate(candidate)
        except CPUTrainingBlocked as exc:
            raise IntelligenceError(exc.code, 409, exc.details) from exc

    @staticmethod
    def load_cpu_candidate_package(content: bytes):
        try:
            return _load_cpu_candidate_package(content)
        except CPUTrainingBlocked as exc:
            raise IntelligenceError(exc.code, 409, exc.details) from exc

    @staticmethod
    def evaluate_cpu_candidate(candidate, dataset) -> dict:
        try:
            return _evaluate_cpu_candidate(candidate, dataset)
        except CPUTrainingBlocked as exc:
            raise IntelligenceError(exc.code, 409, exc.details) from exc


def _verify_actor(connection, identity: dict) -> None:
    workspace_id, user_id, session_id = identity_scope(identity)
    member = connection.execute(
        "SELECT active FROM memberships WHERE workspace_id = ? AND user_id = ?",
        (workspace_id, user_id),
    ).fetchone()
    if member is None or member["active"] != 1:
        raise IntelligenceError("ACCESS_REVOKED", 403)
    if session_id is not None:
        from .auth import utc_now
        session = connection.execute(
            "SELECT revoked_at, expires_at FROM sessions WHERE id = ? AND workspace_id = ? AND user_id = ?",
            (session_id, workspace_id, user_id),
        ).fetchone()
        if session is None or session["revoked_at"] is not None or session["expires_at"] <= utc_now():
            raise IntelligenceError("UNAUTHENTICATED", 401)


def _future_time(value: str) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed > datetime.now(timezone.utc)


def _same_model_input_grant(left, right) -> bool:
    fields = (
        "workspace_id", "user_id", "selection_id", "selection_revision", "route", "model_id",
        "source_ref_sha256", "purpose", "recipient",
    )
    return (type(left) is ModelInputAuthorization and type(right) is ModelInputAuthorization
            and left.allowed is True and right.allowed is True
            and all(getattr(left, field) == getattr(right, field) for field in fields)
            and _future_time(left.expires_at) and _future_time(right.expires_at))
