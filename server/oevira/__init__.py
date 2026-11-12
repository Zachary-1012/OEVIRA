"""OEVIRA modular local product core."""
from __future__ import annotations

import ipaddress
import os
from pathlib import Path
from urllib.parse import urlsplit

from flask import Flask, g, jsonify, request

from .api import ApiError, recover_pending_materials, register_routes
from .auth import AuthenticationError, authenticate, bearer_or_cookie_token, issue_local_capability
from .monitoring import install_monitoring_schema, register_monitoring_routes
from .acquisition_queries import install_query_schema, register_query_routes
from .business import install_business_schema, register_business_routes
from .browser import install_browser_schema
from .browser_api import register_browser_routes
from .documents_api import register_routes as register_document_routes
from .media_api import register_media_routes
from .business_execution import install_business_execution_schema, register_business_execution_routes
from .collaboration import install_collaboration_schema, register_collaboration_boundary, register_collaboration_routes, register_review_routes
from .notes import install_notes_schema, register_notes_routes
from .notifications import install_notifications_schema, register_notifications_routes
from .community import install_community_schema, register_community_routes
from .community import is_public_read_route
from .account import AccountService, install_account_schema
from .account_mailer import SMTPEmailDelivery
from .account_api import is_public_account_route, register_account_routes
from .intelligence import IntelligenceService, install_intelligence_schema
from .intelligence_api import register_routes as register_intelligence_routes
from .store import Store


def _valid_loopback_origin(value: str) -> bool:
    if not isinstance(value, str) or value == "*":
        return False
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        return False
    if parsed.path or parsed.query or parsed.fragment:
        return False
    try:
        return ipaddress.ip_address(parsed.hostname).is_loopback or parsed.hostname.lower() == "localhost"
    except ValueError:
        return False


def _request_origin_allowed(value: str, request_host: str, request_scheme: str, additional: set[str]) -> bool:
    if value in additional:
        return True
    parsed = urlsplit(value)
    return value == f"{request_scheme}://{request_host}" and _valid_loopback_origin(value) and not parsed.path


def create_app(
    data_root: str | Path,
    web_root: str | Path | None = None,
    *,
    allowed_origins=(),
    expected_port: int | None = None,
    bootstrap_file: str | Path | None = None,
    account_mailer=None,
    account_exporter=None,
    account_data_purger=None,
) -> Flask:
    app = Flask(__name__, static_folder=None)
    app.config.update(
        MAX_CONTENT_LENGTH=25 * 1024 * 1024,
        MAX_MATERIAL_BYTES=24 * 1024 * 1024,
        MAX_TEXT_MATERIAL_CHARS=200_000,
        SESSION_COOKIE_NAME="oevira_session",
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Strict",
        EXPECTED_PORT=expected_port,
    )
    origins = set(allowed_origins)
    if any(not _valid_loopback_origin(origin) for origin in origins):
        raise ValueError("Allowed origins must be exact loopback HTTP(S) origins")
    app.config["ALLOWED_ORIGINS"] = origins
    root = Path(data_root).expanduser()
    store = Store(root)
    from .worker import install_worker_schema
    from .source_use import SourceUseService, install_source_use_schema, register_source_use_routes

    install_worker_schema(store)
    install_business_schema(store)
    install_browser_schema(store)
    install_monitoring_schema(store)
    install_intelligence_schema(store)
    install_query_schema(store)
    install_source_use_schema(store)
    app.extensions["oevira.source_use"] = SourceUseService(store)
    install_collaboration_schema(store)
    install_notes_schema(store)
    install_notifications_schema(store)
    install_business_execution_schema(store)
    install_community_schema(store)
    install_account_schema(store)
    recover_pending_materials(store)
    app.extensions["oevira.store"] = store
    app.extensions["oevira.worker_state"] = {"status": "stopped", "last_heartbeat_at": None}
    intelligence_service = IntelligenceService(store)
    app.extensions["oevira.intelligence"] = intelligence_service
    configured_mailer = account_mailer
    if configured_mailer is None:
        configured_mailer = SMTPEmailDelivery.from_environment()
    account_service = AccountService(
        store,
        mailer=configured_mailer,
        account_exporter=account_exporter,
        user_data_purger=account_data_purger,
        public_base_url=os.environ.get("OEVIRA_PUBLIC_BASE_URL") or None,
    )
    app.extensions["oevira.account_service"] = account_service
    if bootstrap_file is not None:
        issue_local_capability(store, bootstrap_file)
    if web_root is None:
        app.config["WEB_ROOT"] = None
    else:
        web = Path(web_root).expanduser().resolve()
        if not web.is_dir():
            raise ValueError("web_root must be an existing directory")
        app.config["WEB_ROOT"] = web

    @app.before_request
    def protect_local_boundary():
        if request.headers.get("X-Forwarded-Host"):
            return jsonify({"error": {"code": "INVALID_HOST", "message": "Forwarded host headers are not accepted", "details": {}}}), 400
        host_value = request.headers.get("Host", "")
        try:
            parsed_host = urlsplit(f"//{host_value}")
            host_name = (parsed_host.hostname or "").lower()
            host_port = parsed_host.port
            loopback = (
                not parsed_host.username
                and not parsed_host.password
                and not parsed_host.path
                and not parsed_host.query
                and not parsed_host.fragment
                and (host_name in {"localhost", "127.0.0.1", "::1"} or ipaddress.ip_address(host_name).is_loopback)
            )
        except ValueError:
            loopback = False
            host_port = None
        if not loopback:
            return jsonify({"error": {"code": "INVALID_HOST", "message": "Only loopback hosts are accepted", "details": {}}}), 400
        if app.config["EXPECTED_PORT"] is not None and host_port != app.config["EXPECTED_PORT"]:
            return jsonify({"error": {"code": "INVALID_HOST", "message": "Host port is not accepted", "details": {}}}), 400
        try:
            if request.remote_addr and not ipaddress.ip_address(request.remote_addr).is_loopback:
                return jsonify({"error": {"code": "LOCAL_ONLY", "message": "This local service accepts loopback connections only", "details": {}}}), 403
        except ValueError:
            return jsonify({"error": {"code": "LOCAL_ONLY", "message": "This local service accepts loopback connections only", "details": {}}}), 403

        origin = request.headers.get("Origin")
        if origin and not _request_origin_allowed(origin, host_value, request.scheme, origins):
            return jsonify({"error": {"code": "ORIGIN_REJECTED", "message": "Origin is not allowed", "details": {}}}), 403
        if request.method == "OPTIONS":
            requested_method = request.headers.get("Access-Control-Request-Method", "")
            if requested_method not in {"GET", "HEAD", "POST", "PATCH", "DELETE", "OPTIONS"}:
                return jsonify({"error": {"code": "METHOD_REJECTED", "message": "Requested method is not allowed", "details": {}}}), 405
            return "", 204

        if request.path.startswith("/api/") and request.path not in {"/api/status", "/api/local-session"}:
            public_account = is_public_account_route(request.path, request.method)
            public_community = is_public_read_route(request.path, request.method)
            token = bearer_or_cookie_token(request, app.config["SESSION_COOKIE_NAME"])
            authorization_header = request.headers.get("Authorization", "")
            bearer_supplied = authorization_header.startswith("Bearer ")
            cookie_token = request.cookies.get(app.config["SESSION_COOKIE_NAME"])
            if public_account:
                # A stale browser cookie must not trap a claimant on the login
                # screen. Explicit Bearer credentials still fail closed, and
                # link flows require g.identity rather than downgrading to login.
                g.identity = None
                if cookie_token:
                    cookie_identity = authenticate(store, cookie_token)
                    if cookie_identity is None:
                        g.clear_stale_session_cookie = True
                    elif not bearer_supplied:
                        g.identity = cookie_identity
                if bearer_supplied:
                    identity = authenticate(store, token)
                    if identity is None:
                        return jsonify({"error": {"code": "UNAUTHENTICATED", "message": "The supplied bearer session is invalid", "details": {}}}), 401
                    g.identity = identity
            elif public_community:
                # Public/community endpoints can be anonymous, but a supplied
                # session must be real so team and unlisted access remains scoped.
                g.identity = None
                if token:
                    identity = authenticate(store, token)
                    if identity is None:
                        return jsonify({"error": {"code": "UNAUTHENTICATED", "message": "The supplied session is invalid", "details": {}}}), 401
                    g.identity = identity
            else:
                identity = authenticate(store, token)
                if identity is None:
                    return jsonify({"error": {"code": "UNAUTHENTICATED", "message": "A valid workspace session is required", "details": {}}}), 401
                g.identity = identity

    @app.after_request
    def add_response_safety_headers(response):
        origin = request.headers.get("Origin")
        if origin and _request_origin_allowed(origin, request.headers.get("Host", ""), request.scheme, origins):
            response.headers["Access-Control-Allow-Origin"] = origin
            response.headers["Access-Control-Allow-Credentials"] = "true"
            response.headers["Access-Control-Allow-Headers"] = "Authorization, Content-Type, Idempotency-Key, Upload-Offset, X-Chunk-SHA256"
            response.headers["Access-Control-Expose-Headers"] = "Upload-Offset, Content-Length, Content-Disposition"
            response.headers["Access-Control-Allow-Methods"] = "GET, HEAD, POST, PATCH, DELETE, OPTIONS"
            response.headers["Vary"] = "Origin"
        if getattr(g, "clear_stale_session_cookie", False):
            response.delete_cookie(
                app.config["SESSION_COOKIE_NAME"],
                path="/",
                secure=bool(app.config.get("SESSION_COOKIE_SECURE", request.is_secure)),
                httponly=True,
                samesite=app.config.get("SESSION_COOKIE_SAMESITE", "Strict"),
            )
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        if request.path.startswith("/api/"):
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    @app.errorhandler(ApiError)
    def handle_api_error(exc: ApiError):
        return jsonify({"error": {"code": exc.code, "message": exc.message, "details": exc.details}}), exc.status

    @app.errorhandler(AuthenticationError)
    def handle_auth_error(exc: AuthenticationError):
        return jsonify({"error": {"code": exc.code, "message": str(exc), "details": getattr(exc, "details", {})}}), exc.status

    @app.errorhandler(404)
    def handle_not_found(exc):
        if request.path.startswith("/api/"):
            return jsonify({"error": {"code": "NOT_FOUND", "message": "The requested resource was not found", "details": {}}}), 404
        return jsonify({"error": {"code": "NOT_FOUND", "message": "The requested page was not found", "details": {}}}), 404

    @app.errorhandler(413)
    def handle_too_large(exc):
        return jsonify({"error": {"code": "REQUEST_TOO_LARGE", "message": "Request exceeds the configured size limit", "details": {}}}), 413

    @app.errorhandler(Exception)
    def handle_unexpected_error(exc):
        app.logger.error("Unhandled request failure (%s)", type(exc).__name__)
        return jsonify({"error": {"code": "INTERNAL_ERROR", "message": "The request could not be completed", "details": {}}}), 500

    register_routes(app)
    register_monitoring_routes(app)
    register_business_routes(app)
    register_browser_routes(app)
    register_query_routes(app)
    from .browser_tools import create_browser_owner_tool_bindings

    for binding in create_browser_owner_tool_bindings(app):
        app.extensions["oevira.intelligence"].tools.bind_owner_tool(binding)
    register_document_routes(app, store)
    register_media_routes(app)
    from .delivery import register_delivery_routes

    register_delivery_routes(app, store)
    from .creation_research import register_research_routes

    register_research_routes(app, store)
    from .revisions import register_revision_routes

    register_revision_routes(app, store)
    register_collaboration_boundary(app)
    register_collaboration_routes(app)
    register_notes_routes(app)
    register_notifications_routes(app)
    register_review_routes(app)
    register_business_execution_routes(app)
    register_community_routes(app)
    register_account_routes(app, account_service)
    register_intelligence_routes(app, store, intelligence_service)
    register_source_use_routes(app)
    return app
