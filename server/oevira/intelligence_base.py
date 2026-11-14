"""Shared, non-authoritative types for the single Intelligence owner."""
from __future__ import annotations


INTELLIGENCE_ARTIFACT_FORMATS = frozenset({"md", "docx", "pptx", "xlsx", "pdf", "html"})
INTELLIGENCE_MEDIA_RENDER_FORMATS = frozenset({"mp4", "wav", "mp3", "m4a"})
INTELLIGENCE_MEDIA_INTERCHANGE_PROFILES = frozenset({"otio", "fcpx_xml", "cmx_3600"})
INTELLIGENCE_OUTPUT_FORMATS = frozenset({
    *INTELLIGENCE_ARTIFACT_FORMATS,
    *INTELLIGENCE_MEDIA_RENDER_FORMATS,
    *INTELLIGENCE_MEDIA_INTERCHANGE_PROFILES,
})


class IntelligenceError(Exception):
    """A stable failure code that does not leak source or provider details."""

    def __init__(self, code: str, status: int = 409, details: dict | None = None):
        super().__init__(code)
        self.code = code
        self.status = status
        self.details = details or {}


def identity_scope(identity: dict) -> tuple[str, str, str | None]:
    """Read actor/workspace IDs from an already-authenticated service identity."""
    if not isinstance(identity, dict):
        raise IntelligenceError("INTELLIGENCE_IDENTITY_REQUIRED", 401)
    user = identity.get("user")
    workspace = identity.get("workspace")
    user_id = user.get("id") if isinstance(user, dict) else None
    workspace_id = workspace.get("id") if isinstance(workspace, dict) else None
    session_id = identity.get("session_id")
    if not isinstance(user_id, str) or not user_id or not isinstance(workspace_id, str) or not workspace_id:
        raise IntelligenceError("INTELLIGENCE_IDENTITY_REQUIRED", 401)
    if session_id is not None and (not isinstance(session_id, str) or not session_id):
        raise IntelligenceError("INTELLIGENCE_IDENTITY_REQUIRED", 401)
    return workspace_id, user_id, session_id
