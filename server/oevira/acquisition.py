"""Bounded public-source reads for the one product acquisition service.

No discovery, account access, scheduler, database or browser fallback lives here.
Every redirect and resolved address is checked before a connection is opened.
"""
from __future__ import annotations

from datetime import datetime, timezone
from html.parser import HTMLParser
from http.client import HTTPConnection, HTTPSConnection, HTTPException
import hashlib
import ipaddress
import re
import socket
import ssl
import time
import truststore
from typing import Callable
from urllib.parse import quote, urljoin, urlsplit, urlunsplit
from urllib.request import getproxies
from urllib.robotparser import RobotFileParser

from lxml import etree, html


USER_AGENT = "OEVIRA/0.1 (+https://github.com/Zachary-1012/OEVIRA)"
MAX_BYTES = 3 * 1024 * 1024
MAX_REDIRECTS = 4
MAX_FEED_ENTRIES = 500
MAX_ROBOTS_SITEMAPS = 100
MAX_DISCOVERED_LINKS = 1000
_SYNTHETIC_PROXY_RANGE = ipaddress.ip_network("198.18.0.0/15")


def _public_addresses(host: str, port: int) -> list[str]:
    addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    ips = list(dict.fromkeys(row[4][0] for row in addresses))
    # Some existing local proxy clients return synthetic benchmark-range IPs
    # through getaddrinfo. Resolve with the same configured OS DNS servers,
    # then enforce and pin actual public addresses; never allow the fake range.
    if ips and all(ipaddress.ip_address(ip) in _SYNTHETIC_PROXY_RANGE for ip in ips):
        proxies = getproxies()
        configured = False
        for kind in ("http", "https"):
            value = proxies.get(kind, "")
            proxy = urlsplit(value if "://" in value else "http://" + value)
            try:
                configured |= bool(proxy.hostname and ipaddress.ip_address(proxy.hostname).is_loopback)
            except ValueError:
                continue
        if configured:
            import dns.exception
            import dns.resolver

            resolver = dns.resolver.Resolver(configure=True)
            resolved = []
            deadline = time.monotonic() + 5
            for record_type in ("A", "AAAA"):
                try:
                    answer = resolver.resolve(host, record_type, lifetime=max(0.1, deadline - time.monotonic()))
                    resolved.extend(item.address for item in answer)
                except (dns.resolver.NoAnswer, dns.resolver.NXDOMAIN):
                    continue
                except dns.exception.DNSException as exc:
                    raise AcquisitionError("SOURCE_DNS_FAILED", "The configured network DNS could not confirm the source address.") from exc
            ips = list(dict.fromkeys(resolved))
    return ips


def open_pinned_socket(ip: str, port: int, timeout: float, *, honor_local_proxy: bool = True) -> socket.socket:
    """Connect to a validated numeric target through the existing local transport."""
    address = ipaddress.ip_address(ip)
    endpoint = None
    if honor_local_proxy and address.is_global:
        proxies = getproxies()
        configured = proxies.get("https" if port == 443 else "http")
        if configured:
            proxy = urlsplit(configured if "://" in configured else "http://" + configured)
            try:
                if (proxy.scheme == "http" and proxy.hostname and ipaddress.ip_address(proxy.hostname).is_loopback
                        and proxy.port and not proxy.username and not proxy.password):
                    endpoint = (proxy.hostname, proxy.port)
            except ValueError:
                pass
    destination = ipaddress.ip_address(endpoint[0]) if endpoint else address
    destination_port = endpoint[1] if endpoint else port
    family = socket.AF_INET6 if destination.version == 6 else socket.AF_INET
    upstream = socket.socket(family, socket.SOCK_STREAM)
    upstream.settimeout(timeout)
    try:
        upstream.connect((destination.compressed, destination_port, 0, 0) if family == socket.AF_INET6 else (destination.compressed, destination_port))
    except BaseException:
        upstream.close()
        raise
    if endpoint is None:
        return upstream
    authority = f"[{address.compressed}]:{port}" if address.version == 6 else f"{address.compressed}:{port}"
    try:
        upstream.sendall(f"CONNECT {authority} HTTP/1.1\r\nHost: {authority}\r\n\r\n".encode("ascii"))
        header = bytearray()
        while not header.endswith(b"\r\n\r\n"):
            block = upstream.recv(1)
            if not block or len(header) >= 16384:
                raise OSError("The configured local proxy did not establish a bounded tunnel")
            header.extend(block)
        if not re.match(rb"HTTP/1\.[01] 200(?: |\r)", header):
            raise OSError("The configured local proxy rejected the pinned target")
        return upstream
    except BaseException:
        upstream.close()
        raise


class AcquisitionError(Exception):
    def __init__(self, code: str, message: str, *, status: int | None = None,
                 retry_after: str | None = None):
        super().__init__(message)
        self.code = code
        self.status = status
        self.retry_after = retry_after


def public_target(url: str) -> tuple[str, str, int, str]:
    """Return the normalized URL and a validated, pinned network address."""
    try:
        parts = urlsplit(url)
    except ValueError as exc:
        raise AcquisitionError("INVALID_SOURCE_URL", "The source URL is malformed.") from exc
    if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
        raise AcquisitionError("INVALID_SOURCE_URL", "Only public HTTP(S) URLs without credentials are supported.")
    if any(ord(char) < 32 for char in url) or len(url) > 4096:
        raise AcquisitionError("INVALID_SOURCE_URL", "The source URL contains control characters or is too long.")
    try:
        port = parts.port or (443 if parts.scheme == "https" else 80)
        host = parts.hostname.encode("idna").decode("ascii")
    except (ValueError, UnicodeError) as exc:
        raise AcquisitionError("INVALID_SOURCE_URL", "The source host or port is invalid.") from exc
    if port not in {80, 443}:
        raise AcquisitionError("SOURCE_PORT_DENIED", "Only standard public web ports are supported.")
    try:
        ips = _public_addresses(host, port)
    except OSError as exc:
        raise AcquisitionError("SOURCE_DNS_FAILED", "The source host could not be resolved.") from exc
    if not ips or any(not ipaddress.ip_address(ip).is_global for ip in ips):
        raise AcquisitionError("PRIVATE_SOURCE_DENIED", "Local, private and special-use network destinations are not permitted.")
    authority = f"[{host}]" if ":" in host else host
    if port != (443 if parts.scheme == "https" else 80):
        authority += f":{port}"
    normalized = urlunsplit((parts.scheme, authority,
                            quote(parts.path or "/", safe="/:@!$&'()*+,;=-._~%"),
                            quote(parts.query, safe="/?@!$&'()*+,;=:-._~%"), ""))
    return normalized, host, port, ips[0]


class _PinnedHTTP(HTTPConnection):
    def __init__(self, host: str, port: int, ip: str, timeout: float):
        super().__init__(host, port, timeout=timeout)
        self.ip = ip

    def connect(self):
        self.sock = open_pinned_socket(self.ip, self.port, self.timeout)


class _PinnedHTTPS(HTTPSConnection):
    def __init__(self, host: str, port: int, ip: str, timeout: float):
        super().__init__(host, port, timeout=timeout, context=truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT))
        self.ip = ip

    def connect(self):
        sock = open_pinned_socket(self.ip, self.port, self.timeout)
        try:
            self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
        except BaseException:
            sock.close()
            raise


def _read_public(url: str, *, headers: dict[str, str], deadline: float,
                 max_bytes: int = MAX_BYTES,
                 allow_url: Callable[[str], bool] | None = None) -> tuple[str, int, dict[str, str], bytes]:
    for redirect in range(MAX_REDIRECTS + 1):
        normalized, host, port, ip = public_target(url)
        if allow_url is not None and not allow_url(normalized):
            raise AcquisitionError("SOURCE_POLICY_DENIED", "The source does not permit this automated read.")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AcquisitionError("SOURCE_TIMEOUT", "The source read exceeded its time budget.")
        parts = urlsplit(normalized)
        connection_type = _PinnedHTTPS if parts.scheme == "https" else _PinnedHTTP
        connection = connection_type(host, port, ip, min(remaining, 10))
        try:
            request_path = parts.path + ("?" + parts.query if parts.query else "")
            connection.request("GET", request_path, headers={
                "User-Agent": USER_AGENT, "Accept-Encoding": "identity",
                "Accept": "text/html,application/xhtml+xml,text/plain,application/rss+xml,application/atom+xml;q=0.9,*/*;q=0.1",
                **headers,
            })
            response = connection.getresponse()
            response_headers = {key.lower(): value for key, value in response.getheaders()}
            if response.status in {301, 302, 303, 307, 308}:
                location = response_headers.get("location")
                if not location or redirect == MAX_REDIRECTS:
                    raise AcquisitionError("SOURCE_REDIRECT_LIMIT", "The source redirects could not be resolved safely.")
                next_url = urljoin(normalized, location)
                if parts.scheme == "https" and urlsplit(next_url).scheme != "https":
                    raise AcquisitionError("SOURCE_DOWNGRADE_DENIED", "An HTTPS source redirected to an insecure connection.")
                if urlsplit(next_url).netloc != parts.netloc:
                    raise AcquisitionError("SOURCE_ORIGIN_CHANGED", "The source moved to another origin; select its URL explicitly before reading it.")
                url = next_url
                continue
            length = response_headers.get("content-length")
            if length and length.isdigit() and int(length) > max_bytes:
                raise AcquisitionError("SOURCE_TOO_LARGE", "This source exceeds the configured read limit.")
            chunks = []
            size = 0
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AcquisitionError("SOURCE_TIMEOUT", "The source read exceeded its time budget.")
                if connection.sock:
                    connection.sock.settimeout(min(remaining, 10))
                chunk = response.read(min(65536, max_bytes + 1 - size))
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
                if size > max_bytes:
                    raise AcquisitionError("SOURCE_TOO_LARGE", "This source exceeds the configured read limit.")
            return normalized, response.status, response_headers, b"".join(chunks)
        except (OSError, HTTPException) as exc:
            raise AcquisitionError("SOURCE_NETWORK_ERROR", "The source could not be read over a verified connection.") from exc
        finally:
            connection.close()
    raise AcquisitionError("SOURCE_REDIRECT_LIMIT", "The source redirects exceeded the configured limit.")


class _PageText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.ignored = 0
        self.title_depth = 0
        self.title_parts: list[str] = []
        self.parts: list[str] = []
        self.metadata: dict[str, str] = {}

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "noscript", "svg", "template"}:
            self.ignored += 1
        if tag == "title":
            self.title_depth += 1
        if tag in {"p", "div", "article", "section", "h1", "h2", "h3", "li", "tr", "br"} and not self.ignored:
            self.parts.append("\n")
        if tag == "meta":
            values = dict(attrs)
            key = values.get("property") or values.get("name")
            if key in {"article:published_time", "date", "author", "og:title"} and values.get("content"):
                self.metadata[key] = values["content"]

    def handle_endtag(self, tag):
        if tag in {"script", "style", "noscript", "svg", "template"} and self.ignored:
            self.ignored -= 1
        if tag == "title" and self.title_depth:
            self.title_depth -= 1
        if tag in {"p", "div", "article", "section", "li", "tr"} and not self.ignored:
            self.parts.append("\n")

    def handle_data(self, data):
        if self.title_depth:
            self.title_parts.append(data)
        if not self.ignored and not self.title_depth:
            self.parts.append(data)


def _source_link(base: str, value: str | None) -> str | None:
    if not value or len(value) > 4096:
        return None
    target = urljoin(base, value.strip())
    try:
        parts = urlsplit(target)
        if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
            return None
        return urlunsplit((parts.scheme, parts.netloc, parts.path or "/", parts.query, ""))
    except ValueError:
        return None


def _same_origin_url(left: str, right: str) -> bool:
    try:
        first, second = urlsplit(left), urlsplit(right)
        first_port = first.port or (443 if first.scheme == "https" else 80)
        second_port = second.port or (443 if second.scheme == "https" else 80)
        return (
            first.scheme.lower() == second.scheme.lower()
            and (first.hostname or "").lower() == (second.hostname or "").lower()
            and first_port == second_port
        )
    except ValueError:
        return False


def _robots_sitemaps(lines: list[str], source_url: str) -> tuple[list[dict], bool]:
    candidates = []
    seen = set()
    truncated = False
    for line in lines:
        match = re.match(r"^\s*sitemap\s*:\s*(\S+)\s*$", line, re.IGNORECASE)
        if not match:
            continue
        target = _source_link(source_url, match.group(1))
        if not target or not _same_origin_url(target, source_url) or target in seen:
            continue
        seen.add(target)
        if len(candidates) >= MAX_ROBOTS_SITEMAPS:
            truncated = True
            continue
        candidates.append({"url": target, "title": "Sitemap", "same_origin": True, "kind": "robots_sitemap"})
    return candidates, truncated


def _readable_html(decoded: str, source_url: str) -> dict:
    """Extract declared primary content while preserving the original response separately."""
    page = _PageText()
    page.feed(decoded)
    markup = re.sub(r"^\s*<\?xml[^?]*\?>", "", decoded) or "<html><body></body></html>"
    document = html.document_fromstring(markup, parser=html.HTMLParser(no_network=True))
    for node in document.xpath("//script|//style|//noscript|//svg|//template|//*[@hidden]|//*[@aria-hidden='true']"):
        node.drop_tree()
    for node in list(document.iter()):
        if not isinstance(node.tag, str) or node.getparent() is None:
            continue
        style = node.get("style") or ""
        explicitly_hidden = re.search(r"(?:^|;)\s*(?:display\s*:\s*none|visibility\s*:\s*hidden)\s*(?:!important\s*)?(?:;|$)", style, re.I)
        inline_header = node.tag.lower() in {"ix:header", "ix:hidden"}
        if explicitly_hidden or inline_header:
            node.drop_tree()
    primary = document.xpath("//main|//*[@role='main']")
    if not primary:
        primary = document.xpath("//article")
    # A nested <article> or main landmark must not duplicate its own parent.
    primary_ids = {id(node) for node in primary}
    primary = [node for node in primary if not any(id(parent) in primary_ids for parent in node.iterancestors())]
    scope = "declared_primary_content" if primary else "static_document_body"
    if not primary:
        primary = document.xpath("//body") or [document]
    blocks = []
    links = []
    seen_urls = set()
    for element in document.xpath("//link[@href]"):
        relation = {part.lower() for part in (element.get("rel") or "").split()}
        content_type = (element.get("type") or "").split(";", 1)[0].strip().lower()
        if "alternate" not in relation or content_type not in {"application/rss+xml", "application/atom+xml"}:
            continue
        target = _source_link(source_url, element.get("href"))
        if not target or target in seen_urls:
            continue
        seen_urls.add(target)
        links.append({
            "url": target,
            "title": " ".join((element.get("title") or "RSS/Atom feed").split())[:500],
            "same_origin": _same_origin_url(target, source_url),
            "kind": "declared_feed",
        })
    for node in primary:
        for element in node.iter():
            if element.tag in {"br", "p", "div", "section", "article", "h1", "h2", "h3", "li", "tr", "blockquote"}:
                if element.text:
                    element.text = "\n" + element.text
                if element.tail:
                    element.tail = "\n" + element.tail
        text = "\n".join(line for raw in node.text_content().splitlines()
                         if (line := re.sub(r"[\t \r\f\v]+", " ", raw).strip()))
        if text:
            blocks.append(text)
        for anchor in node.xpath(".//a[@href]"):
            target = _source_link(source_url, anchor.get("href"))
            if target and target not in seen_urls:
                seen_urls.add(target)
                links.append({"url": target, "title": " ".join(anchor.text_content().split())[:500],
                              "same_origin": _same_origin_url(target, source_url), "kind": "page_link"})
    title = page.metadata.get("og:title") or "".join(page.title_parts).strip() or source_url
    next_links = document.xpath("//link[contains(concat(' ',normalize-space(@rel),' '),' next ')]/@href")
    return {"text": "\n\n".join(blocks), "title": title, "metadata": page.metadata,
            "extraction_scope": scope, "discovered_links": links[:MAX_DISCOVERED_LINKS],
            "links_truncated": len(links) > MAX_DISCOVERED_LINKS,
            "next_url": _source_link(source_url, next_links[0]) if next_links else None}


def _read_feed(content: bytes, source_url: str) -> dict:
    """Parse RSS/Atom or bounded XML Sitemap entries into readable source metadata."""
    parser = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False, huge_tree=False)
    try:
        root = etree.fromstring(content, parser=parser)
    except etree.XMLSyntaxError as exc:
        raise AcquisitionError("SOURCE_FEED_INVALID", "The source feed is not well-formed XML.") from exc
    if root.getroottree().docinfo.doctype:
        raise AcquisitionError("SOURCE_FEED_INVALID", "Feeds with document type declarations are not accepted.")
    name = etree.QName(root).localname
    if name in {"urlset", "sitemapindex"}:
        entry_name = "url" if name == "urlset" else "sitemap"
        nodes = root.xpath("./*[local-name()=$name]", name=entry_name)
        entries = []
        for node in nodes[:MAX_FEED_ENTRIES]:
            locations = node.xpath("./*[local-name()='loc']/text()")
            target = _source_link(source_url, locations[0].strip() if locations else None)
            if not target:
                continue
            last_modified_values = node.xpath("./*[local-name()='lastmod']/text()")
            last_modified = last_modified_values[0].strip() if last_modified_values else None
            kind = "sitemap_url" if name == "urlset" else "sitemap_index"
            entries.append({
                "native_id": None, "title": None, "url": target, "text": "",
                "author": None, "published_at": None, "updated_at": last_modified, "kind": kind,
            })
        rendered = [" ".join(part for part in (entry["url"], entry["updated_at"]) if part) for entry in entries]
        invalid_entries = min(len(nodes), MAX_FEED_ENTRIES) - len(entries)
        truncated = len(nodes) > MAX_FEED_ENTRIES or invalid_entries > 0
        scope = "sitemap_urls" if name == "urlset" else "sitemap_index"
        return {
            "text": "\n".join(rendered),
            "title": "Sitemap index" if name == "sitemapindex" else "Sitemap URL set",
            "entries": entries, "entries_truncated": truncated, "entry_count_observed": len(nodes),
            "links_truncated": truncated, "extraction_scope": scope, "next_url": None,
            "discovered_links": [],
        }
    if name not in {"rss", "feed", "RDF"}:
        raise AcquisitionError("SOURCE_FORMAT_UNSUPPORTED", "The XML source is not a supported RSS or Atom feed.")

    def child_text(node, local_name):
        found = node.xpath("./*[local-name()=$name]", name=local_name)
        return "".join(found[0].itertext()).strip() if found else None

    channel = root.find("channel") if name == "rss" else root
    if channel is None:
        raise AcquisitionError("SOURCE_FEED_INVALID", "The RSS source has no channel.")
    title = child_text(channel, "title") or source_url
    nodes = root.xpath("//*[local-name()='item' or local-name()='entry']")
    entries = []
    for node in nodes[:MAX_FEED_ENTRIES]:
        link_nodes = node.xpath("./*[local-name()='link']")
        link_value = next((link.get("href") or link.text for link in link_nodes
                           if link.get("rel", "alternate") == "alternate"), None)
        link = _source_link(source_url, link_value)
        native_id = child_text(node, "id") or child_text(node, "guid")
        body = child_text(node, "encoded") or child_text(node, "content") or child_text(node, "description") or child_text(node, "summary") or ""
        if "<" in body and ">" in body:
            body = _readable_html(body, link or source_url)["text"]
        authors = node.xpath("./*[local-name()='author']/*[local-name()='name']/text()")
        author = ", ".join(authors) or child_text(node, "author") or child_text(node, "creator")
        entries.append({"native_id": native_id, "title": child_text(node, "title"), "url": link,
                        "text": body, "author": author,
                        "published_at": child_text(node, "published") or child_text(node, "pubDate") or child_text(node, "date"),
                        "updated_at": child_text(node, "updated")})
    next_links = channel.xpath("./*[local-name()='link' and @rel='next']/@href")
    rendered = []
    for entry in entries:
        parts = [entry["title"], entry["url"], entry["author"], entry["published_at"], entry["text"]]
        rendered.append("\n".join(part for part in parts if part))
    return {"text": "\n\n".join(rendered), "title": title, "entries": entries,
            "entries_truncated": len(nodes) > MAX_FEED_ENTRIES,
            "entry_count_observed": len(nodes), "extraction_scope": "feed_entries",
            "next_url": _source_link(source_url, next_links[0]) if next_links else None,
            "discovered_links": [{"url": entry["url"], "title": entry["title"],
                                  "same_origin": _same_origin_url(entry["url"], source_url), "kind": "feed_entry"}
                                 for entry in entries if entry["url"]]}


def acquire_url(url: str, *, etag: str | None = None,
                last_modified: str | None = None, timeout: float = 25) -> dict:
    """Read one explicitly selected public URL; retain native time separately."""
    deadline = time.monotonic() + timeout
    normalized, _, _, _ = public_target(url)
    parts = urlsplit(normalized)
    robots_url = urlunsplit((parts.scheme, parts.netloc, "/robots.txt", "", ""))
    _, robots_status, robots_headers, robots_bytes = _read_public(robots_url, headers={}, deadline=deadline, max_bytes=256 * 1024)
    allow_url = None
    robots_sitemaps = []
    robots_sitemaps_truncated = False
    if robots_status == 200:
        robots_lines = robots_bytes.decode("utf-8", errors="replace").splitlines()
        policy = RobotFileParser()
        policy.parse(robots_lines)
        if not policy.can_fetch("OEVIRA", normalized):
            raise AcquisitionError("SOURCE_POLICY_DENIED", "The source does not permit this automated read.")
        allow_url = lambda target: policy.can_fetch("OEVIRA", target)
        robots_sitemaps, robots_sitemaps_truncated = _robots_sitemaps(robots_lines, normalized)
    elif robots_status in {401, 403, 429} or robots_status >= 500:
        raise AcquisitionError("SOURCE_POLICY_UNAVAILABLE", "The source access policy could not be confirmed.", status=robots_status,
                               retry_after=robots_headers.get("retry-after"))
    conditional = {}
    if etag:
        conditional["If-None-Match"] = etag
    if last_modified:
        conditional["If-Modified-Since"] = last_modified
    final_url, status, headers, content = _read_public(normalized, headers=conditional, deadline=deadline, allow_url=allow_url)
    captured_at = datetime.now(timezone.utc).isoformat()
    if status == 304:
        return {"status": "unchanged", "url": final_url, "captured_at": captured_at,
                "etag": headers.get("etag", etag), "last_modified": headers.get("last-modified", last_modified)}
    if status != 200:
        code = {401: "SOURCE_AUTH_REQUIRED", 403: "SOURCE_ACCESS_DENIED", 429: "SOURCE_RATE_LIMITED"}.get(status, "SOURCE_HTTP_ERROR")
        raise AcquisitionError(code, f"The source returned HTTP {status}.", status=status, retry_after=headers.get("retry-after"))
    if headers.get("content-encoding", "identity").lower() != "identity":
        raise AcquisitionError("SOURCE_ENCODING_UNSUPPORTED", "The source ignored the supported transfer-encoding request.")
    mime = headers.get("content-type", "").lower()
    if not any(kind in mime for kind in ("text/", "application/xhtml+xml", "application/xml", "application/rss+xml", "application/atom+xml")):
        raise AcquisitionError("SOURCE_FORMAT_UNSUPPORTED", "This source is not a supported textual page or feed.")
    charset = re.search(r"charset\s*=\s*[\"']?([\w-]+)", mime)
    try:
        decoded = content.decode(charset.group(1) if charset else "utf-8")
    except (LookupError, UnicodeDecodeError) as exc:
        raise AcquisitionError("SOURCE_TEXT_ENCODING", "The source text encoding could not be read reliably.") from exc
    extracted = {}
    metadata = {}
    if "html" in mime:
        extracted = _readable_html(decoded, final_url)
        metadata = extracted.pop("metadata")
        text, title = extracted["text"], extracted["title"]
    elif any(kind in mime for kind in ("application/xml", "text/xml", "application/rss+xml", "application/atom+xml")):
        extracted = _read_feed(content, final_url)
        text, title = extracted["text"], extracted["title"]
    else:
        text, title = decoded, final_url
    discovered_links = list(extracted.get("discovered_links", []))
    discovered_urls = {
        link["url"] for link in discovered_links
        if isinstance(link, dict) and isinstance(link.get("url"), str)
    }
    for candidate in robots_sitemaps:
        if candidate["url"] not in discovered_urls:
            discovered_links.append(candidate)
            discovered_urls.add(candidate["url"])
    extracted["discovered_links"] = discovered_links[:MAX_DISCOVERED_LINKS]
    extracted["links_truncated"] = bool(extracted.get("links_truncated")) or robots_sitemaps_truncated or len(discovered_links) > MAX_DISCOVERED_LINKS
    truncated = extracted.get("entries_truncated", False)
    if extracted.get("extraction_scope") in {"sitemap_urls", "sitemap_index"}:
        observed_entries = extracted.get("entry_count_observed", 0)
        parsed_entries = len(extracted.get("entries", []))
        if observed_entries == 0:
            reason = "This sitemap file listed zero locations; it does not establish that the host or other sitemap files contain no pages."
        elif observed_entries > MAX_FEED_ENTRIES:
            reason = f"This sitemap file exceeded the {MAX_FEED_ENTRIES}-entry read bound; listed URLs are only candidates and host coverage is unknown."
        elif parsed_entries < observed_entries:
            reason = f"{observed_entries - parsed_entries} sitemap entries had no usable URL; candidate coverage is incomplete."
        else:
            reason = None
    elif truncated:
        reason = "The feed has more entries than this bounded read; continue from its published pagination when available."
    else:
        reason = None if text.strip() else "The page yielded no readable text; browser rendering may be required."
    limitations = [
        "The captured page is a source observation, not independently verified fact.",
        "Source metadata timestamps are unverified; capture time does not replace publication time.",
        "Only declared primary static content or the observed feed entries are extracted; source bytes are preserved separately. Scripts, unloaded pages and embedded media are not claimed.",
    ]
    if robots_sitemaps or any(link.get("kind") == "declared_feed" for link in extracted.get("discovered_links", []) if isinstance(link, dict)):
        limitations.append("Discovered Feed and Sitemap URLs are candidates only. Acquisition never fetches a candidate until the user opts into the existing same-origin discovery path.")
    if extracted.get("extraction_scope") in {"sitemap_urls", "sitemap_index"}:
        limitations.append("Sitemap loc values identify candidate URLs; they are not captured page originals or proof that every host page is listed.")
    return {
        **extracted,
        "status": "ready" if text.strip() and not truncated else "partial", "reason": reason,
        "url": final_url, "requested_url": normalized, "title": title,
        "content": content, "text": text, "mime_type": mime,
        "captured_at": captured_at,
        "published_at": metadata.get("article:published_time") or metadata.get("date"),
        "published_at_origin": "source_metadata" if any(key in metadata for key in ("article:published_time", "date")) else None,
        "author": metadata.get("author"), "etag": headers.get("etag"),
        "last_modified": headers.get("last-modified"), "acquisition_method": "public_http",
        "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "limitations": limitations,
    }
