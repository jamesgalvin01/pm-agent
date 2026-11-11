"""
command_center.py — the top half of the dashboard home page.

  gather(cur, today)      runs the queries and returns a plain dict
  render(data, ...)       turns that dict into the command-center HTML

Kept apart from the database so the layout can be previewed with any data.
"""
import html
from datetime import timedelta

import charts

INACTIVE = ("complete", "completed", "closed", "cancelled", "canceled", "archived",
            "on hold", "hold", "paused", "dormant")
RAG = {"green": "#2bd4a0", "amber": "#f5a623", "red": "#FF5C8A"}

Q_PROJECTS = """
    SELECT p.id, p.name, p.status, p.rag_status,
           COUNT(t.id) FILTER (WHERE t.status = 'open')                          AS open_n,
           COUNT(t.id) FILTER (WHERE t.status = 'open' AND t.due_date < %(today)s) AS overdue,
           COUNT(t.id) FILTER (WHERE t.status = 'open'
                               AND COALESCE(t.ball_in_court, 'me') = 'waiting')   AS waiting
      FROM projects p LEFT JOIN tasks t ON t.project_id = p.id
     GROUP BY p.id ORDER BY open_n DESC, p.name"""

Q_TOTALS = """
    SELECT COUNT(*) FILTER (WHERE status = 'open'),
           COUNT(*) FILTER (WHERE status = 'open' AND due_date < %(today)s),
           COUNT(*) FILTER (WHERE status = 'open' AND COALESCE(ball_in_court, 'me') = 'waiting'),
           COUNT(*) FILTER (WHERE completed_at >= %(week_start)s)
      FROM tasks"""

Q_DUE = """
    SELECT due_date, COALESCE(ball_in_court, 'me') = 'waiting' AS waiting, COUNT(*)
      FROM tasks
     WHERE status = 'open' AND due_date BETWEEN %(today)s AND %(end)s
     GROUP BY 1, 2"""

Q_CLOSED = """
    SELECT (completed_at AT TIME ZONE 'America/New_York')::date, COUNT(*)
      FROM tasks
     WHERE completed_at >= %(closed_start)s
     GROUP BY 1"""

Q_LEADS = """
    SELECT COALESCE(SUM(value) FILTER (WHERE status NOT IN ('Won', 'Lost')), 0),
           COUNT(*) FILTER (WHERE status NOT IN ('Won', 'Lost'))
      FROM leads"""

Q_LEAD_MONTHS = """
    SELECT (created_at AT TIME ZONE 'America/New_York')::date, COALESCE(value, 0), status,
           (updated_at AT TIME ZONE 'America/New_York')::date
      FROM leads"""

DAYS = 14


def _params(today):
    return {"today": today, "end": today + timedelta(days=DAYS - 1),
            "closed_start": today - timedelta(days=DAYS - 1),
            "week_start": today - timedelta(days=today.weekday())}


def gather(cur, today) -> dict:
    p = _params(today)
    cur.execute(Q_PROJECTS, p)
    projects = [dict(zip(("id", "name", "status", "rag", "open", "overdue", "waiting"), r))
                for r in cur.fetchall()]
    cur.execute(Q_TOTALS, p)
    open_n, overdue, waiting, closed_week = cur.fetchone()
    cur.execute(Q_DUE, p)
    due = cur.fetchall()
    cur.execute(Q_CLOSED, p)
    closed = cur.fetchall()
    cur.execute(Q_LEADS)
    pipeline, live_leads = cur.fetchone()
    cur.execute(Q_LEAD_MONTHS)
    leads = cur.fetchall()
    return build(today, projects, (open_n, overdue, waiting, closed_week), due, closed,
                 (pipeline, live_leads), leads)


def build(today, projects, totals, due, closed, pipeline, leads) -> dict:
    """Shape raw query rows into what render() draws."""
    days = [today + timedelta(days=i) for i in range(DAYS)]
    mine = [0] * DAYS
    theirs = [0] * DAYS
    for d, waiting, n in due:
        i = (d - today).days
        if 0 <= i < DAYS:
            (theirs if waiting else mine)[i] += n
    busiest = max(range(DAYS), key=lambda i: mine[i] + theirs[i])

    cdays = [today - timedelta(days=DAYS - 1 - i) for i in range(DAYS)]
    cmap = {d: n for d, n in closed}

    months = []
    m = today.replace(day=1)
    for _ in range(6):
        months.insert(0, m)
        m = (m - timedelta(days=1)).replace(day=1)
    added, won = [], []
    for start in months:
        end = (start + timedelta(days=32)).replace(day=1)
        added.append(float(sum(v for c, v, s, u in leads if c and c < end)))
        won.append(float(sum(v for c, v, s, u in leads if s == "Won" and u and u < end)))

    active = [p for p in projects if (p["status"] or "").lower() not in INACTIVE]
    for p in active:
        p["on_track"] = 100.0 if not p["open"] else 100.0 * (p["open"] - p["overdue"]) / p["open"]

    return {
        "open": totals[0], "overdue": totals[1], "waiting": totals[2], "closed_week": totals[3],
        "pipeline": float(pipeline[0] or 0), "live_leads": pipeline[1],
        "projects": active,
        "due_labels": [_day(d, today) for d in days], "due_mine": mine, "due_theirs": theirs,
        "busiest": busiest if mine[busiest] + theirs[busiest] else None,
        "closed_labels": [d.strftime("%-d") for d in cdays], "closed": [cmap.get(d, 0) for d in cdays],
        "lead_labels": [mm.strftime("%b") for mm in months], "lead_added": added, "lead_won": won,
    }


def _day(d, today) -> str:
    if d == today:
        return "Today"
    return d.strftime("%a %-d")


def render(data: dict, today_items: list, link, selected_project: int = 0, tier_colors=None,
           tier_labels=None, short_date=str, latest=None) -> str:
    esc = html.escape
    tier_colors = tier_colors or {}
    tier_labels = tier_labels or {}

    def kpi(value, label, href, cls=""):
        return (f"<a class='kpi {cls}' href='{href}'><div class='kpi-v'>{value}</div>"
                f"<div class='kpi-l'>{label}</div></a>")

    kpis = (kpi(charts.money_short(data["pipeline"]), f"Lead pipeline · {data['live_leads']} live", "/leads")
            + kpi(data["open"], "Open action items", "/tasks?filter=open#register")
            + kpi(data["overdue"], "Overdue", "/tasks?filter=open#register", "hot" if data["overdue"] else "")
            + kpi(data["waiting"], "Waiting on others", "/tasks?ball=waiting#register")
            + kpi(data["closed_week"], "Closed this week", "/tasks?filter=complete#register"))

    rings = ""
    for p in data["projects"]:
        on = " on" if selected_project == p["id"] else ""
        rag = RAG.get(p["rag"] or "", "#ABA6DD")
        late = f" · <span class='late'>{p['overdue']} late</span>" if p["overdue"] else ""
        rings += f"""
        <a class='panel ringcard{on}' href='{link(project=p["id"])}#register'>
            {charts.ring(p["on_track"])}
            <div class='rc-t'>
                <div class='rc-n'>{esc(p["name"] or "")}</div>
                <div class='rc-m'>{p["open"]} open{late}</div>
                <div class='rc-rag'><span style='background:{rag}'></span>{esc((p["rag"] or "no status").title())}</div>
            </div>
        </a>"""

    layers = [("Waiting on others", data["due_theirs"], charts.VIOLET),
              ("Yours", data["due_mine"], charts.PINK)]
    work = (f"<div class='d-chart'>{charts.area_chart(data['due_labels'], layers, highlight=data['busiest'])}</div>"
            f"<div class='m-chart'>{charts.area_chart(data['due_labels'], layers, highlight=data['busiest'], w=360, h=230, label_step=4)}</div>")

    items = ""
    for i, t in enumerate(today_items):
        hot = "hot" if i == 0 else ("warm" if i == 1 else "")
        tier = t.get("tier")
        why = tier_labels.get(tier, "") if tier else ""
        due = f"due {short_date(t['due_date'])}" if t.get("due_date") else ""
        meta = " · ".join(x for x in (esc(t.get("project") or "No project"), due, esc(why)) if x)
        u = (latest or {}).get(t["id"])
        last = (f"<div class='ti-u'><i class='ti ti-message-2'></i> {esc(u.get('body') or u.get('action_detail') or '')}</div>"
                if u else "")
        items += f"""
        <div class='today-item {hot}'>
            <form method='post' action='/complete/{t["id"]}'><button title='Mark done' aria-label='Mark done'><i class='ti ti-check'></i></button></form>
            <a class='ti-body' href='/task/{t["id"]}'><div class='ti-d'>{esc(t["description"] or "")}</div><div class='ti-m'>{meta}</div>{last}</a>
        </div>"""
    if not items:
        items = "<div class='empty'>Nothing pressing. Nice.</div>"

    closed = charts.bar_chart(data["closed_labels"], data["closed"])
    leads = charts.line_chart(data["lead_labels"],
                              [("Pipeline added", data["lead_added"], charts.ORANGE),
                               ("Won", data["lead_won"], charts.CYAN)])

    return f"""
    <div class='cc'>
        <div class='kpis'>{kpis}</div>
        <div class='rings'>{rings}</div>
        <div class='cc-mid'>
            <div class='panel'>
                <div class='p-h'><div><div class='p-t'>Workload, next 14 days</div><div class='p-s'>Open items by due date</div></div>
                    <div class='legend'><span><i style='background:{charts.PINK}'></i>Yours</span><span><i style='background:{charts.VIOLET}'></i>Waiting on others</span></div></div>
                {work}
            </div>
            <div class='panel'>
                <div class='p-h'><div><div class='p-t'>Needs you now</div><div class='p-s'>Top of the ranked register</div></div>
                    <a class='p-link' href='#register'>All <i class='ti ti-arrow-down'></i></a></div>
                <div class='today'>{items}</div>
            </div>
        </div>
        <div class='cc-bot'>
            <div class='panel'>
                <div class='p-h'><div><div class='p-t'>Items closed per day</div><div class='p-s'>Last 14 days</div></div>
                    <div class='p-big'>{sum(data["closed"])}</div></div>
                {closed}
            </div>
            <div class='panel'>
                <div class='p-h'><div><div class='p-t'>Lead pipeline</div><div class='p-s'>Running total by month</div></div>
                    <div class='legend'><span><i style='background:{charts.ORANGE}'></i>Added</span><span><i style='background:{charts.CYAN}'></i>Won</span></div></div>
                {leads}
            </div>
        </div>
    </div>"""


CSS = """
        .cc { margin-bottom: 30px; }
        .panel { background: rgba(34,29,88,0.72); border: 1px solid #352F78; border-radius: 16px; padding: 16px 18px;
                 box-shadow: inset 0 1px 0 rgba(255,255,255,0.04); min-width: 0; }
        .kpis { display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 12px; margin-bottom: 14px; }
        .kpi { text-decoration: none; color: inherit; padding: 4px 2px; border-radius: 12px; transition: background .15s; }
        .kpi:hover { background: rgba(255,255,255,0.03); }
        .kpi-v { font-size: 30px; font-weight: 600; color: #FFFFFF; letter-spacing: -0.5px; line-height: 1.1; }
        .kpi.hot .kpi-v { background: linear-gradient(90deg, #FF4F8B, #FF9F6B); -webkit-background-clip: text;
                          background-clip: text; color: transparent; }
        .kpi-l { font-size: 12px; color: #9993D0; margin-top: 3px; }
        .rings { display: grid; grid-template-columns: repeat(auto-fill, minmax(215px, 1fr)); gap: 12px; margin-bottom: 14px; }
        .ringcard { display: flex; align-items: center; gap: 14px; text-decoration: none; color: inherit; padding: 12px 14px;
                    transition: border-color .15s, transform .15s; }
        .ringcard:hover { border-color: #B45CF0; }
        .ringcard.on { border-color: #FF4F8B; box-shadow: 0 0 0 1px #FF4F8B, 0 0 24px rgba(255,79,139,0.25); }
        .rc-n { font-size: 14px; font-weight: 600; color: #F3F1FF; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
        .rc-t { min-width: 0; }
        .rc-m { font-size: 12px; color: #ABA6DD; margin-top: 2px; }
        .rc-m .late { color: #FF8FC4; }
        .rc-rag { font-size: 11px; color: #847FC0; margin-top: 4px; display: flex; align-items: center; gap: 5px; }
        .rc-rag span { width: 7px; height: 7px; border-radius: 50%; display: inline-block; }
        .cc-mid { display: grid; grid-template-columns: minmax(0, 1.75fr) minmax(0, 1fr); gap: 12px; margin-bottom: 12px; }
        .cc-bot { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: 12px; }
        .p-h { display: flex; justify-content: space-between; align-items: flex-start; gap: 10px; margin-bottom: 10px; }
        .p-t { font-size: 14px; font-weight: 600; color: #F3F1FF; }
        .p-s { font-size: 12px; color: #847FC0; margin-top: 1px; }
        .p-big { font-size: 24px; font-weight: 600; color: #FFFFFF; }
        .p-link { font-size: 12px; color: #FF8FC4; text-decoration: none; white-space: nowrap; }
        .legend { display: flex; gap: 12px; font-size: 11px; color: #ABA6DD; flex-wrap: wrap; justify-content: flex-end; }
        .legend i { display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 5px; }
        .chart { display: block; overflow: visible; }
        .m-chart { display: none; }
        .today { display: flex; flex-direction: column; gap: 8px; }
        .today-item { display: flex; gap: 10px; align-items: center; padding: 10px 12px; border-radius: 12px; background: #2C2670; }
        .today-item.hot { background: linear-gradient(90deg, #FF4F8B, #B03FD1); }
        .today-item.warm { background: linear-gradient(90deg, #C445C9, #6E3FD1); }
        .today-item form { margin: 0; flex-shrink: 0; }
        .today-item button { width: 30px; height: 30px; border-radius: 50%; border: 1.5px solid rgba(255,255,255,0.5);
                             background: rgba(255,255,255,0.08); color: #fff; cursor: pointer; font-size: 15px;
                             display: flex; align-items: center; justify-content: center; }
        .today-item button:hover { background: rgba(255,255,255,0.25); }
        .ti-d { font-size: 13px; color: #FFFFFF; line-height: 1.35; font-weight: 500; }
        .ti-m { font-size: 11px; color: rgba(255,255,255,0.72); margin-top: 2px; }
        .ti-body { text-decoration: none; color: inherit; min-width: 0; flex: 1; }
        .ti-u { font-size: 11px; color: rgba(255,255,255,0.88); margin-top: 4px; white-space: nowrap; overflow: hidden;
                text-overflow: ellipsis; }
        @media (max-width: 1000px) {
            .kpis { grid-template-columns: repeat(3, minmax(0, 1fr)); }
            .cc-mid, .cc-bot { grid-template-columns: minmax(0, 1fr); }
        }
        @media (max-width: 760px) {
            .kpis { display: flex; overflow-x: auto; gap: 18px; margin: 0 -14px 12px; padding: 0 14px; }
            .kpis::-webkit-scrollbar, .rings::-webkit-scrollbar { display: none; }
            .kpi { flex: 0 0 auto; }
            .kpi-v { font-size: 24px; }
            .rings { display: flex; overflow-x: auto; margin: 0 -14px 12px; padding: 0 14px 4px; scroll-snap-type: x mandatory; scroll-padding: 0 14px; }
            .d-chart { display: none; }
            .m-chart { display: block; }
            .ringcard { flex: 0 0 230px; scroll-snap-align: start; }
            .panel { padding: 14px; }
            .cc-mid { display: flex; flex-direction: column-reverse; }   /* today list above the chart on phones */
        }
"""
