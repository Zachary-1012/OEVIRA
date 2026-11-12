"""Product-owned installer for the pinned Windows Codex app-server package.

OEVIRA does not redistribute this runtime in its own release. When a user
selects a Responses connection, the product can fetch the exact public GitHub
release asset, verify its published digest and Authenticode signatures, and
install an isolated copy under the product Store root.
"""
from __future__ import annotations

import hashlib
import http.client
import ipaddress
import json
import os
from pathlib import Path, PurePosixPath
import secrets
import socket
import ssl
import subprocess
import tarfile
from urllib.parse import urljoin, urlsplit

import truststore

from .acquisition import AcquisitionError, open_pinned_socket, public_target
from .intelligence_base import IntelligenceError


CODEX_PACKAGE_VERSION = "0.166.0-alpha.5.1"
CODEX_PACKAGE_SOURCE_REVISION = "f79b26ccbb3116b052544666a181012fb45dc49b"
CODEX_RELEASE_TAG = "rust-v0.166.0-alpha.5.1"
CODEX_APP_SERVER_VERSION = f"codex-app-server {CODEX_PACKAGE_VERSION}"
CODEX_PACKAGE_TARGET = "x86_64-pc-windows-msvc"
CODEX_PACKAGE_VARIANT = "codex-app-server"
CODEX_PACKAGE_URL = (
    "https://github.com/openai/codex/releases/download/"
    f"{CODEX_RELEASE_TAG}/codex-app-server-package-{CODEX_PACKAGE_TARGET}.tar.gz"
)
CODEX_PACKAGE_SHA256 = "0a402fbf570b295c698893db07b527450366f4c8a47c226e15f1c9303dadcb98"
CODEX_PACKAGE_SIZE = 137_463_357
CODEX_SOURCE_LICENSE_URL = (
    "https://raw.githubusercontent.com/openai/codex/"
    f"{CODEX_PACKAGE_SOURCE_REVISION}/LICENSE"
)
CODEX_SOURCE_LICENSE_ID = "Apache-2.0"
CODEX_SOURCE_LICENSE_SIZE = 10_926
CODEX_SOURCE_LICENSE_SHA256 = "d17f227e4df5da1600391338865ce0f3055211760a36688f816941d58232d8dc"
CODEX_RELEASE_ASSET_HOSTS = frozenset({
    "github.com", "release-assets.githubusercontent.com", "objects.githubusercontent.com",
})
CODEX_RUNTIME_FILE_HASHES = {
    "codex-package.json": (247, "e51b8f9f5d347c0476fd3b055671083ead06e46b46107265b087a38fab7b6f96"),
    "bin/codex-app-server.exe": (281_741_104, "323fe9ea703126131f86356ccfb954c293b38cf54f37bbff4647d16fe4b5799f"),
    "bin/codex-code-mode-host.exe": (79_036_720, "32729f6397a342753529ee3e69ce485f35e839c12d294ae567cd85b95a292cfe"),
    "codex-path/rg.exe": (4_218_880, "14231169855ec5205cf5a1b6f1db358ff4aed4247c86b69ce8aae647c77f6680"),
    "codex-resources/codex-command-runner.exe": (8_805_680, "8857edaa1da9bcc5a8545c68a4e9929fcb8948fe1dfc7d2b7c9026d0a0684897"),
    "codex-resources/codex-windows-sandbox-setup.exe": (19_439_408, "6d04bf0a009fbf7ef9640b88c7ebb01fc8e24b9ef5e5d9a8ebf8ea92f9b2f201"),
}
CODEX_PACKAGE_MANIFEST = {
    "layoutVersion": 1,
    "version": CODEX_PACKAGE_VERSION,
    "target": CODEX_PACKAGE_TARGET,
    "variant": CODEX_PACKAGE_VARIANT,
    "entrypoint": "bin/codex-app-server.exe",
    "resourcesDir": "codex-resources",
    "pathDir": "codex-path",
}
_MAX_DOWNLOAD_REDIRECTS = 3
_DOWNLOAD_TIMEOUT_SECONDS = 45
_MAX_EXTRACTION_BYTES = 500_000_000
_ACTIVE_INSTALL_POINTER = "active-runtime.json"


class CodexRuntimeInstallError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class _PinnedReleaseConnection(http.client.HTTPSConnection):
    def __init__(self, hostname: str, address, timeout: float):
        context = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.set_alpn_protocols(["http/1.1"])
        super().__init__(hostname, 443, timeout=timeout, context=context)
        self._address = address

    def connect(self) -> None:
        family, socktype, protocol, _canonical, sockaddr = self._address
        sock = open_pinned_socket(sockaddr[0], sockaddr[1], self.timeout, honor_local_proxy=True)
        try:
            self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
        except BaseException:
            sock.close()
            raise


class CodexAppServerRuntimeInstaller:
    """Resolve, verify and repair the one supported local app-server package."""

    def __init__(self, runtime_root: Path, *, signature_verifier,
                 archive_path_for_test: Path | None = None,
                 license_path_for_test: Path | None = None):
        self.runtime_root = Path(runtime_root).resolve(strict=False)
        self.signature_verifier = signature_verifier
        # Test-only input: a previously downloaded official asset. Product
        # callers never pass a path or model supplied by the client.
        self.archive_path_for_test = Path(archive_path_for_test) if archive_path_for_test else None
        self.license_path_for_test = Path(license_path_for_test) if license_path_for_test else None

    @property
    def release_root(self) -> Path:
        return self.runtime_root / "runtimes" / CODEX_PACKAGE_SHA256

    def status(self) -> dict:
        try:
            installation = self._read_active_installation()
        except CodexRuntimeInstallError as exc:
            return {
                "state": "REPAIR_AVAILABLE",
                "reason": exc.code,
                "version": CODEX_PACKAGE_VERSION,
                "source_revision": CODEX_PACKAGE_SOURCE_REVISION,
                "distribution": "official_release_downloaded_on_demand_not_bundled",
            }
        if installation is None:
            return {
                "state": "NOT_INSTALLED_AUTO_INSTALL_ON_USE",
                "version": CODEX_PACKAGE_VERSION,
                "source_revision": CODEX_PACKAGE_SOURCE_REVISION,
                "distribution": "official_release_downloaded_on_demand_not_bundled",
            }
        return {
            "state": "AVAILABLE_NOT_RUN",
            "version": CODEX_APP_SERVER_VERSION,
            "source_revision": CODEX_PACKAGE_SOURCE_REVISION,
            "asset_sha256": CODEX_PACKAGE_SHA256,
            "authenticode_verified": True,
            "distribution": "official_release_downloaded_on_demand_not_bundled",
        }

    def ensure_installed(self) -> Path:
        if os.name != "nt":
            raise CodexRuntimeInstallError("CODEX_RUNTIME_UNSUPPORTED_PLATFORM")
        self.runtime_root.mkdir(parents=True, exist_ok=True)
        try:
            existing = self._read_active_installation()
        except CodexRuntimeInstallError:
            existing = None
        if existing is not None:
            return existing / "bin" / "codex-app-server.exe"

        install_id = f"{CODEX_PACKAGE_SHA256}-{secrets.token_hex(8)}"
        install_parent = self.runtime_root / "runtimes"
        install_parent.mkdir(parents=True, exist_ok=True)
        staging = install_parent / f".install-{install_id}"
        staging.mkdir(parents=False, exist_ok=False)
        try:
            archive = self._verified_archive()
            self._extract_and_verify(archive, staging)
            self._install_source_license(staging)
            self._verify_package_files(staging)
            self._write_install_receipt(staging)
            executable = staging / "bin" / "codex-app-server.exe"
            self._verify_entrypoint(executable, staging)
            final = install_parent / install_id
            os.replace(staging, final)
            self._write_active_pointer(install_id)
            return final / "bin" / "codex-app-server.exe"
        except CodexRuntimeInstallError:
            raise
        except (OSError, tarfile.TarError, ValueError, json.JSONDecodeError) as exc:
            raise CodexRuntimeInstallError("CODEX_RUNTIME_INSTALL_FAILED") from exc

    def _read_active_installation(self) -> Path | None:
        pointer_path = self.runtime_root / _ACTIVE_INSTALL_POINTER
        if not pointer_path.is_file():
            return None
        try:
            pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeError) as exc:
            raise CodexRuntimeInstallError("CODEX_RUNTIME_POINTER_INVALID") from exc
        relative = pointer.get("path") if isinstance(pointer, dict) else None
        if (not isinstance(relative, str) or not relative.startswith("runtimes/")
                or pointer.get("asset_sha256") != CODEX_PACKAGE_SHA256):
            raise CodexRuntimeInstallError("CODEX_RUNTIME_POINTER_INVALID")
        installation = (self.runtime_root / relative).resolve(strict=False)
        if not _is_relative_to(installation, self.runtime_root):
            raise CodexRuntimeInstallError("CODEX_RUNTIME_POINTER_INVALID")
        self._validate_installation(installation)
        return installation

    def _validate_installation(self, installation: Path) -> None:
        try:
            expected_receipt = self._receipt()
            receipt = json.loads((installation / "oevira-runtime.json").read_text(encoding="utf-8"))
            if receipt != expected_receipt:
                raise CodexRuntimeInstallError("CODEX_RUNTIME_INTEGRITY_FAILED")
            self._verify_package_files(installation)
            self._verify_entrypoint(installation / "bin" / "codex-app-server.exe", installation)
        except CodexRuntimeInstallError:
            raise
        except (OSError, ValueError, UnicodeError, json.JSONDecodeError) as exc:
            raise CodexRuntimeInstallError("CODEX_RUNTIME_INTEGRITY_FAILED") from exc

    def _verified_archive(self) -> Path:
        if self.archive_path_for_test is not None:
            archive = self.archive_path_for_test.resolve(strict=True)
            self._verify_archive_digest(archive)
            return archive
        cache = self.runtime_root / "downloads"
        cache.mkdir(parents=True, exist_ok=True)
        archive = cache / f"codex-app-server-{CODEX_PACKAGE_SHA256}.tar.gz"
        if archive.is_file():
            try:
                self._verify_archive_digest(archive)
                return archive
            except CodexRuntimeInstallError:
                quarantined = archive.with_name(f"{archive.name}.invalid-{secrets.token_hex(6)}")
                os.replace(archive, quarantined)
        temporary = cache / f".{archive.name}.download-{secrets.token_hex(6)}"
        try:
            self._download_release(temporary)
            self._verify_archive_digest(temporary)
            os.replace(temporary, archive)
        finally:
            if temporary.exists():
                temporary.unlink()
        return archive

    @staticmethod
    def _verify_archive_digest(archive: Path) -> None:
        try:
            digest = hashlib.sha256()
            size = 0
            with archive.open("rb") as source:
                while chunk := source.read(1024 * 1024):
                    size += len(chunk)
                    if size > CODEX_PACKAGE_SIZE:
                        raise CodexRuntimeInstallError("CODEX_RUNTIME_ASSET_SIZE_MISMATCH")
                    digest.update(chunk)
        except OSError as exc:
            raise CodexRuntimeInstallError("CODEX_RUNTIME_ASSET_UNAVAILABLE") from exc
        if size != CODEX_PACKAGE_SIZE:
            raise CodexRuntimeInstallError("CODEX_RUNTIME_ASSET_SIZE_MISMATCH")
        if digest.hexdigest() != CODEX_PACKAGE_SHA256:
            raise CodexRuntimeInstallError("CODEX_RUNTIME_ASSET_DIGEST_MISMATCH")

    def _download_release(self, destination: Path) -> None:
        current_url = CODEX_PACKAGE_URL
        try:
            for redirect_count in range(_MAX_DOWNLOAD_REDIRECTS + 1):
                parsed = urlsplit(current_url)
                if (parsed.scheme != "https" or parsed.username or parsed.password
                        or parsed.hostname not in CODEX_RELEASE_ASSET_HOSTS):
                    raise CodexRuntimeInstallError("CODEX_RUNTIME_REDIRECT_DENIED")
                normalized, hostname, port, pinned_ip = public_target(current_url)
                if port != 443 or hostname not in CODEX_RELEASE_ASSET_HOSTS:
                    raise CodexRuntimeInstallError("CODEX_RUNTIME_REDIRECT_DENIED")
                # Construct the socket address directly from the exact address
                # validated by Acquisition. Do not ask the OS to resolve the
                # hostname a second time after URL admission.
                parsed_ip = ipaddress.ip_address(pinned_ip)
                family = socket.AF_INET6 if parsed_ip.version == 6 else socket.AF_INET
                sockaddr = ((parsed_ip.compressed, port, 0, 0) if parsed_ip.version == 6
                            else (parsed_ip.compressed, port))
                address = (family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", sockaddr)
                connection = _PinnedReleaseConnection(hostname, address, _DOWNLOAD_TIMEOUT_SECONDS)
                try:
                    request_path = urlsplit(normalized).path
                    if urlsplit(normalized).query:
                        request_path += "?" + urlsplit(normalized).query
                    connection.request("GET", request_path, headers={
                        "Host": hostname,
                        "Accept": "application/octet-stream",
                        "Accept-Encoding": "identity",
                        "User-Agent": "OEVIRA-Intelligence-Runtime/1.0",
                        "Connection": "close",
                    })
                    response = connection.getresponse()
                    response_headers = {key.lower(): value for key, value in response.getheaders()}
                    if response.status in {301, 302, 303, 307, 308}:
                        if redirect_count == _MAX_DOWNLOAD_REDIRECTS:
                            raise CodexRuntimeInstallError("CODEX_RUNTIME_REDIRECT_LIMIT")
                        location = response_headers.get("location")
                        if not location:
                            raise CodexRuntimeInstallError("CODEX_RUNTIME_REDIRECT_INVALID")
                        next_url = urljoin(normalized, location)
                        next_host = urlsplit(next_url).hostname
                        if next_host not in CODEX_RELEASE_ASSET_HOSTS:
                            raise CodexRuntimeInstallError("CODEX_RUNTIME_REDIRECT_DENIED")
                        current_url = next_url
                        continue
                    if response.status != 200:
                        raise CodexRuntimeInstallError("CODEX_RUNTIME_DOWNLOAD_REJECTED")
                    length = response_headers.get("content-length")
                    if length and (not length.isdigit() or int(length) != CODEX_PACKAGE_SIZE):
                        raise CodexRuntimeInstallError("CODEX_RUNTIME_ASSET_SIZE_MISMATCH")
                    digest = hashlib.sha256()
                    size = 0
                    with destination.open("xb") as output:
                        while True:
                            chunk = response.read(1024 * 1024)
                            if not chunk:
                                break
                            size += len(chunk)
                            if size > CODEX_PACKAGE_SIZE:
                                raise CodexRuntimeInstallError("CODEX_RUNTIME_ASSET_SIZE_MISMATCH")
                            digest.update(chunk)
                            output.write(chunk)
                    if size != CODEX_PACKAGE_SIZE:
                        raise CodexRuntimeInstallError("CODEX_RUNTIME_ASSET_SIZE_MISMATCH")
                    if digest.hexdigest() != CODEX_PACKAGE_SHA256:
                        raise CodexRuntimeInstallError("CODEX_RUNTIME_ASSET_DIGEST_MISMATCH")
                    return
                finally:
                    connection.close()
        except CodexRuntimeInstallError:
            raise
        except (OSError, TimeoutError, socket.timeout, http.client.HTTPException,
                ssl.SSLError, AcquisitionError, IndexError) as exc:
            raise CodexRuntimeInstallError("CODEX_RUNTIME_DOWNLOAD_UNAVAILABLE") from exc
        raise CodexRuntimeInstallError("CODEX_RUNTIME_DOWNLOAD_UNAVAILABLE")

    def _extract_and_verify(self, archive: Path, staging: Path) -> None:
        seen: set[str] = set()
        total_size = 0
        allowed_directories = {"bin", "codex-path", "codex-resources"}
        with tarfile.open(archive, mode="r:gz") as package:
            for member in package.getmembers():
                name = member.name.rstrip("/")
                normalized = PurePosixPath(name)
                if (not name or normalized.is_absolute() or ".." in normalized.parts
                        or "\\" in name or name != normalized.as_posix()):
                    raise CodexRuntimeInstallError("CODEX_RUNTIME_PACKAGE_PATH_INVALID")
                if member.isdir():
                    if name not in allowed_directories:
                        raise CodexRuntimeInstallError("CODEX_RUNTIME_PACKAGE_CONTENT_INVALID")
                    continue
                if not member.isfile() or name not in CODEX_RUNTIME_FILE_HASHES or name in seen:
                    raise CodexRuntimeInstallError("CODEX_RUNTIME_PACKAGE_CONTENT_INVALID")
                expected_size, _expected_digest = CODEX_RUNTIME_FILE_HASHES[name]
                if member.size != expected_size:
                    raise CodexRuntimeInstallError("CODEX_RUNTIME_PACKAGE_FILE_SIZE_MISMATCH")
                total_size += member.size
                if total_size > _MAX_EXTRACTION_BYTES:
                    raise CodexRuntimeInstallError("CODEX_RUNTIME_PACKAGE_TOO_LARGE")
                destination = staging.joinpath(*normalized.parts)
                resolved_parent = destination.parent.resolve(strict=False)
                if not _is_relative_to(resolved_parent, staging.resolve(strict=True)):
                    raise CodexRuntimeInstallError("CODEX_RUNTIME_PACKAGE_PATH_INVALID")
                destination.parent.mkdir(parents=True, exist_ok=True)
                source = package.extractfile(member)
                if source is None:
                    raise CodexRuntimeInstallError("CODEX_RUNTIME_PACKAGE_CONTENT_INVALID")
                digest = hashlib.sha256()
                copied = 0
                with source, destination.open("xb") as output:
                    while copied < expected_size:
                        chunk = source.read(min(1024 * 1024, expected_size - copied))
                        if not chunk:
                            raise CodexRuntimeInstallError("CODEX_RUNTIME_PACKAGE_TRUNCATED")
                        copied += len(chunk)
                        digest.update(chunk)
                        output.write(chunk)
                if digest.hexdigest() != _expected_digest:
                    raise CodexRuntimeInstallError("CODEX_RUNTIME_PACKAGE_FILE_DIGEST_MISMATCH")
                seen.add(name)
        if seen != set(CODEX_RUNTIME_FILE_HASHES):
            raise CodexRuntimeInstallError("CODEX_RUNTIME_PACKAGE_INCOMPLETE")

    def _verify_package_files(self, installation: Path) -> None:
        manifest_path = installation / "codex-package.json"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeError) as exc:
            raise CodexRuntimeInstallError("CODEX_RUNTIME_PACKAGE_MANIFEST_INVALID") from exc
        if manifest != CODEX_PACKAGE_MANIFEST:
            raise CodexRuntimeInstallError("CODEX_RUNTIME_PACKAGE_MANIFEST_INVALID")
        for relative, (expected_size, expected_digest) in CODEX_RUNTIME_FILE_HASHES.items():
            path = installation.joinpath(*PurePosixPath(relative).parts)
            try:
                stat = path.stat()
                if not path.is_file() or stat.st_size != expected_size:
                    raise CodexRuntimeInstallError("CODEX_RUNTIME_FILE_SIZE_MISMATCH")
                digest = _file_sha256(path)
            except OSError as exc:
                raise CodexRuntimeInstallError("CODEX_RUNTIME_FILE_UNAVAILABLE") from exc
            if digest != expected_digest:
                raise CodexRuntimeInstallError("CODEX_RUNTIME_FILE_DIGEST_MISMATCH")
        license_path = installation / "LICENSE-CODEX-APACHE-2.0.txt"
        try:
            if license_path.stat().st_size != CODEX_SOURCE_LICENSE_SIZE:
                raise CodexRuntimeInstallError("CODEX_RUNTIME_LICENSE_SIZE_MISMATCH")
            if _file_sha256(license_path) != CODEX_SOURCE_LICENSE_SHA256:
                raise CodexRuntimeInstallError("CODEX_RUNTIME_LICENSE_DIGEST_MISMATCH")
        except OSError as exc:
            raise CodexRuntimeInstallError("CODEX_RUNTIME_LICENSE_UNAVAILABLE") from exc

    def _install_source_license(self, staging: Path) -> None:
        target = staging / "LICENSE-CODEX-APACHE-2.0.txt"
        temporary = staging / f".license-{secrets.token_hex(6)}.tmp"
        if self.license_path_for_test is not None:
            try:
                content = self.license_path_for_test.resolve(strict=True).read_bytes()
            except OSError as exc:
                raise CodexRuntimeInstallError("CODEX_RUNTIME_LICENSE_UNAVAILABLE") from exc
            if (len(content) != CODEX_SOURCE_LICENSE_SIZE
                    or hashlib.sha256(content).hexdigest() != CODEX_SOURCE_LICENSE_SHA256):
                raise CodexRuntimeInstallError("CODEX_RUNTIME_LICENSE_DIGEST_MISMATCH")
            with temporary.open("xb") as output:
                output.write(content)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, target)
            return

        try:
            normalized, hostname, port, pinned_ip = public_target(CODEX_SOURCE_LICENSE_URL)
            if hostname != "raw.githubusercontent.com" or port != 443 or normalized != CODEX_SOURCE_LICENSE_URL:
                raise CodexRuntimeInstallError("CODEX_RUNTIME_LICENSE_SOURCE_INVALID")
            parsed_ip = ipaddress.ip_address(pinned_ip)
            family = socket.AF_INET6 if parsed_ip.version == 6 else socket.AF_INET
            sockaddr = ((parsed_ip.compressed, port, 0, 0) if parsed_ip.version == 6
                        else (parsed_ip.compressed, port))
            address = (family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", sockaddr)
            connection = _PinnedReleaseConnection(hostname, address, _DOWNLOAD_TIMEOUT_SECONDS)
            try:
                parsed_url = urlsplit(normalized)
                connection.request("GET", parsed_url.path, headers={
                    "Host": hostname, "Accept": "text/plain", "Accept-Encoding": "identity",
                    "User-Agent": "OEVIRA-Intelligence-Runtime/1.0", "Connection": "close",
                })
                response = connection.getresponse()
                if response.status != 200:
                    raise CodexRuntimeInstallError("CODEX_RUNTIME_LICENSE_DOWNLOAD_REJECTED")
                length = response.getheader("Content-Length")
                if not length or not length.isdigit() or int(length) != CODEX_SOURCE_LICENSE_SIZE:
                    raise CodexRuntimeInstallError("CODEX_RUNTIME_LICENSE_SIZE_MISMATCH")
                digest = hashlib.sha256()
                size = 0
                with temporary.open("xb") as output:
                    while True:
                        chunk = response.read(4096)
                        if not chunk:
                            break
                        size += len(chunk)
                        if size > CODEX_SOURCE_LICENSE_SIZE:
                            raise CodexRuntimeInstallError("CODEX_RUNTIME_LICENSE_SIZE_MISMATCH")
                        digest.update(chunk)
                        output.write(chunk)
                    output.flush()
                    os.fsync(output.fileno())
                if size != CODEX_SOURCE_LICENSE_SIZE or digest.hexdigest() != CODEX_SOURCE_LICENSE_SHA256:
                    raise CodexRuntimeInstallError("CODEX_RUNTIME_LICENSE_DIGEST_MISMATCH")
                os.replace(temporary, target)
            finally:
                connection.close()
        except CodexRuntimeInstallError:
            raise
        except (OSError, TimeoutError, socket.timeout, http.client.HTTPException,
                ssl.SSLError, AcquisitionError, ValueError) as exc:
            raise CodexRuntimeInstallError("CODEX_RUNTIME_LICENSE_DOWNLOAD_UNAVAILABLE") from exc
        finally:
            if temporary.exists():
                temporary.unlink()

    def _verify_entrypoint(self, executable: Path, working_directory: Path) -> None:
        if not executable.is_file() or not self.signature_verifier(executable):
            raise CodexRuntimeInstallError("CODEX_RUNTIME_SIGNATURE_INVALID")
        temp = working_directory / "probe-temp"
        temp.mkdir(parents=True, exist_ok=True)
        system_root = os.environ.get("SystemRoot") or os.environ.get("WINDIR") or r"C:\Windows"
        environment = {
            "SystemRoot": system_root,
            "WINDIR": system_root,
            "PATH": os.pathsep.join((str(executable.parent), str(Path(system_root) / "System32"), system_root)),
            "TEMP": str(temp), "TMP": str(temp),
            "HOME": str(temp), "USERPROFILE": str(temp), "CODEX_HOME": str(temp),
        }
        try:
            result = subprocess.run(
                [str(executable), "--version"], cwd=str(working_directory), env=environment,
                capture_output=True, text=True, timeout=30, check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise CodexRuntimeInstallError("CODEX_RUNTIME_VERSION_READBACK_FAILED") from exc
        if result.returncode != 0 or result.stdout.strip() != CODEX_APP_SERVER_VERSION:
            raise CodexRuntimeInstallError("CODEX_RUNTIME_VERSION_MISMATCH")

    def _receipt(self) -> dict:
        return {
            "schema": 1,
            "version": CODEX_PACKAGE_VERSION,
            "source_revision": CODEX_PACKAGE_SOURCE_REVISION,
            "release_tag": CODEX_RELEASE_TAG,
            "target": CODEX_PACKAGE_TARGET,
            "variant": CODEX_PACKAGE_VARIANT,
            "entrypoint": CODEX_PACKAGE_MANIFEST["entrypoint"],
            "asset_url": CODEX_PACKAGE_URL,
            "asset_size": CODEX_PACKAGE_SIZE,
            "asset_sha256": CODEX_PACKAGE_SHA256,
            "files": {
                path: {"size": size, "sha256": digest}
                for path, (size, digest) in CODEX_RUNTIME_FILE_HASHES.items()
            },
            "source_license": {
                "id": CODEX_SOURCE_LICENSE_ID,
                "url": CODEX_SOURCE_LICENSE_URL,
                "path": "LICENSE-CODEX-APACHE-2.0.txt",
                "size": CODEX_SOURCE_LICENSE_SIZE,
                "sha256": CODEX_SOURCE_LICENSE_SHA256,
            },
            "distribution": "downloaded_from_official_release_on_demand_not_bundled_by_oevira",
            "third_party_notice_state": "upstream_release_package_does_not_include_notice_manifest",
        }

    def _write_install_receipt(self, staging: Path) -> None:
        receipt_path = staging / "oevira-runtime.json"
        temporary = staging / f".oevira-runtime-{secrets.token_hex(6)}.tmp"
        encoded = json.dumps(self._receipt(), ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        with temporary.open("x", encoding="utf-8", newline="\n") as output:
            output.write(encoded)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, receipt_path)

    def _write_active_pointer(self, install_id: str) -> None:
        pointer = self.runtime_root / _ACTIVE_INSTALL_POINTER
        temporary = self.runtime_root / f".{_ACTIVE_INSTALL_POINTER}-{secrets.token_hex(6)}.tmp"
        value = {"path": f"runtimes/{install_id}", "asset_sha256": CODEX_PACKAGE_SHA256}
        with temporary.open("x", encoding="utf-8", newline="\n") as output:
            output.write(json.dumps(value, sort_keys=True) + "\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, pointer)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False
