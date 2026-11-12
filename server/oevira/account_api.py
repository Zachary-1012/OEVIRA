"""HTTP routes for account lifecycle services.

The app factory owns calling :func:`register_account_routes` and allowing only
the explicit unauthenticated endpoints below through its auth gate. These
routes do not create a separate user/session store.
"""

from __future__ import annotations

import hashlib
from typing import Any
import re
from urllib.parse import urlencode

from flask import Flask, g, jsonify, make_response, redirect, request, send_file

from .account import AccountError, AccountService, SUPPORTED_PROVIDERS
from .account_membership import AccountWorkspaceMemberships
from .auth import SESSION_LIFETIME


_PUBLIC_EXACT_ROUTES = {
    ("GET", "/api/account/capabilities"),
    ("POST", "/api/account/email/register"),
    ("POST", "/api/account/email/verify"),
    ("POST", "/api/account/email/login/request"),
    ("POST", "/api/account/email/login/complete"),
    ("POST", "/api/account/password/login"),
    ("POST", "/api/account/password/reset/request"),
    ("POST", "/api/account/password/reset/complete"),
    ("POST", "/api/account/login-challenges/complete"),
    ("POST", "/api/account/invitations/preview"),
    ("POST", "/api/account/deletion/confirm"),
    ("POST", "/api/account/deletion/cancel/request"),
    ("POST", "/api/account/deletion/cancel"),
}
_PUBLIC_OAUTH_ROUTE = re.compile(r"^/api/account/oauth/(google|apple)/(start|complete|callback)$")


def is_public_account_route(path: str, method: str) -> bool:
    """Return whether a route may be reached without an OEVIRA session.

    The app's before-request hook must still authenticate a supplied cookie or
    bearer token on these paths so OAuth identity linking can use g.identity.
    """
    method = method.upper()
    if (method, path) in _PUBLIC_EXACT_ROUTES:
        return True
    match = _PUBLIC_OAUTH_ROUTE.fullmatch(path)
    if match is None:
        return False
    return method == "POST" if match.group(2) in {"start", "complete"} else method in {"GET", "POST"}


def _body() -> dict[str, Any]:
    if not request.is_json:
        raise AccountError("INVALID_INPUT", "A JSON request body is required", 400)
    value = request.get_json(silent=True)
    if not isinstance(value, dict):
        raise AccountError("INVALID_INPUT", "The JSON body must be an object", 400)
    return value


def _fields(body: dict[str, Any], allowed: set[str]) -> None:
    if set(body) - allowed:
        raise AccountError("INVALID_INPUT", "The request contains unsupported fields", 400)


def _identity() -> dict:
    identity = getattr(g, "identity", None)
    if not isinstance(identity, dict):
        raise AccountError("UNAUTHENTICATED", "A valid account session is required", 401)
    return identity


def _success(data: Any, status: int = 200):
    response = make_response(jsonify({"data": data}), status)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return response


def _session_response(app: Flask, data: dict[str, Any]):
    token = data.get("token")
    response = _success(data)
    if isinstance(token, str) and token:
        response.set_cookie(
            app.config["SESSION_COOKIE_NAME"],
            token,
            httponly=True,
            secure=bool(app.config.get("SESSION_COOKIE_SECURE", request.is_secure)),
            samesite=app.config.get("SESSION_COOKIE_SAMESITE", "Strict"),
            path="/",
            max_age=int(SESSION_LIFETIME.total_seconds()),
        )
    return response


def _clear_session_cookie(app: Flask, response):
    response.delete_cookie(
        app.config["SESSION_COOKIE_NAME"],
        path="/",
        secure=bool(app.config.get("SESSION_COOKIE_SECURE", request.is_secure)),
        httponly=True,
        samesite=app.config.get("SESSION_COOKIE_SAMESITE", "Strict"),
    )
    return response


def register_account_routes(app: Flask, service: AccountService) -> None:
    """Register explicit account routes after the app's error handlers exist."""
    if not isinstance(service, AccountService):
        raise TypeError("service must be an AccountService")
    app.extensions["oevira.account_service"] = service
    membership_service = AccountWorkspaceMemberships(service)
    app.extensions["oevira.account_memberships"] = membership_service
    browser_service = app.extensions.get("oevira.browser")
    if browser_service is not None and service.browser_session_closer is None:
        service.browser_session_closer = lambda engine, session_id: browser_service.runtime.close(engine, session_id)

    def add(rule: str, endpoint: str, view, methods: tuple[str, ...]) -> None:
        if endpoint in app.view_functions:
            raise ValueError(f"Route endpoint is already registered: {endpoint}")
        app.add_url_rule(rule, endpoint, view, methods=list(methods))

    def capabilities():
        configured = {
            provider: service.oauth_callback_ready(provider)
            for provider in sorted(SUPPORTED_PROVIDERS)
        }
        return _success({
            "email_delivery_configured": service._mailer_ready(),
            "email_registration": "AVAILABLE" if service._mailer_ready() else "NOT_CONFIGURED",
            "email_code_login": "AVAILABLE" if service._mailer_ready() else "NOT_CONFIGURED",
            "workspace_invitations": "AVAILABLE" if service._invitation_mailer_ready() else "NOT_CONFIGURED",
            "password_login": "AVAILABLE",
            "password_blocklist_configured": service.password_blocklist_configured,
            "account_export_configured": True,
            "account_export": "AVAILABLE",
            "account_data_purge_configured": service.user_data_purger is not None,
            "account_deletion": "AVAILABLE" if service._mailer_ready() and service.user_data_purger is not None else "NOT_CONFIGURED",
            "workspace_invitations": "AVAILABLE" if service._invitation_mailer_ready() else "NOT_CONFIGURED",
            "oauth": {
                provider: "CONFIGURED_NOT_VERIFIED" if available else (
                    "CALLBACK_NOT_CONFIGURED" if service.provider_configs.get(provider) is not None else "NOT_CONFIGURED"
                )
                for provider, available in configured.items()
            },
        })

    def register_email():
        body = _body()
        _fields(body, {"email", "display_name", "password"})
        return _success(service.register_email(
            body.get("email"), display_name=body.get("display_name"), password=body.get("password")
        ), 202)

    def verify_email():
        body = _body()
        _fields(body, {"reference", "code"})
        result = service.verify_email(body.get("reference"), body.get("code"), local_identity=getattr(g, "identity", None))
        return _session_response(app, result)

    def email_login_request():
        body = _body()
        _fields(body, {"email"})
        return _success(service.request_email_login(body.get("email")), 202)

    def email_login_complete():
        body = _body()
        _fields(body, {"reference", "code", "workspace_id"})
        result = service.complete_email_login(
            body.get("reference"), body.get("code"), workspace_id=body.get("workspace_id"),
            local_identity=getattr(g, "identity", None),
        )
        return _session_response(app, result)

    def password_login():
        body = _body()
        _fields(body, {"email", "password", "workspace_id"})
        result = service.login_password(
            body.get("email"), body.get("password"), workspace_id=body.get("workspace_id"),
            local_identity=getattr(g, "identity", None),
        )
        return _session_response(app, result)

    def password_reset_request():
        body = _body()
        _fields(body, {"email"})
        return _success(service.request_password_reset(body.get("email")), 202)

    def password_reset_complete():
        body = _body()
        _fields(body, {"reference", "code", "new_password"})
        response = _success(service.reset_password(body.get("reference"), body.get("code"), body.get("new_password")))
        return _clear_session_cookie(app, response) if getattr(g, "identity", None) is not None else response

    def complete_login_challenge():
        body = _body()
        _fields(body, {"challenge", "workspace_id"})
        return _session_response(
            app,
            service.complete_login_challenge(body.get("challenge"), body.get("workspace_id")),
        )

    def oauth_start(provider: str):
        if not service.oauth_callback_ready(provider):
            raise AccountError(
                "OAUTH_CALLBACK_UNAVAILABLE",
                "OAuth sign-in requires a configured product URL and the matching provider callback route",
                503,
            )
        body = _body()
        _fields(body, {"code_verifier", "intent"})
        intent = body.get("intent", "login")
        return _success(service.begin_oauth(
            provider,
            code_verifier=body.get("code_verifier"),
            intent=intent,
            identity=getattr(g, "identity", None),
        ))

    def oauth_complete(provider: str):
        body = _body()
        _fields(body, {"code", "state", "code_verifier"})
        result = service.complete_oauth(
            provider,
            code=body.get("code"),
            state=body.get("state"),
            code_verifier=body.get("code_verifier"),
            identity=getattr(g, "identity", None),
        )
        return _session_response(app, result) if result.get("state") == "authenticated" else _success(result)

    def oauth_callback(provider: str):
        if provider not in SUPPORTED_PROVIDERS or not service.oauth_callback_ready(provider):
            raise AccountError("OAUTH_CALLBACK_UNAVAILABLE", "This OAuth callback is not configured", 503)
        config = service.provider_configs[provider]
        request_host = request.host_url.rstrip("/")
        if f"{request_host}{request.path}" != config.redirect_uri:
            raise AccountError("OAUTH_CALLBACK_MISMATCH", "The OAuth callback URL does not match the configured provider URL", 400)
        if request.method == "POST":
            if request.mimetype != "application/x-www-form-urlencoded":
                raise AccountError("OAUTH_RESPONSE_INVALID", "The OAuth provider response format is invalid", 400)
            values = request.form
        else:
            values = request.args
        state_values = values.getlist("state")
        code_values = values.getlist("code")
        error_values = values.getlist("error")
        if len(state_values) != 1 or len(code_values) > 1 or len(error_values) > 1 or bool(code_values) == bool(error_values):
            raise AccountError("OAUTH_RESPONSE_INVALID", "The OAuth provider response is incomplete or ambiguous", 400)
        state = state_values[0]
        if not 32 <= len(state) <= 256:
            raise AccountError("OAUTH_STATE_INVALID", "The OAuth sign-in request is invalid or expired", 401)
        transaction = service.store.row(
            "SELECT 1 AS present FROM account_oauth_transactions WHERE state_hash = ? AND provider = ? AND status = 'pending' AND expires_at > ?",
            (hashlib.sha256(state.encode("utf-8")).hexdigest(), provider, service._stamp()),
        )
        if transaction is None:
            raise AccountError("OAUTH_STATE_INVALID", "The OAuth sign-in request is invalid or expired", 401)
        redirect_values = {"oauth_provider": provider, "oauth_state": state}
        if code_values:
            if not 1 <= len(code_values[0]) <= 4096:
                raise AccountError("OAUTH_RESPONSE_INVALID", "The OAuth authorization code is invalid", 400)
            redirect_values["oauth_code"] = code_values[0]
        else:
            redirect_values["oauth_error"] = "authorization_cancelled" if error_values[0] == "access_denied" else "authorization_failed"
        response = make_response(
            redirect(f"{service.public_base_url}/#{urlencode(redirect_values)}", code=303)
        )
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    def login_methods():
        return _success(service.login_methods(_identity()))

    def unlink_external_identity(identity_id: str):
        return _success(service.unlink_external_identity(_identity(), identity_id))

    def unlink_email():
        return _success(service.unlink_email(_identity()))

    def change_password():
        body = _body()
        _fields(body, {"current_password", "new_password"})
        return _success(service.change_password(_identity(), body.get("current_password"), body.get("new_password")))

    def request_email_change():
        body = _body()
        _fields(body, {"new_email"})
        return _success(service.request_email_change(_identity(), body.get("new_email")), 202)

    def confirm_email_change():
        body = _body()
        _fields(body, {"reference", "new_email", "code"})
        return _success(service.confirm_email_change(_identity(), body.get("reference"), body.get("new_email"), body.get("code")))

    def list_sessions():
        return _success(service.sessions(_identity()))

    def list_workspaces():
        return _success(membership_service.workspaces(_identity()))

    def switch_workspace(workspace_id: str):
        body = _body()
        _fields(body, set())
        return _session_response(
            app,
            membership_service.switch_workspace(_identity(), workspace_id),
        )

    def list_workspace_members(workspace_id: str):
        return _success(membership_service.members(_identity(), workspace_id))

    def list_workspace_invitations(workspace_id: str):
        return _success(membership_service.invitations(_identity(), workspace_id))

    def invite_workspace_member(workspace_id: str):
        body = _body()
        _fields(body, {"email", "role"})
        return _success(
            membership_service.invite(_identity(), workspace_id, body.get("email"), body.get("role")),
            202,
        )

    def resend_workspace_invitation(workspace_id: str, invitation_id: str):
        return _success(membership_service.resend_invitation(_identity(), workspace_id, invitation_id), 202)

    def revoke_workspace_invitation(workspace_id: str, invitation_id: str):
        return _success(membership_service.revoke_invitation(_identity(), workspace_id, invitation_id))

    def accept_workspace_invitation():
        body = _body()
        _fields(body, {"token"})
        return _session_response(app, membership_service.accept_invitation(_identity(), body.get("token")))

    def preview_workspace_invitation():
        body = _body()
        _fields(body, {"token"})
        return _success(membership_service.preview_invitation(body.get("token")))

    def update_workspace_member(workspace_id: str, user_id: str):
        body = _body()
        _fields(body, {"role", "expected_role"})
        return _success(membership_service.set_member_role(
            _identity(), workspace_id, user_id, body.get("role"), body.get("expected_role")
        ))

    def transfer_workspace_owner(workspace_id: str):
        body = _body()
        _fields(body, {"new_owner_user_id", "expected_owner_user_id"})
        return _success(membership_service.transfer_workspace_owner(
            _identity(), workspace_id, body.get("new_owner_user_id"), body.get("expected_owner_user_id")
        ))

    def remove_workspace_member(workspace_id: str, user_id: str):
        return _success(membership_service.remove_member(_identity(), workspace_id, user_id))

    def leave_workspace(workspace_id: str):
        return _session_response(
            app,
            membership_service.remove_member(_identity(), workspace_id, None, leaving=True),
        )

    def get_preferences():
        return _success(service.account_preferences(_identity()))

    def update_preferences():
        body = _body()
        _fields(body, {"model_improvement_consent", "expected_version"})
        return _success(service.set_model_improvement_consent(
            _identity(), body.get("model_improvement_consent"), body.get("expected_version")
        ))

    def create_export():
        return _success(service.create_account_export(_identity()), 201)

    def download_export(export_id: str):
        export = service.account_export_download(_identity(), export_id)
        response = make_response(send_file(
            export["path"],
            mimetype="application/zip",
            as_attachment=True,
            download_name="oevira-account-export.zip",
            conditional=False,
        ))
        response.headers["Cache-Control"] = "no-store"
        response.headers["Pragma"] = "no-cache"
        response.headers["X-Content-SHA256"] = export["sha256"]
        return response

    def preview_local_data_choice():
        body = _body()
        _fields(body, {"reference"})
        return _success(service.preview_local_data_choice(_identity(), body.get("reference")))

    def complete_local_data_choice():
        body = _body()
        _fields(body, {"reference", "choice"})
        result = service.complete_local_data_choice(
            _identity(), body.get("reference"), body.get("choice")
        )
        return _session_response(app, result) if result.get("token") else _success(result)

    def revoke_session(session_id: str):
        result = service.revoke_one_session(_identity(), session_id)
        response = _success(result)
        if session_id == getattr(g, "identity", {}).get("session_id"):
            _clear_session_cookie(app, response)
        return response

    def revoke_all_sessions():
        result = service.revoke_all_user_sessions(_identity())
        response = _success(result)
        return _clear_session_cookie(app, response)

    def logout():
        result = service.logout_current_session(_identity())
        response = _success(result)
        return _clear_session_cookie(app, response)

    def request_deletion():
        return _success(service.request_account_deletion(_identity()), 202)

    def deletion_status():
        return _success(service.account_deletion_status(_identity()))

    def confirm_deletion():
        body = _body()
        _fields(body, {"reference", "code"})
        response = _success(service.confirm_account_deletion(body.get("reference"), body.get("code")))
        return _clear_session_cookie(app, response) if getattr(g, "identity", None) is not None else response

    def request_cancel_deletion():
        body = _body()
        _fields(body, {"email"})
        return _success(service.request_deletion_cancellation(body.get("email")), 202)

    def cancel_deletion():
        body = _body()
        _fields(body, {"reference", "code"})
        return _success(service.cancel_account_deletion(body.get("reference"), body.get("code")))

    add("/api/account/capabilities", "account_capabilities", capabilities, ("GET",))
    add("/api/account/email/register", "account_email_register", register_email, ("POST",))
    add("/api/account/email/verify", "account_email_verify", verify_email, ("POST",))
    add("/api/account/email/login/request", "account_email_login_request", email_login_request, ("POST",))
    add("/api/account/email/login/complete", "account_email_login_complete", email_login_complete, ("POST",))
    add("/api/account/password/login", "account_password_login", password_login, ("POST",))
    add("/api/account/password/reset/request", "account_password_reset_request", password_reset_request, ("POST",))
    add("/api/account/password/reset/complete", "account_password_reset_complete", password_reset_complete, ("POST",))
    add("/api/account/login-challenges/complete", "account_login_challenge_complete", complete_login_challenge, ("POST",))
    add("/api/account/oauth/<provider>/start", "account_oauth_start", oauth_start, ("POST",))
    add("/api/account/oauth/<provider>/complete", "account_oauth_complete", oauth_complete, ("POST",))
    add("/api/account/oauth/<provider>/callback", "account_oauth_callback", oauth_callback, ("GET", "POST"))
    add("/api/account/login-methods", "account_login_methods", login_methods, ("GET",))
    add("/api/account/login-methods/external/<identity_id>", "account_unlink_external_identity", unlink_external_identity, ("DELETE",))
    add("/api/account/login-methods/email", "account_unlink_email", unlink_email, ("DELETE",))
    add("/api/account/password/change", "account_password_change", change_password, ("POST",))
    add("/api/account/email/change/request", "account_email_change_request", request_email_change, ("POST",))
    add("/api/account/email/change/complete", "account_email_change_complete", confirm_email_change, ("POST",))
    add("/api/account/sessions", "account_sessions", list_sessions, ("GET",))
    add("/api/account/workspaces", "account_workspaces", list_workspaces, ("GET",))
    add("/api/account/workspaces/<workspace_id>/switch", "account_workspace_switch", switch_workspace, ("POST",))
    add("/api/account/workspaces/<workspace_id>/members", "account_workspace_members", list_workspace_members, ("GET",))
    add("/api/account/workspaces/<workspace_id>/invitations", "account_workspace_invitations", list_workspace_invitations, ("GET",))
    add("/api/account/workspaces/<workspace_id>/invitations", "account_workspace_invite_member", invite_workspace_member, ("POST",))
    add("/api/account/workspaces/<workspace_id>/invitations/<invitation_id>/resend", "account_workspace_invitation_resend", resend_workspace_invitation, ("POST",))
    add("/api/account/workspaces/<workspace_id>/invitations/<invitation_id>", "account_workspace_invitation_revoke", revoke_workspace_invitation, ("DELETE",))
    add("/api/account/invitations/accept", "account_workspace_invitation_accept", accept_workspace_invitation, ("POST",))
    add("/api/account/invitations/preview", "account_workspace_invitation_preview", preview_workspace_invitation, ("POST",))
    add("/api/account/workspaces/<workspace_id>/members/<user_id>", "account_workspace_member_role", update_workspace_member, ("PATCH",))
    add("/api/account/workspaces/<workspace_id>/members/<user_id>", "account_workspace_member_remove", remove_workspace_member, ("DELETE",))
    add("/api/account/workspaces/<workspace_id>/owner-transfer", "account_workspace_owner_transfer", transfer_workspace_owner, ("POST",))
    add("/api/account/workspaces/<workspace_id>/leave", "account_workspace_leave", leave_workspace, ("POST",))
    add("/api/account/preferences", "account_preferences", get_preferences, ("GET",))
    add("/api/account/preferences", "account_preferences_update", update_preferences, ("PATCH",))
    add("/api/account/exports", "account_export_create", create_export, ("POST",))
    add("/api/account/exports/<export_id>/download", "account_export_download", download_export, ("GET",))
    add("/api/account/local-data/preview", "account_local_data_preview", preview_local_data_choice, ("POST",))
    add("/api/account/local-data/choice", "account_local_data_choice", complete_local_data_choice, ("POST",))
    add("/api/account/sessions", "account_revoke_all_sessions", revoke_all_sessions, ("DELETE",))
    add("/api/account/sessions/<session_id>", "account_revoke_session", revoke_session, ("DELETE",))
    add("/api/account/logout", "account_logout", logout, ("POST",))
    add("/api/account/deletion/request", "account_deletion_request", request_deletion, ("POST",))
    add("/api/account/deletion/status", "account_deletion_status", deletion_status, ("GET",))
    add("/api/account/deletion/confirm", "account_deletion_confirm", confirm_deletion, ("POST",))
    add("/api/account/deletion/cancel/request", "account_deletion_cancel_request", request_cancel_deletion, ("POST",))
    add("/api/account/deletion/cancel", "account_deletion_cancel", cancel_deletion, ("POST",))
