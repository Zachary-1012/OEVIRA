"""Bounded, read-only MCP HTTP client for user-approved Skill connections.

The transport pins each request through the existing Acquisition public-source
policy. Remote tool definitions and results remain untrusted; the caller binds
them to the existing Skill and Material owners before using or saving them.
"""
from __future__ import annotations

import base64
import hashlib
import http.client
import json
import re
import socket
import ssl
import time
from urllib.parse import urlsplit
import uuid

import truststore

from .acquisition import AcquisitionError, open_pinned_socket, public_target


MCP_CURRENT_PROTOCOL = "2026-07-28"
MCP_LEGACY_PROTOCOL = "2025-11-25"
MCP_CLIENT_NAME = "OEVIRA-Intelligence"
MCP_CLIENT_VERSION = "1"
MAX_MCP_ENDPOINT_LENGTH = 2048
MAX_MCP_REQUEST_BYTES = 1 * 1024 * 1024
MAX_MCP_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_MCP_TOOL_COUNT = 512
MAX_MCP_PAGE_COUNT = 10
MAX_MCP_SCHEMA_BYTES = 96 * 1024
MAX_MCP_SCHEMA_DEPTH = 20
MAX_MCP_SCHEMA_NODES = 4000
_TOOL_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
_HEADER_TOKEN = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]{1,64}$")
_SENSITIVE_FIELD = re.compile(r"(?:token|secret|password|credential|api.?key|authorization|cookie)", re.IGNORECASE)
_UNSAFE_PATTERN = re.compile(r"[()\\|]")
_SSE_RESPONSE_TYPE = "text/event-stream"
_JSON_RESPONSE_TYPE = "application/json"


class McpClientError(RuntimeError):
    def __init__(self, code: str, status: int = 502, details: dict | None = None):
        super().__init__(code)
        self.code = code
        self.status = status
        self.details = details or {}


def canonical_json(value) -> bytes:
    try:
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise McpClientError("MCP_JSON_VALUE_INVALID", 400) from exc


def sha256(value) -> str:
    if isinstance(value, str):
        value = value.encode("utf-8")
    return hashlib.sha256(value).hexdigest()


def validate_mcp_endpoint(endpoint: str) -> str:
    if not isinstance(endpoint, str) or not endpoint or len(endpoint) > MAX_MCP_ENDPOINT_LENGTH:
        raise McpClientError("MCP_ENDPOINT_INVALID", 400)
    parsed = urlsplit(endpoint)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.fragment or parsed.query or parsed.port not in {None, 443}
            or any(ord(character) < 33 for character in endpoint)):
        raise McpClientError("MCP_ENDPOINT_INVALID", 400)
    try:
        normalized, host, port, _pinned_ip = public_target(endpoint)
    except AcquisitionError as exc:
        raise McpClientError("MCP_ENDPOINT_NOT_ALLOWED", 403, {"policy_code": exc.code}) from exc
    if host.lower() != parsed.hostname.lower() or port != 443:
        raise McpClientError("MCP_ENDPOINT_INVALID", 400)
    normalized_parts = urlsplit(normalized)
    if normalized_parts.query or normalized_parts.fragment or normalized_parts.username or normalized_parts.password:
        raise McpClientError("MCP_ENDPOINT_INVALID", 400)
    return normalized


def _strict_json(raw: bytes):
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result
    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=unique_object,
                          parse_constant=lambda _value: (_ for _ in ()).throw(ValueError("constant")))
    except (UnicodeDecodeError, ValueError, TypeError) as exc:
        raise McpClientError("MCP_JSON_RESPONSE_INVALID", 502) from exc


def _schema_shape(schema, *, depth: int = 0, counter: list[int] | None = None,
                  static_path: tuple[str, ...] = ()) -> list[dict]:
    counter = counter if counter is not None else [0]
    counter[0] += 1
    if depth > MAX_MCP_SCHEMA_DEPTH or counter[0] > MAX_MCP_SCHEMA_NODES:
        raise McpClientError("MCP_TOOL_SCHEMA_TOO_COMPLEX", 409)
    if type(schema) is bool:
        return []
    if not isinstance(schema, dict):
        raise McpClientError("MCP_TOOL_SCHEMA_INVALID", 409)
    if depth == 0 and len(canonical_json(schema)) > MAX_MCP_SCHEMA_BYTES:
        raise McpClientError("MCP_TOOL_SCHEMA_TOO_LARGE", 413)
    unsupported = {
        "$ref", "$dynamicRef", "$recursiveRef", "patternProperties", "unevaluatedProperties",
        "unevaluatedItems", "not", "if", "then", "else", "contains", "dependentSchemas",
    }
    if unsupported.intersection(schema):
        raise McpClientError("MCP_TOOL_SCHEMA_UNSUPPORTED", 409)
    supported_keywords = {
        "$schema", "$id", "title", "description", "default", "examples", "deprecated", "readOnly", "writeOnly",
        "type", "enum", "const", "properties", "required", "additionalProperties", "items", "allOf", "anyOf",
        "oneOf", "minLength", "maxLength", "pattern", "minItems", "maxItems", "minProperties", "maxProperties",
        "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf", "format", "uniqueItems",
        "x-mcp-header",
    }
    if set(schema) - supported_keywords:
        raise McpClientError("MCP_TOOL_SCHEMA_UNSUPPORTED", 409)
    schema_type = schema.get("type")
    known_types = {"object", "array", "string", "integer", "number", "boolean", "null"}
    if schema_type is not None:
        types = schema_type if isinstance(schema_type, list) else [schema_type]
        if not types or any(item not in known_types for item in types):
            raise McpClientError("MCP_TOOL_SCHEMA_INVALID", 409)
    output_headers = []
    annotation = schema.get("x-mcp-header")
    if annotation is not None:
        if (not static_path or not isinstance(annotation, str) or not _HEADER_TOKEN.fullmatch(annotation)
                or _SENSITIVE_FIELD.search(static_path[-1])):
            raise McpClientError("MCP_TOOL_HEADER_ANNOTATION_INVALID", 409)
        output_headers.append({"path": list(static_path), "name": annotation})
    properties = schema.get("properties", {})
    if not isinstance(properties, dict) or any(not isinstance(name, str) or not name for name in properties):
        raise McpClientError("MCP_TOOL_SCHEMA_INVALID", 409)
    required = schema.get("required", [])
    if (not isinstance(required, list) or any(not isinstance(name, str) or not name for name in required)
            or len(set(required)) != len(required)):
        raise McpClientError("MCP_TOOL_SCHEMA_INVALID", 409)
    enum = schema.get("enum")
    if enum is not None and (not isinstance(enum, list) or not enum):
        raise McpClientError("MCP_TOOL_SCHEMA_INVALID", 409)
    additional = schema.get("additionalProperties", True)
    if type(additional) is not bool and not isinstance(additional, dict):
        raise McpClientError("MCP_TOOL_SCHEMA_INVALID", 409)
    for name, child in properties.items():
        output_headers.extend(_schema_shape(child, depth=depth + 1, counter=counter,
                                            static_path=(*static_path, name)))
    items = schema.get("items")
    if isinstance(items, dict):
        output_headers.extend(_schema_shape(items, depth=depth + 1, counter=counter, static_path=()))
    elif items is not None:
        raise McpClientError("MCP_TOOL_SCHEMA_INVALID", 409)
    if isinstance(additional, dict):
        output_headers.extend(_schema_shape(additional, depth=depth + 1, counter=counter, static_path=()))
    for keyword in ("allOf", "anyOf", "oneOf"):
        children = schema.get(keyword)
        if children is not None:
            if not isinstance(children, list) or not children:
                raise McpClientError("MCP_TOOL_SCHEMA_INVALID", 409)
            for child in children:
                output_headers.extend(_schema_shape(child, depth=depth + 1, counter=counter, static_path=()))
    pattern = schema.get("pattern")
    if pattern is not None:
        if (not isinstance(pattern, str) or len(pattern) > 128 or _UNSAFE_PATTERN.search(pattern)
                or len(re.findall(r"[*+?{]", pattern)) > 1):
            raise McpClientError("MCP_TOOL_SCHEMA_INVALID", 409)
        try:
            re.compile(pattern)
        except re.error as exc:
            raise McpClientError("MCP_TOOL_SCHEMA_INVALID", 409) from exc
    for keyword in ("minLength", "maxLength", "minItems", "maxItems", "minProperties", "maxProperties"):
        value = schema.get(keyword)
        if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
            raise McpClientError("MCP_TOOL_SCHEMA_INVALID", 409)
    for keyword in ("minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf"):
        value = schema.get(keyword)
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))):
            raise McpClientError("MCP_TOOL_SCHEMA_INVALID", 409)
        if keyword == "multipleOf" and value is not None and value <= 0:
            raise McpClientError("MCP_TOOL_SCHEMA_INVALID", 409)
    if "uniqueItems" in schema and type(schema["uniqueItems"]) is not bool:
        raise McpClientError("MCP_TOOL_SCHEMA_INVALID", 409)
    return output_headers


def _matches_type(value, schema_type: str) -> bool:
    if schema_type == "null":
        return value is None
    if schema_type == "object":
        return isinstance(value, dict)
    if schema_type == "array":
        return isinstance(value, list)
    if schema_type == "string":
        return isinstance(value, str)
    if schema_type == "boolean":
        return type(value) is bool
    if schema_type == "integer":
        return type(value) is int
    if schema_type == "number":
        return type(value) in {int, float} and not isinstance(value, bool)
    return False


def validate_schema_instance(schema: dict, instance, *, label: str = "MCP tool arguments") -> None:
    _schema_shape(schema)

    def validate(node: dict, value, path: str, depth: int) -> None:
        if depth > MAX_MCP_SCHEMA_DEPTH:
            raise McpClientError("MCP_TOOL_VALUE_TOO_DEEP", 400)
        if type(node) is bool:
            if not node:
                raise McpClientError("MCP_TOOL_ARGUMENT_SCHEMA_MISMATCH", 400, {"path": path})
            return
        schema_type = node.get("type")
        if schema_type is not None:
            valid_types = schema_type if isinstance(schema_type, list) else [schema_type]
            if not any(_matches_type(value, selected) for selected in valid_types):
                raise McpClientError("MCP_TOOL_ARGUMENT_SCHEMA_MISMATCH", 400, {"path": path})
        if "const" in node and value != node["const"]:
            raise McpClientError("MCP_TOOL_ARGUMENT_SCHEMA_MISMATCH", 400, {"path": path})
        if isinstance(node.get("enum"), list) and value not in node["enum"]:
            raise McpClientError("MCP_TOOL_ARGUMENT_SCHEMA_MISMATCH", 400, {"path": path})
        for keyword, comparison in (("minimum", lambda a, b: a >= b), ("maximum", lambda a, b: a <= b),
                                    ("exclusiveMinimum", lambda a, b: a > b), ("exclusiveMaximum", lambda a, b: a < b)):
            boundary = node.get(keyword)
            if boundary is not None and type(value) in {int, float} and not isinstance(value, bool):
                if not isinstance(boundary, (int, float)) or isinstance(boundary, bool) or not comparison(value, boundary):
                    raise McpClientError("MCP_TOOL_ARGUMENT_SCHEMA_MISMATCH", 400, {"path": path})
        if isinstance(value, str):
            if len(value) < node.get("minLength", 0) or len(value) > node.get("maxLength", 1_000_000):
                raise McpClientError("MCP_TOOL_ARGUMENT_SCHEMA_MISMATCH", 400, {"path": path})
            if isinstance(node.get("pattern"), str) and re.search(node["pattern"], value) is None:
                raise McpClientError("MCP_TOOL_ARGUMENT_SCHEMA_MISMATCH", 400, {"path": path})
        if isinstance(value, list):
            if len(value) < node.get("minItems", 0) or len(value) > node.get("maxItems", 100_000):
                raise McpClientError("MCP_TOOL_ARGUMENT_SCHEMA_MISMATCH", 400, {"path": path})
            if isinstance(node.get("items"), dict):
                for index, child in enumerate(value):
                    validate(node["items"], child, f"{path}[{index}]", depth + 1)
            elif node.get("items") is False:
                raise McpClientError("MCP_TOOL_ARGUMENT_SCHEMA_MISMATCH", 400, {"path": path})
            if node.get("uniqueItems") is True:
                try:
                    canonical_items = [canonical_json(item) for item in value]
                except McpClientError:
                    raise
                if len(set(canonical_items)) != len(canonical_items):
                    raise McpClientError("MCP_TOOL_ARGUMENT_SCHEMA_MISMATCH", 400, {"path": path})
        if isinstance(value, dict):
            if (len(value) < node.get("minProperties", 0)
                    or len(value) > node.get("maxProperties", 100_000)):
                raise McpClientError("MCP_TOOL_ARGUMENT_SCHEMA_MISMATCH", 400, {"path": path})
            required = node.get("required", [])
            properties = node.get("properties", {})
            if not isinstance(required, list) or any(key not in value for key in required):
                raise McpClientError("MCP_TOOL_ARGUMENT_SCHEMA_MISMATCH", 400, {"path": path})
            if not isinstance(properties, dict):
                raise McpClientError("MCP_TOOL_SCHEMA_INVALID", 409)
            for key, child in value.items():
                child_schema = properties.get(key)
                if child_schema is None:
                    additional = node.get("additionalProperties", True)
                    if additional is False:
                        raise McpClientError("MCP_TOOL_ARGUMENT_SCHEMA_MISMATCH", 400, {"path": f"{path}.{key}"})
                    if isinstance(additional, dict):
                        validate(additional, child, f"{path}.{key}", depth + 1)
                elif isinstance(child_schema, dict) or type(child_schema) is bool:
                    validate(child_schema, child, f"{path}.{key}", depth + 1)
        for keyword in ("allOf", "anyOf", "oneOf"):
            branches = node.get(keyword)
            if branches is None:
                continue
            successes = 0
            for branch in branches:
                try:
                    validate(branch, value, path, depth + 1)
                    successes += 1
                except McpClientError:
                    pass
            if (keyword == "allOf" and successes != len(branches)
                    or keyword == "anyOf" and successes == 0
                    or keyword == "oneOf" and successes != 1):
                raise McpClientError("MCP_TOOL_ARGUMENT_SCHEMA_MISMATCH", 400, {"path": path})

    validate(schema, instance, "$", 0)


def validate_schema_definition(schema: dict) -> None:
    _schema_shape(schema)


def _encode_http_value(value) -> str:
    if type(value) is bool:
        raw = "true" if value else "false"
    elif type(value) is int:
        if abs(value) > 9007199254740991:
            raise McpClientError("MCP_TOOL_HEADER_VALUE_INVALID", 400)
        raw = str(value)
    elif isinstance(value, str):
        raw = value
    else:
        raise McpClientError("MCP_TOOL_HEADER_VALUE_INVALID", 400)
    unsafe = (not raw.isascii() or any(ord(char) < 32 and char != "\t" or ord(char) == 127 for char in raw)
              or raw != raw.strip() or raw.startswith("=?base64?") and raw.endswith("?="))
    return f"=?base64?{base64.b64encode(raw.encode('utf-8')).decode('ascii')}?=" if unsafe else raw


def _header_parameters(tool: dict, arguments: dict) -> dict[str, str]:
    schema = tool.get("inputSchema")
    annotations = _schema_shape(schema)
    header_names = [item["name"].lower() for item in annotations]
    if len(header_names) != len(set(header_names)):
        raise McpClientError("MCP_TOOL_HEADER_ANNOTATION_INVALID", 409)
    headers = {}
    for item in annotations:
        value = arguments
        for segment in item["path"]:
            if not isinstance(value, dict) or segment not in value:
                value = None
                break
            value = value[segment]
        if value is not None:
            headers[f"Mcp-Param-{item['name']}"] = _encode_http_value(value)
    return headers


def _event_stream_response(raw: bytes, expected_id) -> dict:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise McpClientError("MCP_SSE_RESPONSE_INVALID", 502) from exc
    if len(raw) > MAX_MCP_RESPONSE_BYTES:
        raise McpClientError("MCP_RESPONSE_TOO_LARGE", 413)
    data_lines = []
    responses = []
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if line == "":
            if data_lines:
                payload = _strict_json("\n".join(data_lines).encode("utf-8"))
                if isinstance(payload, dict) and payload.get("id") == expected_id:
                    responses.append(payload)
                data_lines = []
            continue
        if line.startswith(":"):
            continue
        field, _, value = line.partition(":")
        if value.startswith(" "):
            value = value[1:]
        if field == "data":
            data_lines.append(value)
    if data_lines:
        payload = _strict_json("\n".join(data_lines).encode("utf-8"))
        if isinstance(payload, dict) and payload.get("id") == expected_id:
            responses.append(payload)
    if len(responses) != 1:
        raise McpClientError("MCP_SSE_RESPONSE_MISSING", 502)
    return responses[0]


class McpHttpClient:
    """One user-authorized HTTPS MCP endpoint; no redirects or local processes."""

    def __init__(self, endpoint: str, *, access_token: str | None = None, transport=None):
        self.endpoint = validate_mcp_endpoint(endpoint)
        if access_token is not None and (
            not isinstance(access_token, str) or not access_token or len(access_token) > 8192
            or not access_token.isascii()
            or any(ord(character) < 33 or ord(character) == 127 for character in access_token)
        ):
            raise McpClientError("MCP_CREDENTIAL_INVALID", 400)
        self.access_token = access_token
        self._transport = transport
        self.protocol_version: str | None = None
        self.server_info: dict = {}
        self.server_capabilities: dict = {}
        self.instructions: str | None = None
        self._legacy_session_id: str | None = None
        self._next_id = 0

    def _next_request_id(self) -> str:
        self._next_id += 1
        return f"oevira-{self._next_id}-{uuid.uuid4().hex[:8]}"

    def _post(self, request: dict, *, protocol_version: str, method: str,
              name: str | None = None, extra_headers: dict[str, str] | None = None,
              timeout: float = 30.0, is_cancelled=None) -> tuple[int, dict[str, str], bytes]:
        raw = canonical_json(request)
        if len(raw) > MAX_MCP_REQUEST_BYTES:
            raise McpClientError("MCP_REQUEST_TOO_LARGE", 413)
        parsed = urlsplit(self.endpoint)
        path = parsed.path or "/"
        headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "User-Agent": f"{MCP_CLIENT_NAME}/{MCP_CLIENT_VERSION}",
        }
        if protocol_version == MCP_CURRENT_PROTOCOL:
            headers["MCP-Protocol-Version"] = protocol_version
            headers["Mcp-Method"] = method
            if name is not None:
                headers["Mcp-Name"] = _encode_http_value(name)
        elif protocol_version == MCP_LEGACY_PROTOCOL:
            if self._legacy_session_id:
                headers["Mcp-Session-Id"] = self._legacy_session_id
                headers["MCP-Protocol-Version"] = protocol_version
        else:
            raise McpClientError("MCP_PROTOCOL_VERSION_UNSUPPORTED", 409)
        for header_name, header_value in (extra_headers or {}).items():
            if (not isinstance(header_name, str) or not _HEADER_TOKEN.fullmatch(header_name)
                    or header_name.lower() in {key.lower() for key in headers}
                    or not isinstance(header_value, str)
                    or any(ord(character) < 32 and character != "\t" or ord(character) == 127 for character in header_value)):
                raise McpClientError("MCP_REQUEST_HEADER_INVALID", 400)
            headers[header_name] = header_value
        if self.access_token is not None:
            headers["Authorization"] = f"Bearer {self.access_token}"
        if self._transport is not None:
            if is_cancelled is not None and is_cancelled():
                raise McpClientError("MCP_REQUEST_CANCELLED", 409)
            return self._transport(self.endpoint, headers, raw, timeout)
        try:
            normalized, host, port, pinned_ip = public_target(self.endpoint)
            if normalized != self.endpoint:
                raise McpClientError("MCP_ENDPOINT_CHANGED", 409)
            upstream = open_pinned_socket(pinned_ip, port, timeout, honor_local_proxy=True)
            context = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            context.set_alpn_protocols(["http/1.1"])
            tls = context.wrap_socket(upstream, server_hostname=host)
            try:
                request_lines = [f"POST {path} HTTP/1.1\r\n", f"Host: {host}\r\n"]
                request_lines.extend(f"{key}: {value}\r\n" for key, value in headers.items())
                request_lines.extend(("Accept-Encoding: identity\r\n", "Connection: close\r\n", "\r\n"))
                tls.sendall("".join(request_lines).encode("ascii") + raw)
                response = http.client.HTTPResponse(tls)
                tls.settimeout(min(0.5, timeout))
                response.begin()
                status = response.status
                response_headers = {key.lower(): value for key, value in response.getheaders()}
                length = response_headers.get("content-length")
                if length is not None and length.isdigit() and int(length) > MAX_MCP_RESPONSE_BYTES:
                    raise McpClientError("MCP_RESPONSE_TOO_LARGE", 413)
                started = time.monotonic()
                chunks = []
                size = 0
                while True:
                    if is_cancelled is not None and is_cancelled():
                        raise McpClientError("MCP_REQUEST_CANCELLED", 409)
                    remaining = timeout - (time.monotonic() - started)
                    if remaining <= 0:
                        raise McpClientError("MCP_SERVER_TIMEOUT", 504)
                    tls.settimeout(min(0.5, remaining))
                    try:
                        chunk = response.read1(min(64 * 1024, MAX_MCP_RESPONSE_BYTES + 1 - size))
                    except socket.timeout:
                        continue
                    if not chunk:
                        break
                    chunks.append(chunk)
                    size += len(chunk)
                    if size > MAX_MCP_RESPONSE_BYTES:
                        raise McpClientError("MCP_RESPONSE_TOO_LARGE", 413)
                body = b"".join(chunks)
            finally:
                tls.close()
        except McpClientError:
            raise
        except AcquisitionError as exc:
            raise McpClientError("MCP_ENDPOINT_NOT_ALLOWED", 403, {"policy_code": exc.code}) from exc
        except (OSError, TimeoutError, socket.timeout, http.client.HTTPException, ssl.SSLError) as exc:
            raise McpClientError("MCP_SERVER_UNAVAILABLE", 503) from exc
        if len(body) > MAX_MCP_RESPONSE_BYTES:
            raise McpClientError("MCP_RESPONSE_TOO_LARGE", 413)
        return status, response_headers, body

    def _rpc(self, method: str, params: dict, *, protocol_version: str,
             name: str | None = None, timeout: float = 30.0,
             extra_headers: dict[str, str] | None = None, is_cancelled=None) -> dict:
        is_notification = method.startswith("notifications/")
        request_id = None if is_notification else self._next_request_id()
        if protocol_version == MCP_CURRENT_PROTOCOL:
            if is_notification:
                raise McpClientError("MCP_MODERN_NOTIFICATION_UNSUPPORTED", 409)
            request = {
                "jsonrpc": "2.0", "id": request_id, "method": method,
                "params": params | {"_meta": {
                    "io.modelcontextprotocol/protocolVersion": protocol_version,
                    "io.modelcontextprotocol/clientInfo": {"name": MCP_CLIENT_NAME, "version": MCP_CLIENT_VERSION},
                    "io.modelcontextprotocol/clientCapabilities": {},
                }},
            }
        elif method == "initialize":
            request = {
                "jsonrpc": "2.0", "id": request_id, "method": "initialize",
                "params": {
                    "protocolVersion": protocol_version, "capabilities": {},
                    "clientInfo": {"name": MCP_CLIENT_NAME, "version": MCP_CLIENT_VERSION},
                },
            }
        elif is_notification:
            request = {"jsonrpc": "2.0", "method": method, "params": params}
        else:
            request = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        status, headers, body = self._post(
            request, protocol_version=protocol_version, method=method,
            name=name, extra_headers=extra_headers, timeout=timeout, is_cancelled=is_cancelled,
        )
        if is_notification and status == 202 and not body:
            return {"jsonrpc": "2.0", "result": {}}
        if status < 200 or status >= 300:
            remote = None
            try:
                remote = _strict_json(body) if body else None
            except McpClientError:
                pass
            if (isinstance(remote, dict) and remote.get("jsonrpc") == "2.0"
                    and (remote.get("id") == request_id or is_notification and "id" not in remote)
                    and isinstance(remote.get("error"), dict)):
                error = remote["error"]
                code = error.get("code")
                details = {"http_status": status}
                if type(code) is int:
                    details["protocol_error_code"] = code
                data = error.get("data") if isinstance(error.get("data"), dict) else {}
                supported = data.get("supported")
                if isinstance(supported, list) and all(isinstance(item, str) for item in supported):
                    details["supported_protocol_versions"] = supported[:16]
                raise McpClientError("MCP_REMOTE_REQUEST_REJECTED", 409 if status in {400, 404, 405} else 502, details)
            raise McpClientError("MCP_HTTP_REQUEST_REJECTED", 409 if status in {400, 404, 405} else 502,
                                 {"http_status": status})
        if not body:
            raise McpClientError("MCP_RESPONSE_EMPTY", 502)
        content_type = headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if content_type == _JSON_RESPONSE_TYPE:
            response = _strict_json(body)
        elif content_type == _SSE_RESPONSE_TYPE:
            response = _event_stream_response(body, request_id)
        else:
            raise McpClientError("MCP_RESPONSE_CONTENT_TYPE_INVALID", 502)
        if (not isinstance(response, dict) or response.get("jsonrpc") != "2.0"
                or response.get("id") != request_id):
            raise McpClientError("MCP_JSONRPC_RESPONSE_INVALID", 502)
        if "error" in response:
            error = response.get("error")
            details = {"protocol_error_code": error["code"]} if isinstance(error, dict) and type(error.get("code")) is int else {}
            raise McpClientError("MCP_REMOTE_REQUEST_REJECTED", 409, details)
        if not isinstance(response.get("result"), dict):
            raise McpClientError("MCP_JSONRPC_RESULT_INVALID", 502)
        session_id = headers.get("mcp-session-id")
        if session_id is not None:
            if not session_id or len(session_id) > 512 or any(ord(char) < 33 or ord(char) > 126 for char in session_id):
                raise McpClientError("MCP_SESSION_ID_INVALID", 502)
            self._legacy_session_id = session_id
        return response

    @staticmethod
    def _legacy_fallback_allowed(error: McpClientError) -> bool:
        return error.code == "MCP_HTTP_REQUEST_REJECTED" or (
            error.code == "MCP_REMOTE_REQUEST_REJECTED"
            and (error.details.get("protocol_error_code") == -32601
                 or (error.details.get("protocol_error_code") == -32022
                     and MCP_LEGACY_PROTOCOL in error.details.get("supported_protocol_versions", [])))
        )

    def _discover_legacy(self, *, timeout: float) -> dict:
        self._legacy_session_id = None
        response = self._rpc(
            "initialize", {}, protocol_version=MCP_LEGACY_PROTOCOL, timeout=timeout,
        )
        result = response["result"]
        negotiated = result.get("protocolVersion")
        if negotiated != MCP_LEGACY_PROTOCOL or not isinstance(result.get("capabilities"), dict):
            raise McpClientError("MCP_DISCOVERY_RESULT_INVALID", 502)
        self._rpc("notifications/initialized", {}, protocol_version=MCP_LEGACY_PROTOCOL, timeout=timeout)
        self.protocol_version = negotiated
        self.server_info = _safe_server_info(result.get("serverInfo"))
        self.server_capabilities = result["capabilities"]
        self.instructions = _safe_instructions(result.get("instructions"))
        return {"protocol_version": negotiated, "supported_versions": [negotiated],
                "server_info": self.server_info, "capabilities": self.server_capabilities,
                "instructions": self.instructions, "state": "CONNECTED"}

    def discover(self, *, timeout: float = 30.0) -> dict:
        try:
            response = self._rpc("server/discover", {}, protocol_version=MCP_CURRENT_PROTOCOL, timeout=timeout)
            result = response["result"]
            supported = result.get("supportedVersions")
            if not isinstance(supported, list) or not all(isinstance(item, str) for item in supported):
                raise McpClientError("MCP_DISCOVERY_RESULT_INVALID", 502)
            if MCP_CURRENT_PROTOCOL not in supported:
                if MCP_LEGACY_PROTOCOL in supported:
                    return self._discover_legacy(timeout=timeout)
                raise McpClientError("MCP_PROTOCOL_VERSION_UNSUPPORTED", 409,
                                     {"supported_protocol_versions": supported[:16]})
            if not isinstance(result.get("capabilities"), dict):
                raise McpClientError("MCP_DISCOVERY_RESULT_INVALID", 502)
            if result.get("resultType") != "complete":
                raise McpClientError("MCP_DISCOVERY_RESULT_PARTIAL", 409)
            metadata = result.get("_meta") if isinstance(result.get("_meta"), dict) else {}
            server_info = metadata.get("io.modelcontextprotocol/serverInfo")
            self.protocol_version = MCP_CURRENT_PROTOCOL
            self.server_info = _safe_server_info(server_info)
            self.server_capabilities = result["capabilities"]
            self.instructions = _safe_instructions(result.get("instructions"))
            return {"protocol_version": self.protocol_version, "supported_versions": supported,
                    "server_info": self.server_info, "capabilities": self.server_capabilities,
                    "instructions": self.instructions, "state": "CONNECTED"}
        except McpClientError as exc:
            if not self._legacy_fallback_allowed(exc):
                raise
        return self._discover_legacy(timeout=timeout)

    def list_tools(self, *, timeout: float = 30.0) -> dict:
        if self.protocol_version is None:
            self.discover(timeout=timeout)
        if not isinstance(self.server_capabilities.get("tools"), dict):
            return {"tools": [], "next_page": None, "state": "TOOLS_UNAVAILABLE"}
        tools = []
        tool_names = set()
        cursor = None
        partial = False
        for _page in range(MAX_MCP_PAGE_COUNT):
            params = {"cursor": cursor} if cursor is not None else {}
            response = self._rpc("tools/list", params, protocol_version=self.protocol_version, timeout=timeout)
            result = response["result"]
            raw_tools = result.get("tools")
            result_type = result.get("resultType", "complete") if self.protocol_version == MCP_LEGACY_PROTOCOL else result.get("resultType")
            if result_type not in {"complete", "partial"} or not isinstance(raw_tools, list):
                raise McpClientError("MCP_TOOL_LIST_INVALID", 502)
            partial = partial or result_type == "partial"
            if len(tools) + len(raw_tools) > MAX_MCP_TOOL_COUNT:
                raise McpClientError("MCP_TOOL_LIST_TOO_LARGE", 413)
            for tool in raw_tools:
                if not isinstance(tool, dict):
                    continue
                name = tool.get("name")
                schema = tool.get("inputSchema")
                if not isinstance(name, str) or not _TOOL_NAME.fullmatch(name) or not isinstance(schema, dict):
                    continue
                if name in tool_names:
                    raise McpClientError("MCP_TOOL_NAME_DUPLICATE", 409)
                tool_names.add(name)
                try:
                    _schema_shape(schema)
                    if "outputSchema" in tool:
                        _schema_shape(tool["outputSchema"])
                    header_parameters = _header_parameters(tool, {})
                except McpClientError:
                    # A malformed extension tool remains visible to its owner as
                    # rejected metadata, but cannot be installed or invoked.
                    tools.append({"name": name, "state": "REJECTED_SCHEMA", "schema_sha256": None})
                    continue
                definition = {key: tool[key] for key in ("name", "title", "description", "inputSchema", "outputSchema")
                              if key in tool}
                definition["annotations"] = _safe_annotations(tool.get("annotations"))
                full = {
                    "name": name, "title": _bounded_text(tool.get("title"), 200),
                    "description": _bounded_text(tool.get("description"), 4000),
                    "inputSchema": schema, "outputSchema": tool.get("outputSchema"),
                    "annotations": _safe_annotations(tool.get("annotations")),
                    "definition": definition,
                    "schema_sha256": sha256(canonical_json(definition)),
                    "header_parameters": header_parameters,
                    "state": "REVIEW_REQUIRED",
                }
                tools.append(full)
            cursor = result.get("nextCursor")
            if cursor is None:
                return {"tools": tools, "next_page": None, "state": "PARTIAL" if partial else "COMPLETE"}
            if not isinstance(cursor, str) or not cursor or len(cursor) > 2048:
                raise McpClientError("MCP_TOOL_CURSOR_INVALID", 502)
        return {"tools": tools, "next_page": cursor, "state": "PARTIAL"}

    def call_tool(self, tool: dict, arguments: dict, *, approved: bool,
                  effect: str, timeout: float = 30.0, is_cancelled=None) -> dict:
        if approved is not True:
            raise McpClientError("MCP_TOOL_PERMISSION_REQUIRED", 403)
        if effect not in {"read", "external_effect"}:
            raise McpClientError("MCP_TOOL_EFFECT_INVALID", 400)
        if not isinstance(tool, dict) or not _TOOL_NAME.fullmatch(str(tool.get("name") or "")):
            raise McpClientError("MCP_TOOL_NOT_REVIEWED", 403)
        if not isinstance(arguments, dict):
            raise McpClientError("MCP_TOOL_ARGUMENTS_INVALID", 400)
        if not 0 < timeout <= 60:
            raise McpClientError("MCP_TIMEOUT_INVALID", 400)
        if is_cancelled is not None and not callable(is_cancelled):
            raise McpClientError("MCP_CANCEL_CHECK_INVALID", 400)
        if self.protocol_version is None:
            self.discover(timeout=timeout)
        fresh_list = self.list_tools(timeout=timeout)
        if fresh_list["state"] != "COMPLETE":
            raise McpClientError("MCP_TOOL_LIST_PARTIAL", 409)
        current = next((item for item in fresh_list["tools"] if item.get("name") == tool["name"]), None)
        if (current is None or current.get("state") != "REVIEW_REQUIRED"
                or current.get("schema_sha256") != tool.get("schema_sha256")):
            raise McpClientError("MCP_TOOL_SCHEMA_CHANGED", 409)
        validate_schema_instance(current["inputSchema"], arguments)
        headers = _header_parameters(current, arguments)
        params = {"name": current["name"], "arguments": arguments}
        if is_cancelled is not None and is_cancelled():
            raise McpClientError("MCP_TOOL_CANCELLED", 409)
        try:
            response = self._rpc(
                "tools/call", params, protocol_version=self.protocol_version,
                name=current["name"], extra_headers=headers, timeout=timeout,
                is_cancelled=is_cancelled,
            )
        except McpClientError as exc:
            if exc.code in {"MCP_REQUEST_CANCELLED", "MCP_SERVER_TIMEOUT", "MCP_SERVER_UNAVAILABLE",
                            "MCP_HTTP_REQUEST_REJECTED", "MCP_REMOTE_REQUEST_REJECTED"}:
                raise McpClientError(
                    "MCP_TOOL_OUTCOME_UNKNOWN", 409,
                    {"cause": exc.code, "effect": effect, "automatic_retry": False},
                ) from exc
            raise
        result = response["result"]
        if result.get("resultType", "complete") == "input_required":
            raise McpClientError("MCP_TOOL_INPUT_REQUIRED", 409)
        result_type = result.get("resultType", "complete") if self.protocol_version == MCP_LEGACY_PROTOCOL else result.get("resultType")
        if result_type != "complete" or result.get("isError") is True:
            raise McpClientError("MCP_TOOL_RESULT_FAILED", 502)
        structured = result.get("structuredContent")
        if "outputSchema" in current and "structuredContent" in result:
            validate_schema_instance(current["outputSchema"], structured, label="MCP structured result")
        content = result.get("content", [])
        if not isinstance(content, list) or len(content) > 256:
            raise McpClientError("MCP_TOOL_RESULT_INVALID", 502)
        text_items = []
        for item in content:
            if not isinstance(item, dict):
                raise McpClientError("MCP_TOOL_RESULT_INVALID", 502)
            if item.get("type") != "text" or not isinstance(item.get("text"), str):
                raise McpClientError("MCP_TOOL_RESULT_CONTENT_UNSUPPORTED", 409)
            if len(item["text"].encode("utf-8")) > MAX_MCP_RESPONSE_BYTES:
                raise McpClientError("MCP_RESPONSE_TOO_LARGE", 413)
            text_items.append(item["text"])
        safe_result = {
            "protocol_version": self.protocol_version,
            "server_info": self.server_info,
            "tool_name": current["name"],
            "tool_schema_sha256": current["schema_sha256"],
            "arguments_sha256": sha256(canonical_json(arguments)),
            "content": text_items,
            "structured_content": structured,
            "trust": "UNTRUSTED_EXTERNAL_MCP_RESULT",
        }
        if len(canonical_json(safe_result)) > MAX_MCP_RESPONSE_BYTES:
            raise McpClientError("MCP_RESPONSE_TOO_LARGE", 413)
        return safe_result


def _safe_server_info(value) -> dict:
    if not isinstance(value, dict):
        return {}
    return {
        "name": _bounded_text(value.get("name"), 120),
        "version": _bounded_text(value.get("version"), 80),
    }


def _safe_instructions(value) -> str | None:
    return _bounded_text(value, 4000)


def _bounded_text(value, maximum: int) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = "".join(character for character in value if character.isprintable() or character in "\n\t")
    return cleaned[:maximum]


def _safe_annotations(value) -> dict:
    if not isinstance(value, dict):
        return {}
    return {key: item for key, item in value.items()
            if key in {"readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint"}
            and type(item) is bool}
