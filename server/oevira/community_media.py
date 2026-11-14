"""Release media through the existing Material/Media owners.

This module stores version-bound references to immutable workspace Materials,
Media renders, and Media project versions. It never creates a second media
file store or a second editable timeline.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from fractions import Fraction
import hashlib
import json
from pathlib import PurePosixPath
import re
import secrets
import time
import uuid

from flask import g, make_response, request, send_file

from .api import ApiError, _body, _not_found, _resource_path, _string, _success
from .auth import utc_now
from .collaboration import require_material_access, require_work_access, require_workspace_access, work_commission
from .media import VideoEditingError, fingerprint_local_media, sha256_media_file
from .media_api import (
    _MEDIA_OUTPUT_MIME_TYPES,
    _MEDIA_SOURCE_MIME_TYPES,
    _load_timeline,
    _project_export_document,
    _project_row,
    _version_row,
)


SCHEMA = """
CREATE TABLE IF NOT EXISTS publication_media_drafts (
    publication_id TEXT NOT NULL REFERENCES publications(id),
    publication_version INTEGER NOT NULL CHECK(publication_version > 0),
    member_id TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('rendition','caption','chapters','template_source')),
    source_kind TEXT NOT NULL CHECK(source_kind IN ('material','media_render','media_project','inline')),
    source_id TEXT NOT NULL,
    source_workspace_id TEXT NOT NULL REFERENCES workspaces(id),
    source_version INTEGER NOT NULL CHECK(source_version > 0),
    sha256 TEXT NOT NULL CHECK(length(sha256) = 64),
    size_bytes INTEGER NOT NULL CHECK(size_bytes >= 0),
    mime_type TEXT NOT NULL,
    filename TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    inline_content TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY(publication_id, publication_version, member_id),
    CHECK((source_kind = 'inline' AND inline_content IS NOT NULL)
       OR (source_kind != 'inline' AND inline_content IS NULL))
);
CREATE INDEX IF NOT EXISTS publication_media_drafts_source
    ON publication_media_drafts(source_kind, source_id, source_workspace_id);
CREATE TABLE IF NOT EXISTS publication_media_releases (
    release_id TEXT NOT NULL REFERENCES publication_releases(id),
    member_id TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('rendition','caption','chapters','template_source')),
    source_kind TEXT NOT NULL CHECK(source_kind IN ('material','media_render','media_project','inline')),
    source_id TEXT NOT NULL,
    source_workspace_id TEXT NOT NULL REFERENCES workspaces(id),
    source_version INTEGER NOT NULL CHECK(source_version > 0),
    sha256 TEXT NOT NULL CHECK(length(sha256) = 64),
    size_bytes INTEGER NOT NULL CHECK(size_bytes >= 0),
    mime_type TEXT NOT NULL,
    filename TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    inline_content TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY(release_id, member_id),
    CHECK((source_kind = 'inline' AND inline_content IS NOT NULL)
       OR (source_kind != 'inline' AND inline_content IS NULL))
);
CREATE INDEX IF NOT EXISTS publication_media_releases_source
    ON publication_media_releases(source_kind, source_id, source_workspace_id);
CREATE TABLE IF NOT EXISTS community_media_template_instances (
    media_project_id TEXT PRIMARY KEY REFERENCES media_projects(id),
    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
    release_id TEXT NOT NULL REFERENCES publication_releases(id),
    release_content_hash TEXT NOT NULL,
    source_project_id TEXT,
    source_project_version INTEGER NOT NULL CHECK(source_project_version > 0),
    source_manifest_json TEXT NOT NULL,
    slot_asset_map_json TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS community_media_template_update_history (
    media_project_id TEXT NOT NULL REFERENCES media_projects(id),
    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
    version INTEGER NOT NULL CHECK(version > 1),
    from_release_id TEXT NOT NULL REFERENCES publication_releases(id),
    to_release_id TEXT NOT NULL REFERENCES publication_releases(id),
    from_release_hash TEXT NOT NULL,
    to_release_hash TEXT NOT NULL,
    merge_report_json TEXT NOT NULL,
    resolutions_json TEXT NOT NULL,
    updated_by TEXT NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL,
    PRIMARY KEY(media_project_id,version)
);
CREATE TABLE IF NOT EXISTS release_stream_grants (
    token_hash TEXT PRIMARY KEY,
    release_id TEXT NOT NULL REFERENCES publication_releases(id),
    member_id TEXT NOT NULL,
    purpose TEXT NOT NULL CHECK(purpose IN ('view','download')),
    user_id TEXT REFERENCES users(id),
    session_id TEXT REFERENCES sessions(id),
    workspace_id TEXT REFERENCES workspaces(id),
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS release_stream_grants_expiry
    ON release_stream_grants(expires_at);
"""

_MEDIA_EXTENSIONS = frozenset(_MEDIA_SOURCE_MIME_TYPES)
_IMAGE_EXTENSION_MIME_TYPES = {
    ".bmp": "image/bmp",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}
_IMAGE_DEMUXER_MIME_TYPES = {
    "bmp_pipe": "image/bmp",
    "jpeg_pipe": "image/jpeg",
    "png_pipe": "image/png",
    "webp_pipe": "image/webp",
}
_IMAGE_MIME_EXTENSIONS = {
    "image/bmp": ".bmp",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}
_MAX_PUBLICATION_IMAGES = 100
_MAX_RENDITIONS = 8
_MAX_INLINE_MEDIA_TEXT_BYTES = 512_000
_SUBTITLE_TIME = re.compile(r"^(?:(\d{2}):)?(\d{2}):([0-5]\d)[,.](\d{3})$")
_SUBTITLE_CUE = re.compile(
    r"^\s*((?:\d{2}:)?\d{2}:[0-5]\d[,.]\d{3})\s+-->\s+((?:\d{2}:)?\d{2}:[0-5]\d[,.]\d{3})(?:\s+.*)?$"
)


def _expiry_stamp(*, seconds: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat(timespec="seconds").replace("+00:00", "Z")


def install_community_media_schema(store) -> None:
    connection = store.connect()
    try:
        connection.executescript("BEGIN IMMEDIATE;\n" + SCHEMA + "\nCOMMIT;")
        _upgrade_template_instance_source_reference(connection)
    finally:
        connection.close()


def _upgrade_template_instance_source_reference(connection) -> None:
    columns = {row["name"]: row for row in connection.execute("PRAGMA table_info(community_media_template_instances)")}
    source_project = columns.get("source_project_id")
    if source_project is None or source_project["notnull"] == 0:
        return
    connection.execute("PRAGMA foreign_keys = OFF")
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("""CREATE TABLE community_media_template_instances_source_upgrade (
            media_project_id TEXT PRIMARY KEY REFERENCES media_projects(id),
            workspace_id TEXT NOT NULL REFERENCES workspaces(id),
            release_id TEXT NOT NULL REFERENCES publication_releases(id),
            release_content_hash TEXT NOT NULL,
            source_project_id TEXT,
            source_project_version INTEGER NOT NULL CHECK(source_project_version > 0),
            source_manifest_json TEXT NOT NULL,
            slot_asset_map_json TEXT NOT NULL,
            created_by TEXT NOT NULL REFERENCES users(id),
            created_at TEXT NOT NULL
        )""")
        connection.execute("""INSERT INTO community_media_template_instances_source_upgrade(
            media_project_id,workspace_id,release_id,release_content_hash,source_project_id,
            source_project_version,source_manifest_json,slot_asset_map_json,created_by,created_at)
            SELECT media_project_id,workspace_id,release_id,release_content_hash,source_project_id,
                   source_project_version,source_manifest_json,slot_asset_map_json,created_by,created_at
            FROM community_media_template_instances""")
        connection.execute("DROP TABLE community_media_template_instances")
        connection.execute("ALTER TABLE community_media_template_instances_source_upgrade RENAME TO community_media_template_instances")
        connection.commit()
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            raise RuntimeError("Media template lineage erasure migration failed foreign-key validation")
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise
    finally:
        connection.execute("PRAGMA foreign_keys = ON")


def _json(value) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as error:
        raise ApiError("INVALID_INPUT", "Media publication metadata must be finite JSON", 400) from error


def _fraction_json(value) -> dict | None:
    if value is None:
        return None
    fraction = value if isinstance(value, Fraction) else Fraction(str(value))
    return {"num": fraction.numerator, "den": fraction.denominator}


def _source_path(
    store, workspace_id: str, source_kind: str, source_id: str, source_version: int,
    *, allow_sidecar: bool = False, check_running=None,
):
    if source_kind == "material":
        row = store.row(
            "SELECT id,workspace_id,owner_user_id,work_id,title,filename,format,status,sha256,relative_path,size_bytes "
            "FROM materials WHERE id=? AND workspace_id=?",
            (source_id, workspace_id),
        )
        if row is None:
            raise _not_found()
        path = _resource_path(store, row["relative_path"])
        if not path.is_file() or path.is_symlink():
            raise ApiError("COMMUNITY_MEDIA_SOURCE_UNAVAILABLE", "The selected media original is unavailable", 409)
        if _digest_media_file(path, check_running) != row["sha256"]:
            raise ApiError("COMMUNITY_MEDIA_SOURCE_CHANGED", "The selected media original failed integrity verification", 409)
        suffix = PurePosixPath(row["filename"]).suffix.lower()
        mime = _MEDIA_SOURCE_MIME_TYPES.get(suffix) or _IMAGE_EXTENSION_MIME_TYPES.get(suffix)
        if mime is None and allow_sidecar:
            mime = {
                ".vtt": "text/vtt",
                ".srt": "application/x-subrip",
                ".json": "application/json",
            }.get(suffix)
        if mime is None:
            raise ApiError("COMMUNITY_MEDIA_TYPE_UNSUPPORTED", "Choose a supported image, audio or video Material", 415)
        return dict(row), path, mime

    if source_kind == "media_render":
        row = store.row(
            "SELECT r.*,j.kind AS job_kind FROM media_renders r JOIN jobs j ON j.id=r.job_id "
            "WHERE r.id=? AND r.workspace_id=? AND r.status='completed'",
            (source_id, workspace_id),
        )
        if row is None or row["job_kind"] != "media_render":
            raise _not_found()
        if row["project_version"] != source_version:
            raise ApiError("VERSION_CONFLICT", "The selected render is not bound to this Media version", 409)
        path = _resource_path(store, row["relative_path"])
        if not path.is_file() or path.is_symlink():
            raise ApiError("COMMUNITY_MEDIA_SOURCE_UNAVAILABLE", "The selected Media render is unavailable", 409)
        if _digest_media_file(path, check_running) != row["sha256"]:
            raise ApiError("COMMUNITY_MEDIA_SOURCE_CHANGED", "The selected Media render failed integrity verification", 409)
        mime = _MEDIA_OUTPUT_MIME_TYPES.get(row["format"])
        if mime is None:
            raise ApiError("COMMUNITY_MEDIA_TYPE_UNSUPPORTED", "The selected render format cannot be published", 415)
        return dict(row), path, mime

    raise ApiError("INVALID_INPUT", "Unsupported media publication source", 400)


def require_media_source_access(connection, identity: dict, publication, *, write: bool = False) -> None:
    """Authorize a media source using the existing Workspace/Material owners."""
    kind, source_id = publication["source_kind"], publication["source_id"]
    workspace_id = identity["workspace"]["id"]
    if publication["workspace_id"] != workspace_id:
        raise _not_found()
    if kind == "media_material":
        material_id = source_id
        row = connection.execute(
            "SELECT work_id FROM materials WHERE id=? AND workspace_id=?",
            (material_id, workspace_id),
        ).fetchone()
        if row is None:
            raise _not_found()
        require_material_access(connection, identity, material_id)
        if write:
            if row["work_id"]:
                require_work_access(connection, identity, row["work_id"], write=True)
            else:
                require_workspace_access(connection, identity, write=True)
        return
    if kind == "media_render":
        row = connection.execute(
            "SELECT p.work_id FROM media_renders r JOIN media_projects p ON p.id=r.project_id "
            "WHERE r.id=? AND r.workspace_id=? AND r.project_version=?",
            (source_id, workspace_id, publication["source_version"]),
        ).fetchone()
        if row is None:
            raise _not_found()
        if row["work_id"]:
            require_work_access(connection, identity, row["work_id"], write=write)
        else:
            require_workspace_access(connection, identity, write=write)
        return
    if kind == "media_project":
        row = connection.execute(
            "SELECT work_id FROM media_projects WHERE id=? AND workspace_id=?",
            (source_id, workspace_id),
        ).fetchone()
        if row is None:
            raise _not_found()
        if row["work_id"]:
            require_work_access(connection, identity, row["work_id"], write=write)
        else:
            require_workspace_access(connection, identity, write=write)
        return
    raise _not_found()


def _media_asset_summary(asset) -> dict:
    streams = asset.to_dict()["streams"]
    video = next((item for item in streams if item.get("codec_type") == "video"), {})
    audio = next((item for item in streams if item.get("codec_type") == "audio"), {})
    rate = video.get("avg_frame_rate")
    summary = {
        "media_kind": asset.media_kind,
        "duration": _fraction_json(asset.duration_sec),
        "video": None if not asset.has_video else {
            "width": video.get("width"),
            "height": video.get("height"),
            "codec": video.get("codec_name"),
            "frame_rate": rate,
        },
        "audio": None if asset.audio_stream_count == 0 else {
            "codec": audio.get("codec_name"),
            "sample_rate": audio.get("sample_rate"),
            "channels": audio.get("channels"),
        },
    }
    if asset.is_still_image:
        summary["image"] = {
            "width": video.get("width"),
            "height": video.get("height"),
            "color_profile": asset.format_metadata.get("image_color_profile"),
        }
    return summary


def _require_source_scope(store, identity: dict, work_id: str | None, *, write: bool) -> None:
    with store.transaction(immediate=False) as connection:
        if work_id:
            require_work_access(connection, identity, work_id, write=write)
        else:
            require_workspace_access(connection, identity, write=write)


def _new_source_member(store, workspace_id: str, source_kind: str, source_id: str, source_version: int, role: str, label: str):
    from .media_api import _new_media_asset

    row, path, mime = _source_path(store, workspace_id, source_kind, source_id, source_version)
    if source_kind == "material":
        material = row
    else:
        material = {
            "id": row["id"],
            "filename": path.name,
            "sha256": row["sha256"],
        }
    asset = _new_media_asset(path, material) if source_kind == "material" else fingerprint_local_media(str(path), display_filename=path.name)
    if asset.is_still_image:
        detected_mime = _IMAGE_DEMUXER_MIME_TYPES.get(asset.format_metadata.get("format_name"))
        if detected_mime is None or mime != detected_mime:
            raise ApiError("COMMUNITY_IMAGE_TYPE_MISMATCH", "The image filename, bytes and detected image format must agree", 415)
        mime = detected_mime
        if not PurePosixPath(label).suffix:
            label = f"{label}{_IMAGE_MIME_EXTENSIONS[detected_mime]}"
    elif asset.media_kind not in {"video", "video_audio", "audio"}:
        raise ApiError("COMMUNITY_MEDIA_TYPE_UNSUPPORTED", "Choose a supported still image, video or audio source", 415)
    if source_kind == "media_render" and ((row["output_kind"] == "video") != asset.has_video):
        raise ApiError("MEDIA_RENDER_OUTPUT_MISMATCH", "The rendered output stream inventory is inconsistent", 409)
    return {
        "member_id": str(uuid.uuid4()),
        "role": role,
        "source_kind": source_kind,
        "source_id": source_id,
        "source_workspace_id": workspace_id,
        "source_version": source_version,
        "sha256": row["sha256"],
        "size_bytes": row["size_bytes"],
        "mime_type": mime,
        "filename": label,
        "metadata": {**_media_asset_summary(asset),
                     "dependency_ids": ([source_id] if source_kind == "material" else [])},
        "inline_content": None,
    }


def _member_public(member: dict) -> dict:
    metadata = member.get("metadata", {})
    if not isinstance(metadata, dict):
        metadata = {}
    return {
        "id": member["member_id"],
        "role": member["role"],
        "mime_type": member["mime_type"],
        "filename": member["filename"],
        "sha256": member["sha256"],
        "size_bytes": member["size_bytes"],
        "metadata": {key: value for key, value in metadata.items() if key not in {"dependency_ids", "source_material_ids", "private_slot_map", "manifest"}},
    }


def _check_media_dependencies(content: dict, required: list[dict], *, publication_license: dict):
    dependencies = content.get("dependencies", [])
    by_ref = {(item["id"], item["version"], item["sha256"]): item for item in dependencies}
    for source in required:
        dependency = by_ref.get((source["id"], source["version"], source["sha256"]))
        if dependency is None:
            raise ApiError("MEDIA_DEPENDENCY_LICENSE_REQUIRED", "Add a separate rights record for every source used by this media package", 409,
                           {"source_id": source["id"], "version": source["version"], "sha256": source["sha256"]})
        if dependency["included"] is not source["included"]:
            raise ApiError("MEDIA_DEPENDENCY_PACKAGE_STATE_MISMATCH", "Dependency package state must match the selected media members", 400,
                           {"dependency_id": dependency["id"]})
        rights = dependency["license"]
        if not rights["view"] or not rights["redistribute"]:
            raise ApiError("MEDIA_DEPENDENCY_RIGHTS_INSUFFICIENT", "Every source used in a public media member must permit viewing and distribution", 403,
                           {"dependency_id": dependency["id"]})
        if publication_license["download"] and not rights["download"]:
            raise ApiError("MEDIA_DEPENDENCY_RIGHTS_INSUFFICIENT", "The media package cannot grant downloads that a source dependency does not permit", 403)
        if publication_license["adapt"] and not rights["adapt"]:
            raise ApiError("MEDIA_DEPENDENCY_RIGHTS_INSUFFICIENT", "The media package cannot grant adaptation when a source dependency forbids it", 403)
        if publication_license["commercial"] and not rights["commercial"]:
            raise ApiError("MEDIA_DEPENDENCY_RIGHTS_INSUFFICIENT", "The media package cannot grant commercial use when a source dependency forbids it", 403)
        source["dependency_id"] = dependency["id"]


def prepare_media_publication(store, identity: dict, source_kind: str, source_id: str, source_version: int,
                             media_selection: dict | None, content: dict, publication_kind: str,
                             *, private_draft: bool = False) -> dict:
    """Validate current Media/Material sources and freeze only safe public metadata."""
    from .media_rendering import chapter_document, subtitle_documents

    workspace_id = identity["workspace"]["id"]
    license_record = content["license"]
    selection = {} if media_selection is None else media_selection
    if type(selection) is not dict:
        raise ApiError("INVALID_MEDIA_SELECTION", "Media selection must be an object", 400)
    caption_language = selection.get("caption_language")
    if caption_language is not None and (
        type(caption_language) is not str
        or len(caption_language) > 40
        or not re.fullmatch(r"[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*", caption_language)
    ):
        raise ApiError("INVALID_MEDIA_SELECTION", "Caption language must be a valid language tag", 400)
    for field in ("include_captions", "include_chapters"):
        if field in selection and type(selection[field]) is not bool:
            raise ApiError("INVALID_MEDIA_SELECTION", f"{field} must be a boolean", 400)
    members: list[dict] = []
    required_sources: list[dict] = []
    media_kind = None
    template_manifest = None
    media_project_info = None
    timeline = None
    source_work_id = None
    source_work_ids: list[str] = []

    if source_kind == "media_material":
        if publication_kind != "work" or source_version != 1:
            raise ApiError("INVALID_MEDIA_SOURCE", "A direct Material source publishes as a version-one media work", 400)
        member = _new_source_member(
            store, workspace_id, "material", source_id, 1, "rendition",
            f"media-{len(members) + 1}",
        )
        material = store.row("SELECT work_id FROM materials WHERE id=? AND workspace_id=?", (source_id, workspace_id))
        source_work_id = None if material is None else material["work_id"]
        media_kind = member["metadata"]["media_kind"]
        if source_work_id:
            source_work_ids.append(source_work_id)
        if media_kind == "image":
            selected_images = selection.get("items")
            if selected_images is None:
                selected_images = [{"item_id": "primary", "material_id": source_id}]
            if (not isinstance(selected_images, list) or not 1 <= len(selected_images) <= _MAX_PUBLICATION_IMAGES
                    or any(not isinstance(item, dict) or set(item) != {"item_id", "material_id"} for item in selected_images)):
                raise ApiError("INVALID_MEDIA_SELECTION", "Select an ordered list of one to 100 still-image Materials", 400)
            item_ids = []
            for index, selected in enumerate(selected_images):
                item_id = _string(selected["item_id"], "media_selection.items.item_id", maximum=150)
                material_id = _string(selected["material_id"], "media_selection.items.material_id", maximum=80)
                if item_id in item_ids:
                    raise ApiError("INVALID_MEDIA_SELECTION", "Image item ids must be unique", 400)
                item_ids.append(item_id)
                if index == 0 and material_id != source_id:
                    raise ApiError("INVALID_MEDIA_SELECTION", "source_id must match the first ordered image Material", 409)
                if index == 0:
                    image_member = member
                    image_row = material
                else:
                    image_row = store.row("SELECT work_id FROM materials WHERE id=? AND workspace_id=?", (material_id, workspace_id))
                    if image_row is None:
                        raise _not_found()
                    with store.transaction(immediate=False) as connection:
                        require_material_access(connection, identity, material_id)
                        if image_row["work_id"]:
                            require_work_access(connection, identity, image_row["work_id"])
                    image_member = _new_source_member(
                        store, workspace_id, "material", material_id, 1, "rendition", f"image-{index + 1}",
                    )
                if image_member["metadata"].get("media_kind") != "image":
                    raise ApiError("COMMUNITY_IMAGE_TYPE_REQUIRED", "Every ordered image item must be a supported still-image Material", 415)
                image_member["metadata"].update({"item_id": item_id, "position": index})
                image_extension = _IMAGE_MIME_EXTENSIONS[image_member["mime_type"]]
                image_member["filename"] = f"image-{index + 1}{image_extension}"
                members.append(image_member)
                required_sources.append({"id": material_id, "version": 1, "sha256": image_member["sha256"], "included": True})
                if image_row and image_row["work_id"]:
                    source_work_ids.append(image_row["work_id"])
            media_kind = "image" if len(selected_images) == 1 else "image_set"
            allowed_selection = {"items"}
        else:
            item_id = selection.get("item_id", "primary")
            item_id = _string(item_id, "media_selection.item_id", maximum=150)
            member["metadata"].update({"item_id": item_id, "position": 0})
            members.append(member)
            required_sources.append({"id": source_id, "version": 1, "sha256": member["sha256"], "included": True})
            allowed_selection = {"item_id", "caption_material_id", "chapter_material_id", "caption_language"}
        if set(selection) - allowed_selection:
            raise ApiError("INVALID_MEDIA_SELECTION", "Direct media selection contains unsupported fields", 400)
        for field, role, suffixes, mime in (
            ("caption_material_id", "caption", {".vtt", ".srt"}, {".vtt": "text/vtt", ".srt": "application/x-subrip"}),
            ("chapter_material_id", "chapters", {".json"}, {".json": "application/json"}),
        ):
            sidecar_id = selection.get(field)
            if sidecar_id is None:
                continue
            if type(sidecar_id) is not str or not sidecar_id:
                raise ApiError("INVALID_MEDIA_SELECTION", f"{field} must reference a saved Material", 400)
            sidecar = store.row("SELECT id,filename,sha256,relative_path,size_bytes,work_id FROM materials WHERE id=? AND workspace_id=?", (sidecar_id, workspace_id))
            if sidecar is None:
                raise _not_found()
            with store.transaction(immediate=False) as connection:
                require_material_access(connection, identity, sidecar_id)
                if sidecar["work_id"]:
                    require_work_access(connection, identity, sidecar["work_id"])
            extension = PurePosixPath(sidecar["filename"]).suffix.lower()
            if extension not in suffixes or sidecar["size_bytes"] > _MAX_INLINE_MEDIA_TEXT_BYTES:
                raise ApiError("MEDIA_SIDECAR_INVALID", "Captions must be SRT/WebVTT and chapters must be a bounded JSON file", 415)
            path = _resource_path(store, sidecar["relative_path"])
            raw = path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != sidecar["sha256"]:
                raise ApiError("COMMUNITY_MEDIA_SOURCE_CHANGED", "The selected media sidecar failed integrity verification", 409)
            if role == "chapters":
                try:
                    chapter_payload = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, ValueError) as error:
                    raise ApiError("MEDIA_CHAPTERS_INVALID", "The chapter sidecar is not valid UTF-8 JSON", 400) from error
                if not isinstance(chapter_payload, dict) or chapter_payload.get("schema") != "oevira.media.chapters.v1" or not isinstance(chapter_payload.get("chapters"), list):
                    raise ApiError("MEDIA_CHAPTERS_INVALID", "The chapter sidecar does not match the Media chapter schema", 400)
                _validate_public_chapters(chapter_payload, duration=member["metadata"]["duration"])
                sanitized = _json(chapter_payload)
                sidecar_member = _inline_member(
                    workspace_id, role, "chapters.json", "application/json; charset=utf-8",
                    sanitized, {"chapters": chapter_payload["chapters"], "dependency_ids": [sidecar_id]},
                )
                sidecar_member["source_id"] = "inline"
                sidecar_member["source_kind"] = "inline"
                sidecar_member["sha256"] = hashlib.sha256(sanitized.encode("utf-8")).hexdigest()
                sidecar_member["size_bytes"] = len(sanitized.encode("utf-8"))
                sidecar_member["source_version"] = 1
                required_sources.append({"id": sidecar_id, "version": 1, "sha256": sidecar["sha256"], "included": True})
                members.append(sidecar_member)
                continue
            _validate_public_captions(raw, extension, duration=member["metadata"]["duration"])
            dep = {"id": sidecar_id, "version": 1, "sha256": sidecar["sha256"]}
            required_sources.append({**dep, "included": True})
            sidecar_member = {
                "member_id": str(uuid.uuid4()), "role": role, "source_kind": "material",
                "source_id": sidecar_id, "source_workspace_id": workspace_id,
                "source_version": 1, "sha256": sidecar["sha256"], "size_bytes": len(raw),
                "mime_type": mime[extension], "filename": f"{role}{extension}",
                "metadata": {
                    "language": caption_language if role == "caption" else None,
                    "dependency_ids": [sidecar_id],
                },
                "inline_content": None,
            }
            members.append(sidecar_member)
    elif source_kind == "media_render":
        if publication_kind != "work" or type(source_version) is not int or source_version < 1:
            raise ApiError("INVALID_MEDIA_SOURCE", "A completed Media render publishes as a version-bound media work", 400)
        render_ids = selection.get("rendition_ids", [source_id])
        if not isinstance(render_ids, list) or not 1 <= len(render_ids) <= _MAX_RENDITIONS or any(type(item) is not str for item in render_ids) or len(set(render_ids)) != len(render_ids) or source_id not in render_ids:
            raise ApiError("INVALID_MEDIA_SELECTION", "Select the primary render and a bounded set of unique quality renditions", 400)
        if set(selection) - {"rendition_ids", "caption_language", "include_captions", "include_chapters", "item_id"}:
            raise ApiError("INVALID_MEDIA_SELECTION", "Render selection contains unsupported fields", 400)
        render_rows = []
        project_id = None
        for render_id in render_ids:
            row = store.row("SELECT r.*,j.kind AS job_kind FROM media_renders r JOIN jobs j ON j.id=r.job_id WHERE r.id=? AND r.workspace_id=? AND r.status='completed'", (render_id, workspace_id))
            if row is None or row["job_kind"] != "media_render" or row["project_version"] != source_version:
                raise ApiError("MEDIA_RENDER_VERSION_MISMATCH", "Every quality rendition must be a completed render of the selected Media project version", 409)
            if project_id is not None and row["project_id"] != project_id:
                raise ApiError("MEDIA_RENDER_VERSION_MISMATCH", "Quality renditions must come from the same Media project", 409)
            if render_rows and row["output_kind"] != render_rows[0]["output_kind"]:
                raise ApiError("MEDIA_RENDITION_SET_INVALID", "A rendition set cannot mix audio-only and video outputs", 409)
            project_id = row["project_id"]
            render_rows.append(row)
        project = _project_row(store, workspace_id, project_id)
        source_work_id = project["work_id"]
        _require_source_scope(store, identity, source_work_id, write=False)
        source_state_row = _version_row(store, workspace_id, project_id, source_version)
        state = json.loads(source_state_row["state_json"])
        timeline, _ = _load_timeline(store, workspace_id, state, verify_files=True)
        required_sources.extend({"id": asset.source_material_id, "version": 1, "sha256": asset.sha256, "included": False} for asset in timeline.assets)
        source_material_ids = sorted({asset.source_material_id for asset in timeline.assets})
        media_kind = "video" if render_rows[0]["output_kind"] == "video" else "audio"
        for index, row in enumerate(render_rows, 1):
            member = _new_source_member(store, workspace_id, "media_render", row["id"], source_version, "rendition", f"rendition-{index}.{row['format']}")
            member["metadata"].update({"dependency_ids": source_material_ids, "item_id": _string(selection.get("item_id", "primary"), "media_selection.item_id", maximum=150), "position": index - 1})
            member["metadata"]["quality"] = row["metadata_json"] and json.loads(row["metadata_json"]).get("output_spec")
            members.append(member)
        if selection.get("include_captions") or selection.get("include_chapters"):
            frame_data = json.loads(render_rows[0]["metadata_json"] or "{}").get("output_spec", {}).get("frame_rate", {})
            frame_rate = Fraction(frame_data.get("num", 30), frame_data.get("den", 1)) if isinstance(frame_data, dict) else Fraction(30, 1)
            if selection.get("include_captions"):
                text = subtitle_documents(timeline, frame_rate=frame_rate).get("captions.vtt")
                if text:
                    members.append(_inline_member(workspace_id, "caption", "captions.vtt", "text/vtt; charset=utf-8", text, {"language": selection.get("caption_language")}))
            if selection.get("include_chapters"):
                chapters = chapter_document(timeline)
                if chapters:
                    _validate_public_chapters(chapters, duration=timeline.duration_sec)
                    members.append(_inline_member(workspace_id, "chapters", "chapters.json", "application/json; charset=utf-8", _json(chapters), {"chapters": chapters["chapters"]}))
    elif source_kind == "media_project":
        if publication_kind != "template" or type(source_version) is not int or source_version < 1:
            raise ApiError("INVALID_MEDIA_SOURCE", "Editable Media templates must reference an exact Media project version", 400)
        if set(selection) - {"preview_render_ids", "include_captions", "include_chapters", "slot_labels", "caption_language"}:
            raise ApiError("INVALID_MEDIA_SELECTION", "Media template selection contains unsupported fields", 400)
        project = _project_row(store, workspace_id, source_id)
        source_work_id = project["work_id"]
        _require_source_scope(store, identity, source_work_id, write=True)
        manifest = _project_export_document(store, workspace_id, source_id, source_version)
        timeline = _load_timeline(store, workspace_id, json.loads(_version_row(store, workspace_id, source_id, source_version)["state_json"]), verify_files=True)[0]
        source_slots = manifest.get("source_slots", [])
        if not isinstance(source_slots, list) or not source_slots:
            raise ApiError("MEDIA_TEMPLATE_EMPTY", "An editable Media template needs at least one source slot", 409)
        safe_template, private_slot_map = _media_template_manifest(manifest, selection.get("slot_labels", {}), include_captions=selection.get("include_captions", True), include_chapters=selection.get("include_chapters", True))
        if selection.get("include_captions", True):
            required_sources.extend(
                {"id": asset.source_material_id, "version": 1, "sha256": asset.sha256, "included": False}
                for asset in timeline.assets
            )
        template_member = {
            "member_id": str(uuid.uuid4()), "role": "template_source", "source_kind": "media_project",
            "source_id": source_id, "source_workspace_id": workspace_id, "source_version": source_version,
            "sha256": hashlib.sha256(_json(manifest).encode("utf-8")).hexdigest(), "size_bytes": len(_json(manifest).encode("utf-8")),
            "mime_type": "application/vnd.oevira.media-project-manifest+json", "filename": "media-template.json",
            "metadata": {"manifest": manifest, "slot_map": private_slot_map, "template_schema": safe_template},
            "inline_content": None,
        }
        members.append(template_member)
        media_kind = project["kind"]
        preview_ids = selection.get("preview_render_ids", [])
        if not isinstance(preview_ids, list) or len(preview_ids) > _MAX_RENDITIONS or any(type(item) is not str for item in preview_ids) or len(set(preview_ids)) != len(preview_ids):
            raise ApiError("INVALID_MEDIA_SELECTION", "Choose a bounded set of unique, completed preview renditions", 400)
        if preview_ids:
            # A demo render includes source content, so each source asset needs an explicit, compatible license.
            demo_selection = {"rendition_ids": preview_ids, "include_captions": selection.get("include_captions", True), "include_chapters": selection.get("include_chapters", True), "caption_language": selection.get("caption_language")}
            demo = prepare_media_publication(store, identity, "media_render", preview_ids[0], source_version, demo_selection, content, "work")
            members.extend(member for member in demo["members"] if member["role"] != "template_source")
            required_sources.extend(demo["required_sources"])
        else:
            if selection.get("include_captions") or selection.get("include_chapters"):
                if selection.get("include_captions"):
                    text = subtitle_documents(timeline).get("captions.vtt")
                    if text:
                        members.append(_inline_member(workspace_id, "caption", "captions.vtt", "text/vtt; charset=utf-8", text, {"language": selection.get("caption_language")}))
                if selection.get("include_chapters"):
                    chapters = chapter_document(timeline)
                    if chapters:
                        _validate_public_chapters(chapters, duration=timeline.duration_sec)
                        members.append(_inline_member(workspace_id, "chapters", "chapters.json", "application/json; charset=utf-8", _json(chapters), {"chapters": chapters["chapters"]}))
        template_manifest = safe_template
    else:
        raise ApiError("INVALID_MEDIA_SOURCE", "Select a saved media Material, completed render, or exact Media project version", 400)

    if required_sources and not private_draft:
        _check_media_dependencies(content, required_sources, publication_license=license_record)
    elif not required_sources:
        _check_media_dependencies(content, [], publication_license=license_record)
    public_members = [_member_public(item) for item in members if item["role"] != "template_source"]
    image_members = [item for item in public_members if item["role"] == "rendition" and item.get("metadata", {}).get("media_kind") == "image"]
    rendition_members = [item for item in public_members if item["role"] == "rendition" and item.get("metadata", {}).get("media_kind") != "image"]
    return {
        "source_kind": source_kind,
        "source_id": source_id,
        "source_version": source_version,
        "source_workspace_id": workspace_id,
        "source_work_id": source_work_id,
        "source_work_ids": list(dict.fromkeys(source_work_ids)),
        "template_manifest": template_manifest,
        "media_kind": media_kind,
        "members": members,
        "public_media": {
            "schema": "oevira.media.release/1",
            "kind": media_kind,
            "renditions": rendition_members,
            "images": image_members,
            "captions": [item for item in public_members if item["role"] == "caption"],
            "chapters": [item for item in public_members if item["role"] == "chapters"],
            "template": template_manifest,
            "rights_state": "private_draft_rights_pending" if private_draft else "author_attested_source_permissions",
            "independent_media_review": "not_established",
        },
        "required_sources": required_sources,
    }


def _inline_member(workspace_id: str, role: str, filename: str, mime_type: str, content: str, metadata: dict) -> dict:
    raw = content.encode("utf-8")
    if len(raw) > _MAX_INLINE_MEDIA_TEXT_BYTES:
        raise ApiError("MEDIA_TEXT_ASSET_TOO_LARGE", "Caption or chapter attachment exceeds the publication limit", 413)
    return {
        "member_id": str(uuid.uuid4()), "role": role, "source_kind": "inline", "source_id": "inline",
        "source_workspace_id": workspace_id, "source_version": 1,
        "sha256": hashlib.sha256(raw).hexdigest(), "size_bytes": len(raw),
        "mime_type": mime_type, "filename": filename, "metadata": metadata, "inline_content": content,
    }


def _validate_public_chapters(chapters: dict, *, duration=None) -> None:
    raw = chapters.get("chapters")
    if not isinstance(raw, list) or len(raw) > 10000:
        raise ApiError("MEDIA_CHAPTERS_INVALID", "The chapter manifest is invalid", 409)
    if duration is not None and not isinstance(duration, Fraction):
        duration = _chapter_time(duration)
    safe = []
    previous_end = Fraction(0)
    for index, item in enumerate(raw):
        if not isinstance(item, dict) or set(item) - {"chapter_id", "title", "start_sec", "end_sec", "source_ranges"}:
            raise ApiError("MEDIA_CHAPTERS_INVALID", "The chapter manifest contains unsupported fields", 409)
        title = _string(item.get("title"), "chapter.title", maximum=500)
        start = _chapter_time(item.get("start_sec"))
        end = _chapter_time(item.get("end_sec"))
        if start < previous_end or end <= start or (duration is not None and end > duration):
            raise ApiError("MEDIA_CHAPTERS_INVALID", "Chapter ranges must be ordered, non-overlapping and within the selected programme", 409)
        safe.append({
            "chapter_id": f"chapter-{index+1}", "title": title,
            "start_sec": _fraction_json(start), "end_sec": _fraction_json(end),
        })
        previous_end = end
    chapters["chapters"] = safe


def _chapter_time(value) -> Fraction:
    if (not isinstance(value, dict) or set(value) != {"num", "den"}
            or type(value["num"]) is not int or type(value["den"]) is not int or value["den"] <= 0):
        raise ApiError("MEDIA_CHAPTERS_INVALID", "Chapter boundaries must use exact rational seconds", 409)
    return Fraction(value["num"], value["den"])


def _validate_public_captions(raw: bytes, extension: str, *, duration) -> None:
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise ApiError("MEDIA_CAPTIONS_INVALID", "Captions must be valid UTF-8 text", 400) from error
    if "\x00" in text or (extension == ".vtt" and not text.lstrip().startswith("WEBVTT")):
        raise ApiError("MEDIA_CAPTIONS_INVALID", "Choose valid WebVTT or SubRip caption text", 400)
    if not isinstance(duration, Fraction):
        duration = _chapter_time(duration)

    def parse_timestamp(value: str) -> Fraction:
        match = _SUBTITLE_TIME.fullmatch(value)
        if match is None:
            raise ApiError("MEDIA_CAPTIONS_INVALID", "Caption timestamps must use exact millisecond timecodes", 400)
        hours, first, seconds, milliseconds = match.groups()
        first_value = int(first)
        if hours is None and first_value > 59:
            raise ApiError("MEDIA_CAPTIONS_INVALID", "Caption minute fields must be between 00 and 59", 400)
        total_milliseconds = (
            ((int(hours) * 60 + first_value) if hours is not None else first_value) * 60
            + int(seconds)
        ) * 1000 + int(milliseconds)
        return Fraction(total_milliseconds, 1000)

    cue_count = 0
    previous_start = Fraction(-1)
    for line in text.splitlines():
        if "-->" not in line:
            continue
        match = _SUBTITLE_CUE.fullmatch(line)
        if match is None:
            raise ApiError("MEDIA_CAPTIONS_INVALID", "A caption cue has an invalid time range", 400)
        start, end = map(parse_timestamp, match.groups())
        if start < previous_start or end <= start or end > duration:
            raise ApiError("MEDIA_CAPTIONS_INVALID", "Caption cues must be ordered and fit within the programme duration", 400)
        previous_start = start
        cue_count += 1
    if cue_count == 0:
        raise ApiError("MEDIA_CAPTIONS_INVALID", "The caption file contains no timed cues", 400)


def _media_template_manifest(manifest: dict, raw_labels, *, include_captions: bool, include_chapters: bool) -> tuple[dict, dict]:
    if type(raw_labels) is not dict or any(type(key) is not str or type(value) is not str for key, value in raw_labels.items()):
        raise ApiError("INVALID_MEDIA_TEMPLATE", "Media source labels must be text", 400)
    timeline = deepcopy(manifest["timeline"])
    source_slots = manifest.get("source_slots", [])
    known_ids = {"slot-" + hashlib.sha256(item["asset_id"].encode("utf-8")).hexdigest()[:16] for item in source_slots}
    if set(raw_labels) - known_ids:
        raise ApiError("INVALID_MEDIA_TEMPLATE", "A source label refers to an unknown Media template slot", 400)
    source_map = {}
    slots = []
    for index, source in enumerate(source_slots, 1):
        internal = source["asset_id"]
        public_id = "slot-" + hashlib.sha256(internal.encode("utf-8")).hexdigest()[:16]
        source_map[public_id] = internal
        label = raw_labels.get(public_id, f"Source {index}")
        label = _string(label, "slot label", maximum=200)
        slots.append({
            "id": public_id,
            "label": label,
            "media_kind": source["media_kind"],
            "required_streams": source["required_streams"],
            "source_inventory": source["source_inventory"],
            "minimum_source_duration_sec": source["minimum_source_duration_sec"],
            "replacement_required": True,
        })
    timeline.pop("assets", None)
    # Transcript words are tied to the author's exact source files. Reusing
    # them with replacement media would create a false source/time binding.
    timeline.pop("transcripts", None)
    if not include_captions:
        timeline["overlays"] = [item for item in timeline.get("overlays", []) if item.get("kind") != "caption"]
    if not include_chapters:
        timeline["chapters"] = []
    for track in timeline.get("tracks", []):
        for clip in track.get("clips", []):
            old_id = clip.get("media_asset_id")
            if old_id not in source_map.values():
                raise ApiError("MEDIA_TEMPLATE_INVALID", "The Media template graph references an unknown source slot", 409)
            clip["media_asset_id"] = next(slot_id for slot_id, asset_id in source_map.items() if asset_id == old_id)
    for item in timeline.get("video_color_interpretations", []):
        old_id = item.get("asset_id")
        if old_id in source_map.values():
            item["asset_id"] = next(slot_id for slot_id, asset_id in source_map.items() if asset_id == old_id)
    public = {
        "schema": "oevira-media-template/1",
        "project_kind": manifest["project"]["kind"],
        "format_ratio": manifest["project"]["format_ratio"],
        "slots": slots,
        "graph": timeline,
        "source_package_state": "replacement_slots_only",
        "limitations": ["Source media files are not included; select authorized replacement Materials for every slot."],
    }
    _json(public)
    return public, source_map


def save_draft_media_members(connection, publication_id: str, publication_version: int, members: list[dict]) -> None:
    now = utc_now()
    for member in members:
        connection.execute(
            """INSERT INTO publication_media_drafts(
                   publication_id,publication_version,member_id,role,source_kind,source_id,
                   source_workspace_id,source_version,sha256,size_bytes,mime_type,filename,
                   metadata_json,inline_content,created_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                publication_id, publication_version, member["member_id"], member["role"],
                member["source_kind"], member["source_id"], member["source_workspace_id"],
                member["source_version"], member["sha256"], member["size_bytes"],
                member["mime_type"], member["filename"], _json(member.get("metadata", {})),
                member.get("inline_content"), now,
            ),
        )


def copy_draft_media_version(connection, publication_id: str, source_version: int, target_version: int) -> None:
    connection.execute(
        """INSERT INTO publication_media_drafts(
               publication_id,publication_version,member_id,role,source_kind,source_id,
               source_workspace_id,source_version,sha256,size_bytes,mime_type,filename,
               metadata_json,inline_content,created_at
           ) SELECT publication_id,?,member_id,role,source_kind,source_id,source_workspace_id,
                    source_version,sha256,size_bytes,mime_type,filename,metadata_json,inline_content,created_at
             FROM publication_media_drafts WHERE publication_id=? AND publication_version=?""",
        (target_version, publication_id, source_version),
    )


def _draft_members(connection, publication_id: str, publication_version: int) -> list[dict]:
    rows = [dict(row) for row in connection.execute(
        "SELECT * FROM publication_media_drafts WHERE publication_id=? AND publication_version=?",
        (publication_id, publication_version),
    ).fetchall()]
    return sorted(rows, key=_member_order_key)


def _release_members(connection, release_id: str) -> list[dict]:
    rows = [dict(row) for row in connection.execute(
        "SELECT * FROM publication_media_releases WHERE release_id=?",
        (release_id,),
    ).fetchall()]
    return sorted(rows, key=_member_order_key)


def _member_order_key(row: dict) -> tuple:
    try:
        metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else json.loads(row.get("metadata_json") or "{}")
    except (TypeError, ValueError):
        metadata = {}
    position = metadata.get("position", 0) if isinstance(metadata, dict) else 0
    if type(position) is not int or position < 0:
        position = 0
    role_order = {"rendition": 0, "caption": 1, "chapters": 2, "template_source": 3}
    return (role_order.get(row.get("role"), 9), position, row.get("member_id", ""))


def align_draft_media_item_order(connection, publication_id: str, publication_version: int, blocks: list[dict]) -> None:
    """Keep the media member manifest in the exact native block order."""
    item_ids = [
        block["item_id"] for block in blocks
        if isinstance(block, dict) and block.get("kind") in {"image", "video", "audio"}
        and isinstance(block.get("item_id"), str)
    ]
    positions = {item_id: index for index, item_id in enumerate(item_ids)}
    if not positions:
        return
    rows = connection.execute(
        "SELECT member_id,metadata_json FROM publication_media_drafts WHERE publication_id=? AND publication_version=? AND role='rendition'",
        (publication_id, publication_version),
    ).fetchall()
    for row in rows:
        try:
            metadata = json.loads(row["metadata_json"] or "{}")
        except (TypeError, ValueError):
            raise ApiError("COMMUNITY_MEDIA_METADATA_INVALID", "A selected media member has invalid metadata", 409)
        if not isinstance(metadata, dict):
            raise ApiError("COMMUNITY_MEDIA_METADATA_INVALID", "A selected media member has invalid metadata", 409)
        item_id = metadata.get("item_id") or "primary"
        if item_id in positions:
            metadata["position"] = positions[item_id]
            connection.execute(
                "UPDATE publication_media_drafts SET metadata_json=? WHERE publication_id=? AND publication_version=? AND member_id=?",
                (_json(metadata), publication_id, publication_version, row["member_id"]),
            )


def _row_public_member(row: dict) -> dict:
    try:
        metadata = json.loads(row["metadata_json"] or "{}")
    except (TypeError, ValueError):
        metadata = {}
    return {
        "id": row["member_id"],
        "role": row["role"],
        "mime_type": row["mime_type"],
        "filename": row["filename"],
        "sha256": row["sha256"],
        "size_bytes": row["size_bytes"],
        "metadata": {key: value for key, value in metadata.items() if key not in {"dependency_ids", "source_material_ids", "private_slot_map", "manifest"}},
    }


def publication_media_fingerprint(connection, publication_id: str, publication_version: int, content: dict) -> str | None:
    rows = _draft_members(connection, publication_id, publication_version)
    if not rows:
        return None
    values = [
        {
            "member_id": row["member_id"],
            "role": row["role"],
            "source_kind": row["source_kind"],
            "source_id": row["source_id"],
            "source_workspace_id": row["source_workspace_id"],
            "source_version": row["source_version"],
            "sha256": row["sha256"],
            "size_bytes": row["size_bytes"],
            "mime_type": row["mime_type"],
            "metadata_json": row["metadata_json"],
        }
        for row in rows
    ]
    return hashlib.sha256(_json({"content": content, "media_members": values}).encode("utf-8")).hexdigest()


def _digest_media_file(path, check_running=None) -> str:
    if check_running is None:
        return sha256_media_file(path)
    digest = hashlib.sha256()
    last_check = time.monotonic()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
            if time.monotonic() - last_check >= 0.1:
                check_running()
                last_check = time.monotonic()
    check_running()
    return digest.hexdigest()


def _verify_member_source(store, row: dict, check_running=None) -> None:
    if row["source_kind"] == "inline":
        raw = (row["inline_content"] or "").encode("utf-8")
        if len(raw) != row["size_bytes"] or hashlib.sha256(raw).hexdigest() != row["sha256"]:
            raise ApiError("COMMUNITY_MEDIA_SOURCE_CHANGED", "A caption or chapter attachment failed integrity verification", 409)
        return
    if row["source_kind"] in {"material", "media_render"}:
        source, _path, _mime = _source_path(
            store, row["source_workspace_id"], row["source_kind"], row["source_id"], row["source_version"],
            allow_sidecar=row["role"] in {"caption", "chapters"},
            check_running=check_running,
        )
        if source["sha256"] != row["sha256"] or source["size_bytes"] != row["size_bytes"]:
            raise ApiError("COMMUNITY_MEDIA_SOURCE_CHANGED", "A selected media member changed after review", 409)
        return
    if row["source_kind"] == "media_project":
        from .media_api import _project_export_document

        manifest = _project_export_document(store, row["source_workspace_id"], row["source_id"], row["source_version"])
        digest = hashlib.sha256(_json(manifest).encode("utf-8")).hexdigest()
        if digest != row["sha256"] or len(_json(manifest).encode("utf-8")) != row["size_bytes"]:
            raise ApiError("COMMUNITY_MEDIA_SOURCE_CHANGED", "The Media template source changed after review", 409)
        return
    raise ApiError("COMMUNITY_MEDIA_SOURCE_INVALID", "The media publication source reference is invalid", 409)


def verify_draft_media_members(store, connection, publication_id: str, publication_version: int, *, check_running=None) -> list[dict]:
    rows = _draft_members(connection, publication_id, publication_version)
    for row in rows:
        if check_running is not None:
            check_running()
        _verify_member_source(store, row, check_running=check_running)
    return rows


def validate_draft_media_licenses(
    content: dict, rows: list[dict], purpose: str = "view", *, private_draft: bool = False,
) -> None:
    if private_draft:
        return
    for row in rows:
        if row["role"] != "template_source":
            _validate_member_dependency_license(content, row, purpose)


def _validate_member_dependency_license(content: dict, member: dict, purpose: str) -> None:
    from .community import _check_license

    _check_license(content.get("license", {}), purpose)
    try:
        metadata = json.loads(member.get("metadata_json") or "{}")
    except (TypeError, ValueError):
        metadata = {}
    dependency_ids = metadata.get("dependency_ids", [])
    dependencies = content.get("dependencies", [])
    selected = [item for item in dependencies if item.get("id") in dependency_ids]
    if len(selected) != len(set(dependency_ids)):
        raise ApiError("MEDIA_DEPENDENCY_LICENSE_REQUIRED", "This media member is missing a reviewed dependency license", 403)
    for dependency in selected:
        _check_license(dependency["license"], purpose)
        if purpose in {"view", "download"} and not dependency["license"].get("redistribute"):
            raise ApiError("MEDIA_DEPENDENCY_RIGHTS_INSUFFICIENT", "A source dependency does not grant public distribution", 403)


def copy_draft_media_to_release(connection, publication_id: str, publication_version: int, release_id: str) -> list[dict]:
    members = _draft_members(connection, publication_id, publication_version)
    for member in members:
        connection.execute(
            """INSERT INTO publication_media_releases(
                   release_id,member_id,role,source_kind,source_id,source_workspace_id,source_version,
                   sha256,size_bytes,mime_type,filename,metadata_json,inline_content,created_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                release_id, member["member_id"], member["role"], member["source_kind"],
                member["source_id"], member["source_workspace_id"], member["source_version"],
                member["sha256"], member["size_bytes"], member["mime_type"], member["filename"],
                member["metadata_json"], member["inline_content"], member["created_at"],
            ),
        )
    return members


def public_media_manifest(connection, release_id: str) -> list[dict]:
    return [_row_public_member(row) for row in _release_members(connection, release_id) if row["role"] != "template_source"]


def draft_media_manifest(connection, publication_id: str, publication_version: int) -> list[dict]:
    return [_row_public_member(row) for row in _draft_members(connection, publication_id, publication_version) if row["role"] != "template_source"]


def validate_release_media_licenses(connection, release_id: str, content: dict, purpose: str) -> None:
    for member in _release_members(connection, release_id):
        if member["role"] != "template_source":
            _validate_member_dependency_license(content, member, purpose)


def _member_response(store, member: dict, purpose: str):
    if member["source_kind"] == "inline":
        raw = (member["inline_content"] or "").encode("utf-8")
        if hashlib.sha256(raw).hexdigest() != member["sha256"] or len(raw) != member["size_bytes"]:
            raise ApiError("COMMUNITY_MEDIA_INTEGRITY_ERROR", "A caption or chapter attachment failed integrity verification", 409)
        response = make_response(raw)
        response.headers["Content-Type"] = member["mime_type"]
        response.headers["Content-Length"] = str(len(raw))
        response.headers["ETag"] = f'"{member["sha256"]}"'
        response.headers["Accept-Ranges"] = "bytes"
    else:
        source, path, _mime = _source_path(
            store, member["source_workspace_id"], member["source_kind"], member["source_id"], member["source_version"],
            allow_sidecar=member["role"] in {"caption", "chapters"},
        )
        if source["sha256"] != member["sha256"] or source["size_bytes"] != member["size_bytes"]:
            raise ApiError("COMMUNITY_MEDIA_SOURCE_CHANGED", "A published media member no longer matches its reviewed version", 409)
        response = send_file(
            path,
            mimetype=member["mime_type"],
            as_attachment=purpose == "download",
            download_name=member["filename"],
            conditional=True,
            etag=member["sha256"],
        )
        response.headers["Accept-Ranges"] = "bytes"
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


def erase_publication_media(
    connection,
    publication_ids: list[str],
    release_ids: list[str],
    release_licenses: dict[str, dict] | None = None,
) -> None:
    release_licenses = release_licenses or {}
    if publication_ids:
        markers = ",".join("?" for _ in publication_ids)
        connection.execute(f"DELETE FROM publication_media_drafts WHERE publication_id IN ({markers})", tuple(publication_ids))
    if release_ids:
        markers = ",".join("?" for _ in release_ids)
        instances = connection.execute(
            f"SELECT media_project_id,release_id,source_manifest_json FROM community_media_template_instances WHERE release_id IN ({markers})",
            tuple(release_ids),
        ).fetchall()
        for instance in instances:
            lineage = json.loads(instance["source_manifest_json"] or "{}")
            template = lineage.get("template")
            if not isinstance(template, dict):
                template = {}
            license_record = release_licenses.get(instance["release_id"])
            if not isinstance(license_record, dict):
                license_record = lineage.get("license")
            # Preserve the recipient's licensed project and update history while
            # removing source-account project identifiers and private manifests.
            connection.execute(
                "UPDATE community_media_template_instances SET source_project_id=NULL,source_manifest_json=? WHERE media_project_id=?",
                (_json({"template": template, "license": license_record, "source_manifest_erased": True}), instance["media_project_id"]),
            )
        connection.execute(f"DELETE FROM publication_media_releases WHERE release_id IN ({markers})", tuple(release_ids))
        connection.execute(f"DELETE FROM release_stream_grants WHERE release_id IN ({markers})", tuple(release_ids))


def media_publication_capabilities(store) -> dict:
    from .media import _ffprobe_path

    return {
        "upload_source": True,
        "probe": "AVAILABLE" if _ffprobe_path() else "NOT_CONFIGURED",
        "publication_delivery": "local_review_only",
        "media_playback_range": "AVAILABLE",
    }


def replay_media_template_instantiation(store, identity: dict, release_id: str, data: dict, idempotency_key: str):
    """Return an exact previously created copy without reopening a withdrawn source."""
    from .api import ApiError
    from .collaboration import require_workspace_access
    from .media_api import _project_view

    workspace_id = identity["workspace"]["id"]
    with store.transaction(immediate=False) as connection:
        require_workspace_access(connection, identity)
        prior = connection.execute(
            "SELECT request_hash,resource_id FROM idempotency_records WHERE workspace_id=? AND operation='media_template_instantiate' AND key=?",
            (workspace_id, idempotency_key),
        ).fetchone()
        if prior is None:
            return None
        release = connection.execute(
            "SELECT content_hash,snapshot_json FROM publication_releases WHERE id=?",
            (release_id,),
        ).fetchone()
        source = connection.execute(
            "SELECT member_id,sha256,metadata_json FROM publication_media_releases WHERE release_id=? AND role='template_source'",
            (release_id,),
        ).fetchone()
        if release is None or source is None:
            raise ApiError("IDEMPOTENCY_CONFLICT", "The original Media template copy cannot be reconciled", 409)
        content = json.loads(release["snapshot_json"])
        fingerprint = hashlib.sha256(_json({
            "release_id": release_id,
            "release_content_hash": release["content_hash"],
            "template_source_member_id": source["member_id"],
            "template_source_sha256": source["sha256"],
            "workspace_id": workspace_id,
            "work_id": data.get("work_id"),
            "title": data.get("title", content["title"]),
            "parameters": data.get("parameters", {}),
            "purpose": data.get("purpose"),
        }).encode("utf-8")).hexdigest()
        if prior["request_hash"] != fingerprint:
            raise ApiError("IDEMPOTENCY_CONFLICT", "This key belongs to another Media template copy", 409)
        project = connection.execute(
            "SELECT * FROM media_projects WHERE id=? AND workspace_id=? AND owner_user_id=?",
            (prior["resource_id"], workspace_id, identity["user"]["id"]),
        ).fetchone()
        instance = connection.execute(
            "SELECT * FROM community_media_template_instances WHERE media_project_id=? AND workspace_id=?",
            (prior["resource_id"], workspace_id),
        ).fetchone()
        if project is None or instance is None:
            raise ApiError("IDEMPOTENCY_CONFLICT", "The original Media template project is unavailable", 409)
        source_metadata = json.loads(source["metadata_json"] or "{}")
        original_slot_map = source_metadata.get("slot_map", {})
        source_assets = source_metadata.get("manifest", {}).get("timeline", {}).get("assets", [])
        initial_version = connection.execute(
            "SELECT state_json FROM media_project_versions WHERE project_id=? AND workspace_id=? AND version=1",
            (project["id"], workspace_id),
        ).fetchone()
        target_assets = json.loads(initial_version["state_json"]).get("timeline", {}).get("assets", []) if initial_version else []
        source_asset_ids = [item.get("asset_id") for item in source_assets if isinstance(item, dict)]
        target_asset_ids = [item.get("asset_id") for item in target_assets if isinstance(item, dict)]
        original_mapping = {}
        for public_slot_id, source_asset_id in original_slot_map.items():
            try:
                original_mapping[public_slot_id] = target_asset_ids[source_asset_ids.index(source_asset_id)]
            except (ValueError, IndexError, TypeError):
                continue
        return {
            "state": "media_template_instantiated",
            "project": _project_view(store, workspace_id, project["id"], 1),
            "source_release_id": release_id,
            "source_release_hash": release["content_hash"],
            "license": content["license"],
            "source_mapping": original_mapping,
            "history_state": "new_private_project",
        }


def instantiate_media_template(store, identity: dict, release, content: dict, data: dict, idempotency_key: str, share_token: str | None = None):
    """Create a new Media project from a licensed published template and replacements."""
    from .api import _string
    from .collaboration import require_work_access
    from .community import _check_license, _release
    from .media_api import _import_media_project, _project_view

    if set(data) - {"work_id", "purpose", "parameters", "title"}:
        raise ApiError("INVALID_INPUT", "Media template use contains unsupported fields", 400)
    purpose = data.get("purpose")
    if purpose not in {"personal", "commercial"}:
        raise ApiError("INVALID_INPUT", "Choose personal or commercial media template use", 400)
    _check_license(content["license"], "adapt")
    if purpose == "commercial":
        _check_license(content["license"], "commercial")
    template = content.get("template")
    if not isinstance(template, dict) or template.get("schema") != "oevira-media-template/1":
        raise ApiError("NOT_TEMPLATE", "This release is not an editable Media template", 400)
    slots = template.get("slots")
    parameters = data.get("parameters", {})
    if not isinstance(slots, list) or not slots or not isinstance(parameters, dict):
        raise ApiError("INVALID_MEDIA_TEMPLATE", "Media template source slots and replacements are required", 400)
    slot_ids = {slot.get("id") for slot in slots if isinstance(slot, dict)}
    if len(slot_ids) != len(slots) or set(parameters) != slot_ids:
        raise ApiError("MEDIA_TEMPLATE_REPLACEMENT_REQUIRED", "Select one authorized Material replacement for every Media source slot", 400,
                       {"required_slots": sorted(slot_ids)})
    if any(type(value) is not str or not value for value in parameters.values()):
        raise ApiError("INVALID_MEDIA_TEMPLATE", "Source slot replacements must reference saved Materials", 400)

    workspace_id = identity["workspace"]["id"]
    work_id = data.get("work_id")
    if work_id is not None and (type(work_id) is not str or not work_id):
        raise ApiError("INVALID_INPUT", "work_id must be a string or null", 400)
    if work_id:
        with store.transaction(immediate=False) as connection:
            require_work_access(connection, identity, work_id, write=True)
    else:
        with store.transaction(immediate=False) as connection:
            require_workspace_access(connection, identity, write=True)

    with store.transaction(immediate=False) as connection:
        current_release, current_content = _release(connection, release["id"], identity, share_token, purpose="adapt")
    if current_release["content_hash"] != release["content_hash"]:
        raise ApiError("VERSION_CONFLICT", "The template release changed; reopen the template and try again", 409)
    release, content = current_release, current_content
    _check_license(content["license"], "adapt")
    if purpose == "commercial":
        _check_license(content["license"], "commercial")
    member = store.row(
        "SELECT * FROM publication_media_releases WHERE release_id=? AND role='template_source'",
        (release["id"],),
    )
    if member is None or member["source_kind"] != "media_project":
        raise ApiError("MEDIA_TEMPLATE_SOURCE_UNAVAILABLE", "The exact Media template source version is unavailable", 409)
    try:
        private_metadata = json.loads(member["metadata_json"] or "{}")
        manifest = private_metadata["manifest"]
        public_slot_map = private_metadata["slot_map"]
    except (KeyError, TypeError, ValueError) as error:
        raise ApiError("MEDIA_TEMPLATE_SOURCE_INVALID", "The saved Media template source manifest is invalid", 409) from error
    if not isinstance(manifest, dict) or not isinstance(public_slot_map, dict) or set(public_slot_map) != slot_ids:
        raise ApiError("MEDIA_TEMPLATE_SOURCE_INVALID", "The saved source slot map does not match this template version", 409)
    source_workspace_id = member["source_workspace_id"]
    source_project_id = member["source_id"]
    source_version = member["source_version"]
    internal_slot_map = {}
    for public_slot_id, internal_asset_id in public_slot_map.items():
        material_id = parameters[public_slot_id]
        internal_slot_map[internal_asset_id] = material_id

    # Check destination Materials with the existing collaboration/material rules before
    # the Media import owner fingerprints the local originals and validates slot streams.
    from .media_api import _store_material_source

    for material_id in parameters.values():
        with store.transaction(immediate=False) as connection:
            require_material_access(connection, identity, material_id, target_work_id=work_id)
            if work_id:
                require_work_access(connection, identity, work_id, write=True)
        _store_material_source(store, workspace_id, material_id, verify=True)

    source_manifest = deepcopy(manifest)
    project_doc = source_manifest.get("project")
    if isinstance(project_doc, dict):
        # The destination records the public release lineage, never the creator's
        # private workspace project identifier.
        project_doc.pop("source_origin", None)
        project_doc["source_origin"] = {
            "source_project_id": None,
            "source_package_sha256": member["sha256"],
            "source_snapshot_sha256": member["sha256"],
            "source_version": source_version,
            "historical_state": "licensed_publication_release",
            "approval_status": "approved",
            "rights_state": "unknown",
            "publication_state": "not_performed",
        }
    title = _string(data.get("title", content["title"]), "title", maximum=200)
    prepared = _import_media_project(
        store,
        identity,
        {
            "manifest": source_manifest,
            "title": title,
            "work_id": work_id,
            "source_slot_map": internal_slot_map,
        },
        persist=False,
    )
    target_timeline_assets = prepared["state"]["timeline"].get("assets", [])
    source_asset_ids = [item.get("asset_id") for item in manifest.get("timeline", {}).get("assets", [])]
    if len(target_timeline_assets) != len(source_asset_ids):
        raise ApiError("MEDIA_TEMPLATE_REPLACEMENT_REQUIRED", "Every source slot needs one verified target Material", 409)
    target_slot_map = {}
    for public_slot_id, old_asset_id in public_slot_map.items():
        try:
            target_index = source_asset_ids.index(old_asset_id)
            target_slot_map[public_slot_id] = target_timeline_assets[target_index]["asset_id"]
        except (ValueError, IndexError, KeyError, TypeError) as error:
            raise ApiError("MEDIA_TEMPLATE_SOURCE_INVALID", "The source slot order no longer matches its version", 409) from error

    fingerprint = hashlib.sha256(_json({
        "release_id": release["id"], "release_content_hash": release["content_hash"],
        "template_source_member_id": member["member_id"], "template_source_sha256": member["sha256"],
        "workspace_id": workspace_id, "work_id": work_id, "title": title,
        "parameters": parameters, "purpose": purpose,
    }).encode("utf-8")).hexdigest()
    project_id = str(uuid.uuid4())
    now = utc_now()
    with store.transaction() as connection:
        current_release = connection.execute(
            "SELECT content_hash,withdrawn_at FROM publication_releases WHERE id=?",
            (release["id"],),
        ).fetchone()
        if current_release is None or current_release["withdrawn_at"] is not None or current_release["content_hash"] != release["content_hash"]:
            raise _not_found()
        _check_license(content["license"], "adapt")
        if purpose == "commercial":
            _check_license(content["license"], "commercial")
        prior = connection.execute(
            "SELECT request_hash,resource_id FROM idempotency_records WHERE workspace_id=? AND operation='media_template_instantiate' AND key=?",
            (workspace_id, idempotency_key),
        ).fetchone()
        if prior is not None:
            if prior["request_hash"] != fingerprint:
                raise ApiError("IDEMPOTENCY_CONFLICT", "This key belongs to a different Media template copy", 409)
            project_id = prior["resource_id"]
            existing = connection.execute("SELECT id FROM media_projects WHERE id=? AND workspace_id=?", (project_id, workspace_id)).fetchone()
            if existing is None:
                raise ApiError("IDEMPOTENCY_CONFLICT", "The original Media template project is unavailable", 409)
        else:
            for material_id in parameters.values():
                require_material_access(connection, identity, material_id, target_work_id=work_id)
            if work_id:
                require_work_access(connection, identity, work_id, write=True)
            else:
                require_workspace_access(connection, identity, write=True)
            connection.execute(
                "INSERT INTO media_projects(id,workspace_id,owner_user_id,work_id,title,kind,current_version,created_at,updated_at,created_by) VALUES(?,?,?,?,?,?,1,?,?,?)",
                (project_id, workspace_id, identity["user"]["id"], work_id, title, prepared["kind"], now, now, identity["user"]["id"]),
            )
            connection.execute(
                "INSERT INTO media_project_versions(project_id,workspace_id,version,parent_version,operation,state_json,undo_stack_json,redo_stack_json,created_at,created_by) VALUES(?,?,1,NULL,'licensed_template_copy',?,'[]','[]',?,?)",
                (project_id, workspace_id, _json(prepared["state"]), now, identity["user"]["id"]),
            )
            if work_id:
                connection.executemany(
                    "INSERT OR IGNORE INTO work_materials(work_id,material_id,workspace_id,created_at) VALUES(?,?,?,?)",
                    [(work_id, material_id, workspace_id, now) for material_id in sorted(set(prepared["material_ids"]))],
                )
            connection.execute(
                "INSERT INTO community_media_template_instances(media_project_id,workspace_id,release_id,release_content_hash,source_project_id,source_project_version,source_manifest_json,slot_asset_map_json,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (project_id, workspace_id, release["id"], release["content_hash"], source_project_id,
                source_version, _json({"manifest": manifest, "template": template, "license": content["license"]}),
                _json({slot_id: {"asset_id": asset_id, "material_id": parameters[slot_id]} for slot_id, asset_id in target_slot_map.items()}),
                 identity["user"]["id"], now),
            )
            connection.execute(
                "INSERT INTO idempotency_records(workspace_id,operation,key,request_hash,resource_type,resource_id,created_at) VALUES(?,'media_template_instantiate',?,?,'media_project',?,?)",
                (workspace_id, idempotency_key, fingerprint, project_id, now),
            )
    return _success({
        "state": "media_template_instantiated",
        "project": _project_view(store, workspace_id, project_id),
        "source_release_id": release["id"],
        "source_release_hash": release["content_hash"],
        "license": content["license"],
        "source_mapping": target_slot_map,
        "history_state": "new_private_project",
    }, 201)


def _template_source_member(store, release_id: str) -> dict:
    member = store.row(
        "SELECT * FROM publication_media_releases WHERE release_id=? AND role='template_source'",
        (release_id,),
    )
    if member is None or member["source_kind"] != "media_project":
        raise ApiError("MEDIA_TEMPLATE_SOURCE_UNAVAILABLE", "The exact Media template source version is unavailable", 409)
    try:
        metadata = json.loads(member["metadata_json"] or "{}")
        manifest, slot_map = metadata["manifest"], metadata["slot_map"]
    except (KeyError, TypeError, ValueError) as error:
        raise ApiError("MEDIA_TEMPLATE_SOURCE_INVALID", "The saved Media template source manifest is invalid", 409) from error
    return {**dict(member), "manifest": manifest, "private_slot_map": slot_map}


def _node_key(items: list) -> str | None:
    for key in ("track_id", "clip_id", "overlay_id", "chapter_id", "transition_id", "slot_id", "id"):
        values = [item.get(key) for item in items if isinstance(item, dict)]
        if len(values) == len(items) and all(isinstance(value, str) and value for value in values) and len(set(values)) == len(values):
            return key
    return None


def _compact_conflict_value(value):
    if isinstance(value, str) and len(value) > 2000:
        return value[:2000] + "…"
    if isinstance(value, (dict, list)):
        try:
            encoded = _json(value)
        except ApiError:
            encoded = ""
        if len(encoded) > 4000:
            return {"summary": "value_too_large_for_inline_conflict", "sha256": hashlib.sha256(encoded.encode("utf-8")).hexdigest(), "bytes": len(encoded.encode("utf-8"))}
    return value


def _editable_template_graph(template: dict) -> dict:
    """Keep the merge to editable Media objects, excluding lineage and local bindings."""
    graph = deepcopy(template.get("graph"))
    slots = deepcopy(template.get("slots"))
    if not isinstance(graph, dict) or not isinstance(slots, list):
        raise ApiError("MEDIA_TEMPLATE_INVALID", "The editable Media template graph is incomplete", 409)
    # Timeline version is owned by Media project history. It is not a template
    # edit and differs by definition between the source and a private copy.
    graph.pop("version", None)
    for slot in slots:
        if not isinstance(slot, dict):
            raise ApiError("MEDIA_TEMPLATE_INVALID", "A Media source slot is malformed", 409)
        for local_key in ("replacement_present", "replacement_media_kind", "replacement_sha256"):
            slot.pop(local_key, None)
    return {
        "project_kind": template.get("project_kind"),
        "format_ratio": template.get("format_ratio"),
        "slots": slots,
        "graph": graph,
    }


def _merge_media_template(base, current, updated, *, path: str, resolutions: dict, changes: list, conflicts: list):
    sentinel = object()

    def same(left, right):
        return left == right

    def conflict_value(old, now, new, conflict_path=None):
        location = conflict_path or path or "/"
        choice = resolutions.get(location)
        conflicts.append({
            "path": location,
            "base": _compact_conflict_value(None if old is sentinel else old),
            "current": _compact_conflict_value(None if now is sentinel else now),
            "template": _compact_conflict_value(None if new is sentinel else new),
            "resolution": choice,
        })
        if choice == "take_template":
            return updated
        return current

    if same(current, base):
        if not same(updated, base):
            changes.append({
                "path": path or "/",
                "current": _compact_conflict_value(current),
                "template": _compact_conflict_value(updated),
            })
        return deepcopy(updated)
    if same(updated, base) or same(current, updated):
        return deepcopy(current)
    if isinstance(base, dict) and isinstance(current, dict) and isinstance(updated, dict):
        result = {}
        for key in sorted(set(base) | set(current) | set(updated)):
            old = base.get(key, sentinel)
            now = current.get(key, sentinel)
            new = updated.get(key, sentinel)
            if old is sentinel and now is sentinel:
                selected = deepcopy(new)
                changes.append({"path": f"{path}/{key}", "current": None, "template": _compact_conflict_value(new)})
            elif old is sentinel and new is sentinel:
                selected = deepcopy(now)
            elif old is sentinel:
                selected = conflict_value(old, now, new, f"{path}/{key}")
            elif now is sentinel and new is sentinel:
                continue
            elif now is sentinel or new is sentinel:
                selected = conflict_value(old, now, new, f"{path}/{key}")
            else:
                selected = _merge_media_template(
                    old, now, new, path=f"{path}/{key}",
                    resolutions=resolutions, changes=changes, conflicts=conflicts,
                )
            if selected is not sentinel:
                result[key] = selected
        return result
    if isinstance(base, list) and isinstance(current, list) and isinstance(updated, list):
        key = _node_key(base) or _node_key(current) or _node_key(updated)
        if key is None:
            return conflict_value(base, current, updated)
        old_map = {item[key]: item for item in base}
        current_map = {item[key]: item for item in current}
        new_map = {item[key]: item for item in updated}
        merged = {}
        all_ids = list(dict.fromkeys([item[key] for item in current] + [item[key] for item in updated] + [item[key] for item in base]))
        for identifier in all_ids:
            old = old_map.get(identifier, sentinel)
            now = current_map.get(identifier, sentinel)
            new = new_map.get(identifier, sentinel)
            if old is sentinel and now is sentinel:
                merged[identifier] = deepcopy(new)
                changes.append({"path": f"{path}/@{identifier}", "current": None, "template": _compact_conflict_value(new)})
            elif old is sentinel and new is sentinel:
                merged[identifier] = deepcopy(now)
            elif old is sentinel:
                selected = conflict_value(old, now, new, f"{path}/@{identifier}")
                if selected is not sentinel:
                    merged[identifier] = selected
            elif now is sentinel and new is sentinel:
                continue
            elif now is sentinel or new is sentinel:
                selected = conflict_value(old, now, new, f"{path}/@{identifier}")
                if selected is not sentinel:
                    merged[identifier] = selected
            else:
                merged[identifier] = _merge_media_template(
                    old, now, new, path=f"{path}/@{identifier}",
                    resolutions=resolutions, changes=changes, conflicts=conflicts,
                )
        old_order = [item[key] for item in base]
        current_order = [item[key] for item in current if item[key] in merged]
        new_order = [item[key] for item in updated if item[key] in merged]
        order_path = f"{path}/$order"
        if current_order == old_order:
            order = new_order
            if new_order != old_order:
                changes.append({"path": order_path, "current": current_order, "template": new_order})
        elif new_order == old_order or current_order == new_order:
            order = current_order
        else:
            choice = resolutions.get(order_path)
            conflicts.append({"path": order_path, "base": old_order, "current": current_order, "template": new_order, "resolution": choice})
            order = new_order if choice == "take_template" else current_order
        extras = [item for item in all_ids if item in merged and item not in order]
        return [merged[item] for item in [*order, *extras] if item in merged]
    return conflict_value(base, current, updated)


def _current_media_template_graph(store, workspace_id: str, project_id: str, version: int, instance: dict):
    from .media_api import _load_timeline, _version_row

    version_row = _version_row(store, workspace_id, project_id, version)
    state = json.loads(version_row["state_json"])
    timeline, _availability = _load_timeline(store, workspace_id, state, verify_files=True)
    timeline_data = timeline.to_dict()
    template_record = json.loads(instance["source_manifest_json"])
    base_template = template_record.get("template")
    if not isinstance(base_template, dict) or base_template.get("schema") != "oevira-media-template/1":
        raise ApiError("MEDIA_TEMPLATE_INSTANCE_INVALID", "The saved template lineage is incomplete", 409)
    slot_bindings = json.loads(instance["slot_asset_map_json"] or "{}")
    asset_by_id = {asset.asset_id: asset for asset in timeline.assets}
    slot_to_asset = {}
    source_asset_to_slot = {}
    for slot_id, binding in slot_bindings.items():
        asset_id = binding.get("asset_id") if isinstance(binding, dict) else binding
        if isinstance(asset_id, str) and asset_id in asset_by_id:
            slot_to_asset[slot_id] = asset_by_id[asset_id]
            source_asset_to_slot[asset_id] = slot_id

    # A user may replace a template clip's source in the existing Media editor.
    # Preserve that replacement under the original template slot by matching the
    # stable clip identity that the editor intentionally keeps across a source swap.
    base_graph = base_template.get("graph", {})
    base_clip_slot = {}
    for track in base_graph.get("tracks", []):
        if not isinstance(track, dict):
            continue
        for clip in track.get("clips", []):
            if isinstance(clip, dict) and isinstance(clip.get("clip_id"), str) and isinstance(clip.get("media_asset_id"), str):
                base_clip_slot[clip["clip_id"]] = clip["media_asset_id"]
    current_slot_asset_ids = {}
    for track in timeline_data.get("tracks", []):
        if not isinstance(track, dict):
            continue
        for clip in track.get("clips", []):
            if not isinstance(clip, dict) or clip.get("clip_id") not in base_clip_slot:
                continue
            asset_id = clip.get("media_asset_id")
            slot_id = base_clip_slot[clip["clip_id"]]
            if isinstance(asset_id, str) and asset_id in asset_by_id:
                previous_id = current_slot_asset_ids.get(slot_id)
                if previous_id is not None and previous_id != asset_id:
                    raise ApiError("MEDIA_TEMPLATE_INSTANCE_INVALID", "One template source slot now points to multiple incompatible Materials", 409,
                                   {"slot_id": slot_id})
                current_slot_asset_ids[slot_id] = asset_id
                slot_to_asset[slot_id] = asset_by_id[asset_id]
                source_asset_to_slot.setdefault(asset_id, slot_id)

    current_slots = []
    seen_slots = set()
    for slot in base_template.get("slots", []):
        if not isinstance(slot, dict) or not isinstance(slot.get("id"), str):
            continue
        current = deepcopy(slot)
        asset = slot_to_asset.get(slot["id"])
        if asset is not None:
            current["replacement_present"] = True
            current["replacement_media_kind"] = asset.media_kind
            current["replacement_sha256"] = asset.sha256
        else:
            current["replacement_present"] = False
        current_slots.append(current)
        seen_slots.add(slot["id"])
    for slot_id, asset in slot_to_asset.items():
        if slot_id in seen_slots:
            continue
        current_slots.append({
            "id": slot_id,
            "label": asset.filename,
            "media_kind": asset.media_kind,
            "required_streams": {"video": asset.video_stream_count, "audio": asset.audio_stream_count},
            "source_inventory": {"video_streams": asset.video_stream_count, "audio_streams": asset.audio_stream_count},
            "minimum_source_duration_sec": {"num": 0, "den": 1},
            "replacement_required": False,
            "replacement_present": True,
            "replacement_media_kind": asset.media_kind,
            "replacement_sha256": asset.sha256,
        })
        seen_slots.add(slot_id)
    for asset in timeline.assets:
        if asset.asset_id in source_asset_to_slot:
            continue
        slot_id = "local-" + hashlib.sha256(asset.asset_id.encode("utf-8")).hexdigest()[:16]
        if slot_id in seen_slots:
            continue
        slot_to_asset[slot_id] = asset
        source_asset_to_slot[asset.asset_id] = slot_id
        streams = asset.to_dict()["streams"]
        current_slots.append({
            "id": slot_id,
            "label": asset.filename,
            "media_kind": asset.media_kind,
            "required_streams": {"video": asset.video_stream_count, "audio": asset.audio_stream_count},
            "source_inventory": {"video_streams": asset.video_stream_count, "audio_streams": asset.audio_stream_count},
            "minimum_source_duration_sec": {"num": 0, "den": 1},
            "replacement_required": False,
            "replacement_present": True,
            "replacement_media_kind": asset.media_kind,
            "replacement_sha256": asset.sha256,
        })
        seen_slots.add(slot_id)

    timeline_data.pop("assets", None)
    for track in timeline_data.get("tracks", []):
        for clip in track.get("clips", []):
            actual_id = clip.get("media_asset_id")
            slot_id = base_clip_slot.get(clip.get("clip_id"), source_asset_to_slot.get(actual_id))
            if slot_id is None or slot_id not in slot_to_asset:
                raise ApiError("MEDIA_TEMPLATE_INSTANCE_INVALID", "A saved Media clip is detached from its source slot", 409)
            clip["media_asset_id"] = slot_id
    for item in timeline_data.get("video_color_interpretations", []):
        actual_id = item.get("asset_id")
        if actual_id in source_asset_to_slot:
            item["asset_id"] = source_asset_to_slot[actual_id]
    return {
        "project_kind": state.get("kind"),
        "format_ratio": timeline.format_ratio,
        "slots": current_slots,
        "graph": timeline_data,
    }, state, version_row, slot_to_asset, source_asset_to_slot


def _template_update_context(store, identity: dict, project_id: str, base_version: int, new_release_id: str, share_token: str | None):
    from .community import _release
    from .media_api import _project_row, _version_row

    workspace_id = identity["workspace"]["id"]
    project = _project_row(store, workspace_id, project_id)
    _require_source_scope(store, identity, project["work_id"], write=False)
    if project["current_version"] != base_version:
        raise ApiError("VERSION_CONFLICT", "The Media project changed; reopen it before comparing a template update", 409,
                       {"current_version": project["current_version"]})
    instance = store.row(
        "SELECT * FROM community_media_template_instances WHERE media_project_id=? AND workspace_id=?",
        (project_id, workspace_id),
    )
    if instance is None:
        raise ApiError("MEDIA_TEMPLATE_LINEAGE_UNAVAILABLE", "This Media project is not linked to a reusable template", 409)
    current_release_row = store.row("SELECT * FROM publication_releases WHERE id=?", (instance["release_id"],))
    if current_release_row is None:
        raise ApiError("MEDIA_TEMPLATE_LINEAGE_UNAVAILABLE", "The source template version is unavailable", 409)
    with store.transaction(immediate=False) as connection:
        new_release, new_content = _release(
            connection, new_release_id, identity, share_token, purpose="adapt"
        )
    new_template = new_content.get("template")
    if (new_release["kind"] != "template" or not isinstance(new_template, dict)
            or new_template.get("schema") != "oevira-media-template/1"):
        raise ApiError("NOT_MEDIA_TEMPLATE", "Choose a published editable Media template", 400)
    if new_release["publication_id"] != current_release_row["publication_id"]:
        raise ApiError("TEMPLATE_LINEAGE_MISMATCH", "Template updates must come from the same author's publication lineage", 409)
    if (new_release["id"] == current_release_row["id"]
            or new_release["published_at"] <= current_release_row["published_at"]):
        raise ApiError("MEDIA_TEMPLATE_VERSION_NOT_NEWER", "Choose a newer published release from this template lineage", 409)
    source_member = store.row(
        "SELECT * FROM publication_media_releases WHERE release_id=? AND role='template_source'",
        (new_release_id,),
    )
    if source_member is None or source_member["source_kind"] != "media_project":
        raise ApiError("MEDIA_TEMPLATE_SOURCE_UNAVAILABLE", "The selected template version has no editable Media source", 409)
    if source_member["source_version"] <= instance["source_project_version"]:
        raise ApiError("MEDIA_TEMPLATE_VERSION_NOT_NEWER", "The published template must point to a newer exact Media project version", 409)
    source_member = dict(source_member)
    try:
        source_member["manifest"] = json.loads(source_member["metadata_json"] or "{}")["manifest"]
    except (KeyError, TypeError, ValueError) as error:
        raise ApiError("MEDIA_TEMPLATE_SOURCE_INVALID", "The selected template manifest is incomplete", 409) from error
    _verify_member_source(store, source_member)
    current_graph, current_state, version_row, slot_to_asset, source_asset_to_slot = _current_media_template_graph(
        store, workspace_id, project_id, base_version, dict(instance)
    )
    try:
        old_template = json.loads(instance["source_manifest_json"])["template"]
    except (KeyError, TypeError, ValueError) as error:
        raise ApiError("MEDIA_TEMPLATE_LINEAGE_UNAVAILABLE", "The project no longer has an exact base template snapshot", 409) from error
    if new_template.get("project_kind") != current_graph["project_kind"]:
        raise ApiError("MEDIA_TEMPLATE_KIND_MISMATCH", "A template update cannot change a Media project from video to podcast or back", 409)
    return {
        "workspace_id": workspace_id,
        "project": project,
        "instance": dict(instance),
        "current_release": dict(current_release_row),
        "new_release": dict(new_release),
        "new_content": new_content,
        "new_template": new_template,
        "source_member": source_member,
        "old_template": old_template,
        "current_graph": current_graph,
        "current_state": current_state,
        "version_row": version_row,
        "slot_to_asset": slot_to_asset,
        "source_asset_to_slot": source_asset_to_slot,
    }


def preview_media_template_update(store, identity: dict, project_id: str, data: dict):
    if set(data) - {"base_version", "release_id", "share_token"} or not {"base_version", "release_id"} <= set(data):
        raise ApiError("INVALID_INPUT", "Template update preview needs a current Media version and a new release", 400)
    from .community import _version

    base_version = _version(data["base_version"])
    release_id = _string(data["release_id"], "release_id", maximum=255)
    context = _template_update_context(store, identity, project_id, base_version, release_id, data.get("share_token"))
    changes, conflicts = [], []
    merged = _merge_media_template(
        _editable_template_graph(context["old_template"]),
        _editable_template_graph(context["current_graph"]),
        _editable_template_graph(context["new_template"]),
        path="", resolutions={}, changes=changes, conflicts=conflicts,
    )
    current_slot_assets = context["slot_to_asset"]
    required_replacements = [
        slot["id"] for slot in merged.get("slots", [])
        if isinstance(slot, dict) and slot.get("replacement_required", True) and (
            slot["id"] not in current_slot_assets
            or not _slot_asset_compatible(current_slot_assets[slot["id"]], slot)
        )
    ]
    preview_hash = hashlib.sha256(_json({
        "project_id": project_id,
        "base_version": base_version,
        "from_release_id": context["current_release"]["id"],
        "from_release_hash": context["instance"]["release_content_hash"],
        "to_release_id": context["new_release"]["id"],
        "to_release_hash": context["new_release"]["content_hash"],
        "current_graph": context["current_graph"],
        "new_template": context["new_template"],
    }).encode("utf-8")).hexdigest()
    return {
        "project_id": project_id,
        "current_version": base_version,
        "from_release_id": context["current_release"]["id"],
        "to_release_id": context["new_release"]["id"],
        "preview_hash": preview_hash,
        "changes": changes,
        "conflicts": conflicts,
        "required_source_replacements": required_replacements,
        "automatic_merge": "proposed_only_until_user_applies",
    }


def _slot_asset_compatible(asset, slot: dict) -> bool:
    if asset.media_kind != slot.get("media_kind"):
        return False
    required = slot.get("required_streams", {})
    if (asset.video_stream_count < required.get("video", 0)
            or asset.audio_stream_count < required.get("audio", 0)):
        return False
    minimum = slot.get("minimum_source_duration_sec", {"num": 0, "den": 1})
    if asset.is_still_image:
        return asset.media_kind == "image"
    try:
        from .media_api import _fraction_time

        minimum_duration = _fraction_time(minimum)
    except (ApiError, VideoEditingError):
        return False
    return asset.duration_sec is not None and asset.duration_sec >= minimum_duration


def _current_template_slot_state(current_graph: dict, slot_asset_map: dict) -> tuple[dict, dict]:
    from .media_api import _load_timeline, _version_row

    slot_asset_objects = {}
    actual_to_slot = {}
    raw_bindings = {}
    for slot_id, value in slot_asset_map.items():
        if isinstance(value, dict):
            asset_id = value.get("asset_id")
            material_id = value.get("material_id")
        else:
            asset_id = value
            material_id = None
        if type(asset_id) is str:
            actual_to_slot[asset_id] = slot_id
            raw_bindings[slot_id] = {"asset_id": asset_id, "material_id": material_id}
    return actual_to_slot, raw_bindings


def apply_media_template_update(store, identity: dict, project_id: str, data: dict):
    from .api import _idempotency_key, _resource_path
    from .collaboration import require_material_access
    from .community import _check_license, _version
    from .media_api import (
        _append_project_version,
        _load_timeline,
        _new_media_asset,
        _project_view,
        _project_row,
        _project_state,
        _store_material_source,
        _version_row,
        _version_stack,
    )

    allowed = {"base_version", "release_id", "preview_hash", "resolutions", "source_slot_map", "share_token"}
    if set(data) - allowed or not {"base_version", "release_id", "preview_hash", "resolutions"} <= set(data):
        raise ApiError("INVALID_INPUT", "Apply a reviewed template update using its current version and preview", 400)
    base_version = _version(data["base_version"])
    release_id = _string(data["release_id"], "release_id", maximum=255)
    preview_hash = _string(data["preview_hash"], "preview_hash", maximum=64)
    resolutions = data["resolutions"]
    replacement_map = data.get("source_slot_map", {})
    if not isinstance(resolutions, dict) or any(type(path) is not str or choice not in {"keep_current", "take_template"} for path, choice in resolutions.items()):
        raise ApiError("INVALID_TEMPLATE_UPDATE_RESOLUTION", "Resolve each conflict by keeping the current edit or selecting the template version", 400)
    if not isinstance(replacement_map, dict) or any(type(slot_id) is not str or type(material_id) is not str for slot_id, material_id in replacement_map.items()):
        raise ApiError("INVALID_MEDIA_SLOT_MAP", "Replacement mapping must map a source slot to a saved Material", 400)
    key = _idempotency_key()
    if not key:
        raise ApiError("IDEMPOTENCY_REQUIRED", "Applying a Media template update requires an idempotency key", 400)
    workspace_id = identity["workspace"]["id"]
    request_hash = hashlib.sha256(_json({
        "project_id": project_id,
        "base_version": base_version,
        "release_id": release_id,
        "preview_hash": preview_hash,
        "resolutions": resolutions,
        "source_slot_map": replacement_map,
    }).encode("utf-8")).hexdigest()
    prior = store.row(
        "SELECT request_hash,resource_id FROM idempotency_records WHERE workspace_id=? AND operation='community_media_template_update' AND key=?",
        (workspace_id, key),
    )
    if prior is not None:
        if prior["request_hash"] != request_hash:
            raise ApiError("IDEMPOTENCY_CONFLICT", "This key belongs to a different Media template update", 409)
        project_text, separator, version_text = prior["resource_id"].rpartition(":")
        if not separator or project_text != project_id:
            raise ApiError("IDEMPOTENCY_CONFLICT", "The original Media template update is unavailable", 409)
        return _success({
            "state": "media_template_updated",
            "project": _project_view(store, workspace_id, project_id, int(version_text)),
            "replayed": True,
        })

    raw_preview = preview_media_template_update(
        store, identity, project_id,
        {"base_version": base_version, "release_id": release_id,
         **({"share_token": data["share_token"]} if data.get("share_token") else {})},
    )
    if raw_preview["preview_hash"] != preview_hash:
        raise ApiError("MEDIA_TEMPLATE_PREVIEW_CHANGED", "The Media project or template changed after preview; compare again", 409,
                       {"current_preview_hash": raw_preview["preview_hash"]})
    unresolved = {item["path"] for item in raw_preview["conflicts"]} - set(resolutions)
    if unresolved:
        raise ApiError("MEDIA_TEMPLATE_CONFLICTS_UNRESOLVED", "Resolve each conflict before applying this template update", 409,
                       {"conflicts": raw_preview["conflicts"], "unresolved_paths": sorted(unresolved)})
    unexpected = set(resolutions) - {item["path"] for item in raw_preview["conflicts"]}
    if unexpected:
        raise ApiError("INVALID_TEMPLATE_UPDATE_RESOLUTION", "A resolution refers to a conflict that is no longer present", 409,
                       {"unknown_paths": sorted(unexpected)})
    context = _template_update_context(
        store, identity, project_id, base_version, release_id,
        data.get("share_token"),
    )
    _require_source_scope(store, identity, context["project"]["work_id"], write=True)
    changes, conflicts = [], []
    merged = _merge_media_template(
        _editable_template_graph(context["old_template"]),
        _editable_template_graph(context["current_graph"]),
        _editable_template_graph(context["new_template"]),
        path="", resolutions=resolutions, changes=changes, conflicts=conflicts,
    )
    slots = merged.get("slots")
    if not isinstance(slots, list):
        raise ApiError("MEDIA_TEMPLATE_MERGE_INVALID", "The merged template has no source slots", 409)
    slot_ids = {slot.get("id") for slot in slots if isinstance(slot, dict)}
    if len(slot_ids) != len(slots) or any(type(slot_id) is not str for slot_id in slot_ids):
        raise ApiError("MEDIA_TEMPLATE_MERGE_INVALID", "The merged source slots are invalid", 409)
    current_state = context["current_state"]
    _current_timeline, _ = _load_timeline(store, workspace_id, current_state, verify_files=True)
    asset_by_slot = dict(context["slot_to_asset"])
    # Preserve added Media sources under their local slots by comparing normalized
    # slot definitions returned by the current project state.
    current_slot_values = {slot["id"]: slot for slot in context["current_graph"]["slots"]}
    for slot in slots:
        slot_id = slot["id"]
        if slot_id in replacement_map:
            material_id = replacement_map[slot_id]
            with store.transaction(immediate=False) as connection:
                require_material_access(connection, identity, material_id, target_work_id=context["project"]["work_id"])
            material, path = _store_material_source(store, workspace_id, material_id, verify=True)
            asset = _new_media_asset(path, material)
            if not _slot_asset_compatible(asset, slot):
                raise ApiError("MEDIA_TEMPLATE_SLOT_INCOMPATIBLE", "The selected Material does not satisfy this source slot's media, stream and duration requirements", 409,
                               {"slot_id": slot_id, "required": slot, "actual": _media_asset_summary(asset)})
            asset_by_slot[slot_id] = asset
        elif slot_id not in asset_by_slot:
            raise ApiError("MEDIA_TEMPLATE_REPLACEMENT_REQUIRED", "Select an authorized replacement Material for each new Media template slot", 409,
                           {"slot_id": slot_id, "required": slot})
        elif not _slot_asset_compatible(asset_by_slot[slot_id], slot):
            raise ApiError("MEDIA_TEMPLATE_REPLACEMENT_REQUIRED", "A changed source slot no longer matches the Media project's current Material; select a replacement", 409,
                           {"slot_id": slot_id, "required": slot})
    if set(replacement_map) - slot_ids:
        raise ApiError("INVALID_MEDIA_SLOT_MAP", "A source replacement refers to a slot not present in the merged version", 400)

    merged_timeline = deepcopy(merged["graph"])
    if not isinstance(merged_timeline, dict):
        raise ApiError("MEDIA_TEMPLATE_MERGE_INVALID", "The merged Media graph is invalid", 409)
    media_assets = []
    slot_to_actual = {}
    unique_assets = {}
    for slot in slots:
        slot_id = slot["id"]
        asset = asset_by_slot[slot_id]
        slot_to_actual[slot_id] = asset.asset_id
        unique_assets.setdefault(asset.asset_id, asset)
    for track in merged_timeline.get("tracks", []):
        for clip in track.get("clips", []):
            slot_id = clip.get("media_asset_id")
            if slot_id not in slot_to_actual:
                raise ApiError("MEDIA_TEMPLATE_MERGE_INVALID", "A merged clip references a missing source slot", 409,
                               {"clip_id": clip.get("clip_id"), "slot_id": slot_id})
            clip["media_asset_id"] = slot_to_actual[slot_id]
    for item in merged_timeline.get("video_color_interpretations", []):
        slot_id = item.get("asset_id")
        if slot_id in slot_to_actual:
            item["asset_id"] = slot_to_actual[slot_id]
    merged_timeline["assets"] = [asset.to_dict() for asset in unique_assets.values()]
    merged_timeline["version"] = base_version + 1
    try:
        from .media import Timeline

        timeline = Timeline.from_dict(
            merged_timeline,
            assets=list(unique_assets.values()),
            verify_local_assets=False,
        )
    except VideoEditingError as error:
        raise ApiError("MEDIA_TEMPLATE_MERGE_INVALID", "The selected Media template update does not produce a valid editable timeline", 409,
                       {"reason": error.args[0] if error.args else "MEDIA_TEMPLATE_MERGE_INVALID"}) from error
    from .media_api import _append_project_version, _project_state, _version_row, _version_stack

    state = _project_state(title=current_state["title"], kind=current_state["kind"], timeline=timeline)
    state["source_origin"] = {
        "source_project_id": None,
        "source_package_sha256": context["source_member"]["sha256"],
        "source_snapshot_sha256": context["source_member"]["sha256"],
        "source_version": context["source_member"]["source_version"],
        "historical_state": "publication_release:" + release_id,
        "approval_status": "approved",
        "rights_state": "unknown",
        "publication_state": "not_performed",
    }
    version_row = _version_row(store, workspace_id, project_id, base_version)
    undo_stack = _version_stack(version_row, "undo_stack_json") + [base_version]
    new_version, _created = _append_project_version(
        store, identity, project_id, base_version=base_version, state=state,
        operation="template_update", idempotency_key=key,
        request_hash=request_hash, undo_stack=undo_stack, redo_stack=[],
    )
    new_slot_asset_map = {slot_id: asset_by_slot[slot_id].asset_id for slot_id in slot_ids}
    now = utc_now()
    with store.transaction() as connection:
        instance = connection.execute(
            "SELECT * FROM community_media_template_instances WHERE media_project_id=? AND workspace_id=?",
            (project_id, workspace_id),
        ).fetchone()
        if instance is None:
            raise ApiError("MEDIA_TEMPLATE_LINEAGE_UNAVAILABLE", "The source template instance changed during update", 409)
        connection.execute(
            "UPDATE community_media_template_instances SET release_id=?,release_content_hash=?,source_project_id=?,source_project_version=?,source_manifest_json=?,slot_asset_map_json=? WHERE media_project_id=? AND workspace_id=?",
            (release_id, context["new_release"]["content_hash"], context["source_member"]["source_id"],
             context["source_member"]["source_version"],
             _json({"manifest": context["source_member"]["manifest"], "template": context["new_template"],
                    "license": context["new_content"]["license"]}),
             _json(new_slot_asset_map), project_id, workspace_id),
        )
        connection.execute(
            "INSERT INTO community_media_template_update_history(media_project_id,workspace_id,version,from_release_id,to_release_id,from_release_hash,to_release_hash,merge_report_json,resolutions_json,updated_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (project_id, workspace_id, new_version, context["current_release"]["id"], release_id,
             context["instance"]["release_content_hash"], context["new_release"]["content_hash"],
             _json({"changes": changes, "conflicts": conflicts}), _json(resolutions),
             identity["user"]["id"], now),
        )
        connection.execute(
            "INSERT INTO idempotency_records(workspace_id,operation,key,request_hash,resource_type,resource_id,created_at) VALUES(?,'community_media_template_update',?,?,'media_project_version',?,?)",
            (workspace_id, key, request_hash, f"{project_id}:{new_version}", now),
        )
    return _success({
        "state": "media_template_updated",
        "project": _project_view(store, workspace_id, project_id, new_version),
        "from_release_id": context["current_release"]["id"],
        "to_release_id": release_id,
        "version": new_version,
        "changes_applied": changes,
        "conflicts_resolved": [item["path"] for item in conflicts],
        "user_edits_preserved": True,
    })


def list_media_sources(store, identity: dict) -> dict:
    workspace_id = identity["workspace"]["id"]
    with store.transaction(immediate=False) as connection:
        require_workspace_access(connection, identity)
        materials = connection.execute(
            "SELECT id,title,filename,format,status,sha256,size_bytes,work_id,created_at FROM materials WHERE workspace_id=? ORDER BY created_at DESC,id",
            (workspace_id,),
        ).fetchall()
        projects = connection.execute(
            "SELECT id,title,kind,work_id,current_version,updated_at FROM media_projects WHERE workspace_id=? ORDER BY updated_at DESC,id",
            (workspace_id,),
        ).fetchall()
        material_result = []
        for row in materials:
            try:
                require_material_access(connection, identity, row["id"])
            except ApiError:
                continue
            suffix = PurePosixPath(row["filename"]).suffix.lower()
            mime = _MEDIA_SOURCE_MIME_TYPES.get(suffix)
            sidecar_role = "caption" if suffix in {".vtt", ".srt"} else "chapters" if suffix == ".json" else None
            if mime is None and sidecar_role is None:
                continue
            material_result.append({
                "kind": "material" if mime is not None else "sidecar_material",
                "role": "media" if mime is not None else sidecar_role,
                "id": row["id"], "title": row["title"],
                "filename": row["filename"], "format": row["format"],
                "mime_type": mime or ("text/vtt" if suffix == ".vtt" else "application/x-subrip" if suffix == ".srt" else "application/json"),
                "status": row["status"], "sha256": row["sha256"],
                "size_bytes": row["size_bytes"], "work_id": row["work_id"],
            })
        project_result = []
        for project in projects:
            if project["work_id"]:
                try:
                    require_work_access(connection, identity, project["work_id"])
                except ApiError:
                    continue
            render_rows = connection.execute(
                "SELECT r.id,r.project_version,r.output_kind,r.format,r.sha256,r.size_bytes,r.metadata_json,r.created_at "
                "FROM media_renders r JOIN jobs j ON j.id=r.job_id "
                "WHERE r.project_id=? AND r.workspace_id=? AND r.status='completed' AND j.kind='media_render' "
                "ORDER BY r.project_version DESC,r.created_at DESC,r.id",
                (project["id"], workspace_id),
            ).fetchall()
            version_row = connection.execute(
                "SELECT state_json FROM media_project_versions WHERE project_id=? AND workspace_id=? AND version=?",
                (project["id"], workspace_id, project["current_version"]),
            ).fetchone()
            state = json.loads(version_row["state_json"]) if version_row else {}
            try:
                timeline, _ = _load_timeline(store, workspace_id, state)
                captions = any(item.kind == "caption" for item in timeline.overlays)
                chapters = bool(timeline.chapters)
                source_materials = []
                seen_material_ids = set()
                accessible = True
                for asset in timeline.assets:
                    material = connection.execute(
                        "SELECT id,title,filename,format,status,sha256,size_bytes,work_id FROM materials WHERE id=? AND workspace_id=?",
                        (asset.source_material_id, workspace_id),
                    ).fetchone()
                    if material is None or material["sha256"] != asset.sha256:
                        accessible = False
                        continue
                    try:
                        require_material_access(connection, identity, asset.source_material_id)
                        if material["work_id"]:
                            require_work_access(connection, identity, material["work_id"])
                    except ApiError:
                        accessible = False
                        continue
                    if material["id"] in seen_material_ids:
                        continue
                    seen_material_ids.add(material["id"])
                    source_materials.append({
                        "id": material["id"], "title": material["title"],
                        "filename": material["filename"], "format": material["format"],
                        "status": material["status"], "sha256": material["sha256"],
                        "size_bytes": material["size_bytes"], "version": 1,
                        "media_kind": asset.media_kind,
                    })
            except (ApiError, VideoEditingError):
                captions, chapters, accessible, source_materials = False, False, False, []
            public_renders = []
            for render in render_rows:
                render_version = connection.execute(
                    "SELECT state_json FROM media_project_versions WHERE project_id=? AND workspace_id=? AND version=?",
                    (project["id"], workspace_id, render["project_version"]),
                ).fetchone()
                render_materials = []
                if render_version is not None:
                    try:
                        render_timeline, _ = _load_timeline(store, workspace_id, json.loads(render_version["state_json"]))
                        seen_render_materials = set()
                        for asset in render_timeline.assets:
                            material = connection.execute(
                                "SELECT id,title,filename,format,status,sha256,size_bytes,work_id FROM materials WHERE id=? AND workspace_id=?",
                                (asset.source_material_id, workspace_id),
                            ).fetchone()
                            if material is None or material["sha256"] != asset.sha256 or material["id"] in seen_render_materials:
                                continue
                            try:
                                require_material_access(connection, identity, material["id"])
                                if material["work_id"]:
                                    require_work_access(connection, identity, material["work_id"])
                            except ApiError:
                                continue
                            seen_render_materials.add(material["id"])
                            render_materials.append({
                                "id": material["id"], "title": material["title"],
                                "filename": material["filename"], "format": material["format"],
                                "status": material["status"], "sha256": material["sha256"],
                                "size_bytes": material["size_bytes"], "version": 1,
                                "media_kind": asset.media_kind,
                            })
                    except (ApiError, VideoEditingError):
                        render_materials = []
                public_renders.append({
                    "id": render["id"], "version": render["project_version"],
                    "output_kind": render["output_kind"], "format": render["format"],
                    "sha256": render["sha256"], "size_bytes": render["size_bytes"],
                    "metadata": json.loads(render["metadata_json"] or "{}"),
                    "created_at": render["created_at"],
                    "source_materials": render_materials,
                })
            project_result.append({
                "kind": "media_project", "id": project["id"], "title": project["title"],
                "project_kind": project["kind"], "work_id": project["work_id"],
                "version": project["current_version"], "updated_at": project["updated_at"],
                "sources_accessible": accessible, "source_materials": source_materials,
                "captions_available": captions,
                "chapters_available": chapters,
                "renders": public_renders,
            })
    return {"materials": material_result, "media_projects": project_result}


def list_media_template_instances(store, identity: dict) -> list[dict]:
    from .community import _check_license, _release
    from .media_api import _project_row

    workspace_id = identity["workspace"]["id"]
    result = []
    with store.transaction(immediate=False) as connection:
        require_workspace_access(connection, identity)
        rows = connection.execute(
            """SELECT i.*,p.title AS project_title,p.kind AS project_kind,p.work_id,p.current_version
               FROM community_media_template_instances i
               JOIN media_projects p ON p.id=i.media_project_id AND p.workspace_id=i.workspace_id
               WHERE i.workspace_id=? ORDER BY i.created_at DESC,i.media_project_id""",
            (workspace_id,),
        ).fetchall()
        for instance in rows:
            try:
                _require_source_scope(store, identity, instance["work_id"], write=True)
            except ApiError:
                continue
            current_release = connection.execute(
                "SELECT * FROM publication_releases WHERE id=?",
                (instance["release_id"],),
            ).fetchone()
            if current_release is None:
                continue
            candidates = connection.execute(
                """SELECT * FROM publication_releases
                   WHERE publication_id=? AND visibility='public' AND withdrawn_at IS NULL
                     AND publication_version>?
                   ORDER BY publication_version,published_at,id""",
                (current_release["publication_id"], current_release["publication_version"]),
            ).fetchall()
            updates = []
            for candidate in candidates:
                try:
                    _release(connection, candidate["id"], identity, purpose="adapt")
                    content = json.loads(candidate["snapshot_json"])
                    template = content.get("template")
                    if (candidate["kind"] != "template" or not isinstance(template, dict)
                            or template.get("schema") != "oevira-media-template/1"):
                        continue
                    _check_license(content["license"], "adapt")
                except (ApiError, TypeError, ValueError):
                    continue
                source = connection.execute(
                    "SELECT * FROM publication_media_releases WHERE release_id=? AND role='template_source'",
                    (candidate["id"],),
                ).fetchone()
                if source is None or source["source_kind"] != "media_project" or source["source_version"] <= instance["source_project_version"]:
                    continue
                source = dict(source)
                try:
                    _verify_member_source(store, source)
                except ApiError:
                    continue
                updates.append({
                    "id": candidate["id"],
                    "title": content["title"],
                    "publication_version": candidate["publication_version"],
                    "published_at": candidate["published_at"],
                    "content_hash": candidate["content_hash"],
                    "source_version": source["source_version"],
                    "license": content["license"],
                    "slots": [
                        {key: slot.get(key) for key in (
                            "id", "label", "media_kind", "required_streams",
                            "minimum_source_duration_sec", "replacement_required",
                        )}
                        for slot in template.get("slots", []) if isinstance(slot, dict)
                    ],
                })
            result.append({
                "project": {
                    "id": instance["media_project_id"],
                    "title": instance["project_title"],
                    "kind": instance["project_kind"],
                    "work_id": instance["work_id"],
                    "current_version": instance["current_version"],
                },
                "current_release_id": instance["release_id"],
                "current_release_hash": instance["release_content_hash"],
                "source_version": instance["source_project_version"],
                "license": json.loads(instance["source_manifest_json"] or "{}").get("license"),
                "updates": updates,
            })
    return result


def _grant_identity(connection, grant):
    if grant["user_id"] is None:
        return None
    row = connection.execute(
        """SELECT s.id AS session_id,s.user_id,s.workspace_id,u.name AS user_name,
                  w.name AS workspace_name,m.role
           FROM sessions s JOIN users u ON u.id=s.user_id
           JOIN workspaces w ON w.id=s.workspace_id
           JOIN memberships m ON m.user_id=s.user_id AND m.workspace_id=s.workspace_id
           WHERE s.id=? AND s.user_id=? AND s.workspace_id=? AND s.revoked_at IS NULL
             AND s.expires_at>? AND m.active=1""",
        (grant["session_id"], grant["user_id"], grant["workspace_id"], utc_now()),
    ).fetchone()
    if row is None:
        raise _not_found()
    return {
        "session_id": row["session_id"],
        "user_id": row["user_id"],
        "workspace_id": row["workspace_id"],
        "user": {"id": row["user_id"], "name": row["user_name"]},
        "workspace": {"id": row["workspace_id"], "name": row["workspace_name"]},
        "role": row["role"],
    }


def register_community_media_routes(app, store) -> None:
    """Register release media reads and version-bound source discovery."""
    from .community import _check_license, _release

    @app.get("/api/community/media/sources")
    def community_media_sources():
        return _success(list_media_sources(store, g.identity))

    @app.get("/api/community/media/template-instances")
    def community_media_template_instances():
        return _success(list_media_template_instances(store, g.identity))

    @app.post("/api/media/projects/<project_id>/template-updates/preview")
    def community_media_template_update_preview(project_id: str):
        return _success(preview_media_template_update(store, g.identity, project_id, _body()))

    @app.post("/api/media/projects/<project_id>/template-updates/apply")
    def community_media_template_update_apply(project_id: str):
        return apply_media_template_update(store, g.identity, project_id, _body())

    @app.route("/api/moderation/submissions/<publication_id>/media/<member_id>", methods=["GET", "HEAD"])
    def read_moderation_media(publication_id: str, member_id: str):
        from .community import _moderator

        _moderator(app, g.identity)
        with store.transaction(immediate=False) as connection:
            require_workspace_access(connection, g.identity)
            publication = connection.execute(
                "SELECT version,status FROM publications WHERE id=?",
                (publication_id,),
            ).fetchone()
            if publication is None or publication["status"] != "submitted":
                raise _not_found()
            member = connection.execute(
                "SELECT * FROM publication_media_drafts WHERE publication_id=? AND publication_version=? AND member_id=?",
                (publication_id, publication["version"], member_id),
            ).fetchone()
            if member is None or member["role"] == "template_source":
                raise _not_found()
            member = dict(member)
            _verify_member_source(store, member)
            content = json.loads(connection.execute(
                "SELECT content_json FROM publications WHERE id=?", (publication_id,)
            ).fetchone()["content_json"])
            _validate_member_dependency_license(content, member, "view")
        return _member_response(store, member, "view")

    @app.post("/api/releases/<release_id>/media/<member_id>/playback")
    def create_media_playback_grant(release_id: str, member_id: str):
        data = _body()
        if set(data) - {"share_token", "purpose"}:
            raise ApiError("INVALID_INPUT", "Unsupported media playback request", 400)
        purpose = data.get("purpose", "view")
        if purpose not in {"view", "download"}:
            raise ApiError("INVALID_INPUT", "Choose a supported media use", 400)
        now = utc_now()
        with store.transaction() as connection:
            release, content = _release(
                connection, release_id, getattr(g, "identity", None),
                data.get("share_token"), purpose=purpose,
            )
            member = connection.execute(
                "SELECT * FROM publication_media_releases WHERE release_id=? AND member_id=?",
                (release_id, member_id),
            ).fetchone()
            if member is None or member["role"] == "template_source":
                raise _not_found()
            _validate_member_dependency_license(content, dict(member), purpose)
            token = secrets.token_urlsafe(32)
            connection.execute("DELETE FROM release_stream_grants WHERE expires_at<=?", (now,))
            expires_at = _expiry_stamp(seconds=600)
            identity = getattr(g, "identity", None)
            connection.execute(
                "INSERT INTO release_stream_grants(token_hash,release_id,member_id,purpose,user_id,session_id,workspace_id,expires_at,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    hashlib.sha256(token.encode("utf-8")).hexdigest(), release_id, member_id,
                    purpose, None if identity is None else identity["user"]["id"],
                    None if identity is None else identity["session_id"],
                    None if identity is None else identity["workspace"]["id"], expires_at, now,
                ),
            )
        return _success({
            "stream_url": f"/api/community/media/streams/{token}",
            "expires_at": expires_at,
            "purpose": purpose,
            "delivery": "local_review_only",
        })

    @app.route("/api/community/media/streams/<stream_token>", methods=["GET", "HEAD"])
    def stream_release_media(stream_token: str):
        if type(stream_token) is not str or not 40 <= len(stream_token) <= 256:
            raise _not_found()
        token_hash = hashlib.sha256(stream_token.encode("utf-8")).hexdigest()
        now = utc_now()
        with store.transaction(immediate=False) as connection:
            grant = connection.execute(
                "SELECT * FROM release_stream_grants WHERE token_hash=? AND expires_at>?",
                (token_hash, now),
            ).fetchone()
            if grant is None:
                raise _not_found()
            release = connection.execute(
                "SELECT * FROM publication_releases WHERE id=? AND withdrawn_at IS NULL",
                (grant["release_id"],),
            ).fetchone()
            member = connection.execute(
                "SELECT * FROM publication_media_releases WHERE release_id=? AND member_id=?",
                (grant["release_id"], grant["member_id"]),
            ).fetchone()
            if release is None or member is None or member["role"] == "template_source":
                raise _not_found()
            if release["visibility"] in {"private", "team"}:
                identity = _grant_identity(connection, grant)
                # Recheck the current publication/workspace relationship on every Range request.
                _release(connection, release["id"], identity, purpose=grant["purpose"])
            content = json.loads(release["snapshot_json"])
            _validate_member_dependency_license(content, dict(member), grant["purpose"])
            member = dict(member)
        return _member_response(store, member, grant["purpose"])
