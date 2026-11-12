"""Portable, offline interactive HTML delivery for a saved work product."""
from __future__ import annotations

import base64
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
from html import escape
import re


_SCRIPT = r"""(() => {
  const search = document.querySelector('#find');
  const blocks = [...document.querySelectorAll('main > section')];
  const status = document.querySelector('#match');
  const tables = [...document.querySelectorAll('table')];
  const compareText = new Intl.Collator(undefined, {numeric:true, sensitivity:'base'});

  function compareDigits(left, right) {
    return left < right ? -1 : left > right ? 1 : 0;
  }

  function decimalParts(value) {
    const match = /^([+-]?)(\d+)(?:\.(\d+))?$/.exec(value);
    if (!match) return null;
    const integer = match[2].replace(/^0+(?=\d)/, '');
    const fraction = (match[3] ?? '').replace(/0+$/, '');
    const zero = integer === '0' && !fraction;
    return {negative: match[1] === '-' && !zero, integer, fraction};
  }

  function compareDecimals(leftValue, rightValue) {
    const left = decimalParts(leftValue);
    const right = decimalParts(rightValue);
    if (!left || !right) return compareText.compare(leftValue, rightValue);
    if (left.negative !== right.negative) return left.negative ? -1 : 1;
    let magnitude = left.integer.length - right.integer.length;
    if (!magnitude) magnitude = compareDigits(left.integer, right.integer);
    if (!magnitude) {
      const places = Math.max(left.fraction.length, right.fraction.length);
      magnitude = compareDigits(
        left.fraction.padEnd(places, '0'), right.fraction.padEnd(places, '0'));
    }
    return left.negative ? -magnitude : magnitude;
  }

  function compareCells(left, right, column, ascending) {
    const a = left.cells[column];
    const b = right.cells[column];
    const rank = {number:0, date:1, text:2, missing:3};
    const aKind = a.dataset.sortKind ?? 'text';
    const bKind = b.dataset.sortKind ?? 'text';
    if (aKind !== bKind) return (rank[aKind] ?? 2) - (rank[bKind] ?? 2);
    const result = aKind === 'number'
      ? compareDecimals(a.dataset.sortValue, b.dataset.sortValue)
      : aKind === 'date'
        ? compareText.compare(a.dataset.sortValue, b.dataset.sortValue)
        : compareText.compare(a.textContent.trim(), b.textContent.trim());
    return aKind === 'missing' ? 0 : (ascending ? 1 : -1) * result;
  }

  for (const table of tables) {
    const rows = [...table.tBodies[0].rows];
    rows.forEach((row, index) => {
      row.dataset.originalOrder = String(index);
      row.id = `${table.id}-row-${index + 1}`;
    });
    for (const [column, button] of [...table.querySelectorAll('thead button')].entries()) {
      button.addEventListener('click', () => {
        const header = button.parentElement;
        const ascending = header.getAttribute('aria-sort') !== 'ascending';
        const ordered = [...table.tBodies[0].rows].sort((a, b) => {
          const result = compareCells(a, b, column, ascending);
          return result ||
            Number(a.dataset.originalOrder) - Number(b.dataset.originalOrder);
        });
        for (const cell of table.querySelectorAll('th')) cell.removeAttribute('aria-sort');
        header.setAttribute('aria-sort', ascending ? 'ascending' : 'descending');
        table.tBodies[0].append(...ordered);
      });
    }
  }

  function applySearch() {
    const term = search.value.trim().toLocaleLowerCase();
    let shown = 0;
    for (const block of blocks) {
      const rows = [...block.querySelectorAll('tbody tr')];
      if (rows.length) {
        const heading = block.querySelector('h2,h3')?.textContent.toLocaleLowerCase() ?? '';
        const headingMatches = term && heading.includes(term);
        for (const row of rows) {
          row.hidden = Boolean(term && !headingMatches && !row.textContent.toLocaleLowerCase().includes(term));
          const chartBars = block.querySelectorAll(`[data-chart-row="${row.id}"]`);
          for (const bar of chartBars) bar.hidden = row.hidden;
        }
        block.hidden = !headingMatches && !rows.some(row => !row.hidden);
      } else {
        block.hidden = Boolean(term && !block.textContent.toLocaleLowerCase().includes(term));
      }
      if (!block.hidden) {
        shown++;
        if (term) {
          const details = block.querySelector(':scope > details');
          if (details) details.open = true;
        }
      }
    }
    status.textContent = term ? `${shown} 个内容区匹配` : '';
  }

  search.addEventListener('input', applySearch);
  document.querySelector('#clear').addEventListener('click', () => {
    search.value = '';
    applySearch();
    search.focus();
  });

  for (const button of document.querySelectorAll('[data-chart-row]')) {
    button.addEventListener('click', () => {
      const row = document.getElementById(button.dataset.chartRow);
      if (!row) return;
      row.hidden = false;
      row.classList.add('chart-target');
      row.scrollIntoView({block:'center', inline:'nearest'});
      window.setTimeout(() => row.classList.remove('chart-target'), 1800);
      status.textContent = `已定位到：${row.textContent.trim()}`;
    });
  }

  for (const link of document.querySelectorAll('.source-ref')) {
    link.addEventListener('click', () => {
      const sources = document.querySelector('#sources');
      if (sources) sources.open = true;
    });
  }
})();"""

_STYLE = """
:root{color-scheme:light dark;--page:#fff;--ink:#24272e;--muted:#60656f;--line:#dce0e6;--panel:#f4f5f7;--accent:#315ba8;--bar:#6688c5;--focus:#f8d568}
*{box-sizing:border-box}body{margin:0;background:var(--page);color:var(--ink);font:16px/1.75 system-ui,'Microsoft YaHei',sans-serif}
header,main,footer{max-width:1120px;margin:auto;padding:24px 36px}header{padding-top:56px}h1{font-size:32px;line-height:1.35;margin:0 0 12px;letter-spacing:-.025em}h2{font-size:23px;line-height:1.45;margin:0 0 16px}h3{font-size:19px}p{max-width:78ch;white-space:pre-wrap}section{padding:14px 0;border-bottom:1px solid var(--line)}
.section-details>summary{cursor:pointer;list-style:none}.section-details>summary::-webkit-details-marker{display:none}.section-details>summary h2,.section-details>summary h3{display:inline-block;margin:0;padding:12px 0}.section-details>summary:after{content:'＋';float:right;color:var(--muted)}.section-details[open]>summary:after{content:'−'}.section-content{padding:8px 0 16px}
.tools{display:flex;gap:8px;align-items:center;flex-wrap:wrap;padding-top:22px}.tools label{flex:1;min-width:180px}input{font:inherit;width:100%;border:1px solid var(--line);border-radius:10px;padding:9px 12px;background:var(--panel);color:inherit}button{font:inherit;color:inherit;background:transparent;border:1px solid var(--line);border-radius:8px;padding:8px 14px;cursor:pointer}button:hover{background:var(--panel)}:focus-visible{outline:2px solid var(--accent);outline-offset:3px}
.table-scroll{overflow:auto}table{border-collapse:collapse;width:100%;font-size:14px}th,td{text-align:left;vertical-align:top;border-bottom:1px solid var(--line);padding:12px;white-space:pre-wrap}th{background:var(--panel);font-weight:600}th button{border:0;padding:0;text-align:left}th[aria-sort=ascending] button:after{content:' ↑'}th[aria-sort=descending] button:after{content:' ↓'}td{min-width:120px;max-width:400px}[data-chart-row]{width:100%;display:grid;grid-template-columns:minmax(90px,1fr) minmax(100px,2fr) auto;gap:12px;align-items:center;text-align:left;padding:6px 8px;border:0;border-radius:6px}.chart-label{overflow-wrap:anywhere}.bar-axis{height:16px;position:relative;border-left:1px solid var(--muted);border-right:1px solid var(--muted);background:linear-gradient(90deg,transparent 49.7%,var(--line) 49.7%,var(--line) 50.3%,transparent 50.3%)}.bar-axis i{position:absolute;top:4px;height:8px;background:var(--bar);width:var(--bar-width)}.bar-axis .positive{left:50%;border-radius:0 4px 4px 0}.bar-axis .negative{right:50%;border-radius:4px 0 0 4px}.chart-target{background:color-mix(in srgb,var(--focus) 36%,transparent)}.chart-details{margin:18px 0;padding:12px;border:1px solid var(--line);border-radius:10px}.chart-details>summary{cursor:pointer;font-weight:600}.chart-bars{list-style:none;margin:12px 0;padding:0}.chart-bars li{padding:2px 0}.source-ref{font-size:.82em;vertical-align:super;padding:0 2px}.sources{padding:20px 0}small,#match,footer{color:var(--muted)}details{padding:8px 0}summary{cursor:pointer;font-weight:600}li{padding:5px 0;overflow-wrap:anywhere}[hidden]{display:none!important}
@media(prefers-color-scheme:dark){:root{--page:#1c1d21;--ink:#f1f2f5;--muted:#bbc0ca;--line:#454954;--panel:#282b32;--accent:#9abaff;--bar:#9abaff;--focus:#71611c}}
@media(max-width:600px){header,main,footer{padding:20px}h1{font-size:26px}header{padding-top:30px}td{min-width:110px}[data-chart-row]{grid-template-columns:minmax(72px,1fr) minmax(70px,1.2fr) auto;gap:6px;font-size:14px}}
@media(prefers-reduced-motion:reduce){*,*::before,*::after{scroll-behavior:auto!important;animation:none!important;transition:none!important}}
@media print{.tools,.chart-details{display:none}body{color:#111;background:white}section{break-inside:avoid}header,main,footer{padding:12px 0}details{display:block}}
"""

_NUMBER = re.compile(r"^[+-]?(?:\d+|\d{1,3}(?:,\d{3})+)(?:\.\d+)?$")
_ISO_DATE = re.compile(r"^\d{4}-\d{2}(?:-\d{2})?$")
_CITATION_MARKER = re.compile(r"\[\^(\d+)\]|\[\[source:(\d+)\]\]")


def _cells(line: str) -> list[str]:
    return [part.strip().replace(r"\|", "|") for part in re.split(r"(?<!\\)\|", line.strip().strip("|"))]


def _sortable_value(value: str) -> tuple[str, str]:
    text = value.strip()
    if not text:
        return "missing", ""
    if _NUMBER.fullmatch(text):
        try:
            number = Decimal(text.replace(",", ""))
        except InvalidOperation:
            number = Decimal("NaN")
        if number.is_finite():
            normalized = format(number, "f")
            if "." in normalized:
                normalized = normalized.rstrip("0").rstrip(".")
            return "number", "0" if normalized in {"-0", "+0", ""} else normalized
    if _ISO_DATE.fullmatch(text):
        try:
            if len(text) == 7:
                normalized_date = datetime.strptime(text, "%Y-%m").strftime("%Y-%m")
            else:
                normalized_date = date.fromisoformat(text).isoformat()
            return "date", normalized_date
        except ValueError:
            pass
    if "T" in text and (text.endswith("Z") or re.search(r"[+-]\d\d:\d\d$", text)):
        try:
            value_datetime = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if value_datetime.tzinfo is not None:
                return "date", value_datetime.astimezone(timezone.utc).isoformat(timespec="microseconds")
        except ValueError:
            pass
    return "text", text


def _inline(value: str, citation_count: int) -> str:
    escaped = escape(value)

    def link(match: re.Match[str]) -> str:
        source_number = int(match.group(1) or match.group(2))
        if source_number < 1 or source_number > citation_count:
            return match.group(0)
        return (
            f'<a class="source-ref" href="#source-{source_number}" '
            f'aria-label="查看来源 {source_number}">[{source_number}]</a>'
        )

    return _CITATION_MARKER.sub(link, escaped)


def _chart_markup(table_id: str, headers: list[str], rows: list[list[str]]) -> str:
    if len(rows) < 2 or not headers:
        return ""
    charts = []
    for column in range(1, len(headers)):
        parsed = [_sortable_value(row[column] if column < len(row) else "") for row in rows]
        numeric = [Decimal(value) for kind, value in parsed if kind == "number"]
        if len(numeric) < 2 or any(kind not in {"number", "missing"} for kind, _value in parsed):
            continue
        maximum = max((abs(value) for value in numeric), default=Decimal(0))
        bars = []
        for index, (kind, sort_value) in enumerate(parsed):
            label = rows[index][0] if rows[index] else ""
            value = rows[index][column] if column < len(rows[index]) else ""
            if kind == "number":
                number = Decimal(sort_value)
                width = 0.0 if maximum == 0 else float(abs(number) / maximum * Decimal(50))
                direction = "negative" if number < 0 else "positive"
                bar = f'<i class="{direction}" style="--bar-width:{width:.3f}%"></i>'
                shown_value = escape(value)
            else:
                bar = ""
                shown_value = "未知"
            row_id = f"{table_id}-row-{index + 1}"
            label_text = escape(label)
            aria = escape(f"{label}；{headers[column]}：{value}；定位到对应数据行", quote=True)
            bars.append(
                f'<li><button type="button" data-chart-row="{row_id}" aria-label="{aria}">'
                f'<span class="chart-label">{label_text}</span>'
                f'<span class="bar-axis" aria-hidden="true">{bar}</span>'
                f'<strong>{shown_value}</strong></button></li>'
            )
        chart_title = escape(f"{headers[column]} · 按 {headers[0]}")
        charts.append(
            f'<details class="chart-details"><summary>{chart_title}图表</summary>'
            f'<ol class="chart-bars" aria-label="{chart_title}">{"".join(bars)}</ol>'
            '<small>横条按绝对值比例显示，精确数值以表格为准；选择横条可定位到对应行。</small></details>'
        )
    return "".join(charts)


def _table_markup(headers: list[str], rows: list[list[str]], table_number: int,
                  citation_count: int) -> str:
    table_id = f"html-table-{table_number}"
    header_cells = []
    for index, value in enumerate(headers):
        kind, sort_value = _sortable_value(value)
        header_cells.append(
            f'<th scope="col" data-sort-kind="{kind}" data-sort-value="{escape(sort_value, quote=True)}">'
            f'<button type="button">{_inline(value, citation_count)}</button></th>'
        )
    row_html = []
    for row in rows:
        values = row + [""] * max(0, len(headers) - len(row))
        cells = []
        for value in values[:len(headers)]:
            kind, sort_value = _sortable_value(value)
            cells.append(
                f'<td data-sort-kind="{kind}" data-sort-value="{escape(sort_value, quote=True)}">'
                f'{_inline(value, citation_count)}</td>'
            )
        row_html.append(f'<tr>{"".join(cells)}</tr>')
    table = (
        f'<div class="table-scroll"><table id="{table_id}"><thead><tr>{"".join(header_cells)}</tr></thead>'
        f'<tbody>{"".join(row_html)}</tbody></table></div>'
    )
    return table + _chart_markup(table_id, headers, rows)


def _section_markup(items: list[str], index: int) -> str:
    if not items:
        return ""
    match = re.match(r"^<(h[23])>(.*?)</h[23]>$", items[0])
    if not match:
        return f'<section data-section-index="{index}">{"".join(items)}</section>'
    heading = items[0]
    content = "".join(items[1:])
    return (
        f'<section data-section-index="{index}"><details class="section-details" open>'
        f'<summary>{heading}</summary><div class="section-content">{content}</div>'
        '</details></section>'
    )


def render_html(title: str, body: str, citations: list[str]) -> bytes:
    """Render a self-contained, offline report with typed tables and linked views."""
    if not isinstance(title, str) or not isinstance(body, str) or not isinstance(citations, list):
        raise TypeError("HTML export requires a title, body and citation list.")
    lines = body.splitlines()
    sections: list[str] = []
    current: list[str] = []
    index = 0
    table_number = 0

    while index < len(lines):
        line = lines[index]
        if (index + 1 < len(lines) and "|" in line
                and re.fullmatch(r"[\s|:\-]+", lines[index + 1]) and "-" in lines[index + 1]):
            headers = _cells(line)
            rows = []
            index += 2
            while index < len(lines) and "|" in lines[index] and lines[index].strip():
                values = _cells(lines[index])
                if len(values) != len(headers):
                    break
                rows.append(values)
                index += 1
            table_number += 1
            current.append(_table_markup(headers, rows, table_number, len(citations)))
            continue
        match = re.match(r"^(#{1,6})\s+(.+)$", line)
        if match:
            if current:
                sections.append(_section_markup(current, len(sections) + 1))
                current = []
            level = 2 if len(match[1]) <= 2 else 3
            current.append(f'<h{level}>{_inline(match[2], len(citations))}</h{level}>')
        elif line.strip():
            current.append(f'<p>{_inline(line, len(citations))}</p>')
        index += 1
    if current:
        sections.append(_section_markup(current, len(sections) + 1))

    sources = ""
    if citations:
        source_rows = "".join(
            f'<li id="source-{index}">{escape(value.removeprefix("- "))}</li>'
            for index, value in enumerate(citations, start=1)
        )
        sources = (
            '<details class="sources" id="sources"><summary>来源与依据</summary>'
            f'<ol>{source_rows}</ol></details>'
        )
    digest = base64.b64encode(hashlib.sha256(_SCRIPT.encode()).digest()).decode()
    policy = f"default-src 'none'; script-src 'sha256-{digest}'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'; connect-src 'none'"
    result = (
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta http-equiv="Content-Security-Policy" content="' + escape(policy, quote=True) + '">'
        '<title>' + escape(title) + '</title><style>' + _STYLE + '</style></head><body>'
        '<header><h1>' + escape(title) + '</h1><div class="tools">'
        '<label><input id="find" type="search" aria-label="查找内容" placeholder="查找内容、筛选表格…"></label>'
        '<button id="clear" type="button">清除筛选</button>'
        '<span id="match" role="status" aria-live="polite"></span></div></header>'
        '<main>' + ''.join(sections) + sources + '</main>'
        '<footer>内容与数据对应导出的这一版。筛选和排序只改变当前查看方式。</footer>'
        '<script>' + _SCRIPT + '</script></body></html>'
    )
    return result.encode('utf-8')
