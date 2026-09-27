"""A self-contained HTML report (inline SVG charts, light and dark theme) for the localization experiment."""

from __future__ import annotations

import html
import json

from .evaluate import ORDER

LIGHT = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948"]
DARK = ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#008300", "#9085e9", "#e66767"]
CANOPY_ORDER = ["open", "partial", "closed", "unknown"]
SOURCES = {"odom": "wheel odometry", "odom_alt": "LiDAR/visual odometry"}


def _e(v) -> str:
    return html.escape(str(v))


def _fmt(v, unit: str = "", digits: int = 1) -> str:
    if v is None:
        return "–"
    if isinstance(v, float):
        return f"{round(v, digits):g}{unit}"
    return f"{v}{unit}"


def _pct(v) -> str:
    return "–" if v is None else f"{v:.0%}"


def _css() -> str:
    slots = "\n".join(f".s{i + 1} {{ stroke: var(--series-{i + 1}); }} .f{i + 1} {{ fill: var(--series-{i + 1}); }}" for i in range(8))
    light = "".join(f"--series-{i + 1}: {c};" for i, c in enumerate(LIGHT))
    dark = "".join(f"--series-{i + 1}: {c};" for i, c in enumerate(DARK))
    dark_tokens = (f"color-scheme: dark; --surface: #1a1a19; --surface-2: #252523; --border: #3a3a37; --text: #ffffff; "
                   f"--text-2: #c3c2b7; --muted: #96958d; --good: #3fbf3f; --bad: #e66767; --warn-bg: #3a2f10; "
                   f"--warn-ink: #f2d38a; {dark}")
    return f"""
:root {{ color-scheme: light; --surface: #fcfcfb; --surface-2: #f0efec; --border: #dddcd6; --text: #0b0b0b;
        --text-2: #52514e; --muted: #7a7973; --good: #0a7d0a; --bad: #c03434; --warn-bg: #fff4d6; --warn-ink: #6b4a00; {light} }}
@media (prefers-color-scheme: dark) {{ :root:not([data-theme="light"]) {{ {dark_tokens} }} }}
:root[data-theme="dark"] {{ {dark_tokens} }}
body {{ margin: 0; background: var(--surface); color: var(--text); font: 15px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; }}
main {{ max-width: 1100px; margin: 0 auto; padding: 24px 16px 64px; }}
h1 {{ font-size: 24px; margin: 0 0 4px; }} h2 {{ font-size: 18px; margin: 32px 0 8px; }}
.muted {{ color: var(--muted); }} .small {{ font-size: 13px; }}
.banner {{ background: var(--warn-bg); color: var(--warn-ink); padding: 10px 14px; border-radius: 8px; margin: 12px 0; }}
.answer {{ border: 1px solid var(--border); border-left: 4px solid var(--series-1); border-radius: 8px; padding: 4px 16px; background: var(--surface-2); }}
.scroll {{ overflow-x: auto; }}
table {{ border-collapse: collapse; width: 100%; font-size: 13px; margin: 8px 0; }}
th, td {{ text-align: left; padding: 6px 8px; border-bottom: 1px solid var(--border); vertical-align: top; }}
th {{ color: var(--text-2); font-weight: 600; }} td.num, th.num {{ text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }}
tr.current td {{ background: var(--surface-2); }}
td.setup {{ min-width: 190px; }}
.badge {{ display: inline-block; font-size: 11px; border: 1px solid var(--border); border-radius: 9px; padding: 0 6px; margin-left: 4px; color: var(--text-2); }}
.ok {{ color: var(--good); font-weight: 600; }} .no {{ color: var(--bad); font-weight: 600; }}
svg text {{ fill: var(--text-2); font: 11px system-ui, sans-serif; }} svg .strong {{ fill: var(--text); font-weight: 600; }}
svg .axis {{ stroke: var(--border); }} svg .grid {{ stroke: var(--border); stroke-dasharray: 2 3; }}
svg .req {{ stroke: var(--text-2); stroke-dasharray: 5 4; }} svg .ref {{ fill: var(--surface); stroke: var(--text); stroke-width: 2; }}
svg .label {{ paint-order: stroke; stroke: var(--surface); stroke-width: 4px; stroke-linejoin: round; }}
svg .tick {{ stroke: var(--text); stroke-width: 2; }} svg .hollow {{ fill: var(--surface); stroke-width: 1.8; }}
.panels {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(250px, 1fr)); gap: 12px; }}
.panel {{ border: 1px solid var(--border); border-radius: 8px; padding: 6px; }}
.legend {{ display: flex; flex-wrap: wrap; gap: 12px; font-size: 13px; color: var(--text-2); margin: 6px 0; }}
.legend i {{ display: inline-block; width: 18px; height: 3px; vertical-align: middle; margin-right: 5px; border-radius: 2px; }}
.chart {{ width: 100%; height: auto; }}
{slots}
"""


def _available(result: dict) -> list[str]:
    return [m for m in ORDER if result["methods"].get(m, {}).get("available")]


def tracks_chart(result: dict) -> str:
    methods = _available(result)
    pts = [p for m in methods for track in result["methods"][m]["tracks"] for p in track]
    if not pts:
        return ""
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    x0, x1, y0, y1 = min(xs) - 8, max(xs) + 8, min(ys) - 8, max(ys) + 8
    W, H = 250, 200
    k = min(W / (x1 - x0), H / (y1 - y0))
    X = lambda x: (x - x0) * k  # noqa: E731
    Y = lambda y: H - (y - y0) * k  # noqa: E731
    panels = []
    for m in methods:
        r = result["methods"][m]
        parts = []
        for i, track in enumerate(r["tracks"]):
            d = " ".join(f"{'M' if j == 0 else 'L'}{X(x):.1f},{Y(y):.1f}" for j, (x, y) in enumerate(track))
            parts.append(f'<path d="{d}" class="s{i % 8 + 1}" fill="none" stroke-width="1.4" stroke-linejoin="round"/>')
        for o in r["observations"]:
            i = result["runs"].index(o["run"]) if o["run"] in result["runs"] else 0
            parts.append(f'<circle cx="{X(o["xy"][0]):.1f}" cy="{Y(o["xy"][1]):.1f}" r="3" class="f{i % 8 + 1}">'
                         f'<title>{_e(o["tag"])} ({_e(o["run"])}): stand {o["stand"]}, ±{o["sigma"]} m</title></circle>')
        for name, (x, y) in result.get("_refs_xy", {}).items():
            parts.append(f'<circle cx="{X(x):.1f}" cy="{Y(y):.1f}" r="4.5" class="ref"><title>reference {_e(name)}</title></circle>')
        parts.append(f'<line x1="6" x2="{6 + 20 * k:.1f}" y1="{H - 6}" y2="{H - 6}" class="tick"/>'
                     f'<text x="6" y="{H - 10}">20 m</text>')
        panels.append(f'<div class="panel"><div class="small"><b>{_e(r["hardware"])}</b></div>'
                      f'<svg class="chart" viewBox="0 0 {W} {H}" role="img" aria-label="tracks, {_e(m)}">{"".join(parts)}</svg></div>')
    legend = "".join(f'<span><i style="background: var(--series-{i % 8 + 1})"></i>{_e(run)}</span>' for i, run in enumerate(result["runs"]))
    legend += '<span><svg width="12" height="12"><circle cx="6" cy="6" r="4" class="ref"/></svg> reference point</span>'
    legend += '<span>dots: observations of the tagged plants, coloured by run</span>'
    return f'<div class="legend">{legend}</div><div class="panels">{"".join(panels)}</div>'


def _rows_chart(result: dict, methods: list[str], vmax: float, draw_row, marker: tuple[float, str] | None = None) -> str:
    """One row per setup, a shared metre axis."""
    W, row, left = 760, 30, 250
    top = 18 if marker else 0                  # room for the marker label
    H = top + row * len(methods) + 30
    X = lambda v: left + min(v, vmax) / vmax * (W - left - 70)  # noqa: E731
    parts = [f'<line class="axis" x1="{left}" x2="{left}" y1="0" y2="{H - 24}"/>']
    step = 5 if vmax <= 40 else 10
    for g in range(0, int(vmax) + 1, step):
        parts.append(f'<line class="grid" x1="{X(g):.1f}" x2="{X(g):.1f}" y1="0" y2="{H - 24}"/>'
                     f'<text x="{X(g):.1f}" y="{H - 8}" text-anchor="middle">{g} m</text>')
    if marker:                                  # behind the bars and their labels
        v, label = marker
        parts.append(f'<line class="req" x1="{X(v):.1f}" x2="{X(v):.1f}" y1="0" y2="{H - 24}" stroke-width="1.5"/>'
                     f'<text x="{X(v) + 4:.1f}" y="10" class="strong">{_e(label)}</text>')
    for i, m in enumerate(methods):
        r = result["methods"][m]
        y = top + i * row + 6
        cur = " (current)" if m == result.get("current_method") else ""
        parts.append(f'<text x="{left - 8}" y="{y + 14}" text-anchor="end" class="strong">{_e(r["hardware"] + cur)}</text>')
        parts.append(draw_row(r, y, X))
    return f'<svg class="chart" viewBox="0 0 {W} {H}" role="img">{"".join(parts)}</svg>'


def separation_chart(result: dict) -> str:
    methods = _available(result)
    req = result["criteria"]["required_separation_m"]
    vmax = max([result["methods"][m]["separation_needed_m"] or 0 for m in methods] + [req]) * 1.15

    def row(r, y, X):
        v = r["separation_needed_m"]
        if v is None:
            return f'<text x="{X(0) + 6}" y="{y + 14}">not measured</text>'
        w = max(X(v) - X(0), 5)
        return (f'<path d="M{X(0)},{y} h{w - 4:.1f} q4,0 4,4 v10 q0,4 -4,4 h-{w - 4:.1f} z" class="f1"><title>{v} m</title></path>'
                f'<text x="{X(v) + 6:.1f}" y="{y + 14}" class="label">{v:g} m</text>')
    return _rows_chart(result, methods, vmax, row, (req, f"needed: {req:g} m"))


def repeat_chart(result: dict) -> str:
    """Every pair of observations of the same plant: their distance, against the distance up to
    which Forest Care links them into one stand (the tick)."""
    methods = _available(result)
    pairs = [p for m in methods for p in result["methods"][m]["repeat_pairs"]]
    if not pairs:
        return ""
    vmax = max(max(d for d, _ in pairs), max(link for _, link in pairs)) * 1.1

    def row(r, y, X):
        out = []
        links = sorted(link for _, link in r["repeat_pairs"])
        if links:
            med = links[len(links) // 2]
            out.append(f'<line class="tick" x1="{X(med):.1f}" x2="{X(med):.1f}" y1="{y - 2}" y2="{y + 20}"><title>link distance {med} m</title></line>')
        for d, link in r["repeat_pairs"]:
            cls = 'class="f1"' if d <= link else 'class="hollow s2"'
            out.append(f'<circle cx="{X(d):.1f}" cy="{y + 9}" r="3.2" {cls} opacity="0.85"><title>{d} m apart; linked up to {link} m</title></circle>')
        return "".join(out)
    legend = ('<div class="legend"><span><svg width="12" height="12"><circle cx="6" cy="6" r="4" class="f1"/></svg> pair close enough to be linked</span>'
              '<span><svg width="12" height="12"><circle cx="6" cy="6" r="4" class="hollow s2"/></svg> pair too far apart to be linked directly</span>'
              '<span><svg width="10" height="14"><line x1="5" x2="5" y1="0" y2="14" class="tick"/></svg> link distance (median)</span></div>')
    return legend + _rows_chart(result, methods, vmax, row)


def gnss_chart(result: dict) -> str:
    g = result["gnss"].get("gnss") or {}
    classes = [c for c in CANOPY_ORDER if c in g]
    if not classes:
        return ""
    series = [("reported 1σ (median)", lambda c: g[c]["reported_sigma_m"]["median"]),
              ("true error (median)", lambda c: g[c]["true_error_m"]["median"])]
    if not any(g[c]["true_error_m"]["n"] for c in classes):
        series = series[:1]
    vmax = max((f(c) or 0) for _, f in series for c in classes) * 1.25 or 1
    W, H, bottom, group = 520, 220, 30, 150
    Y = lambda v: (H - bottom) - v / vmax * (H - bottom - 20)  # noqa: E731
    parts = [f'<line class="axis" x1="40" x2="{W}" y1="{H - bottom}" y2="{H - bottom}"/>']
    for gi, c in enumerate(classes):
        gx = 60 + gi * group
        for si, (name, f) in enumerate(series):
            v = f(c)
            if v is None:
                continue
            x = gx + si * 44
            parts.append(f'<rect x="{x}" y="{Y(v):.1f}" width="40" height="{(H - bottom) - Y(v):.1f}" rx="3" class="f{si + 1}"><title>{name}: {v} m</title></rect>'
                         f'<text x="{x + 20}" y="{Y(v) - 4:.1f}" text-anchor="middle">{v:g} m</text>')
        parts.append(f'<text x="{gx + 42}" y="{H - 10}" text-anchor="middle" class="strong">{c} ({g[c]["valid_share"]:.0%} valid)</text>')
    legend = "".join(f'<span><i style="background: var(--series-{i + 1}); height: 10px"></i>{_e(n)}</span>' for i, (n, _) in enumerate(series))
    return f'<div class="legend">{legend}</div><svg class="chart" style="max-width:{W}px" viewBox="0 0 {W} {H}" role="img" aria-label="GNSS error by canopy">{"".join(parts)}</svg>'


def drift_chart(result: dict) -> str:
    curves = [(i, run, pts) for i, (src, d) in enumerate(result["drift"].items()) for run, pts in d.get("curves", []) if pts]
    if not curves:
        return ""
    xmax = max(p[0] for _, _, pts in curves for p in pts)
    ymax = max(p[1] for _, _, pts in curves for p in pts) * 1.1 or 1
    W, H, left, bottom = 620, 230, 44, 28
    X = lambda v: left + v / xmax * (W - left - 10)  # noqa: E731
    Y = lambda v: (H - bottom) - v / ymax * (H - bottom - 10)  # noqa: E731
    parts = [f'<line class="axis" x1="{left}" x2="{W}" y1="{H - bottom}" y2="{H - bottom}"/>'
             f'<line class="axis" x1="{left}" x2="{left}" y1="0" y2="{H - bottom}"/>']
    for g in range(0, int(xmax) + 1, 50):
        parts.append(f'<text x="{X(g):.1f}" y="{H - 10}" text-anchor="middle">{g} m</text>')
    ystep = 5 if ymax <= 40 else 10
    for g in range(0, int(ymax) + 1, ystep):
        parts.append(f'<line class="grid" x1="{left}" x2="{W}" y1="{Y(g):.1f}" y2="{Y(g):.1f}"/>'
                     f'<text x="{left - 6}" y="{Y(g) + 4:.1f}" text-anchor="end">{g} m</text>')
    for i, run, pts in curves:
        d = " ".join(f"{'M' if j == 0 else 'L'}{X(x):.1f},{Y(y):.1f}" for j, (x, y) in enumerate(pts))
        dash = ' stroke-dasharray="5 3"' if i else ""
        parts.append(f'<path d="{d}" class="s{i + 1}" fill="none" stroke-width="1.5"{dash}><title>{_e(run)}</title></path>')
    legend = "".join(f'<span><i style="background: var(--series-{i + 1})"></i>{_e(SOURCES.get(src, src))}{" (dashed)" if i else ""}</span>'
                     for i, src in enumerate(result["drift"]))
    return (f'<div class="legend">{legend}<span>one line per run · x: distance driven · y: position error</span></div>'
            f'<svg class="chart" style="max-width:{W}px" viewBox="0 0 {W} {H}" role="img" aria-label="odometry drift">{"".join(parts)}</svg>')


def render(result: dict, experiment_file: str) -> str:
    methods = [m for m in ORDER if m in result["methods"]]
    rec = result["recommendation"]
    truth = any(result["methods"][m].get("trajectory_error_m", {}).get("n") for m in _available(result))
    rows = []
    pair = lambda s: f'{_fmt(s["median"])} / {_fmt(s["p95"], " m")}'  # noqa: E731
    for m in methods:
        r = result["methods"][m]
        current = m == result.get("current_method")
        badge = '<span class="badge">current</span>' if current else ""
        if not r.get("available"):
            rows.append(f'<tr><td><b>{_e(r["hardware"])}</b>{badge}</td><td colspan="8" class="muted">not available: {_e(r["reason"])}</td></tr>')
            continue
        verdict = '<span class="ok">sufficient</span>' if r["sufficient"] else '<span class="no">not sufficient</span>'
        groups = ", ".join("+".join(g) for g in r["stands"]["merged_groups"])
        merged = f'<div class="small muted">one stand: {_e(groups)}</div>' if groups else ""
        honest = _pct(r["coverage_95"])
        if "localization" in r["settings"]:
            honest += f'<div class="small muted">×{_fmt(r["sigma_scale"])}, was {_pct(r["coverage_95_uncorrected"])}</div>'
        rows.append(
            f'<tr class="{"current" if current else ""}"><td class="setup"><b>{_e(r["hardware"])}</b>{badge}<div class="small muted">{_e(m)}</div></td>'
            f'<td>{verdict}</td>'
            + (f'<td class="num">{pair(r["trajectory_error_m"])}</td>' if truth else "")
            + f'<td class="num">{_fmt(r["reference_error_m"]["p95"], " m")}</td>'
            f'<td class="num">{pair(r["track_offset_m"])}</td><td class="num">{pair(r["observation_repeat_m"])}</td>'
            f'<td class="num">{r["stands"]["reidentified"]}/{r["stands"]["plants"]}{merged}</td>'
            f'<td class="num">{_fmt(r["separation_needed_m"], " m")}</td><td class="num">{honest}</td></tr>')
    head = ('<tr><th>Setup</th><th>For Forest Care</th>' + ('<th class="num">True error<br>median / 95 %</th>' if truth else "")
            + '<th class="num">Reference<br>points, 95 %</th><th class="num">Track offset<br>median / 95 %</th>'
              '<th class="num">Same plant<br>median / 95 %</th><th class="num">Plants<br>re-found</th>'
              '<th class="num">Stands stay<br>separate from</th><th class="num">Errors inside<br>95 % circle</th></tr>')
    g = result["gnss"]
    gnss_rows = "".join(
        f'<tr><td>{_e({"gnss": "GNSS", "gnss_rtk": "RTK GNSS"}.get(stream, stream))}</td><td>{_e(c)}</td><td class="num">{v["fixes"]}</td>'
        f'<td class="num">{_pct(v["valid_share"])}</td><td>{_e(", ".join(f"{k}: {n}" for k, n in sorted(v["status"].items())))}</td>'
        f'<td class="num">{_fmt(v["reported_sigma_m"]["median"], " m", 2)}</td>'
        f'<td class="num">{_fmt(v["true_error_m"]["median"], " m", 2)} / {_fmt(v["true_error_m"]["p95"], " m", 2)}</td>'
        f'<td class="num">{_fmt(v["true_vs_reported"])}</td></tr>'
        for stream, classes in g.items()
        for c, v in sorted(classes.items(), key=lambda kv: CANOPY_ORDER.index(kv[0]) if kv[0] in CANOPY_ORDER else 9))
    drift_rows = "".join(
        f'<tr><td>{_e(SOURCES.get(k, k))}</td><td class="num">{_fmt(v["loop_closure_per_100m"]["median"], " m")} '
        f'({v["loop_closure_per_100m"]["n"]} loops)</td><td class="num">{_fmt(v["error_per_100m"]["median"], " m")} / '
        f'{_fmt(v["error_per_100m"]["p95"], " m")}</td></tr>' for k, v in result["drift"].items())
    banner = ('<div class="banner"><b>Simulated data.</b> These runs come from the gateway simulator. They show that the '
              'experiment and its analysis work, and what to expect; they do not measure the real rover. Repeat the '
              'experiment in the field (docs/LOCALIZATION_VALIDATION.md).</div>') if result["simulated"] else ""
    c = result["criteria"]
    operator = next((result["methods"][m].get("true_track_offset_m") for m in _available(result)), None)
    operator_note = "" if not operator else f" The operator's own variation between runs (true tracks) is {_fmt(operator['p95'], ' m')} at 95 %."
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Localization test report</title><style>{_css()}</style></head><body><main>
<h1>Localization test: {_e(result["experiment"])}</h1>
<div class="muted small">{len(result["runs"])} runs · {_e(experiment_file)} · a setup is sufficient if ≥ {c["reid_rate"]:.0%} of the tagged
plants land in the same stand in every run, stands ≥ {c["required_separation_m"]:g} m apart stay separate, and ≥ {c["coverage_95"]:.0%} of errors
fall inside the 95 % uncertainty circle</div>
{banner}
<h2>Answer</h2>
<div class="answer">{"".join(f"<p>{_e(line)}</p>" for line in rec["summary"])}
<p><b>Improve next</b></p><ol>{"".join(f"<li>{_e(x)}</li>" for x in rec["improve_next"])}</ol></div>
<h2>Setups compared</h2>
<div class="scroll"><table><thead>{head}</thead><tbody>{"".join(rows)}</tbody></table></div>
<p class="small muted">Every setup is computed from the same recorded runs. <b>Track offset</b>: distance between the tracks of two
runs, smoothed over 10 s, so it shows systematic shifts rather than jitter.{operator_note} <b>Same plant across runs</b>: distance between
the positions Forest Care gives the same tagged plant in different runs. <b>Stands stay separate from</b>: the distance at which two stands
stay separate in 95 % of cases under Forest Care's stand rule, given the measured errors. <b>Errors inside the 95 % circle</b>: with the
uncertainty correction the experiment recommends (localization.sigma_scale), and the share before it.</p>
<h2>Is the same plant recognised in every run?</h2>{repeat_chart(result)}
<h2>Can two nearby stands be told apart?</h2>{separation_chart(result)}
<h2>Tracks of every run</h2>{tracks_chart(result)}
<h2>GNSS quality under canopy</h2>{gnss_chart(result)}
<div class="scroll"><table><thead><tr><th>Receiver</th><th>Canopy</th><th class="num">Fixes</th><th class="num">Valid</th>
<th>Status (−1 none, 0 single, 1 SBAS, 2 RTK)</th><th class="num">Reported 1σ</th><th class="num">True error median / 95 %</th>
<th class="num">True ÷ reported</th></tr></thead><tbody>{gnss_rows}</tbody></table></div>
<h2>Drift without GNSS</h2>{drift_chart(result)}
<div class="scroll"><table><thead><tr><th>Odometry alone</th><th class="num">Loop closure error per 100 m (field method)</th>
<th class="num">Error per 100 m from the true start, median / 95 %</th></tr></thead>
<tbody>{drift_rows or '<tr><td colspan="3" class="muted">no odometry recorded</td></tr>'}</tbody></table></div>
<h2>Raw results</h2><details><summary class="small">results.json</summary><pre class="small">{_e(json.dumps({k: v for k, v in result.items() if not k.startswith('_')}, indent=1)[:200000])}</pre></details>
</main></body></html>"""
