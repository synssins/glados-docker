"""JSON-to-HTML renderer for smoke reports.

Produces a single self-contained HTML file from a smoke run's JSON
output. No external resources: inline CSS, inline SVG icons, vanilla
JS for collapse/expand only. File is openable directly from a file://
URL and renders identically in Chrome / Firefox / Edge.

CLI:

    python -m tests.smoke._render_report tests/smoke/reports/smoke-XYZ.json

By default the renderer:
    - looks up the most-recent prior JSON in the same directory and
      computes a diff vs that run,
    - reads up to the last 10 JSON reports for a trend sparkline,
    - writes `tests/smoke/reports/smoke-XYZ.html` next to the input,
    - copies the result to `tests/smoke/reports/latest.html`.

Flags:

    --no-diff       suppress the diff banner / per-test badges
    --no-history    suppress the trend sparkline
    --prev <path>   override the diff source
    -o <path>       override the output path
    --no-latest     do not write/overwrite latest.html
"""

from __future__ import annotations

import argparse
import datetime as _dt
import html
import json
import shutil
import sys
from pathlib import Path
from typing import Any

from tests.smoke._diff_report import compute_diff

VERSION = "1.0.0"

# ─── Colors (CSS vars for easy dark-mode later) ──────────────────────────

COLORS = {
    "pass": "#16a34a",
    "fail": "#dc2626",
    "skip": "#737373",
    "error": "#d97706",
    "regressed": "#dc2626",
    "recovered": "#16a34a",
    "new": "#2563eb",
    "background": "#fafafa",
    "card": "#ffffff",
    "text": "#171717",
    "muted": "#737373",
    "border": "#e5e5e5",
    "code_bg": "#f5f5f5",
}

# ─── Inline SVG icons ────────────────────────────────────────────────────


def _icon(kind: str, size: int = 16) -> str:
    """Inline SVG icons. No external font / image references."""
    color = COLORS.get(kind, COLORS["muted"])
    if kind == "pass":
        return _svg(
            size, color,
            '<circle cx="12" cy="12" r="10"/>'
            '<path d="M8 12.5l3 3 5-6" stroke="white" stroke-width="2.4" '
            'stroke-linecap="round" stroke-linejoin="round" fill="none"/>'
        )
    if kind == "fail":
        return _svg(
            size, color,
            '<circle cx="12" cy="12" r="10"/>'
            '<path d="M8 8l8 8M16 8l-8 8" stroke="white" stroke-width="2.4" '
            'stroke-linecap="round" fill="none"/>'
        )
    if kind == "skip":
        return _svg(
            size, color,
            '<circle cx="12" cy="12" r="10"/>'
            '<path d="M7 12h10" stroke="white" stroke-width="2.4" '
            'stroke-linecap="round" fill="none"/>'
        )
    if kind == "error":
        return _svg(
            size, color,
            '<path d="M12 2L22 20H2z"/>'
            '<path d="M12 9v5M12 17v.01" stroke="white" stroke-width="2.4" '
            'stroke-linecap="round" fill="none"/>'
        )
    return _svg(size, color, '<circle cx="12" cy="12" r="10"/>')


def _svg(size: int, color: str, body: str) -> str:
    return (
        f'<svg width="{size}" height="{size}" viewBox="0 0 24 24" '
        f'fill="{color}" xmlns="http://www.w3.org/2000/svg" '
        f'aria-hidden="true">{body}</svg>'
    )


# ─── CSS ─────────────────────────────────────────────────────────────────

CSS = f"""
* {{ box-sizing: border-box; }}
:root {{
  --pass: {COLORS["pass"]};
  --fail: {COLORS["fail"]};
  --skip: {COLORS["skip"]};
  --error: {COLORS["error"]};
  --regressed: {COLORS["regressed"]};
  --recovered: {COLORS["recovered"]};
  --new: {COLORS["new"]};
  --bg: {COLORS["background"]};
  --card: {COLORS["card"]};
  --text: {COLORS["text"]};
  --muted: {COLORS["muted"]};
  --border: {COLORS["border"]};
  --code-bg: {COLORS["code_bg"]};
}}
body {{
  margin: 0;
  padding: 0;
  background: var(--bg);
  color: var(--text);
  font-family: -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
  font-size: 14px;
  line-height: 1.5;
}}
.wrap {{
  max-width: 1200px;
  margin: 0 auto;
  padding: 24px;
}}
.banner {{
  border-radius: 12px;
  padding: 24px 28px;
  color: white;
  margin-bottom: 16px;
  display: flex;
  align-items: center;
  gap: 16px;
}}
.banner.pass {{ background: var(--pass); }}
.banner.fail {{ background: var(--fail); }}
.banner.partial {{ background: var(--error); }}
.banner.empty {{ background: var(--muted); }}
.banner h1 {{
  margin: 0 0 4px 0;
  font-size: 24px;
  font-weight: 600;
}}
.banner .meta {{
  opacity: 0.92;
  font-size: 13px;
}}
.banner .icon {{
  flex-shrink: 0;
}}
.diff-banner {{
  border-radius: 10px;
  padding: 16px 20px;
  margin-bottom: 16px;
  background: var(--card);
  border: 1px solid var(--border);
}}
.diff-banner.regressed {{ border-left: 5px solid var(--regressed); }}
.diff-banner.recovered {{ border-left: 5px solid var(--recovered); }}
.diff-banner.info     {{ border-left: 5px solid var(--new); }}
.diff-banner.unchanged {{ border-left: 5px solid var(--muted); }}
.diff-banner h3 {{ margin: 0 0 6px 0; font-size: 16px; }}
.diff-banner .links a {{
  color: var(--text);
  text-decoration: underline;
  margin-right: 8px;
}}
.diff-banner .meta {{ font-size: 12px; color: var(--muted); }}
.cards {{
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 12px;
  margin-bottom: 16px;
}}
.card {{
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 16px;
}}
.card .label {{ color: var(--muted); font-size: 12px; }}
.card .value {{ font-size: 28px; font-weight: 600; margin-top: 6px; }}
.card.failed .value {{ color: var(--fail); }}
.card.passed .value {{ color: var(--pass); }}
.card.skipped .value {{ color: var(--skip); }}
.card a {{ color: inherit; text-decoration: none; }}
.trend {{
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 14px 16px;
  margin-bottom: 16px;
}}
.trend h4 {{ margin: 0 0 8px 0; font-size: 13px; color: var(--muted); font-weight: 500; }}
.tier-section {{
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: 10px;
  margin-bottom: 16px;
  overflow: hidden;
}}
.tier-header {{
  padding: 14px 18px;
  border-bottom: 1px solid var(--border);
  display: flex;
  align-items: center;
  gap: 10px;
  font-weight: 600;
}}
.tier-header .name {{ flex: 1; }}
.tier-header .duration {{ color: var(--muted); font-size: 12px; font-weight: normal; }}
.test-row {{
  border-bottom: 1px solid var(--border);
  cursor: pointer;
  user-select: none;
}}
.test-row:last-child {{ border-bottom: none; }}
.test-row.regressed {{ background: rgba(220, 38, 38, 0.04); }}
.test-row.fail-row {{ background: rgba(220, 38, 38, 0.03); }}
.test-summary-line {{
  padding: 10px 18px;
  display: grid;
  grid-template-columns: auto 1fr auto auto;
  align-items: center;
  gap: 12px;
}}
.test-name {{ font-weight: 500; }}
.test-id {{ color: var(--muted); font-size: 12px; margin-left: 8px; font-family: ui-monospace, Consolas, monospace; }}
.test-summary {{ color: var(--muted); font-size: 13px; }}
.test-duration {{ color: var(--muted); font-size: 12px; font-variant-numeric: tabular-nums; }}
.chevron {{ transition: transform 150ms; color: var(--muted); }}
.test-row.expanded .chevron {{ transform: rotate(90deg); }}
.test-detail {{
  display: none;
  padding: 12px 18px 16px 50px;
  background: var(--bg);
  border-top: 1px solid var(--border);
}}
.test-row.expanded .test-detail {{ display: block; }}
.detail-grid {{
  display: grid;
  grid-template-columns: 110px 1fr;
  gap: 6px 16px;
  margin: 4px 0;
}}
.detail-grid dt {{ color: var(--muted); font-size: 12px; }}
.detail-grid dd {{ margin: 0; font-size: 13px; }}
pre.codeblock {{
  background: var(--code-bg);
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 10px 12px;
  font-family: ui-monospace, Consolas, monospace;
  font-size: 12px;
  overflow-x: auto;
  white-space: pre-wrap;
  word-break: break-word;
  margin: 6px 0;
}}
pre.logblock .log-line {{ display: block; }}
pre.logblock .log-error {{ color: var(--fail); }}
.badge {{
  display: inline-block;
  padding: 2px 8px;
  border-radius: 999px;
  font-size: 11px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.5px;
}}
.badge.regressed {{ background: var(--regressed); color: white; }}
.badge.recovered {{ background: var(--recovered); color: white; }}
.badge.new {{ background: var(--new); color: white; }}
.badge.flaky {{ background: var(--muted); color: white; }}
.footer {{
  color: var(--muted);
  font-size: 12px;
  text-align: center;
  padding: 24px 12px;
}}
.footer a {{ color: var(--muted); }}
@media (max-width: 720px) {{
  .cards {{ grid-template-columns: repeat(2, 1fr); }}
  .test-summary-line {{ grid-template-columns: auto 1fr auto; gap: 8px; }}
  .test-summary {{ display: none; }}
  .test-detail {{ padding-left: 18px; }}
  .banner h1 {{ font-size: 20px; }}
  .wrap {{ padding: 12px; }}
}}
"""

# ─── JS (collapse/expand only) ───────────────────────────────────────────

JS = """
document.addEventListener('click', function(e) {
  var summary = e.target.closest('.test-summary-line');
  if (!summary) return;
  var row = summary.parentElement;
  if (row && row.classList.contains('test-row')) {
    row.classList.toggle('expanded');
  }
});
document.addEventListener('click', function(e) {
  var jump = e.target.closest('a[data-jump]');
  if (!jump) return;
  e.preventDefault();
  var id = jump.getAttribute('data-jump');
  var node = document.getElementById('test-' + id);
  if (node) {
    node.classList.add('expanded');
    node.scrollIntoView({behavior: 'smooth', block: 'center'});
  }
});
"""


# ─── Public entry ────────────────────────────────────────────────────────


def render_report(
    current: dict[str, Any],
    previous: dict[str, Any] | None = None,
    history: list[dict[str, Any]] | None = None,
    no_diff: bool = False,
    no_history: bool = False,
    prev_html_path: str | None = None,
) -> str:
    diff = compute_diff(current, None if no_diff else previous)
    parts: list[str] = []
    parts.append(_doc_open(current))
    parts.append(_status_banner(current))
    parts.append(_diff_banner(diff, prev_html_path))
    parts.append(_summary_cards(current))
    if not no_history:
        parts.append(_trend(history or []))
    parts.append(_tier_sections(current, diff))
    parts.append(_footer(current))
    parts.append(_doc_close())
    return "".join(parts)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("input_json", type=Path)
    p.add_argument("--prev", type=Path, default=None)
    p.add_argument("--no-diff", action="store_true")
    p.add_argument("--no-history", action="store_true")
    p.add_argument("--no-latest", action="store_true")
    p.add_argument("-o", "--output", type=Path, default=None)
    args = p.parse_args(argv)

    cur = json.loads(args.input_json.read_text(encoding="utf-8"))

    reports_dir = args.input_json.parent
    prev_path: Path | None = args.prev
    history: list[dict[str, Any]] = []

    if prev_path is None and not args.no_diff:
        prev_path = _find_previous(args.input_json, reports_dir)
    prev = None
    prev_html_path: str | None = None
    if prev_path and prev_path.exists():
        try:
            prev = json.loads(prev_path.read_text(encoding="utf-8"))
            html_sibling = prev_path.with_suffix(".html")
            if html_sibling.exists():
                prev_html_path = html_sibling.name
        except (json.JSONDecodeError, OSError):
            prev = None

    if not args.no_history:
        history = _load_history(args.input_json, reports_dir, limit=10)

    out_path = args.output or args.input_json.with_suffix(".html")
    body = render_report(
        cur,
        previous=prev,
        history=history,
        no_diff=args.no_diff,
        no_history=args.no_history,
        prev_html_path=prev_html_path,
    )
    out_path.write_text(body, encoding="utf-8")

    if not args.no_latest:
        latest = reports_dir / "latest.html"
        try:
            shutil.copyfile(out_path, latest)
        except OSError:
            pass
    print(str(out_path))
    return 0


# ─── Section helpers ─────────────────────────────────────────────────────


def _doc_open(current: dict[str, Any]) -> str:
    title = f"GLaDOS Smoke — {current.get('run_id', '')}"
    return (
        "<!DOCTYPE html>"
        '<html lang="en"><head>'
        '<meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{html.escape(title)}</title>"
        f"<style>{CSS}</style>"
        "</head><body><div class='wrap'>"
    )


def _doc_close() -> str:
    return f"</div><script>{JS}</script></body></html>"


def _status_banner(current: dict[str, Any]) -> str:
    summary = current.get("summary", {})
    overall = summary.get("overall", "EMPTY")
    if overall == "PASS":
        klass = "pass"
        headline = "GLaDOS is healthy"
        icon = _icon("pass", 32)
    elif overall == "FAIL":
        if summary.get("errors", 0) > 0 and summary.get("failed", 0) == 0:
            klass = "partial"
            headline = "Tests could not complete"
            icon = _icon("error", 32)
        else:
            klass = "fail"
            headline = "GLaDOS has issues"
            icon = _icon("fail", 32)
    else:
        klass = "empty"
        headline = "No tests ran"
        icon = _icon("skip", 32)

    target = current.get("target_host", "?")
    scheme = current.get("scheme", "https")
    started = current.get("started_at", "")
    commit = (current.get("git_commit") or "")[:8]
    duration = current.get("duration_sec", 0)
    return (
        f"<div class='banner {klass}'>"
        f"<div class='icon'>{icon}</div>"
        f"<div><h1>{html.escape(headline)}</h1>"
        f"<div class='meta'>{html.escape(started)} &middot; "
        f"{html.escape(scheme)}://{html.escape(target)} &middot; "
        f"commit {html.escape(commit) or '?'} &middot; "
        f"{duration}s</div></div>"
        f"</div>"
    )


def _diff_banner(diff: dict[str, Any], prev_html_path: str | None) -> str:
    if not diff or diff.get("previous_run_id") is None:
        return ""

    regressed = diff.get("regressed") or []
    recovered = diff.get("recovered") or []
    new = diff.get("new") or []
    removed = diff.get("removed") or []

    if regressed:
        klass = "regressed"
        headline = f"{len(regressed)} test{_pl(regressed)} regressed since last run"
        body = _id_link_list(regressed)
    elif recovered:
        klass = "recovered"
        headline = f"{len(recovered)} test{_pl(recovered)} recovered since last run"
        body = _id_link_list(recovered)
    elif new or removed:
        klass = "info"
        bits = []
        if new:
            bits.append(f"{len(new)} new")
        if removed:
            bits.append(f"{len(removed)} removed")
        headline = f"Test set changed: {', '.join(bits)}"
        body = ""
        if new:
            body += f"<div>New: {_id_link_list(new)}</div>"
        if removed:
            body += f"<div>Removed: {_id_text_list(removed)}</div>"
    else:
        klass = "unchanged"
        prev_started = diff.get("previous_started_at") or "earlier run"
        headline = f"No status changes since last run ({html.escape(prev_started)})"
        body = ""

    meta_bits: list[str] = []
    if diff.get("previous_started_at"):
        meta_bits.append(f"vs {html.escape(diff['previous_started_at'])}")
    if diff.get("host_changed"):
        meta_bits.append(
            f"comparing across hosts: "
            f"{html.escape(diff.get('previous_host') or '?')} -> "
            f"{html.escape(diff.get('current_host') or '?')}"
        )
    if diff.get("commit_changed"):
        prev_c = (diff.get("previous_commit") or "")[:8]
        cur_c = (diff.get("current_commit") or "")[:8]
        meta_bits.append(f"commit: {html.escape(prev_c)} -> {html.escape(cur_c)}")
    if diff.get("stale"):
        age = diff.get("stale_age_days") or 30
        meta_bits.append(f"baseline is {age} days old (stale)")
    if prev_html_path:
        meta_bits.append(
            f"<a href='{html.escape(prev_html_path)}'>view previous report</a>"
        )

    meta = (" &middot; ".join(meta_bits)) if meta_bits else ""

    return (
        f"<div class='diff-banner {klass}'>"
        f"<h3>{html.escape(headline)}</h3>"
        f"<div class='links'>{body}</div>"
        + (f"<div class='meta'>{meta}</div>" if meta else "")
        + "</div>"
    )


def _summary_cards(current: dict[str, Any]) -> str:
    s = current.get("summary", {})
    fails_id = "first-fail"
    cards = [
        ("Passed", s.get("passed", 0), "passed", None),
        ("Failed", s.get("failed", 0) + s.get("errors", 0), "failed",
         fails_id if (s.get("failed", 0) + s.get("errors", 0)) else None),
        ("Skipped", s.get("skipped", 0), "skipped", None),
        ("Duration", f"{current.get('duration_sec', 0)}s", "", None),
    ]
    out = ["<div class='cards'>"]
    for label, value, klass, jump in cards:
        if jump:
            v_html = (
                f"<a href='#{html.escape(jump)}'>{html.escape(str(value))}</a>"
            )
        else:
            v_html = html.escape(str(value))
        out.append(
            f"<div class='card {klass}'>"
            f"<div class='label'>{html.escape(label)}</div>"
            f"<div class='value'>{v_html}</div>"
            "</div>"
        )
    out.append("</div>")
    return "".join(out)


def _trend(history: list[dict[str, Any]]) -> str:
    if not history or len(history) < 2:
        return ""
    points: list[tuple[float, float, str, int, int]] = []
    for i, run in enumerate(history):
        s = run.get("summary", {})
        total = s.get("total", 0)
        passed = s.get("passed", 0)
        ratio = (passed / total) if total else 0.0
        points.append((i, ratio, run.get("started_at", ""), passed, total))

    width = 600
    height = 60
    margin = 6
    if len(points) == 1:
        coords = [(margin + (width - 2 * margin) / 2, height - margin)]
    else:
        coords = [
            (
                margin + (width - 2 * margin) * (p[0] / (len(points) - 1)),
                height - margin - (height - 2 * margin) * p[1],
            )
            for p in points
        ]
    path = "M" + " L".join(f"{x:.1f},{y:.1f}" for x, y in coords)
    dots = "".join(
        f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3" fill="{COLORS["pass"]}">'
        f'<title>{html.escape(p[2])} — {p[3]}/{p[4]} pass</title></circle>'
        for (x, y), p in zip(coords, points)
    )
    return (
        "<div class='trend'>"
        "<h4>Pass-rate trend (last runs)</h4>"
        f'<svg width="100%" viewBox="0 0 {width} {height}" '
        f'preserveAspectRatio="none" style="height:60px">'
        f'<path d="{path}" fill="none" stroke="{COLORS["pass"]}" stroke-width="2"/>'
        f"{dots}"
        "</svg>"
        "</div>"
    )


def _tier_sections(current: dict[str, Any], diff: dict[str, Any]) -> str:
    out: list[str] = []
    by_id = diff.get("by_id", {}) if diff else {}
    first_fail_emitted = False
    for tier in current.get("tiers", []):
        out.append("<div class='tier-section'>")
        tier_status = tier.get("status", "PASS")
        icon = _icon_for_status(tier_status)
        out.append(
            f"<div class='tier-header'>"
            f"{icon}"
            f"<div class='name'>{html.escape(tier.get('name', ''))}</div>"
            f"<div class='duration'>{tier.get('duration_sec', 0)}s</div>"
            f"</div>"
        )
        for t in tier.get("tests", []):
            tid = t.get("id", "")
            cat = by_id.get(tid)
            badge = _badge_for_category(cat, t)
            status = t.get("status", "PASS")
            expanded = (
                status in ("FAIL", "ERROR")
                or cat == "regressed"
            )
            row_classes = ["test-row"]
            if expanded:
                row_classes.append("expanded")
            if cat == "regressed":
                row_classes.append("regressed")
            elif status in ("FAIL", "ERROR"):
                row_classes.append("fail-row")

            anchor = ""
            if status in ("FAIL", "ERROR") and not first_fail_emitted:
                anchor = " id='first-fail'"
                first_fail_emitted = True

            out.append(
                f"<div class='{' '.join(row_classes)}' id='test-{html.escape(tid)}'{anchor}>"
                f"<div class='test-summary-line'>"
                f"{_icon_for_status(status)}"
                f"<div>"
                f"<span class='test-name'>{html.escape(t.get('name', ''))}</span>"
                f"<span class='test-id'>{html.escape(tid)}</span>"
                f" {badge}"
                f"<div class='test-summary'>{html.escape(t.get('summary') or '')}</div>"
                f"</div>"
                f"<div class='test-duration'>{t.get('duration_sec', 0)}s</div>"
                f"<div class='chevron'>›</div>"
                f"</div>"
                f"{_test_detail(t)}"
                f"</div>"
            )
        out.append("</div>")
    return "".join(out)


def _test_detail(t: dict[str, Any]) -> str:
    details = t.get("details") or {}
    parts = ["<div class='test-detail'>"]

    rows: list[tuple[str, Any]] = []
    if details.get("checked"):
        rows.append(("Checked", details["checked"]))
    if details.get("expected") is not None:
        rows.append(("Expected", details["expected"]))
    if details.get("actual") is not None:
        rows.append(("Actual", details["actual"]))

    if rows:
        parts.append("<dl class='detail-grid'>")
        for k, v in rows:
            parts.append(
                f"<dt>{html.escape(k)}</dt>"
                f"<dd>{_format_value(v)}</dd>"
            )
        parts.append("</dl>")

    if isinstance(details.get("extras"), dict) and details["extras"]:
        parts.append("<dl class='detail-grid'>")
        for k, v in details["extras"].items():
            parts.append(
                f"<dt>{html.escape(str(k))}</dt>"
                f"<dd>{_format_value(v)}</dd>"
            )
        parts.append("</dl>")

    if t.get("error"):
        msg = (t["error"] or {}).get("message", "")
        parts.append(
            "<div style='margin-top:8px'><strong>Error</strong></div>"
            f"<pre class='codeblock'>{html.escape(msg)}</pre>"
        )

    if t.get("logs"):
        parts.append(
            "<div style='margin-top:8px'><strong>Recent logs</strong></div>"
            "<pre class='codeblock logblock'>"
            + "".join(_log_line_html(line) for line in t["logs"])
            + "</pre>"
        )
    parts.append("</div>")
    return "".join(parts)


def _log_line_html(line: str) -> str:
    css = "log-line"
    if any(tok in line for tok in ("ERROR", "CRITICAL", "FATAL", "Traceback")):
        css += " log-error"
    return f"<span class='{css}'>{html.escape(line)}\n</span>"


def _format_value(v: Any) -> str:
    if isinstance(v, (dict, list)):
        try:
            text = json.dumps(v, indent=2, default=str)
            if len(text) > 800:
                text = text[:800] + "\n... (truncated)"
            return f"<pre class='codeblock'>{html.escape(text)}</pre>"
        except (TypeError, ValueError):
            return html.escape(str(v))
    return html.escape(str(v))


def _badge_for_category(cat: str | None, t: dict[str, Any]) -> str:
    if cat == "regressed":
        return "<span class='badge regressed'>regressed</span>"
    if cat == "recovered":
        return "<span class='badge recovered'>recovered</span>"
    if cat == "new":
        return "<span class='badge new'>new</span>"
    if cat == "flaky_candidate":
        return "<span class='badge flaky'>duration changed</span>"
    return ""


def _icon_for_status(status: str) -> str:
    mapping = {
        "PASS": "pass",
        "FAIL": "fail",
        "SKIP": "skip",
        "ERROR": "error",
    }
    return _icon(mapping.get(status, "skip"))


def _id_link_list(ids: list[str]) -> str:
    return " ".join(
        f"<a href='#test-{html.escape(i)}' data-jump='{html.escape(i)}'>"
        f"{html.escape(i)}</a>"
        for i in ids
    )


def _id_text_list(ids: list[str]) -> str:
    return ", ".join(html.escape(i) for i in ids)


def _pl(items: list[Any]) -> str:
    return "" if len(items) == 1 else "s"


def _footer(current: dict[str, Any]) -> str:
    return (
        "<div class='footer'>"
        f"GLaDOS smoke suite v{VERSION} &middot; "
        f"run {html.escape(current.get('run_id', ''))} &middot; "
        f"<a href='SURFACE_MAP.md'>SURFACE_MAP</a> &middot; "
        f"<a href='TEST_PLAN.md'>TEST_PLAN</a>"
        "</div>"
    )


# ─── History / previous-run discovery ────────────────────────────────────


def _find_previous(current: Path, reports_dir: Path) -> Path | None:
    candidates = sorted(
        [
            p
            for p in reports_dir.glob("smoke-*.json")
            if p.is_file() and p != current
        ],
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return candidates[0] if candidates else None


def _load_history(
    current: Path, reports_dir: Path, limit: int = 10
) -> list[dict[str, Any]]:
    files = sorted(
        [p for p in reports_dir.glob("smoke-*.json") if p.is_file()],
        key=lambda p: p.stat().st_mtime,
    )
    files = files[-limit:]
    out: list[dict[str, Any]] = []
    for p in files:
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError):
            continue
    return out


if __name__ == "__main__":
    sys.exit(main())
