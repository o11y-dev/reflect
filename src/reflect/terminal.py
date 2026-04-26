from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime

from reflect.graph import _compute_tool_transitions, _compute_weekly_trends
from reflect.insights import (
    build_observations,
    build_recommendations,
    build_strengths,
    compute_token_economy,
)
from reflect.models import TelemetryStats
from reflect.utils import (
    _bar,
    _fmt_model,
    _fmt_tokens,
    _safe_ratio,
    _sanitize_command_counter,
    _stat_panel,
)


def _render_terminal(  # noqa: C901
    stats: TelemetryStats,
    *,
    publish_url: str | None = None,
    console=None,
    time_range: str = "week",
    since: datetime | None = None,
) -> None:
    from rich import box
    from rich.columns import Columns
    from rich.console import Console
    from rich.panel import Panel
    from rich.rule import Rule
    from rich.table import Table
    from rich.text import Text

    if console is None:
        console = Console(force_terminal=True)

    # ── header ───────────────────────────────────────────────────────────────
    console.print()
    range_labels = {"day": "Last 24h", "week": "Last 7 days", "month": "Last 30 days", "all": "All time"}
    range_label = range_labels.get(time_range, "Last 7 days")
    if since:
        range_label += f"  ({since.strftime('%b %d')} — {datetime.now(tz=UTC).strftime('%b %d')})"
    elif stats.first_event_ts and stats.last_event_ts:
        range_label = f"All time  ({stats.first_event_ts[:10]} → {stats.last_event_ts[:10]})"
    console.print(Rule(f"[bold cyan]AI Usage Dashboard[/]  [dim]{range_label}[/]"))
    console.print()

    # ── Insights ──────────────────────────────────────────────────────────────
    _strengths = build_strengths(stats)
    _observations = build_observations(stats)
    _recommendations = build_recommendations(stats)

    insight_items: list[str] = []
    for s in _strengths[:3]:
        insight_items.append(f"[green]✓[/] {s.replace('**', '')}")
    for o in _observations[:3]:
        insight_items.append(f"[yellow]⚠[/] {o.replace('**', '')}")
    for r in _recommendations[:4]:
        insight_items.append(f"[cyan]→[/] {r}")

    if insight_items:
        console.print(Panel(
            "\n".join(insight_items),
            title="[bold]Insights[/]",
            border_style="bright_cyan",
        ))
        console.print()

    if not stats.total_events:
        console.print(Panel(
            "\n".join([
                "No telemetry yet.",
                "",
                "Run [bold]reflect setup[/] to wire local capture,",
                "run [bold]reflect doctor[/] to confirm telemetry files,",
                "or try [bold]reflect --demo[/] to explore the dashboard with sample data.",
            ]),
            title="[bold]Getting started[/]",
            border_style="yellow",
        ))
        console.print()

    # ── Summary stat cards ───────────────────────────────────────────────────
    prompts   = stats.events_by_type.get("UserPromptSubmit", 0)
    pre_tool  = stats.events_by_type.get("PreToolUse", 0)
    failures  = stats.events_by_type.get("PostToolUseFailure", 0)
    subagents = stats.events_by_type.get("SubagentStart", 0)
    top_model = _fmt_model(stats.models_by_count.most_common(1)[0][0]) if stats.models_by_count else "N/A"
    ratio     = _safe_ratio(pre_tool, prompts)
    fail_pct  = round(100 * _safe_ratio(failures, pre_tool), 1)

    avg_quality = 0.0
    if stats.sessions_seen:
        avg_quality = sum(stats.session_quality_scores.values()) / len(stats.sessions_seen)

    console.print(Columns([
        _stat_panel("Quality Score", f"{avg_quality:.1f}%",        "bold green" if avg_quality > 70 else "yellow"),
        _stat_panel("Sessions",     f"{len(stats.sessions_seen)}", "blue"),
        _stat_panel("Active Days",  f"{stats.days_active}",        "green"),
        _stat_panel("Prompts",      f"{prompts:,}",                "magenta"),
        _stat_panel("Tool/Prompt",  f"{ratio:.1f}:1",              "yellow"),
        _stat_panel("Failure %",    f"{fail_pct}%",                "red"),
        _stat_panel("Subagents",    f"{subagents}",                "purple"),
        _stat_panel("Top Model",    top_model,                     "white"),
    ], equal=True))
    console.print()

    # ── Per-agent comparison ──────────────────────────────────────────────────
    if len(stats.agents) > 1:
        agent_tbl = Table(box=box.SIMPLE_HEAD, show_lines=False)
        agent_tbl.add_column("Agent",    style="bold white", no_wrap=True)
        agent_tbl.add_column("Sessions", justify="right")
        agent_tbl.add_column("Events",   justify="right")
        agent_tbl.add_column("Quality",  no_wrap=True)
        agent_tbl.add_column("Top Model", style="magenta", no_wrap=True)
        agent_tbl.add_column("Top Tools", style="dim")
        agent_tbl.add_column("In Tok",   justify="right", style="cyan")
        agent_tbl.add_column("Out Tok",  justify="right", style="green")
        agent_tbl.add_column("Fail %",   justify="right", style="red")

        for ag_name, ag in sorted(stats.agents.items()):
            ag_top_model = _fmt_model(ag.models_by_count.most_common(1)[0][0]) if ag.models_by_count else "—"
            ag_top_tools = ag.tools_by_count.most_common(1)[0][0] if ag.tools_by_count else "—"
            avg_q = ag.total_quality_score / len(ag.sessions_seen) if ag.sessions_seen else 0
            q_color = "green" if avg_q > 70 else "yellow" if avg_q > 40 else "red"
            ag_tool_calls = ag.events_by_type.get("PreToolUse", 0)
            ag_failures = ag.events_by_type.get("PostToolUseFailure", 0)
            ag_fail_pct = round(100 * ag_failures / ag_tool_calls, 1) if ag_tool_calls else 0

            q_filled = round(avg_q / 100 * 5)
            q_label = "High" if avg_q > 70 else "Med" if avg_q > 40 else "Low"
            quality_cell = _bar(q_filled, 5, q_color) + Text(f" {q_label}", style=q_color)

            agent_tbl.add_row(
                ag_name,
                str(len(ag.sessions_seen)),
                f"{ag.total_events:,}",
                quality_cell,
                ag_top_model,
                ag_top_tools or "—",
                _fmt_tokens(ag.total_input_tokens),
                _fmt_tokens(ag.total_output_tokens),
                f"{ag_fail_pct}%",
            )

        console.print(Panel(agent_tbl, title="[bold]Agent Comparison[/]", border_style="dim"))
        console.print()

    # ── Token usage cards ────────────────────────────────────────────────────
    if stats.total_input_tokens or stats.total_output_tokens:
        economy = compute_token_economy(stats)
        out_in_ratio = _safe_ratio(stats.total_output_tokens, stats.total_input_tokens)
        cache_pct = round(100 * _safe_ratio(stats.total_cache_read_tokens, stats.total_input_tokens), 1)
        console.print(Columns([
            _stat_panel("Input Tokens",  _fmt_tokens(stats.total_input_tokens),  "cyan"),
            _stat_panel("Output Tokens", _fmt_tokens(stats.total_output_tokens), "green"),
            _stat_panel("Cache Create",  _fmt_tokens(stats.total_cache_creation_tokens), "yellow"),
            _stat_panel("Cache Read",    _fmt_tokens(stats.total_cache_read_tokens), "blue"),
            _stat_panel("Out/In Ratio",  f"{out_in_ratio:.2f}", "magenta"),
            _stat_panel("Cache Hit %",   f"{cache_pct}%", "bright_cyan"),
        ], equal=True))
        console.print()

        console.print(Columns([
            _stat_panel("In / Prompt", _fmt_tokens(int(economy["avg_input_per_prompt"])), "cyan"),
            _stat_panel("Out / Prompt", _fmt_tokens(int(economy["avg_output_per_prompt"])), "green"),
            _stat_panel("Top Session Share", f"{economy['top_session_share']:.1f}%", "yellow"),
            _stat_panel("Context-Heavy", str(economy["high_context_sessions"]), "magenta"),
            _stat_panel("Reads / Prompt", f"{economy['reads_per_prompt']:.1f}", "blue"),
            _stat_panel("MCP / Prompt", f"{economy['mcp_per_prompt']:.1f}", "purple"),
            _stat_panel("Heavy-Model Share", f"{economy['heavy_model_share']:.0f}%", "white"),
        ], equal=True))
        console.print()
        if stats.total_cost_usd > 0:
            console.print(Columns([
                _stat_panel("Est. Total Cost", f"${stats.total_cost_usd:,.2f}", "green"),
                _stat_panel("Input Cost", f"${stats.input_cost_usd:,.2f}", "cyan"),
                _stat_panel("Output Cost", f"${stats.output_cost_usd:,.2f}", "yellow"),
                _stat_panel("Pricing Source", stats.pricing_source or "unknown", "blue"),
            ], equal=True))
            console.print()

    # ── Activity heatmap / day breakdown ────────────────────────────────────
    from datetime import date as _date
    from datetime import timedelta as _td

    if time_range in ("day", "week"):
        # Compact daily breakdown for short time ranges
        _today = _date.today()
        _n_days = 1 if time_range == "day" else 7
        _days = [_today - _td(days=_n_days - 1 - i) for i in range(_n_days)]
        _max_day = max((stats.activity_by_day.get(d.strftime("%Y-%m-%d"), 0) for d in _days), default=1) or 1
        _BAR_W = 30
        day_rows: list[Text] = []
        for d in _days:
            key = d.strftime("%Y-%m-%d")
            cnt = stats.activity_by_day.get(key, 0)
            filled = round(cnt / _max_day * _BAR_W) if _max_day else 0
            row = Text(f"{d.strftime('%a %b %d')} ", style="dim")
            row.append_text(_bar(filled, _BAR_W, "cyan"))
            row.append(f" {cnt}", style="dim")
            day_rows.append(row)
        if day_rows:
            console.print(Panel(
                Text("\n").join(day_rows),
                title=f"[bold]Activity — {'Today' if time_range == 'day' else 'This Week'}[/]",
                border_style="dim",
            ))
            console.print()
    else:
        # Year heatmap for --month and --all
        _today = _date.today()
        all_days = [_today - _td(days=364 - i) for i in range(365)]
        active_vals = sorted(v for v in stats.activity_by_day.values() if v > 0)

        def _quartile(arr: list, q: float) -> int:
            return arr[int(len(arr) * q)] if arr else 1

        q1 = _quartile(active_vals, .25)
        q2 = _quartile(active_vals, .50)
        q3 = _quartile(active_vals, .75)

        CELL_STYLES = ["dim white", "blue", "cyan", "bright_cyan", "bold bright_white"]
        CELL_CHARS  = ["·", "▪", "▪", "■", "█"]

        def _cell(c: int) -> Text:
            if c == 0:
                idx = 0
            elif c <= q1:
                idx = 1
            elif c <= q2:
                idx = 2
            elif c <= q3:
                idx = 3
            else:
                idx = 4
            return Text(CELL_CHARS[idx], style=CELL_STYLES[idx])

        start_dow = (all_days[0].weekday() + 1) % 7
        padded: list = [None] * start_dow + all_days
        while len(padded) % 7:
            padded.append(None)
        weeks_grid = [padded[i:i+7] for i in range(0, len(padded), 7)]

        month_row = Text("    ")
        prev_mo = ""
        for week in weeks_grid:
            first = next((d for d in week if d), None)
            mo = first.strftime("%b") if first else "   "
            label = mo if mo != prev_mo else "   "
            month_row.append(f"{label:<3}", style="dim")
            if mo != prev_mo:
                prev_mo = mo

        DAY_LABELS = ["Su", "Mo", "Tu", "We", "Th", "Fr", "Sa"]
        heatmap = Text()
        heatmap.append_text(month_row)
        heatmap.append("\n")
        for dow in range(7):
            heatmap.append(f"{DAY_LABELS[dow]} ", style="dim")
            for week in weeks_grid:
                d = week[dow]
                if d is None:
                    heatmap.append("  ")
                else:
                    c = stats.activity_by_day.get(d.strftime("%Y-%m-%d"), 0)
                    heatmap.append_text(_cell(c))
                    heatmap.append(" ")
            if dow < 6:
                heatmap.append("\n")

        console.print(Panel(heatmap, title="[bold]Activity — Last Year[/]", border_style="dim"))
        console.print()

    # ── Activity by hour + Week-over-week (side by side) ────────────────────
    max_h = max(stats.activity_by_hour.values(), default=1)
    BAR_W = 14
    hour_rows: list[Text] = []
    for h in range(24):
        cnt = stats.activity_by_hour.get(h, 0)
        filled = round(cnt / max_h * BAR_W) if max_h else 0
        row = Text(f"{h:02d}h ", style="dim")
        row.append_text(_bar(filled, BAR_W, "cyan"))
        row.append(f" {cnt}", style="dim")
        hour_rows.append(row)

    hour_left  = Text("\n").join(hour_rows[:12])
    hour_right = Text("\n").join(hour_rows[12:])
    hour_panel = Panel(Columns([hour_left, hour_right]),
                       title="[bold]Activity by Hour (UTC)[/]", border_style="dim")

    weekly_trends = _compute_weekly_trends(stats.activity_by_day)
    wk_panel = None
    if len(weekly_trends) >= 2:
        wk_tbl = Table(box=box.SIMPLE, show_header=True, padding=(0, 1))
        wk_tbl.add_column("Week",     style="dim",   no_wrap=True)
        wk_tbl.add_column("Events",   style="white",   justify="right")
        wk_tbl.add_column("Δ",        justify="right")
        wk_tbl.add_column("Δ%",       justify="right")
        wk_tbl.add_column("Days",     style="dim",     justify="right")
        for w in weekly_trends[-8:]:
            delta = w["delta"]
            delta_str = f"+{delta:,}" if delta > 0 else (f"{delta:,}" if delta < 0 else "—")
            delta_style = "green" if delta > 0 else ("red" if delta < 0 else "dim")
            pct = w["delta_pct"]
            pct_str = f"{pct:+.1f}%" if pct is not None else "—"
            pct_style = "green" if (pct or 0) > 0 else ("red" if (pct or 0) < 0 else "dim")
            wk_tbl.add_row(
                w["week"],
                f"{w['events']:,}",
                Text(delta_str, style=delta_style),
                Text(pct_str, style=pct_style),
                str(w["days_active"]),
            )
        wk_panel = Panel(wk_tbl, title="[bold]Week-over-Week[/]", border_style="dim")

    if wk_panel:
        grid = Table.grid(expand=True)
        grid.add_column(ratio=1)
        grid.add_column(ratio=1)
        grid.add_row(hour_panel, wk_panel)
        console.print(grid)
    else:
        console.print(hour_panel)
    console.print()

    # ── Top tools + Models (side by side) ─────────────────────────────────────
    tool_panel = None
    top_tools = stats.tools_by_count.most_common(12)
    if top_tools:
        max_t = top_tools[0][1]
        tool_tbl = Table(box=box.SIMPLE, show_header=True, padding=(0, 1))
        tool_tbl.add_column("Tool",   style="white",   no_wrap=True, max_width=20)
        tool_tbl.add_column("",       no_wrap=True,    min_width=12)
        tool_tbl.add_column("#",      style="dim",     justify="right")
        tool_tbl.add_column("p50",    style="dim",     justify="right")
        for tool, cnt in top_tools:
            filled = round(cnt / max_t * 12)
            durations = stats.tool_durations_ms.get(tool, [])
            p50 = ""
            if durations:
                s = sorted(durations)
                p50 = f"{s[len(s)//2]:.0f}"
            tool_tbl.add_row(tool, _bar(filled, 12, "blue"), str(cnt), p50)
        tool_panel = Panel(tool_tbl, title="[bold]Top Tools[/]", border_style="dim")

    mod_panel = None
    if stats.models_by_count:
        top_models = stats.models_by_count.most_common(8)
        max_mod = top_models[0][1]
        mod_tbl = Table(box=box.SIMPLE, show_header=False, padding=(0, 1))
        mod_tbl.add_column("Model", style="magenta", no_wrap=True, max_width=20)
        mod_tbl.add_column("",      no_wrap=True,    min_width=12)
        mod_tbl.add_column("#",     style="dim",     justify="right")
        for m, cnt in top_models:
            filled = round(cnt / max_mod * 12)
            mod_tbl.add_row(_fmt_model(m), _bar(filled, 12, "magenta"), str(cnt))
        mod_panel = Panel(mod_tbl, title="[bold]Models[/]", border_style="dim")

    if tool_panel and mod_panel:
        grid = Table.grid(expand=True)
        grid.add_column(ratio=1)
        grid.add_column(ratio=1)
        grid.add_row(tool_panel, mod_panel)
        console.print(grid)
    elif tool_panel:
        console.print(tool_panel)
    elif mod_panel:
        console.print(mod_panel)
    if tool_panel or mod_panel:
        console.print()

    # ── MCP servers + Subagent types (side by side) ──────────────────────────
    mcp_panel = None
    if stats.mcp_servers:
        mcp_calls = {
            srv: stats.mcp_server_before.get(srv, 0) or cnt
            for srv, cnt in stats.mcp_servers.most_common(8)
        }
        top_mcp = sorted(mcp_calls.items(), key=lambda x: x[1], reverse=True)
        max_m = top_mcp[0][1] if top_mcp else 1
        has_avail = bool(stats.mcp_server_after)
        mcp_tbl = Table(box=box.SIMPLE, show_header=has_avail, padding=(0, 1))
        mcp_tbl.add_column("Server",  style="white",  no_wrap=True, max_width=22)
        mcp_tbl.add_column("",        no_wrap=True,   min_width=10)
        mcp_tbl.add_column("#",       style="dim",    justify="right")
        if has_avail:
            mcp_tbl.add_column("OK", style="green", justify="right")
        for srv, calls in top_mcp:
            filled = round(calls / max_m * 10) if max_m else 0
            row = [srv, _bar(filled, 10, "yellow"), str(calls)]
            if has_avail:
                after = stats.mcp_server_after.get(srv, 0)
                avail_pct = f"{100 * after / calls:.0f}%" if calls > 0 else "—"
                color = "green" if (calls == 0 or after / calls >= 0.95) else "yellow" if after / calls >= 0.80 else "red"
                row.append(f"[{color}]{avail_pct}[/]")
            mcp_tbl.add_row(*row)
        mcp_panel = Panel(mcp_tbl, title="[bold]MCP Servers[/]", border_style="dim")

    sub_panel = None
    if stats.subagent_types:
        top_sub = stats.subagent_types.most_common(8)
        max_s = top_sub[0][1]
        total_sa_starts = stats.events_by_type.get("SubagentStart", 0)
        total_sa_stops  = stats.events_by_type.get("SubagentStop", 0)
        has_stops = bool(stats.subagent_stops_by_type) or total_sa_stops > 0
        sub_tbl = Table(box=box.SIMPLE, show_header=True, padding=(0, 1))
        sub_tbl.add_column("Type",       style="white",  no_wrap=True, max_width=20)
        sub_tbl.add_column("",           no_wrap=True,   min_width=10)
        sub_tbl.add_column("#",          style="dim",    justify="right")
        if has_stops:
            sub_tbl.add_column("Done", style="green", justify="right")
        for t, cnt in top_sub:
            filled = round(cnt / max_s * 10)
            row = [t, _bar(filled, 10, "purple"), str(cnt)]
            if has_stops:
                done = stats.subagent_stops_by_type.get(t, 0)
                if done > 0:
                    effective_done = min(done, cnt)
                    rate_pct = f"{100 * effective_done / cnt:.0f}%"
                    color = "green" if effective_done / cnt >= 0.9 else "yellow" if effective_done / cnt >= 0.7 else "red"
                    row.append(f"[{color}]{rate_pct}[/]")
                else:
                    global_rate = _safe_ratio(total_sa_stops, total_sa_starts)
                    row.append(f"[dim]~{100 * global_rate:.0f}%[/]")
            sub_tbl.add_row(*row)
        title = f"[bold]Subagents[/] [dim]({total_sa_stops}/{total_sa_starts})[/]"
        sub_panel = Panel(sub_tbl, title=title, border_style="dim")

    if mcp_panel and sub_panel:
        grid = Table.grid(expand=True)
        grid.add_column(ratio=1)
        grid.add_column(ratio=1)
        grid.add_row(mcp_panel, sub_panel)
        console.print(grid)
    elif mcp_panel:
        console.print(mcp_panel)
    elif sub_panel:
        console.print(sub_panel)
    if mcp_panel or sub_panel:
        console.print()

    # ── Model usage over time ─────────────────────────────────────────────────
    if stats.model_by_day:
        all_model_days = sorted(stats.model_by_day.keys())
        # Assign a consistent color+char per model
        MODEL_STYLES = ["cyan", "blue", "magenta", "yellow", "green", "bright_white"]
        all_models_ordered = [m for m, _ in stats.models_by_count.most_common()]
        model_style = {m: MODEL_STYLES[i % len(MODEL_STYLES)] for i, m in enumerate(all_models_ordered)}

        max_day_total = max(sum(ctr.values()) for ctr in stats.model_by_day.values())
        BAR_H = 8  # max bar height in chars

        chart_lines: list[Text] = []

        # header: date labels (show every ~7 days)
        label_row = Text("      ")
        for i, day in enumerate(all_model_days):
            show = i == 0 or i == len(all_model_days) - 1 or int(day[8:]) % 7 == 1
            label = day[5:] if show else "     "   # MM-DD
            label_row.append(f"{label:<6}", style="dim")
        chart_lines.append(label_row)

        # vertical bar: for each height level from top down
        for level in range(BAR_H, 0, -1):
            row = Text(f"  {level * max_day_total // BAR_H:>4} ")
            for day in all_model_days:
                ctr = stats.model_by_day.get(day, Counter())
                day_total = sum(ctr.values())
                filled_height = round(day_total / max_day_total * BAR_H) if max_day_total else 0
                if filled_height >= level:
                    # pick the dominant model's color for this cell
                    dominant = ctr.most_common(1)[0][0] if ctr else ""
                    style = model_style.get(dominant, "white")
                    row.append("█", style=style)
                else:
                    row.append("░", style="grey23")
                row.append(" ", style="")
            chart_lines.append(row)

        # x axis
        chart_lines.append(Text("       " + "──────" * len(all_model_days), style="dim"))

        # legend
        legend = Text("  ")
        for m in all_models_ordered[:6]:
            legend.append("█ ", style=model_style.get(m, "white"))
            legend.append(_fmt_model(m) + "  ", style="dim")
        chart_lines.append(legend)

        chart_text = Text("\n").join(chart_lines)
        console.print(Panel(chart_text, title="[bold]Model Usage Over Time[/]", border_style="dim"))
        console.print()

    # ── Sessions ──────────────────────────────────────────────────────────────
    multi_agent = len(stats.agents) > 1
    sess_tbl = Table(box=box.SIMPLE_HEAD, show_lines=False)
    sess_tbl.add_column("Session",       style="cyan",    no_wrap=True, max_width=24)
    if multi_agent:
        sess_tbl.add_column("Agent",     style="dim",     no_wrap=True, max_width=7)
    sess_tbl.add_column("Started (UTC)", style="dim",     no_wrap=True, min_width=16)
    sess_tbl.add_column("Score",         justify="right", style="bold green", min_width=5)
    sess_tbl.add_column("In Tok",        justify="right", style="cyan", min_width=6)

    for sid in sorted(stats.sessions_seen, key=lambda s: stats.session_events.get(s, 0), reverse=True):
        # Derive session name from first prompt preview
        conv_events = stats.session_conversation.get(sid, [])
        session_name = ""
        for ce in conv_events:
            if ce.get("type") == "prompt" and ce.get("preview"):
                session_name = ce["preview"][:50]
                break
        if not session_name:
            session_name = sid[:12] + "…"

        # Agent for this session
        agent_name = ""
        if multi_agent:
            source_info = stats.session_source.get(sid)
            agent_name = source_info[0] if source_info else ""
            if not agent_name:
                for aname, ag in stats.agents.items():
                    if sid in ag.sessions_seen:
                        agent_name = aname
                        break

        first_ts = stats.session_first_ts.get(sid)
        created = ""
        if first_ts:
            dt = datetime.fromtimestamp(first_ts / 1e9, tz=UTC)
            created = dt.strftime("%Y-%m-%d %H:%M")
        tok = stats.session_tokens.get(sid, {})
        in_tok = _fmt_tokens(tok.get("input", 0)) if tok.get("input") else "[dim]—[/]"

        score = stats.session_quality_scores.get(sid, 0.0)
        score_color = "green" if score > 70 else "yellow" if score > 40 else "red"

        row = [session_name]
        if multi_agent:
            row.append(agent_name or "—")
        row.extend([
            created or "—",
            f"[{score_color}]{score:.0f}[/]",
            in_tok,
        ])
        sess_tbl.add_row(*row)

    console.print(Panel(sess_tbl,
                        title=f"[bold]Sessions[/] [dim]({len(stats.sessions_seen)} total)[/]",
                        border_style="dim"))
    console.print()

    # ── Command patterns ──────────────────────────────────────────────────────
    top_cmds = _sanitize_command_counter(stats.shell_commands).most_common(10)
    if top_cmds:
        max_c = top_cmds[0][1]
        cmd_tbl = Table(box=box.SIMPLE, show_header=False, padding=(0, 1))
        cmd_tbl.add_column("Cmd",   style="white", no_wrap=True, max_width=52)
        cmd_tbl.add_column("",      no_wrap=True,  min_width=16)
        cmd_tbl.add_column("Count", style="dim",   justify="right")
        for cmd, cnt in top_cmds:
            filled = round(cnt / max_c * 16)
            cmd_tbl.add_row(cmd, _bar(filled, 16, "green"), str(cnt))
        console.print(Panel(cmd_tbl, title="[bold]Command Patterns[/]", border_style="dim"))
        console.print()

    # ── Tool transitions ──────────────────────────────────────────────────────
    transitions = _compute_tool_transitions(stats.session_tool_seq)
    if transitions:
        trans_tbl = Table(box=box.SIMPLE, show_header=True, padding=(0, 1))
        trans_tbl.add_column("From",  style="cyan",  no_wrap=True, min_width=20)
        trans_tbl.add_column("→ To",  style="blue",  no_wrap=True, min_width=20)
        trans_tbl.add_column("Count", style="dim",   justify="right")
        for t in transitions[:15]:
            trans_tbl.add_row(t["from"], t["to"], str(t["count"]))
        console.print(Panel(trans_tbl, title="[bold]Tool Transitions[/]", border_style="dim"))
        console.print()

    console.print(Rule("[dim]reflect.o11y.dev[/]"))
    if publish_url:
        console.print()
        console.print(f"  [bold green]Dashboard URL:[/] {publish_url}")
        console.print()
    console.print()
