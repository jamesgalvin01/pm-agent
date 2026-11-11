"""
sphere.py — the Operations Sphere (/sphere).

A rotating 3D map of everything Rowan tracks: every task (open ones bright,
completed ones as a dim cloud), linked to its project and to where it came
from (Outlook, Gmail, Rowan chat, Telegram, dashboard, meetings), plus the
parties you're waiting on, your most frequent email contacts, the lead
pipeline, email reply drafts, and the Outlook inbox's unread count.

gather() reads Supabase, inbox_status() asks Microsoft Graph for the inbox
counts (cached for 2 minutes), render() returns the page body. The route
itself lives in dashboard.py so it gets the normal header and sign-in.
"""

import json
import re
import time

import requests

from outlook_mail import GRAPH, _headers, get_access_token

# Senders that are newsletters, systems or James himself, not people.
_NOT_PEOPLE = {
    "rowan", "openbids", "boi", "south florida events", "bisnow south florida morning brief",
    "zoom", "sfl- cmt scheduling-dl", "crew miami", "manager", "adp totalsource",
    "james galvin", "the biltmore hotel", "blr regulatory update", "2026 annual report",
    "google", "supabase", "microsoft", "linkedin", "docusign", "dropbox",
}
_NOT_PEOPLE_RE = re.compile(r"no-?reply|notification|newsletter|mailer|support|team|alerts?|digest|update|bisnow|seatgeek|panthers|acrobat|smartsheet|openai|chatgpt|paypal|compliance|journal|platform|report|events?|hotel|@", re.I)


def _clean_person(name: str):
    """Return a display name if this sender looks like a real person, else None."""
    if not name or "@" in name:
        return None
    n = name.strip()
    if "," in n:  # "Gary, Kenneth" -> "Kenneth Gary"
        last, first = [p.strip() for p in n.split(",", 1)]
        n = f"{first} {last}"
    n = re.sub(r"\s+-\s+.*$", "", n)  # "Kathleen Ivers - MRM" -> "Kathleen Ivers"
    if n.lower() in _NOT_PEOPLE or _NOT_PEOPLE_RE.search(n) or len(n.split()) < 2:
        return None
    return " ".join(w[:1].upper() + w[1:] if w.islower() else w for w in n.split())


def gather(cur, today) -> dict:
    cur.execute("SELECT id, name FROM projects ORDER BY id")
    projects = [[r[0], r[1]] for r in cur.fetchall()]

    cur.execute("""
        SELECT t.id, COALESCE(t.project_id, 0), LEFT(t.description, 140), t.due_date, t.priority,
               COALESCE(t.ball_in_court, 'me'), t.waiting_on, COALESCE(t.blocking, FALSE),
               COALESCE(t.source, ''), t.last_touched_at::date,
               (SELECT COUNT(*) FROM task_updates u WHERE u.task_id = t.id)
          FROM tasks t WHERE t.status = 'open' ORDER BY t.id""")
    open_tasks = [list(r) for r in cur.fetchall()]

    cur.execute("""
        SELECT COALESCE(project_id, 0), TO_CHAR(DATE_TRUNC('month', created_at), 'YYMM'),
               LEFT(COALESCE(source, 'x'), 3), COUNT(*)
          FROM tasks WHERE status <> 'open' GROUP BY 1, 2, 3""")
    done = [list(r) for r in cur.fetchall()]

    cur.execute("""
        SELECT TO_CHAR(DATE_TRUNC('month', created_at), 'YYMM'), COUNT(*)
          FROM tasks WHERE created_at >= DATE_TRUNC('month', NOW()) - INTERVAL '6 months'
         GROUP BY 1 ORDER BY 1""")
    months = [list(r) for r in cur.fetchall()]

    cur.execute("SELECT status, COUNT(*) FROM email_drafts GROUP BY 1")
    mail = [list(r) for r in cur.fetchall()]

    cur.execute("""
        WITH e AS (
            SELECT COALESCE(NULLIF(d.from_name, ''), d.from_email) AS n, t.project_id AS pid
              FROM email_drafts d LEFT JOIN tasks t ON t.id = d.task_id)
        SELECT n, COUNT(*), MODE() WITHIN GROUP (ORDER BY pid)
          FROM e GROUP BY n ORDER BY 2 DESC LIMIT 80""")
    people, seen = [], set()
    for n, c, p in cur.fetchall():
        clean = _clean_person(n)
        if clean and clean not in seen:
            seen.add(clean)
            people.append([clean, c, p])
        if len(people) >= 22:
            break

    cur.execute("SELECT name, COALESCE(value, 0), status FROM leads WHERE status <> 'Lost' ORDER BY value DESC NULLS LAST")
    leads = [[r[0], float(r[1]), r[2]] for r in cur.fetchall()]

    return {"today": today.isoformat(), "projects": projects, "open": open_tasks, "done": done,
            "months": months, "mail": mail, "people": people, "leads": leads}


_INBOX_CACHE = {"at": 0.0, "val": None}


def inbox_status():
    """Unread/total counts for the Outlook inbox plus the newest few unread, cached 2 min."""
    if time.time() - _INBOX_CACHE["at"] < 120:
        return _INBOX_CACHE["val"]
    val = None
    try:
        tok = get_access_token()
        r = requests.get(f"{GRAPH}/me/mailFolders/inbox", headers=_headers(tok),
                         params={"$select": "unreadItemCount,totalItemCount"}, timeout=10)
        if r.ok:
            j = r.json()
            val = {"unread": j.get("unreadItemCount"), "total": j.get("totalItemCount"), "recent": []}
            r2 = requests.get(f"{GRAPH}/me/mailFolders/inbox/messages", headers=_headers(tok),
                              params={"$filter": "isRead eq false", "$top": 6,
                                      "$select": "subject,from,receivedDateTime"}, timeout=10)
            if r2.ok:
                for m in r2.json().get("value", []):
                    sender = (m.get("from") or {}).get("emailAddress", {})
                    val["recent"].append([sender.get("name") or sender.get("address") or "",
                                          m.get("subject") or "(no subject)",
                                          (m.get("receivedDateTime") or "")[:16]])
            else:
                print(f"[sphere] unread list failed: {r2.status_code} {r2.text[:200]}")
        else:
            print(f"[sphere] inbox count failed: {r.status_code} {r.text[:200]}")
    except Exception as e:
        print(f"[sphere] inbox status unavailable: {e}")
    _INBOX_CACHE.update(at=time.time(), val=val)
    return val


def render(data: dict, updated: str) -> str:
    blob = json.dumps(data, default=str).replace("</", "<\\/")
    return TEMPLATE.replace("__DATA__", blob).replace("__UPDATED__", updated)


TEMPLATE = r"""
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
/* Layout: sphere fills the left, telemetry rail on the right; stacks on phones. */
#sph{--bg:#0c0a26;--panel:rgba(34,29,88,.72);--line:#352F78;--ink:#E6E3FF;--muted:#9993D0;--accent:#B45CF0;--crit:#FF4D6D;--warn:#FBBF24;--ok:#2bd4a0;--cyan:#67e8f9;--mono:"IBM Plex Mono",ui-monospace,Menlo,monospace;
  color:var(--ink);font-size:14px;display:flex;flex-direction:column}
#sph *{box-sizing:border-box}
#sph .top{display:flex;flex-wrap:wrap;align-items:center;gap:6px 18px;padding:9px 16px;border-bottom:1px solid var(--line);font-family:var(--mono);font-size:11px;letter-spacing:.06em;text-transform:uppercase;color:var(--muted)}
#sph .top .t span{color:var(--ink);font-variant-numeric:tabular-nums}
#sph .top .live{margin-left:auto;color:var(--ok)}
#sph .grid{display:grid;grid-template-columns:minmax(0,1fr) 360px;height:calc(100vh - 130px);min-height:560px}
#sph .stage{position:relative;overflow:hidden;border-right:1px solid var(--line);background:radial-gradient(circle at 45% 50%,#17134a 0,#0c0a26 70%)}
#sph canvas{position:absolute;inset:0;width:100%;height:100%;display:block;touch-action:none;cursor:grab}
#sph canvas:active{cursor:grabbing}
#sph .labels{position:absolute;inset:0;pointer-events:none}
#sph .lab{position:absolute;transform:translate(-50%,-140%);font-family:var(--mono);font-size:10.5px;letter-spacing:.05em;white-space:nowrap;padding:2px 6px;border-radius:4px;background:rgba(12,10,38,.78);border:1px solid rgba(255,255,255,.08)}
#sph .lab small{color:var(--muted)}
#sph .lab .unread{color:var(--cyan);font-weight:500}
#sph .tools{position:absolute;left:12px;top:10px;right:12px;display:flex;flex-wrap:wrap;gap:6px;align-items:center;z-index:3;pointer-events:none}
#sph .tools > *{pointer-events:auto}
#sph .search{position:relative}
#sph .chiprow{display:flex;flex-wrap:wrap;gap:6px}
#sph #shapes{flex-basis:100%}
#sph #shapes .chip{font-size:11.5px;padding:6px 13px}
#sph #shapes .chip.on{background:#E6E3FF;color:#14113A}
#sph .shapecap{flex-basis:100%;font-size:12px;color:#CBC7F0;max-width:620px;pointer-events:none;margin:-1px 0 2px 4px}
#sph .search input{width:230px;background:rgba(12,10,38,.88);border:1px solid var(--line);border-radius:999px;color:var(--ink);padding:7px 12px;font:inherit;font-size:13px}
#sph .search input:focus{outline:2px solid var(--accent);outline-offset:1px}
#sph .results{position:absolute;top:38px;left:0;width:310px;max-width:calc(100vw - 40px);background:rgba(20,17,58,.97);border:1px solid var(--line);border-radius:10px;padding:4px;max-height:340px;overflow:auto;box-shadow:0 10px 30px rgba(0,0,0,.5)}
#sph .results[hidden]{display:none}
#sph .results button{display:block;width:100%;text-align:left;background:none;border:0;color:var(--ink);padding:7px 9px;border-radius:6px;font:inherit;font-size:12.5px;cursor:pointer}
#sph .results button:hover,#sph .results button.hi{background:rgba(155,77,235,.22)}
#sph .results button small{color:var(--muted);display:block;font-size:11px}
#sph .chip{background:rgba(12,10,38,.88);border:1px solid var(--line);color:#CBC7F0;border-radius:999px;padding:6px 11px;font-family:var(--mono);font-size:11px;cursor:pointer;white-space:nowrap;min-height:30px}
#sph .chip b{font-weight:500;color:var(--ink);margin-left:3px}
#sph .chip.on{color:#fff;border-color:transparent;background:linear-gradient(90deg,#FF4F8B,#9B4DEB)}
#sph .chip.on b{color:#fff}
#sph .chip.proj{border-color:currentColor}
#sph .tlab{position:absolute;transform:translate(-50%,7px);font-size:11px;max-width:190px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;padding:2px 7px;border-radius:4px;background:rgba(12,10,38,.86);color:var(--ink);pointer-events:auto;cursor:pointer;border-left:3px solid var(--accent)}
#sph .tlab:hover{background:rgba(155,77,235,.35)}
#sph .tip{position:absolute;pointer-events:none;background:rgba(20,17,58,.97);border:1px solid var(--line);border-radius:8px;padding:5px 9px;font-size:12px;line-height:1.35;max-width:270px;z-index:4}
#sph .tip small{display:block;color:var(--muted);font-family:var(--mono);font-size:10px;letter-spacing:.05em;text-transform:uppercase}
#sph .tip[hidden]{display:none}
#sph .card nav{display:flex;justify-content:space-between;align-items:center;gap:8px;margin-top:10px;font-family:var(--mono);font-size:11px;color:var(--muted)}
#sph .card nav button{background:rgba(155,77,235,.2);border:0;color:var(--ink);border-radius:8px;padding:8px 12px;cursor:pointer;font:inherit;min-width:64px}
#sph .card .why{margin:6px 0 8px;font-size:12.5px;color:#CBC7F0}
#sph .st{display:inline-block;font-family:var(--mono);font-size:10px;padding:1px 6px;border-radius:999px;margin-left:4px;vertical-align:1px}
#sph .st.late{background:rgba(255,77,109,.18);color:var(--crit)}
#sph .st.stale{background:rgba(251,191,36,.18);color:var(--warn)}
#sph .st.ok{background:rgba(43,212,160,.16);color:var(--ok)}
#sph .legend{position:absolute;left:12px;bottom:12px;display:flex;flex-wrap:wrap;gap:6px 12px;max-width:calc(100% - 24px);font-family:var(--mono);font-size:10.5px;color:var(--muted);pointer-events:none}
#sph .legend i{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:5px}
#sph .card{position:absolute;left:14px;top:58px;z-index:5;width:min(350px,calc(100% - 28px));max-height:calc(100% - 110px);overflow-y:auto;background:rgba(20,17,58,.95);border:1px solid var(--warn);border-radius:12px;padding:12px 14px;font-size:13px;line-height:1.45;box-shadow:0 10px 40px rgba(0,0,0,.5)}
#sph .card[hidden]{display:none}
#sph .card .k{font-family:var(--mono);font-size:10px;letter-spacing:.08em;text-transform:uppercase;color:var(--warn)}
#sph .card h3{margin:4px 0 8px;font-size:15px;font-weight:600}
#sph .card dl{display:grid;grid-template-columns:auto 1fr;gap:3px 12px;margin:0;font-size:12.5px}
#sph .card dt{color:var(--muted)} #sph .card dd{margin:0;font-variant-numeric:tabular-nums}
#sph .card ul{margin:8px 0 0;padding-left:16px;font-size:12.5px;color:#CBC7F0}
#sph .card a{color:#FF8FC4}
#sph .card .x{position:absolute;right:8px;top:4px;background:none;border:0;color:var(--muted);font-size:20px;cursor:pointer}
#sph .rail{overflow-y:auto;padding:14px;display:flex;flex-direction:column;gap:12px}
#sph .panel{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:12px}
#sph .panel h2{margin:0 0 10px;font-family:var(--mono);font-size:10.5px;font-weight:500;letter-spacing:.1em;text-transform:uppercase;color:var(--muted);display:flex;justify-content:space-between}
#sph .panel h2 span{color:var(--ink)}
#sph .tiles{display:grid;grid-template-columns:repeat(2,1fr);gap:8px}
#sph .tile{background:rgba(12,10,38,.6);border:1px solid var(--line);border-left:3px solid var(--accent);border-radius:8px;padding:8px 10px}
#sph .tile .v{font-size:22px;font-weight:600;font-variant-numeric:tabular-nums}
#sph .tile .l{font-family:var(--mono);font-size:10px;color:var(--muted);letter-spacing:.06em;text-transform:uppercase}
#sph .tile.crit{border-left-color:var(--crit)} #sph .tile.crit .v{color:var(--crit)}
#sph .tile.warn{border-left-color:var(--warn)} #sph .tile.warn .v{color:var(--warn)}
#sph .tile.cyan{border-left-color:var(--cyan)} #sph .tile.cyan .v{color:var(--cyan)}
#sph .row{display:grid;grid-template-columns:110px 1fr 34px;align-items:center;gap:8px;padding:4px 2px;font-size:12.5px;cursor:pointer;border-radius:6px;background:none;border:0;color:inherit;font-family:inherit;text-align:left;width:100%}
#sph .row:hover,#sph .item:hover{background:rgba(155,77,235,.16)}
#sph .row .n{text-align:right;font-family:var(--mono);font-size:12px}
#sph .bar{height:8px;background:rgba(12,10,38,.8);border-radius:3px;overflow:hidden;display:flex}
#sph .bar b{display:block;height:100%}
#sph .list{display:flex;flex-direction:column;gap:2px}
#sph .item{display:grid;grid-template-columns:8px 1fr auto;gap:8px;align-items:start;padding:5px 4px;border-radius:6px;cursor:pointer;background:none;border:0;color:inherit;font:inherit;text-align:left;width:100%}
#sph .item i{width:8px;height:8px;border-radius:50%;margin-top:5px}
#sph .item .d{font-size:12.5px;line-height:1.35}
#sph .item .d small{display:block;color:var(--muted);font-size:11px}
#sph .item .late{font-family:var(--mono);font-size:11px;color:var(--crit);white-space:nowrap}
#sph .item .late.w{color:var(--warn)}
#sph svg text{font-family:var(--mono);font-size:10px;fill:var(--muted)}
#sph .note{font-size:11.5px;color:var(--muted);line-height:1.5;margin:0}
#sph button:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
@media (max-width:900px){
  #sph .grid{grid-template-columns:1fr;height:auto}
  #sph .stage{height:72vh;min-height:440px;border-right:0;border-bottom:1px solid var(--line)}
  #sph .search{flex:1 1 100%} #sph .search input{width:100%}
  #sph .chiprow{flex-wrap:nowrap;overflow-x:auto;width:100%;padding-bottom:2px;scrollbar-width:none}
  #sph .chiprow::-webkit-scrollbar{display:none}
  #sph .legend{font-size:9.5px;gap:4px 9px}
  #sph .shapecap{font-size:11px;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
  #sph #shapes{flex-wrap:nowrap;overflow-x:auto;scrollbar-width:none}
  #sph .rail{overflow:visible}
}
</style>
<div id="sph">
  <div class="top">
    <div class="t">Nodes <span id="hNodes">0</span></div>
    <div class="t">Strands <span id="hEdges">0</span></div>
    <div class="t">Open <span id="hOpen">0</span></div>
    <div class="t">Overdue <span id="hLate" style="color:var(--crit)">0</span></div>
    <div class="t">Unread <span id="hUnread" style="color:var(--cyan)">–</span></div>
    <div class="t">Spin <span id="hSpin">0.0°</span></div>
    <div class="live">Live · updated __UPDATED__</div>
  </div>
  <div class="grid">
    <section class="stage" id="stage" aria-label="3D map of projects, tasks, people and sources">
      <canvas id="gl"></canvas>
      <div class="labels" id="labels"></div>
      <div class="tools">
        <div class="shapecap" id="shapeCap"></div>
        <div class="search"><input id="q" type="search" placeholder="Find a task, person, project…" autocomplete="off" aria-label="Search the sphere"><div class="results" id="results" hidden role="listbox"></div></div>
        <div class="chiprow"><div id="chips" style="display:contents"></div>
        <button class="chip" id="spinBtn" aria-pressed="true">Pause spin</button>
        <button class="chip" id="labBtn" aria-pressed="false">Show task names</button></div>
      </div>
      <div class="tip" id="tip" hidden></div>
      <div class="legend" id="legend"></div>
      <div class="card" id="card" hidden role="dialog" aria-live="polite">
        <button class="x" id="cardX" aria-label="Close details">×</button>
        <div id="cardBody"></div>
      </div>
    </section>
    <div class="rail">
      <div class="panel">
        <h2>Right now</h2>
        <div class="tiles">
          <div class="tile"><div class="v" id="kOpen">0</div><div class="l">Open action items</div></div>
          <div class="tile crit"><div class="v" id="kLate">0</div><div class="l">Past due</div></div>
          <div class="tile warn"><div class="v" id="kWait">0</div><div class="l">Waiting on others</div></div>
          <div class="tile warn"><div class="v" id="kDraft">0</div><div class="l">Reply drafts pending</div></div>
          <div class="tile cyan" style="grid-column:1/-1;cursor:pointer" id="kUnreadTile"><div class="v" id="kUnread">–</div><div class="l" id="kUnreadL">Unread in Outlook inbox</div></div>
        </div>
      </div>
      <div class="panel"><h2>Open by project <span>red = past due</span></h2><div id="byProject"></div></div>
      <div class="panel"><h2>Most overdue <span id="lateCount"></span></h2><div class="list" id="lateList"></div></div>
      <div class="panel"><h2>Ball in their court</h2><div class="list" id="waitList"></div></div>
      <div class="panel"><h2>Items captured per month</h2><svg id="spark" viewBox="0 0 320 120" width="100%" role="img" aria-label="Tasks captured per month"></svg></div>
      <div class="panel"><h2>Lead pipeline <span id="pipeTotal"></span></h2><div class="list" id="leadList"></div></div>
      <p class="note"><b style="color:var(--ink)">How to use:</b> search or pick a filter above the sphere, tap a point or a name, then use ‹ › in the card (or your arrow keys) to step through the matching items. Tap a project in "Open by project" to isolate it.</p>
      <p class="note">Each strand links a task to its project and to where Rowan captured it. Contacts are linked by the project their emails most often feed and by first-name mentions in open tasks. The cyan swarm around Outlook is your unread inbox mail. The page refreshes itself every 10 minutes.</p>
    </div>
  </div>
</div>
<script src="https://cdnjs.cloudflare.com/ajax/libs/three.js/r128/three.min.js"></script>
<script>
(function(){
const DATA = __DATA__;
const TODAY = DATA.today;
const PALETTE = ["#818cf8","#38bdf8","#c084fc","#f472b6","#4ade80","#e2e8f0","#2dd4bf","#fda4af","#a3e635","#fcd34d","#93c5fd","#f0abfc"];
const SHORT = {"Miami Coastline":"MCM General","Grotto":"Grotto","Farbstein Residence":"Farbstein"};
const PROJECTS = {0:{name:"No project assigned",short:"Unassigned",color:"#64748b"}};
DATA.projects.forEach(([id,name],i) => PROJECTS[id] = {name, short:SHORT[name]||name, color:PALETTE[i%PALETTE.length]});
const SOURCES = {
  out:{name:"Outlook", desc:"Emails you flagged in Outlook, plus action items Rowan pulled from your Outlook inbox scans."},
  ema:{name:"Email scan", desc:"Action items Rowan extracted from specific email threads and replies."},
  gma:{name:"Gmail", desc:"Action items Rowan pulled from your Gmail account."},
  row:{name:"Rowan chat", desc:"Tasks Rowan created because you asked it to in conversation: the Chat page on this dashboard, Rowan in Telegram, or a voice note you sent it."},
  tel:{name:"Telegram", desc:"Quick captures you sent with /task or /wait in Telegram."},
  das:{name:"Dashboard", desc:"Tasks you typed in yourself with Add task on the dashboard."},
  mee:{name:"Meetings", desc:"Action items Rowan pulled from meeting minutes you sent it."},
  man:{name:"Manual", desc:"Added by hand, outside Rowan."}};
const STALE_DAYS = 5;   // same rule as the register: waiting with no movement for 5+ days
const INBOX = DATA.inbox || null;

const $ = id => document.getElementById(id);
const esc = s => String(s==null?"":s).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const dayDiff = d => d ? Math.round((Date.parse(TODAY) - Date.parse(d)) / 86400000) : null;
const MON = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];
const monName = yymm => MON[+yymm.slice(2)-1];
const fmt$ = v => "$" + (v>=1e6 ? (v/1e6).toFixed(2)+"M" : Math.round(v/1000)+"K");
const plural = (n,w) => `${n} ${w}${n===1?"":"s"}`;
let seed = 7; const rnd = () => { seed = (seed*16807) % 2147483647; return (seed-1)/2147483646; };
const src3 = s => (s||"x").slice(0,3);
const short = (s,n) => s.length>n ? s.slice(0,n-1)+"…" : s;

const tasks = DATA.open.map(r => ({id:r[0],p:PROJECTS[r[1]]?r[1]:0,desc:r[2],due:r[3],pri:r[4]||"medium",bic:r[5],wait:r[6],block:r[7],src:src3(r[8]),srcFull:r[8],touched:r[9],upd:r[10]}));
tasks.forEach(t => { t.late = dayDiff(t.due); t.overdue = t.late !== null && t.late > 0; t.idle = dayDiff(t.touched);
  t.waiting = t.bic==="waiting"; t.stale = t.waiting && t.idle !== null && t.idle >= STALE_DAYS; });
const DONE = DATA.done.map(d => [PROJECTS[d[0]]?d[0]:0, d[1], d[2], +d[3]]);
const statusChips = t => (t.overdue?`<span class="st late">${t.late}d past due</span>`:"") + (t.stale?`<span class="st stale">stale ${t.idle}d</span>`:"");

// ---------- geometry ----------
const R = 10, V = (x,y,z) => new THREE.Vector3(x,y,z);
const dirLL = (lat,lon) => { const a=lat*Math.PI/180, b=lon*Math.PI/180; return V(Math.cos(a)*Math.cos(b), Math.sin(a), Math.cos(a)*Math.sin(b)); };
function jitterDir(base, spread){ const t=V(rnd()-.5,rnd()-.5,rnd()-.5).normalize(); const ang=spread*Math.sqrt(rnd()); const axis=V().crossVectors(base,t).normalize(); return base.clone().applyAxisAngle(axis,ang).normalize(); }
const PRESET = [[-8,20],[28,-30],[-38,-40],[40,55],[-5,-75],[-55,40],[8,85],[55,-75],[-60,-90],[20,120],[-70,100],[65,30]];
const HUBDIR = {p0:dirLL(-30,120), out:dirLL(15,170), ema:dirLL(-20,200), gma:dirLL(45,195), row:dirLL(-50,170),
  tel:dirLL(30,230), das:dirLL(-5,240), mee:dirLL(-45,225), man:dirLL(60,150), pipe:dirLL(78,0)};
DATA.projects.forEach(([id],i) => { const p = PRESET[i] || [rnd()*120-60, rnd()*180-90]; HUBDIR["p"+id] = dirLL(p[0],p[1]); });

const nodes = [], edges = [], hubs = {};
const addNode = n => { nodes.push(n); return n; };
const col = h => new THREE.Color(h);
const openBy = {}, lateBy = {}, doneBy = {}, srcCount = {};
tasks.forEach(t => { openBy[t.p]=(openBy[t.p]||0)+1; if(t.overdue) lateBy[t.p]=(lateBy[t.p]||0)+1; });
DONE.forEach(d => doneBy[d[0]]=(doneBy[d[0]]||0)+d[3]);

Object.keys(PROJECTS).forEach(k => { const P=PROJECTS[k];
  hubs["p"+k] = addNode({pos:HUBDIR["p"+k].clone().multiplyScalar(R*1.02), color:col(P.color), size:4.2+Math.min(3,(openBy[k]||0)/6), alpha:1, pulse:0, pick:true, kind:"project", ref:+k, label:P.short}); });
Object.keys(SOURCES).forEach(k => {
  hubs[k] = addNode({pos:HUBDIR[k].clone().multiplyScalar(R*1.02), color:col(k==="out"&&INBOX&&INBOX.unread?"#67e8f9":"#cbd5ff"), size:k==="out"?4.2:3.2, alpha:.95, pulse:k==="out"&&INBOX&&INBOX.unread?.35:0, pick:true, kind:"source", ref:k, label:SOURCES[k].name}); });
hubs.pipe = addNode({pos:HUBDIR.pipe.clone().multiplyScalar(R*1.02), color:col("#fbbf24"), size:3.6, alpha:1, pulse:0, pick:true, kind:"pipeline", label:"Lead pipeline"});

const months = [...new Set(DONE.map(d=>d[1]))].sort();
const bright = m => .35 + .65*(months.indexOf(m)+1)/Math.max(1,months.length);
DONE.forEach(([p,m,s,c]) => {
  const P = PROJECTS[p], base = HUBDIR["p"+p], spread = Math.min(.95, .16 + .022*Math.sqrt(doneBy[p]||1)), b = bright(m);
  srcCount[s]=(srcCount[s]||0)+c;
  for(let i=0;i<c;i++){
    const n = addNode({pos:jitterDir(base,spread).multiplyScalar(R*(.95+rnd()*.05)), color:col(P.color), size:.9+rnd()*.5, alpha:.55*b, pulse:0, kind:"done", p, m});
    edges.push({a:n.pos, b:hubs["p"+p].pos, c:col(P.color), alpha:.045*b, tag:n});
    if(hubs[s]) edges.push({a:n.pos, b:hubs[s].pos, c:col(P.color), alpha:.03*b, tag:n, toSrc:true});
  }
});
const taskColor = t => t.overdue ? "#ff4d6d" : t.waiting ? "#fbbf24" : PROJECTS[t.p].color;
tasks.forEach(t => {
  srcCount[t.src]=(srcCount[t.src]||0)+1;
  const c = col(taskColor(t));
  t.node = addNode({pos:jitterDir(HUBDIR["p"+t.p],.22).multiplyScalar(R*1.03), color:c, size:t.pri==="high"?3.2:2.6, alpha:1, pulse:t.overdue?1:0, pick:true, kind:"task", ref:t});
  edges.push({a:t.node.pos, b:hubs["p"+t.p].pos, c:col(PROJECTS[t.p].color), alpha:.55, tag:t.node, open:true});
  if(hubs[t.src]) edges.push({a:t.node.pos, b:hubs[t.src].pos, c, alpha:t.overdue?.4:.22, tag:t.node, open:true});
});
const waitParties = {};
tasks.filter(t=>t.waiting).forEach(t => (waitParties[t.wait||"Someone (not named)"] ||= []).push(t));
const partyNodes = Object.entries(waitParties).map(([name,list]) => {
  const n = addNode({pos:jitterDir(HUBDIR["p"+list[0].p],.45).multiplyScalar(R*1.06), color:col("#fbbf24"), size:3, alpha:1, pulse:.5, pick:true, kind:"waiting", ref:{name,list}, label:name});
  list.forEach(t => { t.party = n; edges.push({a:n.pos, b:t.node.pos, c:col("#fbbf24"), alpha:.7, tag:t.node, open:true}); });
  return n;
});
const peopleNodes = DATA.people.map(([name,emails,p]) => {
  if(p && !PROJECTS[p]) p = null;
  const first = name.split(" ")[0].replace(/[^\w'-]/g,"");
  const re = new RegExp("\\b"+first+"\\b","i");
  const mentions = first.length>2 ? tasks.filter(t => re.test(t.desc) || (t.wait && re.test(t.wait))) : [];
  const home = p ?? (mentions[0] ? mentions[0].p : (DATA.projects[0]||[0])[0]);
  const n = addNode({pos:jitterDir(HUBDIR["p"+home]||HUBDIR.p0,.55).multiplyScalar(R*1.08), color:col("#f8fafc"), size:1.6+Math.min(2.2,emails/80), alpha:.95, pulse:0, pick:true, kind:"person", home, ref:{name,emails,p,mentions}});
  if(p) edges.push({a:n.pos, b:hubs["p"+p].pos, c:col(PROJECTS[p].color), alpha:.35, tag:n});
  mentions.forEach(t => edges.push({a:n.pos, b:t.node.pos, c:col("#f8fafc"), alpha:.3, tag:t.node, open:true}));
  if(!p && !mentions.length) edges.push({a:n.pos, b:hubs.out.pos, c:col("#cbd5ff"), alpha:.12, tag:n});
  return n;
});
const leadNodes = DATA.leads.map(([name,val,status]) => {
  const n = addNode({pos:jitterDir(HUBDIR.pipe,.35).multiplyScalar(R*1.04), color:col(status==="Won"?"#4ade80":"#fbbf24"), size:1.8+Math.min(3,val/80000), alpha:1, pulse:0, pick:true, kind:"lead", ref:{name,val,status}});
  edges.push({a:n.pos, b:hubs.pipe.pos, c:col("#fbbf24"), alpha:.5, tag:n});
  return n;
});
const mailTot = {}; DATA.mail.forEach(([s,c]) => mailTot[s]=(mailTot[s]||0)+(+c));
Object.entries(mailTot).forEach(([s,c]) => {
  const cc = s==="pending"?col("#fbbf24"):s==="sent"?col("#4ade80"):col("#5b6399"), a = s==="pending"?.75:s==="sent"?.9:.28;
  for(let i=0;i<c;i++) addNode({pos:V(rnd()*2-1,rnd()*2-1,rnd()*2-1).normalize().multiplyScalar(R*(1.12+rnd()*.22)), color:cc, size:s==="pending"?1.1:.7, alpha:a, pulse:s==="pending"?.25:0, kind:"mail"});
});
const unread = INBOX && INBOX.unread != null ? INBOX.unread : null;
if(unread){
  const swarm = Math.min(unread, 400), spread = Math.min(.26, .08 + .012*Math.sqrt(swarm));
  for(let i=0;i<swarm;i++) addNode({pos:jitterDir(HUBDIR.out,spread).multiplyScalar(R*(1.04+rnd()*.12)), color:col("#67e8f9"), size:1.1+rnd()*.5, alpha:.9, pulse:.4, kind:"unread"});
}

// ---------- three.js ----------
const canvas = $("gl"), stage = $("stage");
const renderer = new THREE.WebGLRenderer({canvas, antialias:true});
renderer.setPixelRatio(Math.min(2, window.devicePixelRatio||1)); renderer.setClearColor(0x110e38, 1);
const scene = new THREE.Scene(), camera = new THREE.PerspectiveCamera(45,1,.1,200);
let camDist = 34, fitted = false; const world = new THREE.Group(); scene.add(world);
const SEG = 10, ctrl = V(); let bow = 1, gain = 1;
function curve(e, out, k){ // writes SEG line segments of the strand into out starting at k
  const f = Math.max(.25, 1 - e.a.distanceTo(e.b)/(R*2.6));
  ctrl.copy(e.a).add(e.b).multiplyScalar(.5).multiplyScalar(1 + (f-1)*bow);
  for(let i=0;i<SEG;i++) for(const t of [i/SEG,(i+1)/SEG]){ const u=1-t;
    out[k]=u*u*e.a.x+2*u*t*ctrl.x+t*t*e.b.x; out[k+1]=u*u*e.a.y+2*u*t*ctrl.y+t*t*e.b.y; out[k+2]=u*u*e.a.z+2*u*t*ctrl.z+t*t*e.b.z; k+=3; }
  return k;
}
const ePos = new Float32Array(edges.length*SEG*6), eBase = new Float32Array(edges.length*SEG*6);
let k = 0;
edges.forEach(e => { e.k = k; const r=e.c.r*e.alpha, g=e.c.g*e.alpha, b=e.c.b*e.alpha;
  for(let j=0;j<SEG*2;j++){ eBase[k+j*3]=r; eBase[k+j*3+1]=g; eBase[k+j*3+2]=b; }
  k = curve(e, ePos, k); });
const eCol = eBase.slice();
const eGeo = new THREE.BufferGeometry();
eGeo.setAttribute("position", new THREE.BufferAttribute(ePos,3)); eGeo.setAttribute("color", new THREE.BufferAttribute(eCol,3));
world.add(new THREE.LineSegments(eGeo, new THREE.LineBasicMaterial({vertexColors:true, transparent:true, blending:THREE.AdditiveBlending, depthWrite:false})));
// highlighted strands for the selected point
const hlGeo = new THREE.BufferGeometry();
const hlLines = new THREE.LineSegments(hlGeo, new THREE.LineBasicMaterial({vertexColors:true, transparent:true, opacity:1, blending:THREE.AdditiveBlending, depthWrite:false}));
world.add(hlLines);

const N = nodes.length, A = n => new Float32Array(n);
const pPos=A(N*3), pCol=A(N*3), pSize=A(N), pAlpha=A(N), pPulse=A(N), pIdx=A(N);
nodes.forEach((n,i) => { pPos.set([n.pos.x,n.pos.y,n.pos.z],i*3); pCol.set([n.color.r,n.color.g,n.color.b],i*3); pSize[i]=n.size; pAlpha[i]=n.alpha; pPulse[i]=n.pulse; pIdx[i]=i; n.i=i; });
const baseSize = pSize.slice(), baseAlpha = pAlpha.slice();
const pGeo = new THREE.BufferGeometry();
[["position",pPos,3],["color",pCol,3],["size",pSize,1],["alpha",pAlpha,1],["pulse",pPulse,1],["idx",pIdx,1]].forEach(([nm,arr,sz]) => pGeo.setAttribute(nm, new THREE.BufferAttribute(arr,sz)));
const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
const pMat = new THREE.ShaderMaterial({
  uniforms:{uTime:{value:0}, uScale:{value:1}, uSel:{value:-1}, uHov:{value:-1}},
  vertexShader:`attribute float size; attribute vec3 color; attribute float alpha; attribute float pulse; attribute float idx;
    uniform float uTime; uniform float uScale; uniform float uSel; uniform float uHov; varying vec3 vC; varying float vA; varying float vRing;
    void main(){ vec4 mv = modelViewMatrix*vec4(position,1.0);
      float p = 1.0 + pulse*0.45*sin(uTime*4.0 + position.x);
      bool sel = abs(idx - uSel) < 0.5; bool hov = abs(idx - uHov) < 0.5;
      float s = sel ? 2.6 : (hov ? 1.9 : 1.0);
      vRing = sel ? 1.0 : (hov ? 0.6 : 0.0);
      gl_PointSize = size*p*s*uScale*(260.0/-mv.z); vC = color; vA = (sel||hov) ? 1.0 : alpha; gl_Position = projectionMatrix*mv; }`,
  fragmentShader:`varying vec3 vC; varying float vA; varying float vRing;
    void main(){ float d = length(gl_PointCoord-0.5); if(d>0.5) discard;
      float a = pow(smoothstep(0.5,0.0,d),1.8) + 0.35*smoothstep(0.18,0.0,d);
      float ring = vRing * smoothstep(0.06,0.0,abs(d-0.42));
      vec3 c = mix(vC*(0.8+0.6*smoothstep(0.2,0.0,d)), vec3(1.0), ring);
      gl_FragColor = vec4(c, max(a*vA, ring)); }`,
  transparent:true, depthWrite:false, blending:THREE.AdditiveBlending});
world.add(new THREE.Points(pGeo, pMat));


// ---------- labels ----------
const labelled = nodes.filter(n => n.label);
labelled.forEach(n => { const el = document.createElement("div"); el.className="lab"; let extra="";
  if(n.kind==="project") extra = ` <small>${openBy[n.ref]||0} open</small>`;
  if(n.kind==="source") extra = ` <small>${(srcCount[n.ref]||0).toLocaleString()}</small>`;
  if(n.kind==="source" && n.ref==="out") extra += unread!=null ? ` <span class="unread">· ${unread.toLocaleString()} unread</span>` : ` <small>· unread n/a</small>`;
  if(n.kind==="waiting") extra = ` <small>owes you ${n.ref.list.length}</small>`;
  el.innerHTML = esc(n.label)+extra;
  el.style.color = n.kind==="waiting" ? "#fbbf24" : n.kind==="source" ? (n.ref==="out"?"#67e8f9":"#cbd5ff") : n.kind==="pipeline" ? "#fbbf24" : "#"+n.color.getHexString();
  if(n.kind==="waiting") el.style.fontSize="9.5px";
  if(n.kind==="source" && n.ref==="out") { el.style.borderColor="rgba(103,232,249,.5)"; el.style.fontSize="11.5px"; }
  $("labels").appendChild(el); n.el = el; });
// task name tags (shown when filtering, or with "Show task names")
tasks.forEach(t => { const el = document.createElement("div"); el.className="tlab"; el.textContent = short(t.desc, 34);
  el.style.borderLeftColor = taskColor(t); el.style.display="none"; el.title = t.desc;
  el.addEventListener("click", ev => { ev.stopPropagation(); select(t.node); });
  $("labels").appendChild(el); t.el = el; });

// ---------- filter state ----------
const state = {filter:"all", project:null, showNames:false, spin:!reduceMotion};
const FILTERS = [
  ["all","Everything",()=>true],
  ["late","Past due",t=>t.overdue],
  ["wait","Waiting on others",t=>t.waiting],
  ["chat","From Rowan chat",t=>t.srcFull==="rowan_chat"],
  ["week","Due next 7 days",t=>t.late!==null && t.late<=0 && t.late>=-7]];
const fPred = () => FILTERS.find(f=>f[0]===state.filter)[2];
const taskMatch = t => (state.project===null || t.p===state.project) && fPred()(t);
const filtering = () => state.filter!=="all" || state.project!==null;
function nodeVisible(n){
  if(!filtering()) return true;
  switch(n.kind){
    case "task": return taskMatch(n.ref);
    case "project": return state.project!==null ? n.ref===state.project : tasks.some(t=>t.p===n.ref && taskMatch(t));
    case "source": return tasks.some(t=>t.src===n.ref && taskMatch(t));
    case "waiting": return n.ref.list.some(taskMatch);
    case "person": return n.ref.mentions.some(taskMatch);
    default: return false;
  }
}
function applyFilter(){
  const on = filtering();
  nodes.forEach(n => { n.vis = nodeVisible(n); const i=n.i;
    pAlpha[i] = baseAlpha[i] * (n.vis ? 1 : .07) * (shape!=="sphere" && n.kind==="done" ? .6 : 1);
    pSize[i] = baseSize[i] * (n.vis ? (on && n.kind==="task" ? 1.35 : 1) : .7); });
  pGeo.attributes.alpha.needsUpdate = true; pGeo.attributes.size.needsUpdate = true;
  edges.forEach(e => { const doneF = shape==="sphere" || !e.tag || e.tag.kind!=="done" ? 1 : (e.toSrc ? .07 : .3);
    const f = gain * doneF * (!on ? 1 : (e.tag && e.tag.vis ? (e.open ? 1.7 : 1) : .035));
    for(let j=0;j<SEG*6;j++) eCol[e.k+j] = Math.min(1, eBase[e.k+j]*f); });
  eGeo.attributes.color.needsUpdate = true;
  labelled.forEach(n => n.el.dataset.dim = n.vis ? "" : "1");
  const matches = tasks.filter(taskMatch);
  tasks.forEach(t => t.el.dataset.show = (state.showNames || (on && matches.length <= (stage.clientWidth < 600 ? 12 : 40))) && (!on || taskMatch(t)) ? "1" : "");
  renderChips();
}
function renderChips(){
  const counts = {}; FILTERS.forEach(([key,,pred]) => counts[key] = tasks.filter(t => (state.project===null||t.p===state.project) && pred(t)).length);
  let h = FILTERS.map(([key,lab]) => `<button class="chip ${state.filter===key?"on":""}" data-f="${key}" aria-pressed="${state.filter===key}">${lab}<b>${counts[key]}</b></button>`).join("");
  if(state.project!==null) h += `<button class="chip proj on" data-clearproj="1" style="background:${PROJECTS[state.project].color};color:#14113A">${esc(PROJECTS[state.project].short)} ×</button>`;
  $("chips").innerHTML = h;
}
$("chips").addEventListener("click", e => { const b=e.target.closest("button"); if(!b) return;
  if(b.dataset.f){ state.filter=b.dataset.f; }
  if(b.dataset.clearproj){ state.project=null; }
  applyFilter(); closeCard(); });
function isolateProject(p){ state.project = p; applyFilter(); focusOn(hubs["p"+p]); }
$("spinBtn").addEventListener("click", () => { state.spin=!state.spin; auto=state.spin; $("spinBtn").textContent = state.spin?"Pause spin":"Resume spin"; $("spinBtn").setAttribute("aria-pressed", state.spin); });
$("labBtn").addEventListener("click", () => { state.showNames=!state.showNames; $("labBtn").classList.toggle("on",state.showNames); $("labBtn").setAttribute("aria-pressed",state.showNames); applyFilter(); });
if(!state.spin) $("spinBtn").textContent = "Resume spin";

// ---------- interaction ----------
let rotX=.25, rotY=0, auto=state.spin, dragging=false, moved=0, last=null, idle=0, target=null, pinch0=null, hover=null;
const pointers = new Map();
canvas.addEventListener("pointerdown", e => { canvas.setPointerCapture(e.pointerId); pointers.set(e.pointerId,{x:e.clientX,y:e.clientY}); dragging=true; moved=0; last={x:e.clientX,y:e.clientY}; auto=false; target=null; hideTip(); });
canvas.addEventListener("pointermove", e => {
  if(!pointers.has(e.pointerId)){ if(e.pointerType==="mouse") hoverAt(e.clientX,e.clientY); return; }
  pointers.set(e.pointerId,{x:e.clientX,y:e.clientY});
  if(pointers.size===2){ const [a,b]=[...pointers.values()], d=Math.hypot(a.x-b.x,a.y-b.y); if(pinch0) camDist=Math.min(60,Math.max(12,camDist*pinch0/d)); pinch0=d; moved+=10; return; }
  const dx=e.clientX-last.x, dy=e.clientY-last.y; moved+=Math.abs(dx)+Math.abs(dy); rotY+=dx*.006; rotX=Math.max(-1.4,Math.min(1.4,rotX+dy*.006)); last={x:e.clientX,y:e.clientY}; });
const endPtr = e => { pointers.delete(e.pointerId); if(pointers.size<2) pinch0=null;
  if(!pointers.size){ dragging=false; if(moved<8) pick(e.clientX,e.clientY,e.pointerType==="mouse"?20:32); idle=performance.now(); } };
canvas.addEventListener("pointerup", endPtr); canvas.addEventListener("pointercancel", endPtr);
canvas.addEventListener("pointerleave", () => { hover=null; pMat.uniforms.uHov.value=-1; hideTip(); });
canvas.addEventListener("wheel", e => { e.preventDefault(); camDist=Math.min(60,Math.max(12,camDist*(1+e.deltaY*.001))); }, {passive:false});
const pickables = nodes.filter(n=>n.pick), wp = V();
function screenOf(n, rect){ wp.copy(n.pos).applyMatrix4(world.matrixWorld); const depth=wp.distanceTo(camera.position), z=wp.z; wp.project(camera);
  return {x:(wp.x+1)/2*rect.width, y:(1-wp.y)/2*rect.height, depth, z}; }
const KIND_BONUS = {task:10, waiting:8, project:6, source:4, lead:4, pipeline:3, person:0};
function nearest(cx,cy,radius){ const rect=canvas.getBoundingClientRect(); let best=null, bd=1e9;
  pickables.forEach(n => { if(filtering() && !n.vis) return; const s=screenOf(n,rect); if(s.z < -3) return;
    const d=Math.hypot(s.x+rect.left-cx, s.y+rect.top-cy); if(d<radius){ const sc=d + s.depth*.4 - (KIND_BONUS[n.kind]||0); if(sc<bd){bd=sc;best=n;} } });
  return best; }
function pick(cx,cy,radius){ const n = nearest(cx,cy,radius); n ? select(n) : closeCard(); }
let hoverRaf = 0;
function hoverAt(cx,cy){ if(hoverRaf) return; hoverRaf = requestAnimationFrame(() => { hoverRaf=0;
  const n = nearest(cx,cy,18); hover=n; pMat.uniforms.uHov.value = n ? n.i : -1; canvas.style.cursor = n ? "pointer" : "grab";
  if(n) showTip(n,cx,cy); else hideTip(); }); }
function tipText(n){ const r=n.ref;
  switch(n.kind){
    case "task": return [`Task · ${PROJECTS[r.p].short}${r.overdue?` · ${r.late}d past due`:r.waiting?" · waiting":""}`, short(r.desc,90)];
    case "project": return ["Project", `${PROJECTS[r].name} · ${openBy[r]||0} open`];
    case "source": return ["Where tasks come from", SOURCES[r].name + (r==="out"&&unread!=null?` · ${unread} unread`:"")];
    case "waiting": return ["Waiting on", `${r.name} · owes you ${plural(r.list.length,"item")}`];
    case "person": return ["Contact", `${r.name} · ${r.emails} emails`];
    case "lead": return [`Lead · ${r.status}`, `${r.name} · ${fmt$(r.val)}`];
    case "pipeline": return ["Lead pipeline", `${DATA.leads.length} leads`];
  } return ["",""]; }
function showTip(n,cx,cy){ const [k,t]=tipText(n), rect=stage.getBoundingClientRect(), tip=$("tip");
  tip.innerHTML=`<small>${esc(k)}</small>${esc(t)}`; tip.hidden=false;
  tip.style.left=Math.min(rect.width-280, cx-rect.left+14)+"px"; tip.style.top=(cy-rect.top+14)+"px"; }
function hideTip(){ $("tip").hidden=true; }

// selection + highlighted strands
let selected = null, navList = [], navIdx = -1;
function highlight(n){
  const list = edges.filter(e => n.kind==="done" ? false
    : (e.tag===n) || (e.b===n.pos && (e.open || n.kind==="pipeline" || n.kind==="person")) || (e.a===n.pos));
  const pos = new Float32Array(list.length*SEG*6), colr = new Float32Array(list.length*SEG*6); let q=0;
  list.forEach(e => { const c = e.c.clone().lerp(new THREE.Color("#ffffff"), .35);
    for(let j=0;j<SEG*2;j++){ colr[q+j*3]=c.r; colr[q+j*3+1]=c.g; colr[q+j*3+2]=c.b; } q = curve(e, pos, q); });
  hlGeo.setAttribute("position", new THREE.BufferAttribute(pos,3)); hlGeo.setAttribute("color", new THREE.BufferAttribute(colr,3));
  hlGeo.computeBoundingSphere();
}
function buildNav(n){
  if(n.kind==="task"){ const base = filtering() ? tasks.filter(taskMatch) : tasks.filter(t=>t.p===n.ref.p);
    return base.slice().sort((a,b)=>(b.late??-999)-(a.late??-999)).map(t=>t.node); }
  if(n.kind==="waiting") return partyNodes;
  if(n.kind==="person") return peopleNodes;
  if(n.kind==="lead") return leadNodes;
  if(n.kind==="project") return Object.keys(PROJECTS).filter(k=>openBy[k]).sort((a,b)=>openBy[b]-openBy[a]).map(k=>hubs["p"+k]);
  if(n.kind==="source") return Object.keys(SOURCES).map(k=>hubs[k]);
  return [n];
}
function select(n, keepNav){
  selected = n; pMat.uniforms.uSel.value = n.i; highlight(n);
  if(!keepNav){ navList = buildNav(n); }
  navIdx = navList.indexOf(n);
  showCard(n); hideTip(); focusOn(n);
}
function step(d){ if(!navList.length) return; navIdx = (navIdx + d + navList.length) % navList.length; select(navList[navIdx], true); }
function closeCard(){ $("card").hidden=true; selected=null; pMat.uniforms.uSel.value=-1; hlGeo.setAttribute("position", new THREE.BufferAttribute(new Float32Array(0),3)); }
$("cardX").addEventListener("click", closeCard);
document.addEventListener("keydown", e => { if(e.target.tagName==="INPUT") return;
  if(e.key==="Escape") closeCard(); if(!$("card").hidden && e.key==="ArrowRight") step(1); if(!$("card").hidden && e.key==="ArrowLeft") step(-1); });
$("card").addEventListener("click", e => { const b=e.target.closest("[data-step],[data-go],[data-iso]"); if(!b) return;
  if(b.dataset.step) step(+b.dataset.step);
  if(b.dataset.go){ const t=tasks.find(x=>x.id==b.dataset.go); if(t) select(t.node); }
  if(b.dataset.iso) isolateProject(+b.dataset.iso); });

const lateText = t => t.late===null ? "No due date" : t.late>0 ? `${plural(t.late,"day")} past due` : t.late===0 ? "Due today" : `Due in ${plural(-t.late,"day")}`;
const srcName = s => ({rowan_chat:"Rowan chat (you asked Rowan)",outlook_flag:"Flagged in Outlook",email:"Email scan",dashboard:"Dashboard (you typed it in)",meeting:"Meeting notes",telegram_task:"Telegram /task",telegram_wait:"Telegram /wait",outlook:"Outlook inbox scan",gmail:"Gmail",email_reply:"Email reply"}[s]||s||"Unknown");
const taskLink = t => `<button class="chip" style="padding:2px 8px;min-height:0" data-go="${t.id}">view</button>`;
function inboxHtml(){
  if(!INBOX) return `<dl><dt>Unread in inbox</dt><dd>Couldn't reach Outlook just now</dd></dl>`;
  return `<dl><dt>Unread in inbox</dt><dd style="color:var(--cyan)">${(INBOX.unread||0).toLocaleString()}</dd><dt>Total in inbox</dt><dd>${(INBOX.total||0).toLocaleString()}</dd></dl>`
    + (INBOX.recent && INBOX.recent.length ? `<ul>${INBOX.recent.map(m=>`<li><b>${esc(m[0])}</b> · ${esc(m[1])}</li>`).join("")}</ul>` : "")
    + `<p style="margin:8px 0 0"><a href="/emails">Open reply drafts →</a></p>`;
}
function showCard(n){ let h=""; const r=n.ref;
  if(n.kind==="task"){ const t=r;
    h=`<div class="k" style="color:${t.overdue?"var(--crit)":t.waiting?"var(--warn)":"var(--accent)"}">Task #${t.id} · ${esc(t.pri)} priority${t.block?" · blocking":""}</div><h3>${esc(t.desc)}${statusChips(t)}</h3>
    <dl><dt>Project</dt><dd>${esc(PROJECTS[t.p].name)}</dd><dt>Due</dt><dd>${t.due||"—"} · ${lateText(t)}</dd>
    <dt>Ball in court</dt><dd>${t.waiting?`<b style="color:var(--warn)">Theirs</b> · waiting on ${esc(t.wait||"someone")}`:"Yours"}</dd>
    ${t.waiting?`<dt>Last movement</dt><dd>${t.idle===null?"—":t.idle===0?"Today":plural(t.idle,"day")+" ago"}${t.stale?' <span class="st stale">stale</span>':""}</dd>`:""}
    <dt>Came from</dt><dd>${esc(srcName(t.srcFull))}</dd><dt>Last touched</dt><dd>${t.touched||"—"}</dd><dt>Progress notes</dt><dd>${t.upd||"None logged"}</dd></dl>
    <p style="margin:8px 0 0"><a href="/task/${t.id}?back=/sphere">Open task to log progress, push the date or mark done →</a></p>`;
  } else if(n.kind==="project"){ const p=r, list=tasks.filter(t=>t.p===p).sort((a,b)=>(b.late??-999)-(a.late??-999));
    h=`<div class="k">Project</div><h3>${esc(PROJECTS[p].name)}</h3><dl><dt>Open</dt><dd>${openBy[p]||0}</dd><dt>Past due</dt><dd>${lateBy[p]||0}</dd><dt>Waiting on others</dt><dd>${list.filter(t=>t.waiting).length}</dd><dt>Completed</dt><dd>${(doneBy[p]||0).toLocaleString()}</dd></dl>`
      + (list.length?`<ul>${list.slice(0,6).map(t=>`<li>${esc(t.desc)} ${taskLink(t)} <span style="color:${t.overdue?"var(--crit)":"var(--muted)"}">· ${lateText(t)}</span></li>`).join("")}</ul>`:"")
      + `<p style="margin:10px 0 0;display:flex;gap:8px;flex-wrap:wrap">${state.project===p?"":`<button class="chip on" data-iso="${p}">Show only this project</button>`}${p?`<a href="/tasks?project=${p}#register" style="align-self:center">Open register →</a>`:""}</p>`;
  } else if(n.kind==="source"){ const open = tasks.filter(t=>t.src===r).sort((a,b)=>(b.late??-999)-(a.late??-999));
    h=`<div class="k" style="color:${r==="out"?"var(--cyan)":"var(--warn)"}">Where tasks come from</div><h3>${esc(SOURCES[r].name)}</h3><p class="why">${esc(SOURCES[r].desc)}</p>
      <dl><dt>Items captured, all time</dt><dd>${(srcCount[r]||0).toLocaleString()}</dd><dt>Still open</dt><dd>${open.length}${open.filter(t=>t.overdue).length?` · <span style="color:var(--crit)">${open.filter(t=>t.overdue).length} past due</span>`:""}</dd></dl>`
      + (open.length?`<ul>${open.slice(0,8).map(t=>`<li>${esc(t.desc)} ${taskLink(t)}</li>`).join("")}</ul>`:"")
      + (r==="row"?`<p style="margin:10px 0 0"><button class="chip" data-f-chat="1">Show only Rowan chat items</button></p>`:"")
      + (r==="out" ? inboxHtml() : "");
  } else if(n.kind==="waiting"){ const list=r.list.slice().sort((a,b)=>(b.idle??0)-(a.idle??0));
    h=`<div class="k">Ball in their court</div><h3>${esc(r.name)} owes you ${plural(list.length,"item")}</h3>
      <p class="why">You marked these "waiting on ${esc(r.name)}" in Rowan, so they're off your plate until they respond. Rowan flags one as <span class="st stale">stale</span> after ${STALE_DAYS} days with no movement, which is your cue to chase.</p>
      <ul>${list.map(t=>`<li>${esc(t.desc)} ${taskLink(t)}<br><span style="color:var(--muted)">${esc(PROJECTS[t.p].short)} · ${lateText(t)} · last movement ${t.idle===0?"today":plural(t.idle??0,"day")+" ago"}</span>${statusChips(t)}</li>`).join("")}</ul>`;
  } else if(n.kind==="person"){ h=`<div class="k" style="color:#f8fafc">Contact</div><h3>${esc(r.name)}</h3><dl><dt>Emails scanned</dt><dd>${r.emails}</dd><dt>Most-linked project</dt><dd>${r.p?esc(PROJECTS[r.p].name):"None linked yet"}</dd><dt>Named in open tasks</dt><dd>${r.mentions.length}</dd></dl>`+(r.mentions.length?`<ul>${r.mentions.map(t=>`<li>${esc(t.desc)} ${taskLink(t)}</li>`).join("")}</ul>`:"");
  } else if(n.kind==="lead"){ h=`<div class="k">Lead · ${esc(r.status)}</div><h3>${esc(r.name)}</h3><dl><dt>Contract value</dt><dd>${fmt$(r.val)}</dd></dl><p style="margin:8px 0 0"><a href="/leads">Open leads →</a></p>`;
  } else if(n.kind==="pipeline"){ h=`<div class="k">Lead pipeline</div><h3>${DATA.leads.length} leads · ${fmt$(DATA.leads.reduce((s,l)=>s+l[1],0))}</h3><ul>${DATA.leads.map(l=>`<li>${esc(l[0])} · ${fmt$(l[1])} · ${esc(l[2])}</li>`).join("")}</ul>`; }
  if(navList.length>1) h += `<nav><button data-step="-1" aria-label="Previous">‹ Prev</button><span>${navIdx+1} of ${navList.length}</span><button data-step="1" aria-label="Next">Next ›</button></nav>`;
  $("cardBody").innerHTML=h; $("card").style.top = (document.querySelector("#sph .tools").offsetHeight + 18) + "px"; $("card").hidden=false; }
$("card").addEventListener("click", e => { if(e.target.closest("[data-f-chat]")){ state.filter="chat"; applyFilter(); } });

// ---------- search ----------
const index = [
  ...tasks.map(t => ({n:t.node, text:t.desc, sub:`Task · ${PROJECTS[t.p].short} · ${lateText(t)}`})),
  ...Object.keys(PROJECTS).map(k => ({n:hubs["p"+k], text:PROJECTS[k].name, sub:`Project · ${openBy[k]||0} open`})),
  ...partyNodes.map(n => ({n, text:n.ref.name, sub:`Waiting on · owes you ${plural(n.ref.list.length,"item")}`})),
  ...peopleNodes.map(n => ({n, text:n.ref.name, sub:`Contact · ${n.ref.emails} emails`})),
  ...leadNodes.map(n => ({n, text:n.ref.name, sub:`Lead · ${n.ref.status} · ${fmt$(n.ref.val)}`})),
  ...Object.keys(SOURCES).map(k => ({n:hubs[k], text:SOURCES[k].name, sub:"Where tasks come from"}))];
let hits = [], hiIdx = 0;
function runSearch(){ const q=$("q").value.trim().toLowerCase(), box=$("results");
  if(!q){ box.hidden=true; return; }
  const words = q.split(/\s+/);
  hits = index.filter(x => words.every(w => (x.text+" "+x.sub).toLowerCase().includes(w))).slice(0,10); hiIdx = 0;
  box.innerHTML = hits.length ? hits.map((x,i)=>`<button role="option" class="${i===0?"hi":""}" data-hit="${i}">${esc(short(x.text,70))}<small>${esc(x.sub)}</small></button>`).join("") : `<div class="note" style="padding:8px">No matches</div>`;
  box.hidden=false; }
$("q").addEventListener("input", runSearch);
$("q").addEventListener("keydown", e => { const box=$("results");
  if(e.key==="ArrowDown"||e.key==="ArrowUp"){ e.preventDefault(); if(!hits.length) return; hiIdx=(hiIdx+(e.key==="ArrowDown"?1:-1)+hits.length)%hits.length; [...box.querySelectorAll("button")].forEach((b,i)=>b.classList.toggle("hi",i===hiIdx)); }
  if(e.key==="Enter" && hits[hiIdx]){ choose(hits[hiIdx]); }
  if(e.key==="Escape"){ $("q").value=""; box.hidden=true; } });
$("results").addEventListener("click", e => { const b=e.target.closest("[data-hit]"); if(b) choose(hits[+b.dataset.hit]); });
function choose(x){ $("results").hidden=true; $("q").blur();
  if(filtering() && !x.n.vis){ state.filter="all"; state.project=null; applyFilter(); }
  select(x.n); }
document.addEventListener("click", e => { if(!e.target.closest(".search")) $("results").hidden=true; });

// ---------- side panels ----------
const openCt=tasks.length, lateCt=tasks.filter(t=>t.overdue).length, waitCt=tasks.filter(t=>t.waiting).length;
$("kOpen").textContent=openCt; $("kLate").textContent=lateCt; $("kWait").textContent=waitCt; $("kDraft").textContent=mailTot.pending||0;
$("hOpen").textContent=openCt; $("hLate").textContent=lateCt; $("hNodes").textContent=N.toLocaleString(); $("hEdges").textContent=edges.length.toLocaleString();
if(unread!=null){ $("kUnread").textContent=unread.toLocaleString(); $("hUnread").textContent=unread.toLocaleString(); $("kUnreadL").textContent=`Unread in Outlook inbox · ${(INBOX.total||0).toLocaleString()} total`; }
else { $("kUnreadL").textContent="Unread in Outlook · couldn't reach Outlook"; }
const tileGo = (id, fn) => { const el=$(id).closest(".tile"); el.style.cursor="pointer"; el.setAttribute("role","button"); el.tabIndex=0; el.addEventListener("click", fn); el.addEventListener("keydown", e=>{ if(e.key==="Enter") fn(); }); };
tileGo("kUnread", () => select(hubs.out));
tileGo("kLate", () => { state.filter="late"; state.project=null; applyFilter(); closeCard(); });
tileGo("kWait", () => { state.filter="wait"; state.project=null; applyFilter(); closeCard(); });
tileGo("kOpen", () => { state.filter="all"; state.project=null; applyFilter(); closeCard(); });
const maxOpen = Math.max(1, ...Object.values(openBy));
$("byProject").innerHTML = Object.keys(PROJECTS).filter(k=>openBy[k]).sort((a,b)=>openBy[b]-openBy[a]).map(k => { const o=openBy[k], l=lateBy[k]||0;
  return `<button class="row" data-iso="${k}" title="Show only ${esc(PROJECTS[k].short)}"><span style="color:${PROJECTS[k].color}">${esc(PROJECTS[k].short)}</span><span class="bar"><b style="width:${l/maxOpen*100}%;background:var(--crit)"></b><b style="width:${(o-l)/maxOpen*100}%;background:${PROJECTS[k].color};opacity:.7"></b></span><span class="n">${o}</span></button>`; }).join("") || `<p class="note">Nothing open.</p>`;
const late = tasks.filter(t=>t.overdue).sort((a,b)=>b.late-a.late);
$("lateCount").textContent = late.length;
$("lateList").innerHTML = late.slice(0,8).map(t => `<button class="item" data-task="${t.id}"><i style="background:${PROJECTS[t.p].color}"></i><span class="d">${esc(t.desc)}<small>${esc(PROJECTS[t.p].short)}</small></span><span class="late">${t.late}d</span></button>`).join("") || `<p class="note">Nothing past due.</p>`;
$("waitList").innerHTML = (partyNodes.length ? `<p class="note" style="margin:0 0 6px">Items you've handed off and are waiting on someone else for. Stale = no movement in ${STALE_DAYS}+ days.</p>` : "") + partyNodes.map(n => { const list=n.ref.list, stale=list.filter(t=>t.stale).length, lateN=list.filter(t=>t.overdue).length;
  const tag = stale ? `<span class="late w">${stale} stale</span>` : lateN ? `<span class="late">${lateN} past due</span>` : `<span class="late w" style="color:var(--ok)">moving</span>`;
  return `<button class="item" data-wait="${esc(n.ref.name)}"><i style="background:var(--warn)"></i><span class="d">${esc(n.ref.name)}<small>owes you ${plural(list.length,"item")} · ${esc(PROJECTS[list[0].p].short)}</small></span>${tag}</button>`; }).join("") || `<p class="note">Nobody owes you anything right now.</p>`;
$("pipeTotal").textContent = fmt$(DATA.leads.reduce((s,l)=>s+l[1],0));
$("leadList").innerHTML = leadNodes.map((n,i) => { const l=n.ref; return `<button class="item" data-lead="${i}"><i style="background:${l.status==="Won"?"var(--ok)":"var(--warn)"}"></i><span class="d">${esc(l.name)}<small>${esc(l.status)}</small></span><span class="late" style="color:var(--ink)">${fmt$(l.val)}</span></button>`; }).join("");
(function(){ const ms=DATA.months.map(m=>m[0]), vals=DATA.months.map(m=>+m[1]); if(!vals.length) return; const mx=Math.max(...vals);
  const W=320,H=120,pl=8,pr=8,pt=14,pb=18,bw=(W-pl-pr)/ms.length; let s="";
  [.5,1].forEach(f => { const y=pt+(H-pt-pb)*(1-f); s+=`<line x1="${pl}" x2="${W-pr}" y1="${y}" y2="${y}" stroke="#352F78"/>`; });
  vals.forEach((v,i) => { const h=(H-pt-pb)*v/mx, x=pl+i*bw+4, y=H-pb-h, lastBar=i===vals.length-1;
    s+=`<rect x="${x}" y="${y}" width="${bw-8}" height="${Math.max(1,h)}" rx="2" fill="${lastBar?"#B45CF0":"#4a4394"}"/><text x="${x+(bw-8)/2}" y="${H-5}" text-anchor="middle">${monName(ms[i])}</text><text x="${x+(bw-8)/2}" y="${y-3}" text-anchor="middle" style="fill:#E6E3FF">${v>=1000?(v/1000).toFixed(1)+"k":v}</text>`; });
  $("spark").innerHTML=s; })();
document.querySelector("#sph .rail").addEventListener("click", e => { const b=e.target.closest("[data-iso],[data-task],[data-wait],[data-lead]"); if(!b) return;
  if(b.dataset.iso!==undefined){ isolateProject(+b.dataset.iso); closeCard(); }
  else { let n=null;
    if(b.dataset.task) n=tasks.find(t=>t.id==b.dataset.task).node;
    if(b.dataset.wait) n=partyNodes.find(x=>x.ref.name===b.dataset.wait);
    if(b.dataset.lead) n=leadNodes[+b.dataset.lead];
    if(n){ if(filtering() && !n.vis){ state.filter="all"; state.project=null; applyFilter(); } select(n); } }
  if(window.innerWidth<=900) stage.scrollIntoView({behavior:"smooth"}); });
function focusOn(n){ const d=n.pos.clone(); if(d.lengthSq()<.01) return; d.normalize(); target={y:Math.atan2(-d.x,d.z), x: shape==='sphere' ? Math.asin(d.y) : SHAPES[shape].rotX}; auto=false; }
$("legend").innerHTML = Object.keys(PROJECTS).filter(k=>k!=="0").map(k=>`<span><i style="background:${PROJECTS[k].color}"></i>${esc(PROJECTS[k].short)}</span>`).join("")
  + `<span><i style="background:#ff4d6d"></i>Past due</span><span><i style="background:#fbbf24"></i>Waiting on others / pending draft</span><span><i style="background:#67e8f9"></i>Unread email</span><span><i style="background:#f8fafc"></i>Contact</span>`;

// ---------- shapes: every node gets a position in each layout; switching morphs between them ----------
const TAU = Math.PI*2;
const projOrder = Object.keys(PROJECTS).map(Number).sort((a,b)=>(a===0)-(b===0) || a-b);
const NP = projOrder.length, projIx = {}; projOrder.forEach((p,i)=>projIx[p]=i);
const srcKeys = Object.keys(SOURCES);
const M = Math.max(1, months.length), mIdx = m => Math.max(0, months.indexOf(m));
const maxDone = Math.max(1, ...Object.values(doneBy));
const stackYaw = () => camera.aspect < 1 ? -.8 : 0;   // phones look down the row of buildings
const SHAPES = {
  sphere:{name:"Sphere", cam:34, rotX:.25, bow:1, gain:1, cap:"Every task linked to its project and to where it came from. Bright = open, dim = completed."},
  orbit:{name:"Orbit", cam:46, rotX:.95, bow:0, gain:.9, cap:"Rings are due windows around today: the closer to the center, the more overdue. Each wedge is a project."},
  galaxy:{name:"Galaxy", cam:48, rotX:.8, bow:0, gain:.85, cap:"One spiral arm per project. Open work glows near the core; completed history trails outward by month."},
  torus:{name:"Torus", cam:42, rotX:.62, bow:0, gain:.85, cap:"Projects sit around the ring; inside each section time runs from oldest to newest. Thin sections are quiet projects."},
  helix:{name:"Helix", cam:50, rotX:.12, bow:0, gain:.8, cap:"Height is time: May at the bottom, open work at the top. One strand per project; capture sources run up the core."},
  network:{name:"Network", cam:52, rotX:1.2, bow:0, gain:.9, cap:"Flat map of projects, the parties you're waiting on, contacts and sources. Easiest to read on a phone."},
  skyline:{name:"Skyline", cam:46, rotX:.55, bow:0, gain:.45, cap:"Each project is a city block. Tower height = days since the task last moved; red tops are past due."}};
let shape = "sphere";
const g3 = () => (rnd()+rnd()+rnd()-1.5)/1.5;
const polar = (r,a,y=0) => [Math.cos(a)*r, y, Math.sin(a)*r];
const LAY = {};
nodes.forEach(n => n.home0 = n.pos.clone());
function buildLayout(name, place){
  seed = 11 + Object.keys(LAY).length*97;
  const out = new Float32Array(N*3);
  const put = (n,p) => out.set(p, n.i*3);
  const get = n => [out[n.i*3], out[n.i*3+1], out[n.i*3+2]];
  // first pass: everything that doesn't depend on other nodes
  nodes.forEach(n => { if(n.kind!=="waiting" && n.kind!=="unread") put(n, place(n)); });
  // second pass: waiting-on parties float above their tasks, unread mail swarms around Outlook
  partyNodes.forEach(n => { const c=[0,0,0]; n.ref.list.forEach(t=>{ const q=get(t.node); c[0]+=q[0]; c[1]+=q[1]; c[2]+=q[2]; });
    const L=n.ref.list.length; const v=V(c[0]/L,c[1]/L,c[2]/L); const lift = place.lift ? place.lift(v) : v.clone().normalize().multiplyScalar(1.4);
    put(n, [v.x+lift.x, v.y+lift.y, v.z+lift.z]); });
  const o = V(...get(hubs.out));
  nodes.forEach(n => { if(n.kind==="unread") put(n, [o.x+g3()*1.3, o.y+g3()*1.3, o.z+g3()*1.3]); });
  LAY[name] = out;
}
buildLayout("sphere", n => [n.home0.x, n.home0.y, n.home0.z]);

// ORBIT: rings = due window, wedge = project
(function(){ const W = TAU/NP;
  const ring = t => t.late===null ? 12.6 : t.late>0 ? 2.2 + 3*(1-Math.min(t.late,14)/14) : t.late>=-7 ? 6.2 + (-t.late)/7*1.2 : t.late>=-14 ? 8.4 + (-t.late-7)/7*1 : 10.3 + rnd()*.9;
  const f = n => { const k=n.kind;
    if(k==="task"){ const t=n.ref; return polar(ring(t), projIx[t.p]*W + (rnd()-.5)*W*.8, g3()*.15); }
    if(k==="done") return polar(14.4 + (1-(mIdx(n.m)+1)/M)*5 + rnd()*.8, projIx[n.p]*W + (rnd()-.5)*W*.85, g3()*.3);
    if(k==="project") return polar(13.6, projIx[n.ref]*W, .4);
    if(k==="source") return polar(22, srcKeys.indexOf(n.ref)/srcKeys.length*TAU + W/2, 0);
    if(k==="pipeline") return [0,5.5,0];
    if(k==="lead") return polar(1.6, rnd()*TAU, 5.5+g3()*.4);
    if(k==="person") return polar(16+rnd()*2.5, projIx[n.home]*W + (rnd()-.5)*W*.7, 1.6+rnd());
    if(k==="mail") return polar(24+rnd()*3, rnd()*TAU, g3()*.6);
    return [0,0,0]; };
  f.lift = v => V(0,.9,0).add(v.clone().setY(0).normalize().multiplyScalar(.8));
  buildLayout("orbit", f); })();

// GALAXY: one spiral arm per project, open work near the core
(function(){ const arm = (p, s, spread, ylift=0) => { const a0 = projIx[p]/NP*TAU, r = 1.4 + s*15, a = a0 + s*2.6 + g3()*spread*(.12+s*.22);
    return polar(r, a, g3()*.5*(1-s*.6) + ylift); };
  const f = n => { const k=n.kind;
    if(k==="task") return arm(n.ref.p, n.ref.overdue ? .04+rnd()*.08 : .1+rnd()*.14, .9);
    if(k==="done") return arm(n.p, .3 + (1-(mIdx(n.m)+1)/M)*.55 + rnd()*.1, 1.1);
    if(k==="project"){ const a0=projIx[n.ref]/NP*TAU, s=.17; return polar(1.4+s*15, a0+s*2.6, .5); }
    if(k==="source") return polar(20, srcKeys.indexOf(n.ref)/srcKeys.length*TAU, 0);
    if(k==="pipeline") return [15,5,-15];
    if(k==="lead") return [15+g3()*1.6, 5+g3()*.6, -15+g3()*1.6];
    if(k==="person") return arm(n.home, .42+rnd()*.12, 1.4, .9);
    if(k==="mail"){ const r=2+rnd()*17; return polar(r, rnd()*TAU, g3()*.3); }
    return [0,0,0]; };
  f.lift = v => V(0,1,0);
  buildLayout("galaxy", f); })();

// TORUS: projects around the ring, time runs through each section
(function(){ const RM=10, rm=3.2, W=TAU/NP*.86;
  const tp = (th, ph, rr) => [(RM+rr*Math.cos(ph))*Math.cos(th), rr*Math.sin(ph), (RM+rr*Math.cos(ph))*Math.sin(th)];
  const th0 = p => projIx[p]/NP*TAU;
  const f = n => { const k=n.kind;
    if(k==="done") return tp(th0(n.p) - W/2 + (mIdx(n.m)+rnd())/M*W*.82, rnd()*TAU, rm*(.96+rnd()*.06));
    if(k==="task") return tp(th0(n.ref.p) + W/2 - rnd()*W*.2, g3()*.55, rm*1.1);
    if(k==="project") return tp(th0(n.ref), Math.PI/2, rm*1.25);
    if(k==="source") return polar(4.4, srcKeys.indexOf(n.ref)/srcKeys.length*TAU, 0);
    if(k==="pipeline") return [0,6.5,0];
    if(k==="lead") return polar(1.5, rnd()*TAU, 6.5+g3()*.4);
    if(k==="person") return tp(th0(n.home)+(rnd()-.5)*W, -Math.PI/2+g3()*.5, rm*1.55);
    if(k==="mail") return polar(16.5+rnd()*3, rnd()*TAU, g3()*1.2);
    return [0,0,0]; };
  f.lift = v => v.clone().setY(0).normalize().multiplyScalar(1.6).add(V(0,.6,0));
  buildLayout("torus", f); })();

// HELIX: height = time, one strand per project
const helixY = mi => -12 + mi/Math.max(1,M-1)*19;
(function(){ const strand = (p, y, r=8, j=.25) => polar(r + g3()*.6, y*.42 + projIx[p]/NP*TAU + g3()*j, y);
  const f = n => { const k=n.kind;
    if(k==="done") return strand(n.p, helixY(mIdx(n.m)) + rnd()*(19/Math.max(1,M-1)));
    if(k==="task") return strand(n.ref.p, 10 + rnd()*3.2 + (n.ref.overdue?.6:0), 8.2, .18);
    if(k==="project") return strand(n.ref, 15, 8.6, 0);
    if(k==="source") return polar(.9, srcKeys.indexOf(n.ref)*1.9, -11 + srcKeys.indexOf(n.ref)*3.2);
    if(k==="pipeline") return [0,18.5,0];
    if(k==="lead") return polar(1.6, rnd()*TAU, 18.5+g3()*.5);
    if(k==="person") return strand(n.home, 8 + rnd()*5, 11.5, .5);
    if(k==="mail") return polar(14+rnd()*1.6, rnd()*TAU, -13 + rnd()*30);
    return [0,0,0]; };
  f.lift = v => v.clone().setY(0).normalize().multiplyScalar(1.6).add(V(0,.4,0));
  buildLayout("helix", f); })();

// NETWORK: flat constellation
(function(){ const hub = p => polar(9.5, projIx[p]/NP*TAU - Math.PI/2);
  const around = (p, r, y=0) => { const h=hub(p), a=rnd()*TAU; return [h[0]+Math.cos(a)*r, y, h[2]+Math.sin(a)*r]; };
  const f = n => { const k=n.kind;
    if(k==="project") return hub(n.ref);
    if(k==="task") return around(n.ref.p, 1.3 + rnd()*1.9, .05);
    if(k==="done") return around(n.p, 1.4 + Math.sqrt(rnd())*(1.4 + 3.4*Math.sqrt((doneBy[n.p]||1)/maxDone)), -.05);
    if(k==="source") return polar(18, srcKeys.indexOf(n.ref)/srcKeys.length*TAU + Math.PI/8, 0);
    if(k==="pipeline") return [0,0,0];
    if(k==="lead") return polar(1.8, rnd()*TAU, 0);
    if(k==="person"){ const h=hub(n.home), a=Math.atan2(h[2],h[0]) + g3()*.5, r=14 + rnd()*2; return polar(r, a, 0); }
    if(k==="mail") return polar(21+rnd()*3, rnd()*TAU, -.2);
    return [0,0,0]; };
  f.lift = v => v.clone().setY(0).normalize().multiplyScalar(1.8);
  buildLayout("network", f); })();

// SKYLINE: project blocks; tower height = days since last movement
const BLOCK = {}; (function(){ const cols = Math.ceil(Math.sqrt(NP+1)), sp = 9.5;
  [...projOrder, "lead"].forEach((p,i) => { BLOCK[p] = [(i%cols - (cols-1)/2)*sp, (Math.floor(i/cols) - (Math.ceil((NP+1)/cols)-1)/2)*sp]; }); })();
const towerSlot = {}; projOrder.forEach(p => { const list = tasks.filter(t=>t.p===p); const c = Math.ceil(Math.sqrt(list.length||1)), step = Math.min(1.4, 6/c);
  list.forEach((t,j) => towerSlot[t.id] = [((j%c)-(c-1)/2)*step, (Math.floor(j/c)-(c-1)/2)*step]); });
const towerH = t => .6 + Math.min(t.idle ?? 0, 30)*.32;
(function(){ const f = n => { const k=n.kind;
    if(k==="task"){ const b=BLOCK[n.ref.p], s=towerSlot[n.ref.id]; return [b[0]+s[0], towerH(n.ref), b[1]+s[1]]; }
    if(k==="done"){ const b=BLOCK[n.p]; return [b[0]+(rnd()-.5)*6.6, .02, b[1]+(rnd()-.5)*6.6]; }
    if(k==="project"){ const b=BLOCK[n.ref]; return [b[0]-3.6, .3, b[1]+3.9]; }
    if(k==="source") return polar(23, srcKeys.indexOf(n.ref)/srcKeys.length*TAU, 0);
    if(k==="pipeline"){ const b=BLOCK.lead; return [b[0]-3.6, .3, b[1]+3.9]; }
    if(k==="lead"){ const b=BLOCK.lead, j=leadNodes.indexOf(n); return [b[0]+(j%3-1)*1.8, .6 + n.ref.val/30000, b[1]+(Math.floor(j/3)-.5)*1.8]; }
    if(k==="person"){ const b=BLOCK[n.home], a=rnd()*TAU; return [b[0]+Math.cos(a)*5.2, .2, b[1]+Math.sin(a)*5.2]; }
    if(k==="mail") return [(rnd()-.5)*50, -.8, (rnd()-.5)*50];
    return [0,0,0]; };
  f.lift = () => V(0,2,0);
  buildLayout("skyline", f); })();

// guides drawn for each shape (rings, month marks, city blocks, tower stems)
const guideGroups = {}, guideLabels = [];
const gmat = c => new THREE.LineBasicMaterial({color:c, transparent:true, opacity:0, depthWrite:false});
const circle = (r, y=0, seg=128) => { const p=[]; for(let i=0;i<=seg;i++){ const a=i/seg*TAU; p.push(V(Math.cos(a)*r,y,Math.sin(a)*r)); } return p; };
function guide(shapeKey, pts, color, segments){ const grp = guideGroups[shapeKey] ||= new THREE.Group(); world.add(grp);
  const geo = new THREE.BufferGeometry().setFromPoints(pts); grp.add(segments ? new THREE.LineSegments(geo, gmat(color)) : new THREE.Line(geo, gmat(color))); }
const glabel = (shapeKey, text, v, color) => { const el=document.createElement("div"); el.className="lab"; el.textContent=text; el.style.color=color||"#9993D0"; el.style.fontSize="9.5px"; el.style.opacity=0; $("labels").appendChild(el); guideLabels.push({shape:shapeKey, el, v}); };
for(let lat=-60; lat<=60; lat+=30){ const r=R*Math.cos(lat*Math.PI/180), y=R*Math.sin(lat*Math.PI/180); guide("sphere", circle(r,y,96), 0x2a2470); }
[[5.3,"PAST DUE","#ff4d6d",3.4],[7.5,"NEXT 7 DAYS","#fbbf24",6.8],[9.5,"8–14 DAYS","#9993D0",8.9],[11.4,"LATER","#9993D0",10.8]].forEach(([r,txt,c,lr]) => { guide("orbit", circle(r), r<6?0xa0304f:0x5a4fc0); glabel("orbit", txt, V(0,0,-lr), c); });
glabel("orbit", "TODAY", V(0,.3,0), "#E6E3FF"); glabel("orbit", "NO DUE DATE", V(0,0,-12.6));
[6,10,14,18].forEach(r => guide("galaxy", circle(r,0,128), 0x221d5c));
guide("torus", circle(10), 0x2a2470); guide("torus", circle(13.2), 0x221d5c); guide("torus", circle(6.8), 0x221d5c);
months.forEach((m,i) => { const y=helixY(i); guide("helix", circle(10.6, y, 96), 0x221d5c); glabel("helix", MON[+m.slice(2)-1].toUpperCase(), V(-11.2, y, 0)); });
glabel("helix", "OPEN NOW", V(-11.2, 11.6, 0), "#E6E3FF"); guide("helix", [V(0,-13,0), V(0,19,0)], 0x352F78);
projOrder.forEach(p => guide("network", circle(1.1, 0, 48).map(v => v.add(V(...polar(9.5, projIx[p]/NP*TAU - Math.PI/2)))), 0x352F78));
guide("network", circle(9.5), 0x1d1850);
Object.values(BLOCK).forEach(([x,z]) => { const h=3.6; guide("skyline", [V(x-h,0,z-h),V(x+h,0,z-h),V(x+h,0,z+h),V(x-h,0,z+h),V(x-h,0,z-h)], 0x5a4fc0); });
guide("skyline", tasks.flatMap(t => { const b=BLOCK[t.p], s=towerSlot[t.id]; return [V(b[0]+s[0],0,b[1]+s[1]), V(b[0]+s[0],towerH(t),b[1]+s[1])]; }), 0xb3a8ff, true);
glabel("skyline", "TOWER HEIGHT = DAYS SINCE LAST MOVEMENT", V(0,-.5, Math.max(...Object.values(BLOCK).map(b=>b[1]))+6), "#9993D0");
// STACKING PLAN: one building per project, one floor per open task (soonest due at the bottom),
// colored like a leasing stacking plan; cranes mark who you're waiting on.
const STACK = (function(){
  const FLOOR = .78, SLAB = .62, BW = 3.6, BD = 3, SP = 6.4;
  const floorColor = t => t.overdue ? "#ff4d6d" : t.waiting ? "#fbbf24" : (t.late!==null && t.late>=-7) ? "#B45CF0" : "#5b6399";
  const order = projOrder.slice().sort((a,b)=>(openBy[b]||0)-(openBy[a]||0) || (doneBy[b]||0)-(doneBy[a]||0));
  const cols = [...order, "lead"], x0 = -(cols.length-1)/2*SP;
  const B = {}; cols.forEach((p,i) => { const done = p==="lead" ? 0 : (doneBy[p]||0);
    B[p] = {x:x0+i*SP, pod: p==="lead" ? .3 : .35 + Math.log10(done+1)*.75, floors:[]}; });
  const byDue = (a,b) => (a.due||"9999") < (b.due||"9999") ? -1 : (a.due||"9999") > (b.due||"9999") ? 1 : 0;
  order.forEach(p => tasks.filter(t=>t.p===p).sort(byDue).forEach((t,j) => { t.floor = j; B[p].floors.push(t); }));
  leadNodes.forEach((n,j) => B.lead.floors.push(n));
  const floorY = (p,j) => B[p].pod + j*FLOOR + FLOOR/2;
  const top = p => B[p].pod + B[p].floors.length*FLOOR;
  // cranes: one per party per building
  const cranes = []; const corner = [[2.4,-1.9],[-2.4,-1.9],[2.4,1.9],[-2.4,1.9]];
  order.forEach(p => { const parties = [...new Set(B[p].floors.filter(t=>t.waiting).map(t=>t.party))];
    parties.forEach((party,k) => { const list = B[p].floors.filter(t=>t.party===party), idle = Math.max(...list.map(t=>t.idle??0));
      cranes.push({p, party, list, idle, c:corner[k%4], k}); }); });
  const offices = {}; let oi = 0;
  partyNodes.forEach(n => { const p = n.ref.list[0].p; const sameB = partyNodes.filter(m=>m.ref.list[0].p===p); const k = sameB.indexOf(n);
    offices[n.i] = [B[p].x + (k-(sameB.length-1)/2)*1.7, .45, 6.2]; oi++; });
  return {B, cols, floorY, top, floorColor, cranes, offices, BW, BD, SLAB, FLOOR};
})();
SHAPES.stack = {name:"Stacking plan", cam:62, rotX:.22, lookY:4.2, bow:0, gain:.07, cap:"Each building is a project; each floor an open task, soonest due at the bottom. Red = past due, amber = waiting on someone, purple = due in 7 days, gray = later. The ghosted podium is completed work. Cranes mark who you're waiting on: amber after 5 days without movement, red after 10."};
(function(){ const S = STACK;
  const f = n => { const k=n.kind;
    if(k==="task"){ const b=S.B[n.ref.p]; return [b.x, S.floorY(n.ref.p, n.ref.floor), S.BD/2+.05]; }
    if(k==="done"){ const b=S.B[n.p]; return [b.x+(rnd()-.5)*S.BW*.9, rnd()*b.pod*.95, (rnd()-.5)*S.BD*.9]; }
    if(k==="project"){ const b=S.B[n.ref]; return [b.x, S.top(n.ref)+.5, 0]; }
    if(k==="pipeline") return [S.B.lead.x, S.top("lead")+.5, 0];
    if(k==="lead"){ const j=S.B.lead.floors.indexOf(n); return [S.B.lead.x, S.floorY("lead", j), S.BD/2+.05]; }
    if(k==="source") return [(srcKeys.indexOf(n.ref)-(srcKeys.length-1)/2)*6, .3, -9];
    if(k==="person"){ const b=S.B[n.home]||S.B[0]; return [b.x+(rnd()-.5)*4, .15, -5-rnd()*2]; }
    if(k==="mail") return [(rnd()-.5)*70, -.6, (rnd()-.5)*34];
    return [0,0,0]; };
  f.lift = () => V(0,0,0);
  buildLayout("stack", f);
  // offices replace the averaged position for waiting-on parties
  partyNodes.forEach(n => LAY.stack.set(S.offices[n.i], n.i*3));
  const o = LAY.stack.slice(hubs.out.i*3, hubs.out.i*3+3);
  nodes.forEach(n => { if(n.kind==="unread") LAY.stack.set([o[0]+g3()*1.2, o[1]+.8+Math.abs(g3())*1.2, o[2]+g3()*1.2], n.i*3); });
})();
(function(){ const S = STACK, grp = guideGroups.stack = new THREE.Group(); world.add(grp);
  const add = (obj, op) => { obj.userData.op = op; grp.add(obj); return obj; };
  const slabGeo = new THREE.BoxGeometry(S.BW, S.SLAB, S.BD), slabEdges = new THREE.EdgesGeometry(slabGeo);
  const mat = (c, op, lines) => lines ? new THREE.LineBasicMaterial({color:c, transparent:true, opacity:0, depthWrite:false}) : new THREE.MeshBasicMaterial({color:c, transparent:true, opacity:0, depthWrite:false});
  S.cols.forEach(p => { const b = S.B[p];
    // podium of completed work (ghosted)
    const pg = new THREE.BoxGeometry(S.BW+.5, b.pod, S.BD+.5);
    const pm = add(new THREE.Mesh(pg, mat(p==="lead"?"#fbbf24":PROJECTS[p].color)), .07); pm.position.set(b.x, b.pod/2, 0);
    const pe = add(new THREE.LineSegments(new THREE.EdgesGeometry(pg), mat(p==="lead"?"#fbbf24":PROJECTS[p].color, 0, true)), .45); pe.position.copy(pm.position);
    // floors
    b.floors.forEach((t,j) => { const c = p==="lead" ? (t.ref.status==="Won"?"#4ade80":"#fbbf24") : S.floorColor(t);
      const m = add(new THREE.Mesh(slabGeo, mat(c)), .5); m.position.set(b.x, S.floorY(p,j), 0);
      const e = add(new THREE.LineSegments(slabEdges, mat(c,0,true)), .95); e.position.copy(m.position); });
    // ground pad
    const pad = add(new THREE.LineSegments(new THREE.EdgesGeometry(new THREE.PlaneGeometry(S.BW+2, S.BD+2).rotateX(-Math.PI/2)), mat("#352F78",0,true)), .8); pad.position.set(b.x, 0, 0);
  });
  // cranes
  S.cranes.forEach(cr => { const b = S.B[cr.p], color = cr.idle>=10 ? "#ff4d6d" : cr.idle>=STALE_DAYS ? "#fbbf24" : "#CBC7F0";
    const mx = b.x+cr.c[0], mz = cr.c[1], h = S.top(cr.p) + 1.6 + cr.k*.45;
    const jibEnd = V(b.x - cr.c[0]*.35, h, mz*.4), counter = V(mx + Math.sign(cr.c[0])*1.1, h, mz);
    const pts = [V(mx,0,mz), V(mx,h+.7,mz),  V(mx,h,mz), jibEnd,  V(mx,h+.7,mz), jibEnd,  V(mx,h,mz), counter,  V(mx,h+.7,mz), counter];
    cr.list.forEach(t => { const y = S.floorY(cr.p, t.floor) + S.SLAB/2; const hx = b.x + (jibEnd.x-b.x)*.6, hz = mz*.25; pts.push(V(hx,h,hz), V(hx,y,hz)); });
    add(new THREE.LineSegments(new THREE.BufferGeometry().setFromPoints(pts), mat(color,0,true)), 1);
    const off = S.offices[cr.party.i];
    add(new THREE.LineSegments(new THREE.BufferGeometry().setFromPoints([V(mx,h+.7,mz), V(off[0], off[1]+.4, off[2])]), mat(color,0,true)), .35);
    // counterweight block
    const cw = add(new THREE.Mesh(new THREE.BoxGeometry(.5,.4,.5), mat(color)), .8); cw.position.copy(counter).add(V(0,-.2,0));
  });
  // site offices for the parties you're waiting on
  partyNodes.forEach(n => { const o = S.offices[n.i];
    const m = add(new THREE.LineSegments(new THREE.EdgesGeometry(new THREE.BoxGeometry(1.1,.8,.8)), mat("#fbbf24",0,true)), .9); m.position.set(o[0], o[1], o[2]); });
  // ground line
  const w = (S.cols.length)*6.4/2 + 4;
  add(new THREE.LineSegments(new THREE.BufferGeometry().setFromPoints([V(-w,0,3.4),V(w,0,3.4), V(-w,0,-3.4),V(w,0,-3.4)]), mat("#2a2470",0,true)), .8);
})();
glabel("stack", "FLOORS: SOONEST DUE AT BOTTOM  ·  PODIUM = COMPLETED WORK", V(0, -.6, 4.6), "#9993D0");
glabel("stack", "SITE OFFICES = WHO YOU'RE WAITING ON", V(0, -.6, 8.4), "#fbbf24");

let morph = {t:1, from:null, to:LAY.sphere, bowFrom:1, bowTo:1, gainFrom:1};
let camGoal = null, lookY = 0, stackInit = false;
function setShape(name){
  if(name===shape && morph.t>=1) return;
  morph = {t:0, from:pPos.slice(), to:LAY[name], bowFrom:bow, bowTo:SHAPES[name].bow, gainFrom:gain, gainTo:SHAPES[name].gain, prev:shape};
  shape = name; camGoal = SHAPES[name].cam * (camera.aspect<1 ? (name==="stack" ? 1.15 : Math.min(1.7, .92/camera.aspect)) : 1);
  target = {y: name==="stack" ? stackYaw() : rotY, x:SHAPES[name].rotX};
  renderShapes(); $("shapeCap").textContent = SHAPES[name].cap;
}
function stepMorph(dt){
  if(morph.t>=1) return false;
  morph.t = Math.min(1, morph.t + dt/1.5); const e = morph.t<.5 ? 4*morph.t**3 : 1-(-2*morph.t+2)**3/2;
  for(let i=0;i<N*3;i++) pPos[i] = morph.from[i] + (morph.to[i]-morph.from[i])*e;
  nodes.forEach(n => n.pos.set(pPos[n.i*3], pPos[n.i*3+1], pPos[n.i*3+2]));
  pGeo.attributes.position.needsUpdate = true; pGeo.computeBoundingSphere();
  bow = morph.bowFrom + (morph.bowTo-morph.bowFrom)*e; gain = morph.gainFrom + (morph.gainTo-morph.gainFrom)*e;
  let k=0; edges.forEach(ed => { k = curve(ed, ePos, k); }); eGeo.attributes.position.needsUpdate = true; eGeo.computeBoundingSphere();
  if(morph.t>=1 || Math.round(morph.t*20)%5===0) applyFilter();
  if(selected) highlight(selected);
  return true;
}
function renderShapes(){ if(!$("shapes")) return; $("shapes").innerHTML = Object.entries(SHAPES).map(([k,s]) => `<button class="chip ${k===shape?"on":""}" data-shape="${k}" aria-pressed="${k===shape}">${s.name}</button>`).join(""); }
$("shapes") && $("shapes").addEventListener("click", e => { const b=e.target.closest("[data-shape]"); if(b) setShape(b.dataset.shape); });
renderShapes();

// open in the Stacking plan every time (other shapes are one tap away)
(function(){ const S = "stack";
  pPos.set(LAY[S]); nodes.forEach(n => n.pos.set(pPos[n.i*3], pPos[n.i*3+1], pPos[n.i*3+2])); pGeo.attributes.position.needsUpdate = true;
  shape = S; bow = SHAPES[S].bow; gain = SHAPES[S].gain; rotX = SHAPES[S].rotX; rotY = 0; stackInit = true; lookY = SHAPES[S].lookY||0;
  morph = {t:1, from:null, to:LAY[S], bowFrom:bow, bowTo:bow, gainFrom:gain, gainTo:gain, prev:null};
  let k=0; edges.forEach(ed => { k = curve(ed, ePos, k); }); eGeo.attributes.position.needsUpdate = true;
  renderShapes(); $("shapeCap").textContent = SHAPES[S].cap; })();

// ---------- loop ----------
function resize(){ const r=stage.getBoundingClientRect(); renderer.setSize(r.width,r.height,false); camera.aspect=r.width/r.height; camera.updateProjectionMatrix();
  pMat.uniforms.uScale.value=Math.max(.7,Math.min(1.25,Math.min(r.height,r.width*1.3)/650));
  if(!fitted && r.width>0){ camDist = SHAPES[shape].cam * (camera.aspect<1 ? (shape==="stack" ? 1.15 : Math.min(1.7, .92/camera.aspect)) : 1); if(shape==="stack") rotY = stackYaw(); fitted = true; } }
new ResizeObserver(resize).observe(stage); resize();
applyFilter();
const clock = new THREE.Clock();
let lastT = performance.now();
function frame(){ const t=clock.getElapsedTime(), now=performance.now(), dt=Math.min(.05,(now-lastT)/1000); lastT=now;
  stepMorph(dt);
  lookY += ((SHAPES[shape].lookY||0) - lookY)*.06;
  if(camGoal!==null){ camDist += (camGoal-camDist)*.06; if(Math.abs(camGoal-camDist)<.05) camGoal=null; }
  Object.entries(guideGroups).forEach(([key,grp]) => { const o = key===shape ? morph.t : key===morph.prev ? 1-morph.t : 0;
    grp.visible = o>0.01; grp.children.forEach(c => c.material.opacity = o*(c.userData.op ?? (key==="skyline"||key==="orbit"?.85:.6))); });
  if(state.spin && !dragging && !auto && !target && !hover && performance.now()-idle>6000 && $("card").hidden) auto=true;
  if(auto && !hover){ if(shape==="stack") rotY += (stackYaw() + 0.38*Math.sin(t*.12) - rotY)*.02; else rotY+=.0016; }
  if(target){ const wrap=a=>Math.atan2(Math.sin(a),Math.cos(a)), dy=wrap(target.y-rotY); rotY+=dy*.08; rotX+=(target.x-rotX)*.08; if(Math.abs(dy)<.002&&Math.abs(target.x-rotX)<.002) target=null; }
  world.rotation.set(rotX,rotY,0,"XYZ"); camera.position.set(0,lookY,camDist); camera.lookAt(0,lookY,0);
  pMat.uniforms.uTime.value = reduceMotion?0:t; world.updateMatrixWorld();
  const rect = canvas.getBoundingClientRect();
  guideLabels.forEach(gl => { const o = gl.shape===shape ? morph.t : 0; if(o<.02){ gl.el.style.opacity=0; return; }
    wp.copy(gl.v).applyMatrix4(world.matrixWorld); const z=wp.z; wp.project(camera);
    gl.el.style.left=((wp.x+1)/2*rect.width)+"px"; gl.el.style.top=((1-wp.y)/2*rect.height)+"px"; gl.el.style.opacity = (z>-4?1:.35)*o; });
  labelled.forEach(n => { const s=screenOf(n,rect); n.el.style.left=s.x+"px"; n.el.style.top=s.y+"px";
    n.el.style.opacity = n.el.dataset.dim ? .1 : s.z>-2 ? (n.kind==="waiting"?.9:1) : .15; });
  const shown = [];
  tasks.forEach(tk => { const el=tk.el; if(!el.dataset.show && selected!==tk.node){ el.style.display="none"; return; }
    const s=screenOf(tk.node,rect); if(s.z<-1){ el.style.display="none"; return; }
    shown.push({el, x:s.x, y:s.y, sel:selected===tk.node}); });
  shown.sort((a,b)=>a.y-b.y);
  const placed = [];   // nudge overlapping name tags apart so they stay readable
  shown.forEach(o => { let y=o.y; for(const q of placed){ if(Math.abs(q.x-o.x)<170 && Math.abs(q.y-y)<19) y=q.y+19; }
    if(y > rect.height-34 && !o.sel){ o.el.style.display="none"; return; }
    placed.push({x:o.x,y}); o.el.style.display="block"; o.el.style.left=o.x+"px"; o.el.style.top=y+"px"; o.el.style.zIndex=o.sel?3:1; });
  $("hSpin").textContent=(((rotY*180/Math.PI)%360+360)%360).toFixed(1)+"°";
  renderer.render(scene,camera); requestAnimationFrame(frame); }
frame();
// refresh the data every 10 minutes when you're not in the middle of something
setInterval(() => { if($("card").hidden && !dragging && !filtering() && document.activeElement!==$("q")) location.reload(); }, 600000);
})();
</script>
"""
