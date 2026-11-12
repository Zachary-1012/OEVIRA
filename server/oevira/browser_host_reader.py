"""Bounded, read-only DOM context extraction from the live browser host."""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any
from urllib.parse import parse_qsl, quote, quote_plus, unquote, urlsplit, urlunsplit

from flask import g, request

from .api import ApiError, _success
from .auth import utc_now
from .browser import BrowserError, MAX_CAPTURE_CHARACTERS, browser_safe_url


MAX_READER_FRAMES = 20
MAX_READER_NODES = 4096
MAX_READER_BLOCKS = 1000
MAX_READER_BLOCK_CHARACTERS = 2_000
# Reserve space for the two newlines between the maximum number of blocks.
MAX_READER_RAW_CHARACTERS = MAX_CAPTURE_CHARACTERS - (2 * (MAX_READER_BLOCKS - 1))
MAX_READER_LINKS = 200
MAX_READER_LINK_TEXT_CHARACTERS = 10_000
MAX_READER_CONTROLS = 200
MAX_READER_CONTROL_CHARACTERS = 10_000
MAX_READER_CONTROL_LABEL_NODES = 512
MAX_READER_SELECTION_CHARACTERS = 10_000
FRAME_LOCATOR_TIMEOUT_MS = 2_000
READER_COMMAND_TIMEOUT_SECONDS = 45
SELECTION_CONTEXT_CHARACTERS = 160
_SENSITIVE_QUERY_NAME = re.compile(
    r"(?:token|secret|password|passwd|credential|session|cookie|auth|code|nonce|signature|sig)$", re.I,
)
_SENSITIVE_PATH_SEGMENT = re.compile(r"^(?:token|secret|password|passwd|credential|session|cookie|nonce|signature|sig)$", re.I)
_SENSITIVE_PATH_PAIR = re.compile(
    r"^(?:token|secret|password|passwd|credential|session|cookie|nonce|signature|sig)=([^/]*)$", re.I,
)


_READABLE_CONTEXT_SCRIPT = r"""
(body, limits) => {
    if (!body) {
        return {
            status: "body_missing", title: document.title || "", blocks: [], links: [],
            nodes_examined: 0, characters_examined: 0, links_examined: 0,
            nodes_truncated: false, text_truncated: false, links_truncated: false,
            selected_text: "", selection_truncated: false, selection_protected: false
        };
    }

    const maxNodes = Math.max(0, Number(limits.max_nodes) || 0);
    const maxCharacters = Math.max(0, Number(limits.max_characters) || 0);
    const maxBlocks = Math.max(0, Number(limits.max_blocks) || 0);
    const maxBlockCharacters = Math.max(0, Number(limits.max_block_characters) || 0);
    const maxLinks = Math.max(0, Number(limits.max_links) || 0);
    const maxLinkCharacters = Math.max(0, Number(limits.max_link_characters) || 0);
    const maxControls = Math.max(0, Number(limits.max_controls) || 0);
    const maxControlCharacters = Math.max(0, Number(limits.max_control_characters) || 0);
    const maxControlLabelNodes = Math.max(0, Number(limits.max_control_label_nodes) || 0);
    const maxSelectionCharacters = Math.max(0, Number(limits.max_selection_characters) || 0);
    const excludedTags = new Set([
        "script", "style", "noscript", "template", "svg", "canvas", "iframe", "object", "embed"
    ]);
    const valueControlTags = new Set(["input", "textarea", "select", "option"]);
    const controlTags = new Set(["input", "textarea", "select", "button"]);
    const blockTags = new Set([
        "body", "main", "article", "section", "header", "footer", "nav", "div",
        "h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "blockquote", "button",
        "td", "th", "pre", "figcaption", "dt", "dd", "summary"
    ]);
    let nodesExamined = 0;
    let charactersExamined = 0;
    let linksExamined = 0;
    let linkCharacters = 0;
    let controlsExamined = 0;
    let controlCharacters = 0;
    let controlLabelNodesExamined = 0;
    let nodesTruncated = false;
    let textTruncated = false;
    let blocksTruncated = false;
    let blockTextTruncated = false;
    let controlsTruncated = false;
    let linksTruncated = false;
    const blocks = new Map();
    const links = [];
    const linkByElement = new Map();
    const controls = [];
    const visibilityCache = new WeakMap();

    function visibleChain(element) {
        const chain = [];
        for (let current = element; current; current = current.parentElement) {
            const cached = visibilityCache.get(current);
            if (cached === false) {
                for (const ancestor of chain) visibilityCache.set(ancestor, false);
                return false;
            }
            if (cached === true) break;
            chain.push(current);
            if (current.hidden || current.getAttribute("aria-hidden") === "true" || current.isContentEditable) {
                for (const ancestor of chain) visibilityCache.set(ancestor, false);
                return false;
            }
            const tag = current.tagName ? current.tagName.toLowerCase() : "";
            if (excludedTags.has(tag)) {
                for (const ancestor of chain) visibilityCache.set(ancestor, false);
                return false;
            }
            const style = window.getComputedStyle(current);
            if (style.display === "none" || style.visibility === "hidden" || style.visibility === "collapse" || style.opacity === "0") {
                for (const ancestor of chain) visibilityCache.set(ancestor, false);
                return false;
            }
            if (current === body) break;
        }
        for (const ancestor of chain) visibilityCache.set(ancestor, true);
        return true;
    }

    function cssPath(element) {
        const parts = [];
        for (let current = element; current && current !== body; current = current.parentElement) {
            const tag = current.tagName.toLowerCase();
            let index = 1;
            for (let sibling = current.previousElementSibling; sibling; sibling = sibling.previousElementSibling) {
                if (sibling.tagName === current.tagName) index += 1;
            }
            parts.unshift(tag + ":nth-of-type(" + index + ")");
        }
        return parts.length ? "body/" + parts.join("/") : "body";
    }

    function bounds(element) {
        const rect = element.getBoundingClientRect();
        return {
            x: Math.round(rect.x * 100) / 100,
            y: Math.round(rect.y * 100) / 100,
            width: Math.round(rect.width * 100) / 100,
            height: Math.round(rect.height * 100) / 100
        };
    }

    function withinValueControl(element, stopAt) {
        for (let current = element; current && current !== stopAt; current = current.parentElement) {
            const tag = current.tagName ? current.tagName.toLowerCase() : "";
            if (valueControlTags.has(tag) || current.isContentEditable) return true;
        }
        return false;
    }

    function labelText(element) {
        const ariaLabel = element.getAttribute("aria-label") || "";
        if (ariaLabel) {
            const available = maxControlCharacters - controlCharacters;
            const value = Array.from(ariaLabel).slice(0, Math.max(0, available)).join("");
            controlCharacters += Array.from(value).length;
            if (value.length < ariaLabel.length) controlsTruncated = true;
            return value;
        }
        const placeholder = element.getAttribute("placeholder") || "";
        const labels = element.labels ? Array.from(element.labels).slice(0, 4) : [];
        const parts = [];
        for (const label of labels) {
            const walker = document.createTreeWalker(label, NodeFilter.SHOW_TEXT);
            let node;
            while ((node = walker.nextNode())) {
                if (controlLabelNodesExamined >= maxControlLabelNodes) {
                    controlsTruncated = true;
                    break;
                }
                controlLabelNodesExamined += 1;
                if (withinValueControl(node.parentElement, label)) continue;
                const value = node.nodeValue || "";
                for (const character of value) {
                    if (controlCharacters >= maxControlCharacters) {
                        controlsTruncated = true;
                        break;
                    }
                    parts.push(character);
                    controlCharacters += 1;
                }
                if (controlCharacters >= maxControlCharacters) break;
            }
            if (controlCharacters >= maxControlCharacters || controlLabelNodesExamined >= maxControlLabelNodes) break;
        }
        const label = parts.join("").replace(/\s+/g, " ").trim();
        if (label) return label;
        if (placeholder && controlCharacters < maxControlCharacters) {
            const available = maxControlCharacters - controlCharacters;
            const value = Array.from(placeholder).slice(0, available).join("");
            controlCharacters += Array.from(value).length;
            if (Array.from(value).length < Array.from(placeholder).length) controlsTruncated = true;
            return value;
        }
        return "";
    }

    function ensureControl(element) {
        const tag = element.tagName ? element.tagName.toLowerCase() : "";
        const type = tag === "input" ? (element.getAttribute("type") || "text").toLowerCase() : tag;
        if (tag === "input" && type === "hidden") return;
        if (!visibleChain(element)) return;
        if (controls.length >= maxControls) {
            controlsTruncated = true;
            return;
        }
        const explicitRole = element.getAttribute("role");
        const inputRoles = {
            checkbox: "checkbox", radio: "radio", button: "button", submit: "button", reset: "button",
            range: "slider", number: "spinbutton", search: "searchbox", email: "textbox", password: "textbox"
        };
        const implicitRole = tag === "input" ? (inputRoles[type] || "textbox")
            : tag === "textarea" ? "textbox"
            : tag === "select" ? (element.multiple ? "listbox" : "combobox")
            : "button";
        const label = labelText(element);
        controls.push({
            locator: cssPath(element),
            tag,
            type: type.slice(0, 30),
            role: (explicitRole || implicitRole).slice(0, 80),
            label: Array.from(label).slice(0, 200).join(""),
            disabled: Boolean(element.disabled),
            bounds: bounds(element)
        });
        controlsExamined += 1;
    }

    function nearestBlock(element) {
        for (let current = element; current && current !== body.parentElement; current = current.parentElement) {
            if (current.tagName && blockTags.has(current.tagName.toLowerCase())) return current;
        }
        return body;
    }

    function ensureBlock(element) {
        let block = blocks.get(element);
        if (block) return block;
        const tag = element.tagName ? element.tagName.toLowerCase() : "body";
        const explicitRole = element.getAttribute ? element.getAttribute("role") : null;
        const implicitRoles = {
            a: element.hasAttribute && element.hasAttribute("href") ? "link" : null,
            article: "article", blockquote: "blockquote", button: "button",
            h1: "heading", h2: "heading", h3: "heading", h4: "heading", h5: "heading", h6: "heading",
            header: element.parentElement === body ? "banner" : null,
            footer: element.parentElement === body ? "contentinfo" : null,
            li: "listitem", main: "main", nav: "navigation", ol: "list", p: "paragraph",
            section: element.getAttribute && (element.getAttribute("aria-label") || element.getAttribute("aria-labelledby")) ? "region" : null,
            table: "table", td: "cell", th: element.getAttribute("scope") === "row" ? "rowheader" : "columnheader", ul: "list"
        };
        const role = explicitRole || implicitRoles[tag] || null;
        const label = element.getAttribute ? element.getAttribute("aria-label") : null;
        const level = /^h[1-6]$/.test(tag) ? Number(tag.slice(1)) : null;
        block = {
            path: cssPath(element), tag, role: role ? role.slice(0, 80) : null,
            aria_label: label ? label.slice(0, 300) : null,
            heading_level: level, bounds: bounds(element), parts: [], characters: 0
        };
        if (blocks.size >= maxBlocks) {
            blocksTruncated = true;
            throw new Error("__OEVIRA_BLOCK_LIMIT__");
        }
        blocks.set(element, block);
        return block;
    }

    function ensureLink(element) {
        let link = linkByElement.get(element);
        if (link) return link;
        if (links.length >= maxLinks) {
            linksTruncated = true;
            return null;
        }
        const href = element.href || element.getAttribute("href") || "";
        if (!href || href.length > 4096) {
            linksTruncated = true;
            return null;
        }
        link = {
            path: cssPath(element), href,
            title: (element.getAttribute("title") || "").slice(0, 300),
            text_parts: [], text_characters: 0,
            bounds: bounds(element)
        };
        linkByElement.set(element, link);
        links.push(link);
        linksExamined += 1;
        return link;
    }

    const filter = {
        acceptNode(node) {
            nodesExamined += 1;
            if (nodesExamined > maxNodes) {
                nodesTruncated = true;
                throw new Error("__OEVIRA_NODE_LIMIT__");
            }
            if (node.nodeType === Node.ELEMENT_NODE) {
                const tag = node.tagName.toLowerCase();
                if (controlTags.has(tag)) {
                    ensureControl(node);
                    return tag === "button" ? NodeFilter.FILTER_SKIP : NodeFilter.FILTER_REJECT;
                }
                const role = (node.getAttribute("role") || "").toLowerCase();
                if (["checkbox", "radio", "switch", "combobox", "listbox", "textbox", "searchbox", "slider", "spinbutton"].includes(role)) {
                    ensureControl(node);
                }
                if (excludedTags.has(tag) || !visibleChain(node)) return NodeFilter.FILTER_REJECT;
                return tag === "a" ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_SKIP;
            }
            if (node.nodeType === Node.TEXT_NODE) {
                if (!node.nodeValue || !node.nodeValue.trim() || withinValueControl(node.parentElement, body) || !visibleChain(node.parentElement)) {
                    return NodeFilter.FILTER_REJECT;
                }
                return NodeFilter.FILTER_ACCEPT;
            }
            return NodeFilter.FILTER_REJECT;
        }
    };

    const walker = document.createTreeWalker(body, NodeFilter.SHOW_ELEMENT | NodeFilter.SHOW_TEXT, filter);
    try {
        let node;
        while ((node = walker.nextNode())) {
            if (node.nodeType === Node.ELEMENT_NODE) {
                ensureLink(node);
                continue;
            }
            const blockElement = nearestBlock(node.parentElement);
            const block = ensureBlock(blockElement);
            const anchor = node.parentElement ? node.parentElement.closest("a") : null;
            const link = anchor ? linkByElement.get(anchor) : null;
            for (const character of node.nodeValue) {
                if (block.characters >= maxBlockCharacters) {
                    blockTextTruncated = true;
                    block.truncated = true;
                    break;
                }
                if (charactersExamined >= maxCharacters) {
                    textTruncated = true;
                    block.truncated = true;
                    throw new Error("__OEVIRA_TEXT_LIMIT__");
                }
                block.parts.push(character);
                block.characters += 1;
                charactersExamined += 1;
                if (link && linkCharacters < maxLinkCharacters && link.text_characters < 300) {
                    link.text_parts.push(character);
                    link.text_characters += 1;
                    linkCharacters += 1;
                } else if (link) {
                    linksTruncated = true;
                }
            }
        }
    } catch (error) {
        if (error && error.message === "__OEVIRA_NODE_LIMIT__") nodesTruncated = true;
        else if (error && error.message === "__OEVIRA_TEXT_LIMIT__") textTruncated = true;
        else if (error && error.message === "__OEVIRA_BLOCK_LIMIT__") blocksTruncated = true;
        else throw error;
    }

    const selection = window.getSelection();
    let selectionProtected = false;
    let selectedText = "";
    let selectionTruncated = false;
    if (selection && selection.toString()) {
        const protectedNode = node => {
            let element = node && node.nodeType === Node.ELEMENT_NODE ? node : (node ? node.parentElement : null);
            for (; element && element !== body; element = element.parentElement) {
                const tag = element.tagName ? element.tagName.toLowerCase() : "";
                if (excludedTags.has(tag) || element.isContentEditable || element.hidden || element.getAttribute("aria-hidden") === "true") return true;
            }
            return false;
        };
        selectionProtected = protectedNode(selection.anchorNode) || protectedNode(selection.focusNode);
        if (!selectionProtected) {
            let count = 0;
            const selectedParts = [];
            for (const character of selection.toString()) {
                if (count >= maxSelectionCharacters) {
                    selectionTruncated = true;
                    break;
                }
                selectedParts.push(character);
                count += 1;
            }
            selectedText = selectedParts.join("");
        }
    }

    return {
        status: blocks.size ? "read" : (blocksTruncated ? "partial" : "empty"),
        title: (document.title || "").slice(0, 500),
        blocks: Array.from(blocks.values()).map(block => ({
            path: block.path, tag: block.tag, role: block.role,
            aria_label: block.aria_label, heading_level: block.heading_level,
            bounds: block.bounds, text: block.parts.join(""), truncated: Boolean(block.truncated)
        })),
        links: links.map(link => ({
            path: link.path, href: link.href, title: link.title,
            text: link.text_parts.join(""), bounds: link.bounds
        })),
        controls,
        nodes_examined: nodesExamined,
        characters_examined: charactersExamined,
        links_examined: linksExamined,
        controls_examined: controlsExamined,
        control_characters_examined: controlCharacters,
        control_label_nodes_examined: controlLabelNodesExamined,
        nodes_truncated: nodesTruncated,
        text_truncated: textTruncated,
        blocks_truncated: blocksTruncated,
        block_text_truncated: blockTextTruncated,
        controls_truncated: controlsTruncated,
        blocks_examined: blocks.size,
        links_truncated: linksTruncated,
        selected_text: selectedText,
        selection_truncated: selectionTruncated,
        selection_protected: selectionProtected
    };
}
"""


def _safe_browser_url(value: str) -> tuple[str, list[str], str]:
    if not isinstance(value, str) or len(value) > 4096:
        return "about:blank", [], "too_long"
    try:
        parsed = urlsplit(value)
    except ValueError:
        return "about:blank", [], "unavailable"
    if parsed.scheme == "about" and parsed.path in {"blank", "srcdoc"}:
        return f"about:{parsed.path}", [], "available"
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
        return "about:blank", [], "non_http_url"
    try:
        safe, redacted = browser_safe_url(value)
    except BrowserError:
        return "about:blank", [], "unavailable"
    safe_parts = urlsplit(safe)
    path_segments = safe_parts.path.split("/")
    path_redacted = False
    for index, segment in enumerate(path_segments):
        decoded = unquote(segment)
        pair = _SENSITIVE_PATH_PAIR.fullmatch(decoded)
        if pair:
            key = decoded.split("=", 1)[0]
            path_segments[index] = quote(f"{key}=[redacted]", safe="")
            redacted.append(key[:80])
            path_redacted = True
        elif (
            _SENSITIVE_PATH_SEGMENT.fullmatch(decoded)
            and index + 1 < len(path_segments)
            and len(unquote(path_segments[index + 1])) >= 12
        ):
            path_segments[index + 1] = quote("[redacted]", safe="")
            redacted.append(decoded[:80])
            path_redacted = True
    if path_redacted:
        safe = urlunsplit((safe_parts.scheme, safe_parts.netloc, "/".join(path_segments), safe_parts.query, ""))
    return safe, redacted, "redacted" if redacted else "available"


def _safe_display_text(value: str, raw_urls: str | list[str], maximum: int) -> str:
    if not isinstance(value, str):
        return ""
    result = "".join(character for character in value if character.isprintable())
    urls = [raw_urls] if isinstance(raw_urls, str) else raw_urls
    secrets = set()
    for raw_url in urls:
        try:
            parsed = urlsplit(raw_url)
            query = parse_qsl(parsed.query, keep_blank_values=True)
        except (TypeError, ValueError):
            parsed = None
            query = []
        secrets.update(secret for name, secret in query if secret and _SENSITIVE_QUERY_NAME.search(name))
        if parsed is not None:
            path_segments = [unquote(segment) for segment in parsed.path.split("/")]
            for index, segment in enumerate(path_segments):
                pair = _SENSITIVE_PATH_PAIR.fullmatch(segment)
                if pair:
                    if pair.group(1):
                        secrets.add(pair.group(1))
                elif (
                    _SENSITIVE_PATH_SEGMENT.fullmatch(segment)
                    and index + 1 < len(path_segments)
                    and len(path_segments[index + 1]) >= 12
                ):
                    secrets.add(path_segments[index + 1])
    for secret in sorted(secrets, key=len, reverse=True):
        for candidate in {secret, quote(secret, safe=""), quote_plus(secret)}:
            if candidate:
                result = result.replace(candidate, "█" * len(candidate))
    return result[:maximum]


def _frame_bounds(frame) -> tuple[dict | None, str]:
    if frame.parent_frame is None:
        return None, "main_frame"
    try:
        current = frame
        bounds = None
        while current.parent_frame is not None:
            element = current.frame_element()
            bounds = element.bounding_box()
            if not element.is_visible() or not bounds or bounds["width"] <= 0 or bounds["height"] <= 0:
                return bounds, "not_visible"
            current = current.parent_frame
        return bounds, "visible"
    except Exception:
        return None, "unavailable"


def _frame_id_map(frames: list[Any], main_frame) -> dict[int, str]:
    result = {id(main_frame): "main"}
    for frame in frames[:MAX_READER_FRAMES]:
        if frame is main_frame:
            continue
        try:
            parent = frame.parent_frame
            parent_id = result.get(id(parent), "main")
            siblings = parent.child_frames if parent is not None else []
            index = siblings.index(frame) + 1 if frame in siblings else 1
            result[id(frame)] = f"{parent_id}/iframe:{index}"
        except Exception:
            result[id(frame)] = f"frame:{len(result)}"
    return result


def _reader_text_and_blocks(
    frame_id: str,
    raw_blocks: list[dict],
    raw_urls: str | list[str],
    *,
    max_characters: int,
    max_blocks: int,
) -> tuple[str, list[dict], bool]:
    pieces = []
    blocks = []
    offset = 0
    truncated = False
    for raw in raw_blocks:
        if len(blocks) >= max_blocks:
            truncated = True
            break
        source_text = _safe_display_text(raw.get("text", ""), raw_urls, max_characters)
        text = " ".join(source_text.split())
        if not text:
            continue
        separator = "\n\n" if pieces else ""
        remaining = max_characters - offset - len(separator)
        if remaining <= 0:
            truncated = True
            break
        block_truncated = bool(raw.get("truncated"))
        if len(text) > remaining:
            text = text[:remaining]
            truncated = True
            block_truncated = True
        start = offset + len(separator)
        end = start + len(text)
        block = {
            "block_id": f"{frame_id}/{raw.get('path', 'body')}",
            "frame_id": frame_id,
            "tag": raw.get("tag") if isinstance(raw.get("tag"), str) else None,
            "role": _safe_display_text(raw.get("role", ""), raw_urls, 80) or None,
            "aria_label": _safe_display_text(raw.get("aria_label", ""), raw_urls, 300) or None,
            "heading_level": raw.get("heading_level"),
            "bounds": raw.get("bounds"),
            "coordinate_space": "frame_viewport_css_px",
            "text": text,
            "truncated": block_truncated,
            "start_offset": start,
            "end_offset": end,
        }
        blocks.append(block)
        pieces.append(separator + text)
        offset = end
    return "".join(pieces), blocks, truncated


def _normalized_with_offsets(text: str) -> tuple[str, list[tuple[int, int]]]:
    characters = []
    spans = []
    index = 0
    while index < len(text):
        if text[index].isspace():
            start = index
            while index < len(text) and text[index].isspace():
                index += 1
            if characters and index < len(text):
                characters.append(" ")
                spans.append((start, index))
            continue
        characters.append(text[index])
        spans.append((index, index + 1))
        index += 1
    return "".join(characters), spans


def _selection_context(frame_text: str, selected: str, *, truncated: bool, protected: bool) -> dict:
    base = {"quote": "", "quote_sha256": None, "start_offset": None, "end_offset": None,
            "context_before": "", "context_after": "", "offset_unit": "unicode_codepoints",
            "location_state": "not_selected"}
    if protected:
        return {**base, "location_state": "protected_content"}
    if not selected:
        return base
    if truncated:
        return {**base, "quote": selected, "location_state": "selection_truncated"}
    normalized_text, spans = _normalized_with_offsets(frame_text)
    normalized_quote = " ".join(selected.split())
    if not normalized_quote:
        return base
    positions = []
    start = normalized_text.find(normalized_quote)
    while start >= 0:
        positions.append(start)
        if len(positions) > 1:
            break
        start = normalized_text.find(normalized_quote, start + 1)
    if len(positions) != 1:
        return {**base, "quote": selected, "quote_sha256": hashlib.sha256(selected.encode("utf-8")).hexdigest(),
                "location_state": "ambiguous_or_unresolved" if positions else "outside_captured_snapshot"}
    normalized_start = positions[0]
    normalized_end = normalized_start + len(normalized_quote)
    source_start = spans[normalized_start][0]
    source_end = spans[normalized_end - 1][1]
    quote = frame_text[source_start:source_end]
    return {
        "quote": quote,
        "quote_sha256": hashlib.sha256(quote.encode("utf-8")).hexdigest(),
        "start_offset": source_start,
        "end_offset": source_end,
        "context_before": frame_text[max(0, source_start - SELECTION_CONTEXT_CHARACTERS):source_start],
        "context_after": frame_text[source_end:source_end + SELECTION_CONTEXT_CHARACTERS],
        "offset_unit": "unicode_codepoints",
        "location_state": "unique_reader_snapshot_match",
    }


def _safe_reader_link(frame_id: str, link: dict, raw_frame_url: str) -> dict | None:
    url, redacted, state = _safe_browser_url(link.get("href", ""))
    if state == "non_http_url" or url == "about:blank":
        return None
    return {
        "frame_id": frame_id,
        "path": link.get("path"),
        "url": url,
        "redacted_query_keys": redacted,
        "url_state": state,
        "title": _safe_display_text(link.get("title", ""), [raw_frame_url, link.get("href", "")], 300),
        "text": _safe_display_text(link.get("text", ""), [raw_frame_url, link.get("href", "")], 500),
        "bounds": link.get("bounds"),
        "coordinate_space": "frame_viewport_css_px",
    }


def _safe_reader_control(frame_id: str, control: dict, raw_urls: list[str]) -> dict | None:
    locator = control.get("locator")
    if not isinstance(locator, str) or not locator.startswith("body/") or len(locator) > 2_000:
        return None
    tag = control.get("tag")
    if not isinstance(tag, str) or tag not in {"input", "textarea", "select", "button", "div", "span"}:
        return None
    return {
        "control_id": f"{frame_id}/{locator}",
        "frame_id": frame_id,
        "locator": locator,
        "tag": tag,
        "type": _safe_display_text(control.get("type", ""), raw_urls, 30),
        "role": _safe_display_text(control.get("role", ""), raw_urls, 80) or None,
        "label": _safe_display_text(control.get("label", ""), raw_urls, 200),
        "disabled": bool(control.get("disabled")),
        "bounds": control.get("bounds"),
        "coordinate_space": "frame_viewport_css_px",
    }


def _read_browser_context(runtime, engine: str, session_id: str, page_id: str | None,
                          *, include_frames: bool, include_storage: bool, before_dispatch=None):
    """Read bounded DOM context on the owning page thread, optionally with protected-save state."""
    if page_id is not None and (not isinstance(page_id, str) or not page_id or len(page_id) > 80):
        raise BrowserError("BROWSER_READ_INVALID", 400)
    if not isinstance(include_frames, bool):
        raise BrowserError("BROWSER_READ_INVALID", 400)
    requested_page_id = page_id

    def operation(worker, live):
        if before_dispatch is not None:
            try:
                before_dispatch()
            except BrowserError as exc:
                exc.details = {**exc.details, "dispatch_state": "not_started"}
                raise
        state = worker._page(live, requested_page_id)
        page_id = state.page_id
        page = state.page
        start = worker._snapshot(live, state.page_id, include_text=False)
        all_frames = list(page.frames)
        main_frame = page.main_frame
        candidates = all_frames if include_frames else [main_frame]
        frames_to_read = candidates[:MAX_READER_FRAMES]
        frame_ids = _frame_id_map(all_frames, main_frame)
        remaining_nodes = MAX_READER_NODES
        remaining_characters = MAX_READER_RAW_CHARACTERS
        remaining_blocks = MAX_READER_BLOCKS
        returned_characters = 0
        remaining_links = MAX_READER_LINKS
        remaining_link_characters = MAX_READER_LINK_TEXT_CHARACTERS
        remaining_controls = MAX_READER_CONTROLS
        remaining_control_characters = MAX_READER_CONTROL_CHARACTERS
        remaining_control_label_nodes = MAX_READER_CONTROL_LABEL_NODES
        remaining_selection_characters = MAX_READER_SELECTION_CHARACTERS
        frame_results = []
        all_links = []
        all_controls = []
        nodes_examined = 0
        characters_examined = 0
        controls_examined = 0
        control_characters_examined = 0
        control_label_nodes_examined = 0
        hidden_frames = 0
        failed_frames = 0
        timed_out_frames = 0
        omitted_frames = max(0, len(all_frames) - len(frames_to_read))
        frames_not_requested = max(0, len(all_frames) - 1) if not include_frames else 0
        frame_start_urls: dict[str, str] = {}
        any_truncated = False

        for frame in frames_to_read:
            frame_id = frame_ids.get(id(frame), "main" if frame is main_frame else f"frame:{len(frame_results)}")
            if frame.is_detached():
                failed_frames += 1
                frame_results.append({"frame_id": frame_id, "parent_frame_id": None,
                                      "url": "about:blank", "source_url": "about:blank", "title": "", "status": "detached",
                                      "coverage": {"error_code": "BROWSER_FRAME_DETACHED"},
                                      "text": "", "text_sha256": hashlib.sha256(b"").hexdigest(), "blocks": [],
                                      "selection": _selection_context("", "", truncated=False, protected=False)})
                continue
            raw_url = frame.url or "about:blank"
            safe_url, redacted_query_keys, url_state = _safe_browser_url(raw_url)
            frame_start_urls[frame_id] = raw_url
            parent = frame.parent_frame
            parent_frame_id = frame_ids.get(id(parent)) if parent is not None else None
            frame_bounds, visibility = _frame_bounds(frame)
            if visibility == "not_visible":
                hidden_frames += 1
                frame_results.append({"frame_id": frame_id, "parent_frame_id": parent_frame_id,
                                      "url": safe_url, "redacted_query_keys": redacted_query_keys,
                                      "source_url": safe_url,
                                      "url_state": url_state, "title": "", "status": "not_visible",
                                      "bounds": frame_bounds, "bounds_coordinate_space": "main_frame_viewport_css_px",
                                      "coverage": {"scope": "visible_frames_only"},
                                      "text": "", "text_sha256": hashlib.sha256(b"").hexdigest(), "blocks": [],
                                      "selection": _selection_context("", "", truncated=False, protected=False)})
                continue
            if visibility == "unavailable":
                failed_frames += 1
                frame_results.append({"frame_id": frame_id, "parent_frame_id": parent_frame_id,
                                      "url": safe_url, "redacted_query_keys": redacted_query_keys,
                                      "source_url": safe_url,
                                      "url_state": url_state, "title": "", "status": "unavailable",
                                      "bounds": frame_bounds, "bounds_coordinate_space": "main_frame_viewport_css_px",
                                      "coverage": {"error_code": "BROWSER_FRAME_UNAVAILABLE"},
                                      "text": "", "text_sha256": hashlib.sha256(b"").hexdigest(), "blocks": [],
                                      "selection": _selection_context("", "", truncated=False, protected=False)})
                continue
            if remaining_nodes <= 0 or remaining_characters <= 0 or remaining_blocks <= 0:
                omitted_frames += 1
                any_truncated = True
                raw_url = frame.url or "about:blank"
                safe_url, redacted_query_keys, url_state = _safe_browser_url(raw_url)
                parent = frame.parent_frame
                parent_frame_id = frame_ids.get(id(parent)) if parent is not None else None
                frame_results.append({
                    "frame_id": frame_id,
                    "parent_frame_id": parent_frame_id,
                    "url": safe_url,
                    "source_url": safe_url,
                    "redacted_query_keys": redacted_query_keys,
                    "url_state": url_state,
                    "title": "",
                    "status": "omitted_budget",
                    "bounds": frame_bounds,
                    "bounds_coordinate_space": "main_frame_viewport_css_px",
                    "coverage": {"error_code": "BROWSER_FRAME_BUDGET_EXHAUSTED"},
                    "text": "",
                    "text_sha256": hashlib.sha256(b"").hexdigest(),
                    "blocks": [],
                    "selection": _selection_context("", "", truncated=False, protected=False),
                })
                continue
            try:
                raw = frame.locator("body").evaluate(
                    _READABLE_CONTEXT_SCRIPT,
                    {
                        "max_nodes": remaining_nodes,
                        "max_characters": remaining_characters,
                        "max_blocks": remaining_blocks,
                        "max_block_characters": MAX_READER_BLOCK_CHARACTERS,
                        "max_links": remaining_links,
                        "max_link_characters": remaining_link_characters,
                        "max_controls": remaining_controls,
                        "max_control_characters": remaining_control_characters,
                        "max_control_label_nodes": remaining_control_label_nodes,
                        "max_selection_characters": min(remaining_selection_characters, remaining_characters),
                    },
                    timeout=FRAME_LOCATOR_TIMEOUT_MS,
                )
                if not isinstance(raw, dict):
                    raise ValueError("reader script returned an invalid result")
            except Exception as exc:
                error_type = type(exc).__name__
                is_timeout = "Timeout" in error_type
                if is_timeout:
                    timed_out_frames += 1
                else:
                    failed_frames += 1
                frame_results.append({"frame_id": frame_id, "parent_frame_id": parent_frame_id,
                                      "url": safe_url, "redacted_query_keys": redacted_query_keys,
                                      "source_url": safe_url,
                                      "url_state": url_state, "title": "", "status": "timeout" if is_timeout else "unavailable",
                                      "bounds": frame_bounds, "bounds_coordinate_space": "main_frame_viewport_css_px",
                                      "coverage": {"error_code": "BROWSER_FRAME_READ_TIMEOUT" if is_timeout else "BROWSER_FRAME_READ_UNAVAILABLE"},
                                      "text": "", "text_sha256": hashlib.sha256(b"").hexdigest(), "blocks": [],
                                      "selection": _selection_context("", "", truncated=False, protected=False)})
                continue

            if raw.get("status") == "body_missing":
                failed_frames += 1
                frame_results.append({"frame_id": frame_id, "parent_frame_id": parent_frame_id,
                                      "url": safe_url, "redacted_query_keys": redacted_query_keys,
                                      "source_url": safe_url,
                                      "url_state": url_state, "title": "", "status": "body_missing",
                                      "bounds": frame_bounds, "bounds_coordinate_space": "main_frame_viewport_css_px",
                                      "coverage": {"error_code": "BROWSER_FRAME_BODY_MISSING"},
                                      "text": "", "text_sha256": hashlib.sha256(b"").hexdigest(), "blocks": [],
                                      "selection": _selection_context("", "", truncated=False, protected=False)})
                continue

            raw_links = raw.get("links", []) if isinstance(raw.get("links"), list) else []
            redaction_urls = [raw_url] + [
                link.get("href", "") for link in raw_links if isinstance(link, dict) and isinstance(link.get("href"), str)
            ]
            output_budget = max(
                0,
                MAX_CAPTURE_CHARACTERS - returned_characters - (2 * max(0, remaining_blocks - 1)),
            )
            frame_text, blocks, python_truncated = _reader_text_and_blocks(
                frame_id,
                raw.get("blocks", []),
                redaction_urls,
                max_characters=output_budget,
                max_blocks=remaining_blocks,
            )
            selection_text = raw.get("selected_text", "") if isinstance(raw.get("selected_text"), str) else ""
            selection_text = _safe_display_text(selection_text, redaction_urls, MAX_READER_SELECTION_CHARACTERS)
            selection_characters_used = min(len(selection_text), remaining_selection_characters)
            remaining_selection_characters -= selection_characters_used
            if selection_characters_used < len(selection_text):
                selection_text = selection_text[:selection_characters_used]
                raw["selection_truncated"] = True
            frame_title = _safe_display_text(raw.get("title", ""), raw_url, 500)
            frame_digest = hashlib.sha256(frame_text.encode("utf-8")).hexdigest()
            for block in blocks:
                block["page_revision"] = start["page_revision"]
                block["snapshot_sha256"] = frame_digest
            selection = _selection_context(
                frame_text,
                selection_text,
                truncated=bool(raw.get("selection_truncated")),
                protected=bool(raw.get("selection_protected")),
            )
            selection.update({
                "frame_id": frame_id,
                "page_revision": start["page_revision"],
                "snapshot_sha256": frame_digest,
            })
            safe_links = [item for item in (_safe_reader_link(frame_id, link, raw_url) for link in raw_links if isinstance(link, dict)) if item]
            raw_controls = raw.get("controls", []) if isinstance(raw.get("controls"), list) else []
            safe_controls = [
                item for item in (_safe_reader_control(frame_id, control, redaction_urls)
                                  for control in raw_controls if isinstance(control, dict)) if item
            ]
            for link in safe_links:
                link["page_revision"] = start["page_revision"]
                link["snapshot_sha256"] = frame_digest
            for control in safe_controls:
                control["page_revision"] = start["page_revision"]
                control["snapshot_sha256"] = frame_digest
            all_links.extend(safe_links)
            all_controls.extend(safe_controls)
            nodes_used = min(int(raw.get("nodes_examined", 0) or 0), remaining_nodes)
            characters_used = min(int(raw.get("characters_examined", 0) or 0), remaining_characters)
            blocks_used = min(int(raw.get("blocks_examined", len(raw.get("blocks", []))) or 0), remaining_blocks)
            controls_used = min(int(raw.get("controls_examined", len(raw_controls)) or 0), remaining_controls)
            control_characters_used = min(int(raw.get("control_characters_examined", 0) or 0), remaining_control_characters)
            control_label_nodes_used = min(int(raw.get("control_label_nodes_examined", 0) or 0), remaining_control_label_nodes)
            remaining_nodes -= nodes_used
            remaining_characters -= characters_used
            remaining_blocks -= blocks_used
            remaining_links = max(0, remaining_links - int(raw.get("links_examined", 0) or 0))
            remaining_link_characters = max(0, remaining_link_characters - sum(len(item["text"]) for item in safe_links))
            remaining_controls -= controls_used
            remaining_control_characters -= control_characters_used
            remaining_control_label_nodes -= control_label_nodes_used
            nodes_examined += nodes_used
            characters_examined += characters_used
            controls_examined += controls_used
            control_characters_examined += control_characters_used
            control_label_nodes_examined += control_label_nodes_used
            returned_characters += len(frame_text)
            frame_truncated = bool(
                raw.get("nodes_truncated") or raw.get("text_truncated") or raw.get("blocks_truncated")
                or raw.get("block_text_truncated") or raw.get("links_truncated") or python_truncated
                or raw.get("selection_truncated") or raw.get("controls_truncated")
            )
            any_truncated = any_truncated or frame_truncated
            frame_result = {
                "frame_id": frame_id,
                "parent_frame_id": parent_frame_id,
                "page_revision": start["page_revision"],
                "url": safe_url,
                "source_url": safe_url,
                "redacted_query_keys": redacted_query_keys,
                "url_state": url_state,
                "title": frame_title,
                "source_title": frame_title,
                "status": "partial" if frame_truncated else raw.get("status", "empty"),
                "bounds": frame_bounds,
                "bounds_coordinate_space": "main_frame_viewport_css_px",
                "text": frame_text,
                "text_sha256": frame_digest,
                "blocks": blocks,
                "links": safe_links,
                "controls": safe_controls,
                "selection": selection,
                "coverage": {
                    "scope": "visible_dom_blocks",
                    "nodes_examined": nodes_used,
                    "characters_examined": characters_used,
                    "links_examined": int(raw.get("links_examined", 0) or 0),
                    "nodes_truncated": bool(raw.get("nodes_truncated")),
                    "text_truncated": bool(raw.get("text_truncated")),
                    "blocks_truncated": bool(raw.get("blocks_truncated") or python_truncated),
                    "block_text_truncated": bool(raw.get("block_text_truncated")),
                    "links_truncated": bool(raw.get("links_truncated")),
                    "selection_truncated": bool(raw.get("selection_truncated")),
                    "controls_truncated": bool(raw.get("controls_truncated")),
                    "controls_examined": controls_used,
                    "control_characters_examined": control_characters_used,
                    "control_label_nodes_examined": control_label_nodes_used,
                },
            }
            # Content capture is versioned by source text and attribution, not
            # animation coordinates or a user's changing selection highlight.
            frame_content = {
                "frame_id": frame_id, "url": safe_url, "title": frame_title,
                "text_sha256": frame_digest,
                "coverage": {key: frame_result["coverage"].get(key) for key in (
                    "nodes_truncated", "text_truncated", "blocks_truncated", "block_text_truncated",
                )},
            }
            frame_hash = hashlib.sha256(json.dumps(frame_content, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
            frame_result["frame_hash_scope"] = "reader_frame_content_v1"
            frame_result["frame_sha256"] = frame_hash
            for block in blocks:
                block["frame_sha256"] = frame_hash
            selection["frame_sha256"] = frame_hash
            for link in safe_links:
                link["frame_sha256"] = frame_hash
            for control in safe_controls:
                control["frame_sha256"] = frame_hash
            frame_results.append(frame_result)

        final = worker._snapshot(live, state.page_id, include_text=False)
        if (final["page_id"] != start["page_id"] or final["url"] != start["url"]
                or final["page_revision"] != start["page_revision"]):
            raise BrowserError("BROWSER_PAGE_CHANGED", 409, {"current_page_revision": final["page_revision"]})
        for frame in frames_to_read:
            frame_id = frame_ids.get(id(frame))
            previous_url = frame_start_urls.get(frame_id) if frame_id else None
            if previous_url is not None and (frame.is_detached() or frame.url != previous_url):
                raise BrowserError("BROWSER_PAGE_CHANGED", 409, {"current_page_revision": final["page_revision"]})

        frame_text_by_id = {item["frame_id"]: item["text"] for item in frame_results}
        main_text = frame_text_by_id.get("main", "")
        digest_source = json.dumps(
            [{
                "frame_id": item["frame_id"],
                "frame_sha256": item.get("frame_sha256"),
                "url": item["url"],
                "title": item.get("title", ""),
                "status": item["status"],
                "text_sha256": item["text_sha256"],
                "blocks": [
                    {key: block.get(key) for key in (
                        "block_id", "frame_id", "tag", "role", "aria_label", "heading_level", "bounds", "text", "truncated",
                        "start_offset", "end_offset",
                    )}
                    for block in item.get("blocks", [])
                ],
                "links": [
                    {key: link.get(key) for key in ("url", "text", "title", "bounds")}
                    for link in item.get("links", [])
                ],
                "controls": [
                    {key: control.get(key) for key in ("control_id", "locator", "tag", "type", "role", "label", "disabled", "bounds")}
                    for control in item.get("controls", [])
                ],
                "bounds": item.get("bounds"),
                "selection": item.get("selection", {}),
            }
             for item in frame_results],
            ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")
        snapshot_digest = hashlib.sha256(digest_source).hexdigest()
        coverage_status = "partial" if (
            any_truncated or failed_frames or timed_out_frames or hidden_frames or omitted_frames or frames_not_requested
        ) else "complete"
        result = {
            "session_id": session_id,
            "engine": live.engine,
            "page_id": state.page_id,
            "page_revision": final["page_revision"],
            "url": final["url"],
            "redacted_query_keys": final.get("redacted_query_keys", []),
            "title": _safe_display_text(final["title"], page.url, 500),
            "text": main_text,
            "text_sha256": hashlib.sha256(main_text.encode("utf-8")).hexdigest(),
            "snapshot_version": {
                "schema": 1,
                "page_revision": final["page_revision"],
                "sha256": snapshot_digest,
                "captured_at": utc_now(),
            },
            "scope": "main_frame_and_visible_iframes" if include_frames else "main_frame_only",
            "offset_unit": "unicode_codepoints",
            "offset_basis": "sanitized_reader_frame_text",
            "frames": frame_results,
            "links": all_links,
            "controls": all_controls,
            "coverage": {
                "status": coverage_status,
                "scope": "visible_dom_blocks",
                "include_frames": include_frames,
                "frames_seen": len(all_frames),
                "frames_read": sum(item["status"] in {"read", "empty", "partial"} for item in frame_results),
                "hidden_frames": hidden_frames,
                "failed_frames": failed_frames,
                "timed_out_frames": timed_out_frames,
                "frames_omitted": omitted_frames,
                "frames_not_requested": frames_not_requested,
                "nodes_examined": nodes_examined,
                "characters_examined": characters_examined,
                "controls_examined": controls_examined,
                "control_characters_examined": control_characters_examined,
                "control_label_nodes_examined": control_label_nodes_examined,
                "links_returned": len(all_links),
                "controls_returned": len(all_controls),
                "truncated": any_truncated or bool(omitted_frames),
                "limitations": [
                    "This is a bounded readable DOM snapshot, not proof that all lazy, hidden, canvas, media, or cross-origin protected content was captured.",
                    "Input values, password fields, form subtrees, cookies, and browser storage are not read.",
                    "Offsets use Unicode codepoints within this sanitized Reader snapshot, not source HTML bytes or JavaScript UTF-16 offsets.",
                ],
            },
        }
        if include_storage:
            return {"snapshot": result, "storage_payload": worker._storage_payload(live)}
        return result

    return runtime._call(engine, session_id, operation, timeout=READER_COMMAND_TIMEOUT_SECONDS)


def read_browser_context(runtime, engine: str, session_id: str, page_id: str | None = None,
                         *, include_frames: bool = True, before_dispatch=None) -> dict:
    """Read bounded visible DOM blocks and accessible frame context from one live page."""
    if not isinstance(include_frames, bool):
        raise BrowserError("BROWSER_READ_INVALID", 400)
    return _read_browser_context(
        runtime, engine, session_id, page_id, include_frames=include_frames, include_storage=False,
        before_dispatch=before_dispatch,
    )


def capture_reader_context(
    runtime,
    engine: str,
    session_id: str,
    page_id: str,
    frame_id: str,
    expected_frame_sha256: str,
    block_id: str | None = None,
    expected_page_revision: int | None = None,
    before_dispatch=None,
) -> dict:
    """Build a save-ready snapshot from a freshly re-read frame or block.

    The caller supplies only stable Reader identifiers and hashes. Source text,
    offsets, URL, title, and protected browser state all come from the live
    worker operation; client-provided text is never accepted.
    """
    if not isinstance(page_id, str) or not page_id or len(page_id) > 80:
        raise BrowserError("BROWSER_READ_INVALID", 400)
    if not isinstance(frame_id, str) or not frame_id or len(frame_id) > 256:
        raise BrowserError("BROWSER_READ_INVALID", 400)
    if not isinstance(expected_frame_sha256, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", expected_frame_sha256):
        raise BrowserError("BROWSER_READ_INVALID", 400)
    if block_id is not None and (not isinstance(block_id, str) or not block_id or len(block_id) > 2_048):
        raise BrowserError("BROWSER_READ_INVALID", 400)
    if expected_page_revision is not None and (
        isinstance(expected_page_revision, bool) or not isinstance(expected_page_revision, int) or expected_page_revision < 0
    ):
        raise BrowserError("BROWSER_READ_INVALID", 400)

    captured = _read_browser_context(
        runtime, engine, session_id, page_id, include_frames=True, include_storage=True,
        before_dispatch=before_dispatch,
    )
    reader_snapshot = captured["snapshot"]
    snapshot = select_reader_snapshot(
        reader_snapshot, frame_id, expected_frame_sha256, block_id=block_id,
        expected_page_revision=expected_page_revision,
    )
    return {"snapshot": snapshot, "storage_payload": captured["storage_payload"]}


def select_reader_snapshot(reader_snapshot: dict, frame_id: str, expected_frame_sha256: str,
                           *, block_id: str | None = None, expected_page_revision: int | None = None) -> dict:
    """Select from an owner-issued snapshot, preserving its original coordinates."""
    if not isinstance(frame_id, str) or not isinstance(expected_frame_sha256, str):
        raise BrowserError("BROWSER_READ_INVALID", 400)
    if expected_page_revision is not None and reader_snapshot["page_revision"] != expected_page_revision:
        raise BrowserError(
            "BROWSER_PAGE_CHANGED", 409,
            {"current_page_revision": reader_snapshot["page_revision"]},
        )
    frame = next((item for item in reader_snapshot["frames"] if item["frame_id"] == frame_id), None)
    if frame is None or frame.get("status") not in {"read", "empty", "partial"}:
        raise BrowserError("BROWSER_PAGE_READ_UNAVAILABLE", 409)
    if frame.get("frame_sha256", "").lower() != expected_frame_sha256.lower():
        raise BrowserError(
            "BROWSER_PAGE_CHANGED", 409,
            {
                "current_page_revision": reader_snapshot["page_revision"],
                "current_frame_sha256": frame.get("frame_sha256"),
            },
        )

    frame_text = frame["text"]
    if block_id is None:
        text = frame_text
        selection_context = dict(frame["selection"])
        selection_context["frame_scope"] = frame_id
        text_truncated = bool(
            frame["coverage"].get("nodes_truncated")
            or frame["coverage"].get("text_truncated")
            or frame["coverage"].get("blocks_truncated")
            or frame["coverage"].get("block_text_truncated")
        )
        scope = "reader_frame"
    else:
        block = next((item for item in frame["blocks"] if item["block_id"] == block_id), None)
        if block is None:
            raise BrowserError("BROWSER_READ_INVALID", 400)
        text = block["text"]
        start_offset = block["start_offset"]
        end_offset = block["end_offset"]
        selection_context = {
            "frame_scope": frame_id,
            "frame_id": frame_id,
            "quote": text,
            "quote_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "visible_text_match_count": 1,
            "start_offset": start_offset,
            "end_offset": end_offset,
            "context_before": frame_text[max(0, start_offset - SELECTION_CONTEXT_CHARACTERS):start_offset],
            "context_after": frame_text[end_offset:end_offset + SELECTION_CONTEXT_CHARACTERS],
            "offset_unit": "unicode_codepoints",
            "offset_basis": "sanitized_reader_frame_text",
            "location_state": "reader_block_partial" if block.get("truncated") else "reader_block_match",
            "page_revision": reader_snapshot["page_revision"],
            "snapshot_sha256": frame["text_sha256"],
            "frame_sha256": frame["frame_sha256"],
        }
        text_truncated = bool(block.get("truncated"))
        scope = "reader_block"

    if not text.strip():
        raise BrowserError("BROWSER_PAGE_TEXT_UNAVAILABLE", 409)
    snapshot = {
        "session_id": reader_snapshot["session_id"],
        "engine": reader_snapshot["engine"],
        "page_id": reader_snapshot["page_id"],
        "page_revision": reader_snapshot["page_revision"],
        "url": reader_snapshot["url"],
        "title": reader_snapshot["title"],
        "source_url": frame["source_url"],
        "source_title": frame["source_title"],
        "frame_id": frame_id,
        "frame_sha256": frame["frame_sha256"],
        "snapshot_sha256": reader_snapshot["snapshot_version"]["sha256"],
        "captured_at": reader_snapshot["snapshot_version"]["captured_at"],
        "page_text_sha256": frame["text_sha256"],
        "text": text,
        "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "text_characters": len(text),
        "text_truncated": text_truncated,
        "scope": scope,
        "capture_scope": scope,
        "selection_context": selection_context,
        "coverage": frame["coverage"],
        "content_trust": "UNTRUSTED_WEB_CONTENT",
        "redacted_query_keys": frame["redacted_query_keys"],
        "limitations": reader_snapshot["coverage"]["limitations"],
    }
    return snapshot


def register_browser_reader_routes(app) -> None:
    """Register a no-store Reader API over the already-owned BrowserService session."""
    if "browser_read_context" in app.view_functions:
        return
    from .browser_api import _browser_error

    store = app.extensions.get("oevira.store")
    service = app.extensions.get("oevira.browser")
    if store is None or service is None:
        raise RuntimeError("Browser Reader routes require the existing BrowserService and Store")
    host = app.extensions.get("oevira.browser_host")
    runtime = getattr(host, "runtime", None)
    if runtime is None or not callable(getattr(runtime, "_call", None)):
        raise RuntimeError("Browser Reader routes require the existing BrowserHostRuntime")

    @app.get("/api/browser-sessions/<session_id>/context")
    def browser_read_context(session_id: str):
        try:
            identity = g.identity
            if set(request.args.keys()) - {"page_id", "include_frames", "page_revision"}:
                raise ApiError("INVALID_INPUT", "The browser context query contains an unsupported field", 400)
            page_id = request.args.get("page_id") or None
            include_frames_value = request.args.get("include_frames", "true").lower()
            if include_frames_value not in {"true", "false", "1", "0"}:
                raise ApiError("INVALID_INPUT", "include_frames must be true or false", 400)
            include_frames = include_frames_value in {"true", "1"}
            expected_revision = request.args.get("page_revision")
            if expected_revision is not None:
                try:
                    expected_revision = int(expected_revision)
                except ValueError as exc:
                    raise ApiError("INVALID_INPUT", "page_revision must be a nonnegative integer", 400) from exc
                if expected_revision < 0:
                    raise ApiError("INVALID_INPUT", "page_revision must be a nonnegative integer", 400)

            data = {"page_id": page_id, "include_frames": include_frames}
            if expected_revision is not None:
                data["page_revision"] = expected_revision
            response = _success(service.read_context(identity, session_id, data))
            response[0].headers["Cache-Control"] = "no-store, private"
            response[0].headers["Pragma"] = "no-cache"
            return response
        except BrowserError as exc:
            raise _browser_error(exc)
