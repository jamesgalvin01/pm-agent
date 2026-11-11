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
#sph .corner{position:absolute;right:12px;top:10px;font-family:var(--mono);font-size:10px;color:var(--muted);letter-spacing:.08em;pointer-events:none}
#sph .legend{position:absolute;left:12px;bottom:12px;display:flex;flex-wrap:wrap;gap:6px 12px;max-width:calc(100% - 24px);font-family:var(--mono);font-size:10.5px;color:var(--muted);pointer-events:none}
#sph .legend i{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:5px}
#sph .card{position:absolute;left:14px;top:14px;width:min(350px,calc(100% - 28px));max-height:calc(100% - 60px);overflow-y:auto;background:rgba(20,17,58,.95);border:1px solid var(--warn);border-radius:12px;padding:12px 14px;font-size:13px;line-height:1.45;box-shadow:0 10px 40px rgba(0,0,0,.5)}
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
  #sph .stage{height:62vh;min-height:400px;border-right:0;border-bottom:1px solid var(--line)}
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
      <div class="corner">DRAG TO ROTATE · PINCH TO ZOOM · TAP A POINT</div>
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
const SOURCES = {out:{name:"Outlook"}, ema:{name:"Email scan"}, gma:{name:"Gmail"}, row:{name:"Rowan chat"},
  tel:{name:"Telegram"}, das:{name:"Dashboard"}, mee:{name:"Meetings"}, man:{name:"Manual"}};
const INBOX = DATA.inbox || null;

const $ = id => document.getElementById(id);
const esc = s => String(s==null?"":s).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const daysLate = due => due ? Math.round((Date.parse(TODAY) - Date.parse(due)) / 86400000) : null;
const MON = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];
const monName = yymm => MON[+yymm.slice(2)-1];
const fmt$ = v => "$" + (v>=1e6 ? (v/1e6).toFixed(2)+"M" : Math.round(v/1000)+"K");
let seed = 7; const rnd = () => { seed = (seed*16807) % 2147483647; return (seed-1)/2147483646; };
const src3 = s => (s||"x").slice(0,3);

const tasks = DATA.open.map(r => ({id:r[0],p:PROJECTS[r[1]]?r[1]:0,desc:r[2],due:r[3],pri:r[4]||"medium",bic:r[5],wait:r[6],block:r[7],src:src3(r[8]),srcFull:r[8],touched:r[9],upd:r[10]}));
tasks.forEach(t => { t.late = daysLate(t.due); t.overdue = t.late !== null && t.late > 0; });
const DONE = DATA.done.map(d => [PROJECTS[d[0]]?d[0]:0, d[1], d[2], +d[3]]);

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

// completed work: dim cloud per project, brighter when recent
const months = [...new Set(DONE.map(d=>d[1]))].sort();
const bright = m => .35 + .65*(months.indexOf(m)+1)/Math.max(1,months.length);
DONE.forEach(([p,m,s,c]) => {
  const P = PROJECTS[p], base = HUBDIR["p"+p], spread = Math.min(.95, .16 + .022*Math.sqrt(doneBy[p]||1)), b = bright(m);
  srcCount[s]=(srcCount[s]||0)+c;
  for(let i=0;i<c;i++){
    const n = addNode({pos:jitterDir(base,spread).multiplyScalar(R*(.95+rnd()*.05)), color:col(P.color), size:.9+rnd()*.5, alpha:.55*b, pulse:0});
    edges.push({a:n.pos, b:hubs["p"+p].pos, c:col(P.color), alpha:.045*b});
    if(hubs[s]) edges.push({a:n.pos, b:hubs[s].pos, c:col(P.color), alpha:.03*b});
  }
});
tasks.forEach(t => {
  srcCount[t.src]=(srcCount[t.src]||0)+1;
  const P = PROJECTS[t.p];
  const c = t.overdue ? col("#ff4d6d") : t.bic==="waiting" ? col("#fbbf24") : col(P.color);
  t.node = addNode({pos:jitterDir(HUBDIR["p"+t.p],.22).multiplyScalar(R*1.03), color:c, size:t.pri==="high"?3:2.4, alpha:1, pulse:t.overdue?1:0, pick:true, kind:"task", ref:t});
  edges.push({a:t.node.pos, b:hubs["p"+t.p].pos, c:col(P.color), alpha:.55});
  if(hubs[t.src]) edges.push({a:t.node.pos, b:hubs[t.src].pos, c, alpha:t.overdue?.4:.22});
});
const waitParties = {};
tasks.filter(t=>t.bic==="waiting" && t.wait).forEach(t => (waitParties[t.wait] ||= []).push(t));
Object.entries(waitParties).forEach(([name,list]) => {
  const n = addNode({pos:jitterDir(HUBDIR["p"+list[0].p],.45).multiplyScalar(R*1.06), color:col("#fbbf24"), size:2.8, alpha:1, pulse:.5, pick:true, kind:"waiting", ref:{name,list}, label:name});
  list.forEach(t => edges.push({a:n.pos, b:t.node.pos, c:col("#fbbf24"), alpha:.7}));
});
DATA.people.forEach(([name,emails,p]) => {
  if(p && !PROJECTS[p]) p = null;
  const first = name.split(" ")[0].replace(/[^\w'-]/g,"");
  const re = new RegExp("\\b"+first+"\\b","i");
  const mentions = first.length>2 ? tasks.filter(t => re.test(t.desc) || (t.wait && re.test(t.wait))) : [];
  const home = p ?? (mentions[0] ? mentions[0].p : (DATA.projects[0]||[0])[0]);
  const n = addNode({pos:jitterDir(HUBDIR["p"+home]||HUBDIR.p0,.55).multiplyScalar(R*1.08), color:col("#f8fafc"), size:1.4+Math.min(2.2,emails/80), alpha:.95, pulse:0, pick:true, kind:"person", ref:{name,emails,p,mentions}});
  if(p) edges.push({a:n.pos, b:hubs["p"+p].pos, c:col(PROJECTS[p].color), alpha:.35});
  mentions.forEach(t => edges.push({a:n.pos, b:t.node.pos, c:col("#f8fafc"), alpha:.3}));
  if(!p && !mentions.length) edges.push({a:n.pos, b:hubs.out.pos, c:col("#cbd5ff"), alpha:.12});
});
DATA.leads.forEach(([name,val,status]) => {
  const n = addNode({pos:jitterDir(HUBDIR.pipe,.35).multiplyScalar(R*1.04), color:col(status==="Won"?"#4ade80":"#fbbf24"), size:1.6+Math.min(3,val/80000), alpha:1, pulse:0, pick:true, kind:"lead", ref:{name,val,status}});
  edges.push({a:n.pos, b:hubs.pipe.pos, c:col("#fbbf24"), alpha:.5});
});
// reply drafts: outer haze (pending = gold, sent = green)
const mailTot = {}; DATA.mail.forEach(([s,c]) => mailTot[s]=(mailTot[s]||0)+(+c));
Object.entries(mailTot).forEach(([s,c]) => {
  const cc = s==="pending"?col("#fbbf24"):s==="sent"?col("#4ade80"):col("#5b6399"), a = s==="pending"?.75:s==="sent"?.9:.28;
  for(let i=0;i<c;i++) addNode({pos:V(rnd()*2-1,rnd()*2-1,rnd()*2-1).normalize().multiplyScalar(R*(1.12+rnd()*.22)), color:cc, size:s==="pending"?1.1:.7, alpha:a, pulse:s==="pending"?.25:0});
});
// unread inbox mail: cyan swarm orbiting the Outlook hub
const unread = INBOX && INBOX.unread != null ? INBOX.unread : null;
if(unread){
  const swarm = Math.min(unread, 400), spread = Math.min(.26, .08 + .012*Math.sqrt(swarm));
  for(let i=0;i<swarm;i++) addNode({pos:jitterDir(HUBDIR.out,spread).multiplyScalar(R*(1.04+rnd()*.12)), color:col("#67e8f9"), size:1.1+rnd()*.5, alpha:.9, pulse:.4});
}

// ---------- three.js ----------
const canvas = $("gl"), stage = $("stage");
const renderer = new THREE.WebGLRenderer({canvas, antialias:true, alpha:true});
renderer.setPixelRatio(Math.min(2, window.devicePixelRatio||1)); renderer.setClearColor(0x0c0a26, 0);
const scene = new THREE.Scene(), camera = new THREE.PerspectiveCamera(45,1,.1,200);
let camDist = 34, fitted = false; const world = new THREE.Group(); scene.add(world);
const SEG = 10, ePos = new Float32Array(edges.length*SEG*6), eCol = new Float32Array(edges.length*SEG*6), ctrl = V();
let k = 0;
edges.forEach(e => {
  ctrl.copy(e.a).add(e.b).multiplyScalar(.5).multiplyScalar(Math.max(.25, 1 - e.a.distanceTo(e.b)/(R*2.6)));
  const r=e.c.r*e.alpha, g=e.c.g*e.alpha, b=e.c.b*e.alpha;
  for(let i=0;i<SEG;i++) for(const t of [i/SEG,(i+1)/SEG]){ const u=1-t;
    ePos[k]=u*u*e.a.x+2*u*t*ctrl.x+t*t*e.b.x; ePos[k+1]=u*u*e.a.y+2*u*t*ctrl.y+t*t*e.b.y; ePos[k+2]=u*u*e.a.z+2*u*t*ctrl.z+t*t*e.b.z;
    eCol[k]=r; eCol[k+1]=g; eCol[k+2]=b; k+=3; }
});
const eGeo = new THREE.BufferGeometry();
eGeo.setAttribute("position", new THREE.BufferAttribute(ePos,3)); eGeo.setAttribute("color", new THREE.BufferAttribute(eCol,3));
world.add(new THREE.LineSegments(eGeo, new THREE.LineBasicMaterial({vertexColors:true, transparent:true, blending:THREE.AdditiveBlending, depthWrite:false})));
const N = nodes.length, A = n => new Float32Array(n);
const pPos=A(N*3), pCol=A(N*3), pSize=A(N), pAlpha=A(N), pPulse=A(N), pIdx=A(N);
nodes.forEach((n,i) => { pPos.set([n.pos.x,n.pos.y,n.pos.z],i*3); pCol.set([n.color.r,n.color.g,n.color.b],i*3); pSize[i]=n.size; pAlpha[i]=n.alpha; pPulse[i]=n.pulse; pIdx[i]=i; n.i=i; });
const pGeo = new THREE.BufferGeometry();
[["position",pPos,3],["color",pCol,3],["size",pSize,1],["alpha",pAlpha,1],["pulse",pPulse,1],["idx",pIdx,1]].forEach(([nm,arr,sz]) => pGeo.setAttribute(nm, new THREE.BufferAttribute(arr,sz)));
const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
const pMat = new THREE.ShaderMaterial({
  uniforms:{uTime:{value:0}, uScale:{value:1}, uSel:{value:-1}},
  vertexShader:`attribute float size; attribute vec3 color; attribute float alpha; attribute float pulse; attribute float idx;
    uniform float uTime; uniform float uScale; uniform float uSel; varying vec3 vC; varying float vA;
    void main(){ vec4 mv = modelViewMatrix*vec4(position,1.0);
      float p = 1.0 + pulse*0.45*sin(uTime*4.0 + position.x);
      float sel = (abs(idx - uSel) < 0.5) ? 2.2 : 1.0;
      gl_PointSize = size*p*sel*uScale*(260.0/-mv.z); vC = color; vA = alpha; gl_Position = projectionMatrix*mv; }`,
  fragmentShader:`varying vec3 vC; varying float vA;
    void main(){ float d = length(gl_PointCoord-0.5); if(d>0.5) discard;
      float a = pow(smoothstep(0.5,0.0,d),1.8) + 0.35*smoothstep(0.18,0.0,d);
      gl_FragColor = vec4(vC*(0.8+0.6*smoothstep(0.2,0.0,d)), a*vA); }`,
  transparent:true, depthWrite:false, blending:THREE.AdditiveBlending});
world.add(new THREE.Points(pGeo, pMat));
for(let lat=-60; lat<=60; lat+=30){ const r=R*Math.cos(lat*Math.PI/180), y=R*Math.sin(lat*Math.PI/180), pts=[];
  for(let i=0;i<=96;i++){ const a=i/96*Math.PI*2; pts.push(V(Math.cos(a)*r,y,Math.sin(a)*r)); }
  world.add(new THREE.Line(new THREE.BufferGeometry().setFromPoints(pts), new THREE.LineBasicMaterial({color:0x2a2470, transparent:true, opacity:.6}))); }

// labels
const labelled = nodes.filter(n => n.label);
labelled.forEach(n => { const el = document.createElement("div"); el.className="lab"; let extra="";
  if(n.kind==="project") extra = ` <small>${openBy[n.ref]||0} open</small>`;
  if(n.kind==="source") extra = ` <small>${(srcCount[n.ref]||0).toLocaleString()}</small>`;
  if(n.kind==="source" && n.ref==="out") extra += unread!=null ? ` <span class="unread">· ${unread.toLocaleString()} unread</span>` : ` <small>· unread n/a</small>`;
  if(n.kind==="waiting") extra = ` <small>waiting</small>`;
  el.innerHTML = esc(n.label)+extra;
  el.style.color = n.kind==="waiting" ? "#fbbf24" : n.kind==="source" ? (n.ref==="out"?"#67e8f9":"#cbd5ff") : n.kind==="pipeline" ? "#fbbf24" : "#"+n.color.getHexString();
  if(n.kind==="waiting") el.style.fontSize="9.5px";
  if(n.kind==="source" && n.ref==="out") { el.style.borderColor="rgba(103,232,249,.5)"; el.style.fontSize="11.5px"; }
  $("labels").appendChild(el); n.el = el; });

// interaction
let rotX=.25, rotY=0, auto=!reduceMotion, dragging=false, moved=0, last=null, idle=0, target=null, pinch0=null;
const pointers = new Map();
canvas.addEventListener("pointerdown", e => { canvas.setPointerCapture(e.pointerId); pointers.set(e.pointerId,{x:e.clientX,y:e.clientY}); dragging=true; moved=0; last={x:e.clientX,y:e.clientY}; auto=false; target=null; });
canvas.addEventListener("pointermove", e => { if(!pointers.has(e.pointerId)) return; pointers.set(e.pointerId,{x:e.clientX,y:e.clientY});
  if(pointers.size===2){ const [a,b]=[...pointers.values()], d=Math.hypot(a.x-b.x,a.y-b.y); if(pinch0) camDist=Math.min(60,Math.max(16,camDist*pinch0/d)); pinch0=d; moved+=10; return; }
  const dx=e.clientX-last.x, dy=e.clientY-last.y; moved+=Math.abs(dx)+Math.abs(dy); rotY+=dx*.006; rotX=Math.max(-1.4,Math.min(1.4,rotX+dy*.006)); last={x:e.clientX,y:e.clientY}; });
const endPtr = e => { pointers.delete(e.pointerId); if(pointers.size<2) pinch0=null; if(!pointers.size){ dragging=false; if(moved<6) pick(e.clientX,e.clientY); idle=performance.now(); } };
canvas.addEventListener("pointerup", endPtr); canvas.addEventListener("pointercancel", endPtr);
canvas.addEventListener("wheel", e => { e.preventDefault(); camDist=Math.min(60,Math.max(16,camDist*(1+e.deltaY*.001))); }, {passive:false});
const pickables = nodes.filter(n=>n.pick), wp = V();
function screenOf(n, rect){ wp.copy(n.pos).applyMatrix4(world.matrixWorld); const depth=wp.distanceTo(camera.position), z=wp.z; wp.project(camera);
  return {x:(wp.x+1)/2*rect.width, y:(1-wp.y)/2*rect.height, depth, z}; }
function pick(cx,cy){ const rect=canvas.getBoundingClientRect(); let best=null, bd=1e9;
  pickables.forEach(n => { const s=screenOf(n,rect), d=Math.hypot(s.x+rect.left-cx, s.y+rect.top-cy); if(d<16){ const sc=d+s.depth*.8; if(sc<bd){bd=sc;best=n;} } });
  best ? select(best) : closeCard(); }
function select(n){ pMat.uniforms.uSel.value=n.i; showCard(n); }
function closeCard(){ $("card").hidden=true; pMat.uniforms.uSel.value=-1; }
$("cardX").addEventListener("click", closeCard);
const lateText = t => t.late===null ? "No due date" : t.late>0 ? `${t.late} day${t.late>1?"s":""} past due` : t.late===0 ? "Due today" : `Due in ${-t.late} day${t.late<-1?"s":""}`;
const srcName = s => ({rowan_chat:"Rowan chat",outlook_flag:"Flagged in Outlook",email:"Email scan",dashboard:"Dashboard",meeting:"Meeting notes",telegram_task:"Telegram",telegram_wait:"Telegram",outlook:"Outlook",gmail:"Gmail",email_reply:"Email reply"}[s]||s||"Unknown");
function inboxHtml(){
  if(!INBOX) return `<dl><dt>Unread in inbox</dt><dd>Couldn't reach Outlook just now</dd></dl>`;
  return `<dl><dt>Unread in inbox</dt><dd style="color:var(--cyan)">${(INBOX.unread||0).toLocaleString()}</dd><dt>Total in inbox</dt><dd>${(INBOX.total||0).toLocaleString()}</dd></dl>`
    + (INBOX.recent && INBOX.recent.length ? `<ul>${INBOX.recent.map(m=>`<li><b>${esc(m[0])}</b> · ${esc(m[1])}</li>`).join("")}</ul>` : "")
    + `<p style="margin:8px 0 0"><a href="/emails">Open reply drafts →</a></p>`;
}
function showCard(n){ let h="";
  if(n.kind==="task"){ const t=n.ref;
    h=`<div class="k" style="color:${t.overdue?"var(--crit)":t.bic==="waiting"?"var(--warn)":"var(--accent)"}">Task #${t.id} · ${esc(t.pri)} priority${t.block?" · blocking":""}</div><h3>${esc(t.desc)}</h3>
    <dl><dt>Project</dt><dd>${esc(PROJECTS[t.p].name)}</dd><dt>Due</dt><dd>${t.due||"—"} · ${lateText(t)}</dd><dt>Ball in court</dt><dd>${t.bic==="waiting"?"Waiting on "+esc(t.wait||"someone"):"You"}</dd>
    <dt>Came from</dt><dd>${esc(srcName(t.srcFull))}</dd><dt>Last touched</dt><dd>${t.touched||"—"}</dd><dt>Progress notes</dt><dd>${t.upd||"None logged"}</dd></dl>
    <p style="margin:8px 0 0"><a href="/task/${t.id}?back=/sphere">Open task, log progress →</a></p>`;
  } else if(n.kind==="project"){ const p=n.ref, list=tasks.filter(t=>t.p===p).sort((a,b)=>(b.late??-999)-(a.late??-999));
    h=`<div class="k">Project</div><h3>${esc(PROJECTS[p].name)}</h3><dl><dt>Open</dt><dd>${openBy[p]||0}</dd><dt>Past due</dt><dd>${lateBy[p]||0}</dd><dt>Waiting on others</dt><dd>${list.filter(t=>t.bic==="waiting").length}</dd><dt>Completed</dt><dd>${(doneBy[p]||0).toLocaleString()}</dd></dl>`
      + (list.length?`<ul>${list.slice(0,6).map(t=>`<li>${esc(t.desc)} <span style="color:${t.overdue?"var(--crit)":"var(--muted)"}">· ${lateText(t)}</span></li>`).join("")}</ul>`:"")
      + (p?`<p style="margin:8px 0 0"><a href="/?project=${p}#register">Open this register →</a></p>`:"");
  } else if(n.kind==="source"){ h=`<div class="k" style="color:${n.ref==="out"?"var(--cyan)":"var(--warn)"}">Capture source</div><h3>${esc(SOURCES[n.ref].name)}</h3><dl><dt>Items captured</dt><dd>${(srcCount[n.ref]||0).toLocaleString()}</dd><dt>Still open</dt><dd>${tasks.filter(t=>t.src===n.ref).length}</dd></dl>` + (n.ref==="out" ? inboxHtml() : "");
  } else if(n.kind==="waiting"){ const {name,list}=n.ref; h=`<div class="k">Ball in their court</div><h3>${esc(name)}</h3><ul>${list.map(t=>`<li><a href="/task/${t.id}?back=/sphere">${esc(t.desc)}</a> · ${lateText(t)}</li>`).join("")}</ul>`;
  } else if(n.kind==="person"){ const r=n.ref; h=`<div class="k" style="color:#f8fafc">Contact</div><h3>${esc(r.name)}</h3><dl><dt>Emails scanned</dt><dd>${r.emails}</dd><dt>Most-linked project</dt><dd>${r.p?esc(PROJECTS[r.p].name):"None linked yet"}</dd><dt>Named in open tasks</dt><dd>${r.mentions.length}</dd></dl>`+(r.mentions.length?`<ul>${r.mentions.map(t=>`<li>${esc(t.desc)}</li>`).join("")}</ul>`:"");
  } else if(n.kind==="lead"){ const l=n.ref; h=`<div class="k">Lead · ${esc(l.status)}</div><h3>${esc(l.name)}</h3><dl><dt>Contract value</dt><dd>${fmt$(l.val)}</dd></dl><p style="margin:8px 0 0"><a href="/leads">Open leads →</a></p>`;
  } else if(n.kind==="pipeline"){ h=`<div class="k">Lead pipeline</div><h3>${DATA.leads.length} leads · ${fmt$(DATA.leads.reduce((s,l)=>s+l[1],0))}</h3><ul>${DATA.leads.map(l=>`<li>${esc(l[0])} · ${fmt$(l[1])} · ${esc(l[2])}</li>`).join("")}</ul>`; }
  $("cardBody").innerHTML=h; $("card").hidden=false; }

// side panels
const openCt=tasks.length, lateCt=tasks.filter(t=>t.overdue).length, waitCt=tasks.filter(t=>t.bic==="waiting").length;
$("kOpen").textContent=openCt; $("kLate").textContent=lateCt; $("kWait").textContent=waitCt; $("kDraft").textContent=mailTot.pending||0;
$("hOpen").textContent=openCt; $("hLate").textContent=lateCt; $("hNodes").textContent=N.toLocaleString(); $("hEdges").textContent=edges.length.toLocaleString();
if(unread!=null){ $("kUnread").textContent=unread.toLocaleString(); $("hUnread").textContent=unread.toLocaleString(); $("kUnreadL").textContent=`Unread in Outlook inbox · ${(INBOX.total||0).toLocaleString()} total`; }
else { $("kUnreadL").textContent="Unread in Outlook · couldn't reach Outlook"; }
$("kUnreadTile").addEventListener("click", () => { focusOn(hubs.out); select(hubs.out); });
const maxOpen = Math.max(1, ...Object.values(openBy));
$("byProject").innerHTML = Object.keys(PROJECTS).filter(k=>openBy[k]).sort((a,b)=>openBy[b]-openBy[a]).map(k => { const o=openBy[k], l=lateBy[k]||0;
  return `<button class="row" data-hub="p${k}"><span style="color:${PROJECTS[k].color}">${esc(PROJECTS[k].short)}</span><span class="bar"><b style="width:${l/maxOpen*100}%;background:var(--crit)"></b><b style="width:${(o-l)/maxOpen*100}%;background:${PROJECTS[k].color};opacity:.7"></b></span><span class="n">${o}</span></button>`; }).join("") || `<p class="note">Nothing open.</p>`;
const late = tasks.filter(t=>t.overdue).sort((a,b)=>b.late-a.late);
$("lateCount").textContent = late.length;
$("lateList").innerHTML = late.slice(0,8).map(t => `<button class="item" data-task="${t.id}"><i style="background:${PROJECTS[t.p].color}"></i><span class="d">${esc(t.desc)}<small>${esc(PROJECTS[t.p].short)}</small></span><span class="late">${t.late}d</span></button>`).join("") || `<p class="note">Nothing past due.</p>`;
$("waitList").innerHTML = Object.entries(waitParties).map(([name,list]) => { const worst=Math.max(...list.map(t=>t.late??-999));
  return `<button class="item" data-wait="${esc(name)}"><i style="background:var(--warn)"></i><span class="d">${esc(name)}<small>${list.length} item${list.length>1?"s":""} · ${esc(PROJECTS[list[0].p].short)}</small></span><span class="late ${worst>0?"":"w"}">${worst>0?worst+"d late":"on time"}</span></button>`; }).join("") || `<p class="note">Nobody owes you anything right now.</p>`;
$("pipeTotal").textContent = fmt$(DATA.leads.reduce((s,l)=>s+l[1],0));
$("leadList").innerHTML = DATA.leads.map(l => `<div class="item" style="cursor:default"><i style="background:${l[2]==="Won"?"var(--ok)":"var(--warn)"}"></i><span class="d">${esc(l[0])}<small>${esc(l[2])}</small></span><span class="late" style="color:var(--ink)">${fmt$(l[1])}</span></div>`).join("");
(function(){ const ms=DATA.months.map(m=>m[0]), vals=DATA.months.map(m=>+m[1]); if(!vals.length) return; const mx=Math.max(...vals);
  const W=320,H=120,pl=8,pr=8,pt=14,pb=18,bw=(W-pl-pr)/ms.length; let s="";
  [.5,1].forEach(f => { const y=pt+(H-pt-pb)*(1-f); s+=`<line x1="${pl}" x2="${W-pr}" y1="${y}" y2="${y}" stroke="#352F78"/>`; });
  vals.forEach((v,i) => { const h=(H-pt-pb)*v/mx, x=pl+i*bw+4, y=H-pb-h, lastBar=i===vals.length-1;
    s+=`<rect x="${x}" y="${y}" width="${bw-8}" height="${Math.max(1,h)}" rx="2" fill="${lastBar?"#B45CF0":"#4a4394"}"/><text x="${x+(bw-8)/2}" y="${H-5}" text-anchor="middle">${monName(ms[i])}</text><text x="${x+(bw-8)/2}" y="${y-3}" text-anchor="middle" style="fill:#E6E3FF">${v>=1000?(v/1000).toFixed(1)+"k":v}</text>`; });
  $("spark").innerHTML=s; })();
document.querySelector("#sph .rail").addEventListener("click", e => { const b=e.target.closest("[data-hub],[data-task],[data-wait]"); if(!b) return; let n=null;
  if(b.dataset.hub) n=hubs[b.dataset.hub]; if(b.dataset.task) n=tasks.find(t=>t.id==b.dataset.task).node; if(b.dataset.wait) n=nodes.find(x=>x.kind==="waiting"&&x.ref.name===b.dataset.wait);
  if(n){ focusOn(n); select(n); if(window.innerWidth<=900) stage.scrollIntoView({behavior:"smooth"}); } });
function focusOn(n){ const d=n.pos.clone().normalize(); target={y:Math.atan2(-d.x,d.z), x:Math.asin(d.y)}; auto=false; }
$("legend").innerHTML = Object.keys(PROJECTS).filter(k=>k!=="0").map(k=>`<span><i style="background:${PROJECTS[k].color}"></i>${esc(PROJECTS[k].short)}</span>`).join("")
  + `<span><i style="background:#ff4d6d"></i>Past due</span><span><i style="background:#fbbf24"></i>Waiting / pending draft</span><span><i style="background:#67e8f9"></i>Unread email</span><span><i style="background:#f8fafc"></i>Contact</span>`;

function resize(){ const r=stage.getBoundingClientRect(); renderer.setSize(r.width,r.height,false); camera.aspect=r.width/r.height; camera.updateProjectionMatrix(); pMat.uniforms.uScale.value=Math.max(.6,Math.min(1.2,Math.min(r.height,r.width*1.3)/700));
  if(!fitted && r.width>0){ camDist = camera.aspect<1 ? Math.min(60, 31/camera.aspect) : 34; fitted = true; } }
new ResizeObserver(resize).observe(stage); resize();
const clock = new THREE.Clock();
function frame(){ const t=clock.getElapsedTime();
  if(!dragging && !auto && !target && !reduceMotion && performance.now()-idle>6000 && $("card").hidden) auto=true;
  if(auto) rotY+=.0018;
  if(target){ const wrap=a=>Math.atan2(Math.sin(a),Math.cos(a)), dy=wrap(target.y-rotY); rotY+=dy*.08; rotX+=(target.x-rotX)*.08; if(Math.abs(dy)<.002&&Math.abs(target.x-rotX)<.002) target=null; }
  world.rotation.set(rotX,rotY,0,"XYZ"); camera.position.set(0,0,camDist); camera.lookAt(0,0,0);
  pMat.uniforms.uTime.value = reduceMotion?0:t; world.updateMatrixWorld();
  const rect = canvas.getBoundingClientRect();
  labelled.forEach(n => { const s=screenOf(n,rect); n.el.style.left=s.x+"px"; n.el.style.top=s.y+"px"; n.el.style.opacity = s.z>-2 ? (n.kind==="waiting"?.85:1) : .12; });
  $("hSpin").textContent=(((rotY*180/Math.PI)%360+360)%360).toFixed(1)+"°";
  renderer.render(scene,camera); requestAnimationFrame(frame); }
frame();
// refresh the data every 10 minutes unless a detail card is open
setInterval(() => { if($("card").hidden && !dragging) location.reload(); }, 600000);
})();
</script>
"""
