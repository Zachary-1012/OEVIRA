"""Per-context loopback proxy that pins browser sockets to vetted IPs."""
from __future__ import annotations

import base64
import hmac
import ipaddress
import select
import secrets
import socket
import socketserver
import threading
from typing import Callable
from urllib.parse import urlsplit, urlunsplit

from .acquisition import AcquisitionError, open_pinned_socket, public_target


_MAX_HEADER_BYTES = 64 * 1024
_CONNECT_TIMEOUT_SECONDS = 10
_SOCKET_POLL_SECONDS = 1


class _ThreadingProxyServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = False
    daemon_threads = True
    request_queue_size = 32


class PinnedBrowserProxy:
    """Route one browser context through a loopback proxy with DNS pinning.

    ``public_target`` remains the source of URL/port/address policy. Its
    returned numeric address is used directly for the socket connection, so
    the browser cannot resolve the hostname again between policy validation
    and connect.
    """

    def __init__(self, target_resolver: Callable[[str], tuple[str, str, int, str]] = public_target):
        self._target_resolver = target_resolver
        self._username = "oevira-browser"
        self._password = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii").rstrip("=")
        credential = f"{self._username}:{self._password}".encode("ascii")
        encoded_credential = base64.b64encode(credential).decode("ascii")
        self._expected_authorization = f"Basic {encoded_credential}"
        self._closed = False
        self._lock = threading.Lock()
        self._active_connections: set[socket.socket] = set()
        self._blocked_target_count = 0

        owner = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self) -> None:
                owner._handle(self.request)

        self._server = _ThreadingProxyServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            kwargs={"poll_interval": 0.1},
            name="oevira-browser-pinned-proxy",
            daemon=True,
        )
        self._thread.start()

    @property
    def playwright_settings(self) -> dict[str, str]:
        host, port = self._server.server_address[:2]
        return {
            "server": f"http://{host}:{port}",
            "username": self._username,
            "password": self._password,
        }

    @property
    def blocked_target_count(self) -> int:
        with self._lock:
            return self._blocked_target_count

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)
        with self._lock:
            active = list(self._active_connections)
        for connection in active:
            self._close_socket(connection)

    @staticmethod
    def _close_socket(connection: socket.socket) -> None:
        try:
            connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            connection.close()
        except OSError:
            pass

    def _handle(self, client: socket.socket) -> None:
        upstream: socket.socket | None = None
        with self._lock:
            if self._closed:
                self._close_socket(client)
                return
            self._active_connections.add(client)
        try:
            client.settimeout(_CONNECT_TIMEOUT_SECONDS)
            message = self._read_message(client)
            if message is None:
                return
            request_line, headers, initial_body = message
            if not self._authorized(headers):
                self._respond(client, 407, "Proxy Authentication Required", (
                    ("Proxy-Authenticate", 'Basic realm="OEVIRA browser"'),
                ))
                return

            try:
                method, target, version = self._parse_request_line(request_line)
                if method == "CONNECT":
                    target_url = self._connect_target_url(target)
                    upstream = self._connect_pinned(target_url)
                    with self._lock:
                        if self._closed:
                            self._close_socket(upstream)
                            upstream = None
                            return
                        self._active_connections.add(upstream)
                    client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                    if initial_body:
                        upstream.sendall(initial_body)
                    self._relay(client, upstream)
                    return
                self._forward_http(client, method, target, version, headers, initial_body)
            except AcquisitionError:
                with self._lock:
                    self._blocked_target_count += 1
                self._respond(client, 403, "Forbidden")
            except (UnicodeError, ValueError):
                self._respond(client, 400, "Bad Request")
            except OSError:
                self._respond(client, 502, "Bad Gateway")
        except (OSError, ValueError):
            return
        finally:
            if upstream is not None:
                self._close_socket(upstream)
                with self._lock:
                    self._active_connections.discard(upstream)
            with self._lock:
                self._active_connections.discard(client)
            self._close_socket(client)

    def _read_message(self, client: socket.socket) -> tuple[bytes, list[tuple[bytes, bytes]], bytes] | None:
        data = bytearray()
        boundary = -1
        while boundary < 0:
            chunk = client.recv(min(8192, _MAX_HEADER_BYTES + 1 - len(data)))
            if not chunk:
                return None
            data.extend(chunk)
            if len(data) > _MAX_HEADER_BYTES:
                raise ValueError("Proxy request headers exceed the limit.")
            boundary = data.find(b"\r\n\r\n")
        lines = bytes(data[:boundary]).split(b"\r\n")
        if not lines or not lines[0]:
            raise ValueError("Proxy request line is missing.")
        headers: list[tuple[bytes, bytes]] = []
        for line in lines[1:]:
            name, separator, value = line.partition(b":")
            if not separator or not name.strip():
                raise ValueError("Proxy request header is malformed.")
            headers.append((name.strip(), value.strip()))
        return lines[0], headers, bytes(data[boundary + 4:])

    def _authorized(self, headers: list[tuple[bytes, bytes]]) -> bool:
        values = [
            value.decode("ascii", errors="ignore").strip()
            for name, value in headers
            if name.lower() == b"proxy-authorization"
        ]
        return len(values) == 1 and hmac.compare_digest(values[0], self._expected_authorization)

    @staticmethod
    def _parse_request_line(request_line: bytes) -> tuple[str, str, str]:
        pieces = request_line.split(b" ", 2)
        if len(pieces) != 3:
            raise ValueError("Proxy request line is malformed.")
        method = pieces[0].decode("ascii").upper()
        target = pieces[1].decode("ascii")
        version = pieces[2].decode("ascii")
        if version not in {"HTTP/1.0", "HTTP/1.1"}:
            raise ValueError("Proxy HTTP version is unsupported.")
        return method, target, version

    @staticmethod
    def _connect_target_url(authority: str) -> str:
        if not authority or any(char in authority for char in "/?#@"):
            raise ValueError("CONNECT authority is malformed.")
        parsed = urlsplit("https://" + authority)
        if not parsed.hostname or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment:
            raise ValueError("CONNECT authority is malformed.")
        return urlunsplit(("https", parsed.netloc, "/", "", ""))

    def _forward_http(
        self,
        client: socket.socket,
        method: str,
        target: str,
        version: str,
        headers: list[tuple[bytes, bytes]],
        initial_body: bytes,
    ) -> None:
        parsed = urlsplit(target)
        scheme = parsed.scheme.lower()
        if scheme not in {"http", "ws"} or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
            raise ValueError("Only HTTP and WebSocket proxy requests are supported.")
        policy_scheme = "http" if scheme == "ws" else scheme
        policy_url = urlunsplit((policy_scheme, parsed.netloc, parsed.path or "/", parsed.query, ""))
        _normalized, host, port, ip = self._target_resolver(policy_url)
        self._check_host_header(headers, host, port, policy_scheme)
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query
        has_upgrade = any(name.lower() == b"upgrade" for name, _ in headers)
        connection_upgrade = any(
            name.lower() == b"connection" and b"upgrade" in {part.strip().lower() for part in value.split(b",")}
            for name, value in headers
        )
        if has_upgrade != connection_upgrade:
            raise ValueError("HTTP Upgrade headers are inconsistent.")

        request_line = f"{method} {path} {version}".encode("ascii")
        output_headers: list[bytes] = []
        has_host = False
        for name, value in headers:
            lowered = name.lower()
            if lowered in {b"proxy-authorization", b"proxy-connection"}:
                continue
            if lowered == b"host":
                has_host = True
            if not has_upgrade and lowered == b"connection":
                continue
            output_headers.append(name + b": " + value)
        if not has_host:
            authority = f"[{host}]" if ":" in host else host
            default_port = 443 if policy_scheme == "https" else 80
            if port != default_port:
                authority += f":{port}"
            output_headers.append(b"Host: " + authority.encode("ascii"))
        if not has_upgrade:
            output_headers.append(b"Connection: close")
        upstream = self._connect_pinned_result(port, ip)
        try:
            with self._lock:
                if self._closed:
                    return
                self._active_connections.add(upstream)
            upstream.sendall(b"\r\n".join((request_line, *output_headers, b"", b"")) + initial_body)
            self._relay(client, upstream)
        finally:
            with self._lock:
                self._active_connections.discard(upstream)
            self._close_socket(upstream)

    @staticmethod
    def _check_host_header(
        headers: list[tuple[bytes, bytes]], host: str, port: int, scheme: str,
    ) -> None:
        host_values = [value.decode("ascii").strip() for name, value in headers if name.lower() == b"host"]
        if len(host_values) > 1:
            raise ValueError("Multiple Host headers are not supported.")
        if not host_values:
            return
        host_header = host_values[0]
        if any(char in host_header for char in "/?#@"):
            raise ValueError("Host header is malformed.")
        parsed = urlsplit("http://" + host_header)
        try:
            header_port = parsed.port or (443 if scheme == "https" else 80)
            header_host = parsed.hostname.encode("idna").decode("ascii").lower() if parsed.hostname else ""
            target_host = host.encode("idna").decode("ascii").lower()
        except (UnicodeError, ValueError) as exc:
            raise ValueError("Host header is malformed.") from exc
        if parsed.username or parsed.password or header_host != target_host or header_port != port:
            raise ValueError("Host header does not match the requested authority.")

    def _connect_pinned(self, url: str) -> socket.socket:
        _normalized, _host, port, ip = self._target_resolver(url)
        return self._connect_pinned_result(port, ip)

    @staticmethod
    def _connect_pinned_result(port: int, ip: str) -> socket.socket:
        if not ip:
            raise AcquisitionError("SOURCE_DNS_FAILED", "The source target was not resolved to a numeric address.")
        try:
            address = ipaddress.ip_address(ip)
        except ValueError as exc:
            raise AcquisitionError("SOURCE_DNS_FAILED", "The source target was not resolved to a numeric address.") from exc
        upstream = open_pinned_socket(address.compressed, port, _CONNECT_TIMEOUT_SECONDS)
        try:
            upstream.settimeout(None)
            return upstream
        except OSError:
            upstream.close()
            raise

    def _relay(self, client: socket.socket, upstream: socket.socket) -> None:
        open_sockets = {client, upstream}
        while open_sockets:
            with self._lock:
                if self._closed:
                    return
            readable, _, exceptional = select.select(
                list(open_sockets), [], list(open_sockets), _SOCKET_POLL_SECONDS,
            )
            if exceptional:
                return
            for source in readable:
                try:
                    data = source.recv(64 * 1024)
                except OSError:
                    return
                target = upstream if source is client else client
                if not data:
                    open_sockets.discard(source)
                    try:
                        target.shutdown(socket.SHUT_WR)
                    except OSError:
                        pass
                    continue
                try:
                    target.sendall(data)
                except OSError:
                    return

    @staticmethod
    def _respond(client: socket.socket, status: int, reason: str,
                 headers: tuple[tuple[str, str], ...] = ()) -> None:
        lines = [f"HTTP/1.1 {status} {reason}", "Content-Length: 0", "Connection: close"]
        lines.extend(f"{name}: {value}" for name, value in headers)
        try:
            client.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("ascii"))
        except OSError:
            pass
