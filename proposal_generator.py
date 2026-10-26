"""MCM proposal generator.

Fills Miami Coastline Management's Word proposal template (letterhead + logo kept
as-is) from a short spec: proposal letter, Attachment A (scope), Attachment B
(fee table with computed totals) and Attachment C (MCM standard terms).

Usage (CLI):  python proposal_generator.py template.docx spec.json out.docx
Usage (code): docx_bytes, summary = build_proposal(template_bytes, spec)
"""
import copy
import datetime
import io
import json
import re
import sys

from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn

# ---------------------------------------------------------------------------
# Standard content
# ---------------------------------------------------------------------------

TERMS_DEFAULTS = {
    "liability_cap": "the total fees paid to the Consultant under this Agreement",
    "principal_rate": 250,
    "pm_rate": 150,
    "escalation_pct": 3,
    "retainer_months": 1,
    "late_interest_pct": 1.5,
    "reimbursable_markup_pct": 10,
    "placement_fee_pct": 50,
    "convenience_notice_days": 30,
}

GENERAL_SCOPE = [
    "Through all stages report regularly to the client on the status of the project and obtain decisions needed.",
    "In conjunction with other consultants, initiate action if any aspect of the project appears likely to fail to "
    "achieve the client’s objectives, public obligations and program. Agree suitable corrective action and monitor "
    "its implementation.",
    "Advise the client on the statutory approvals and notifications required throughout the execution of the project.",
    "In conjunction with other consultants, coordinate quality assurance policies, plans and responsibilities for "
    "the project. Jobsite safety remains the responsibility of the contractor.",
    "Establish a regular meetings structure, specifying who shall attend and their responsibilities.",
    "Attend Client, Design, Project, and other meetings as provided under this appointment.",
    "Prepare regular / monthly reports.",
    "Advise Client of any decisions required and obtain authorization.",
]

_SMALL = {1: "one", 2: "two", 3: "three", 5: "five", 7: "seven", 10: "ten", 12: "twelve", 14: "fourteen",
          15: "fifteen", 21: "twenty-one", 30: "thirty", 45: "forty-five", 60: "sixty", 90: "ninety"}


def _days(n):
    return f"{_SMALL[n]} ({n})" if n in _SMALL else f"{n}"


def _pct(x):
    return f"{x:g}"


def terms_sections(v):
    """MCM standard Attachment C. Each section: (heading, [paragraphs]);
    a paragraph that is a list becomes (a), (b), ... sub-items."""
    retainer = v["retainer_months"]
    retainer_txt = "one month’s fee" if retainer == 1 else f"{_SMALL.get(retainer, retainer)} months’ fees"
    return [
        ("1. Standard of Care", [
            "The Consultant will perform its services with the reasonable skill and care that other consultants "
            "providing similar owner’s representative services in the same area would use on a comparable project "
            "under similar conditions (the “Standard of Care”). No other warranty or guarantee, express or implied, "
            "is made, including any guarantee of project cost, schedule, or outcome."]),
        ("2. Role of the Consultant", [
            "The Consultant acts as the Owner’s advisor and representative. The Owner retains sole authority for all "
            "project decisions, approvals, and contracts. The Consultant has no authority to bind the Owner, execute "
            "contracts, or authorize payments on the Owner’s behalf unless the Owner grants that authority in "
            "writing. The Consultant is an independent contractor."]),
        ("3. Excluded Services", [
            "The Consultant is not a design professional and does not provide architectural, engineering, or other "
            "design services. The Consultant does not control, and is not responsible for, construction means, "
            "methods, techniques, sequences, procedures, jobsite safety, or the identification, handling, or removal "
            "of hazardous materials. The Consultant is not responsible for the work, errors, omissions, delays, or "
            "defaults of the Owner’s architects, engineers, contractors, or other consultants, or for their failure "
            "to perform in accordance with their contracts or the law. The Consultant will promptly notify the Owner "
            "of any non-compliance it actually becomes aware of."]),
        ("4. Reliance", [
            "The Consultant may rely on the information, drawings, specifications, budgets, schedules, and "
            "instructions provided by the Owner and the Owner’s consultants and contractors, and is not responsible "
            "for verifying their accuracy or completeness."]),
        ("5. Additional Services", [
            "Services not described in Attachment A, including services required by changes in the project’s "
            "scope, size, or delivery method, are Additional Services. The Consultant will perform Additional "
            "Services only after the Owner authorizes them in writing (email is sufficient). Additional Services "
            "will be billed at the following hourly rates unless a fixed fee is agreed: Principal "
            f"${v['principal_rate']:,} per hour; Project Manager ${v['pm_rate']:,} per hour."]),
        ("6. Schedule Extension", [
            "The fee in Attachment B is based on the number of months stated there. If the project extends beyond "
            "that period for reasons not caused by the Consultant, the Consultant’s services and monthly fee will "
            "continue at the then-current monthly rate until the services are complete or the Agreement is "
            "terminated."]),
        ("7. Fees, Payment, and Retainer", [[
            "The Consultant submits invoices within the first week of each month, and the Owner pays on or before "
            "the 30th of that same month.",
            f"On execution of this Agreement, the Owner will pay a retainer equal to {retainer_txt}, which the "
            "Consultant will apply to its final invoice.",
            f"Monthly fees will increase by {_pct(v['escalation_pct'])}% on each anniversary of the date of this "
            "Agreement.",
            "Reimbursable expenses approved in advance by the Owner will be billed at cost plus "
            f"{_pct(v['reimbursable_markup_pct'])}%.",
            f"Balances not paid when due accrue interest at {_pct(v['late_interest_pct'])}% per month, or the "
            "maximum rate allowed by law if lower, from the day after the due date until paid.",
            "If the Owner fails to pay when due, the Consultant may suspend services on seven (7) days’ written "
            "notice. The Consultant has no liability for delay or damage caused by a suspension. Before resuming, "
            "the Consultant must be paid all amounts due plus its reasonable costs of suspending and resuming "
            "services.",
        ]]),
        ("8. Limitation of Liability", [[
            "To the fullest extent permitted by law, the Consultant’s total liability to the Owner for all claims, "
            "losses, damages, and expenses arising out of or relating to this Agreement or the project, whether in "
            f"contract, tort, or otherwise, and including any indemnity obligation, will not exceed "
            f"{v['liability_cap']}.",
            "The Owner and the Consultant each waive all claims against the other for consequential, indirect, "
            "incidental, special, and punitive damages, including lost profits, lost rent, and loss of use, arising "
            "out of or relating to this Agreement, including its termination.",
            "No member, manager, officer, or employee of the Consultant will have any personal liability to the "
            "Owner in connection with this Agreement or the project. The Owner will look solely to the Consultant "
            "entity for any claim.",
        ]]),
        ("9. Indemnity", [[
            "Consultant’s indemnity. To the fullest extent permitted by law, and subject to Section 8, the "
            "Consultant will indemnify and hold harmless the Owner and its members, officers, and employees from "
            "damages, losses, and expenses, including reasonable attorneys’ fees, to the extent caused by the "
            "negligence or willful misconduct of the Consultant in performing its services.",
            "Owner’s indemnity. To the fullest extent permitted by law, the Owner will indemnify and hold harmless "
            "the Consultant and its members, managers, officers, and employees from damages, losses, and expenses, "
            "including reasonable attorneys’ fees and the cost of enforcing this section, to the extent caused by "
            "the negligence or willful misconduct of the Owner or its contractors, architects, engineers, or other "
            "consultants of any tier (other than the Consultant), or by site conditions or hazardous materials at "
            "the project.",
            "This section survives termination of the Agreement.",
        ]]),
        ("10. Insurance", [
            "The Consultant will maintain Commercial General Liability and Professional Liability insurance for the "
            "duration of this Agreement. The Consultant will name the Owner as an additional insured on its "
            "Commercial General Liability policy for third-party bodily injury, property damage, or wrongful death "
            "caused by the Consultant’s negligent acts or omissions, and will provide a certificate of insurance on "
            "execution and on each renewal. The Consultant is exempt from workers’ compensation insurance "
            "requirements."]),
        ("11. Notice and Cure", [
            "If the Owner believes the Consultant has materially breached this Agreement, the Owner will give the "
            "Consultant written notice describing the facts within ten (10) days of learning of the breach, and the "
            "parties will negotiate in good faith to allow the Consultant to cure it."]),
        ("12. Termination", [[
            "Either party may terminate this Agreement on seven (7) days’ written notice if the other party "
            "substantially fails to perform through no fault of the terminating party and does not cure the failure "
            "within that period.",
            "Either party may terminate this Agreement for convenience, without cause, on "
            f"{_days(v['convenience_notice_days'])} days’ written notice to the other. The Consultant will continue "
            "to perform its services during the notice period and will be paid its monthly fee through the end of "
            "the notice period.",
            "On any termination, the Consultant will be paid for all services performed and reimbursable expenses "
            "incurred through the termination date. If the Owner terminates for convenience, or the Consultant "
            "terminates for cause, the Consultant will also be paid its Termination Expenses: reasonable costs "
            "directly attributable to the termination.",
            "If the Consultant terminates for convenience, it will cooperate in an orderly transition of its "
            "services to the Owner or the Owner’s designee during the notice period, and will deliver the "
            "project-specific deliverables described in Section 13 once paid in full.",
        ]]),
        ("13. Work Product and Records", [[
            "Once the Consultant has been paid in full, the Owner may use the project-specific deliverables the "
            "Consultant prepared (such as budgets, schedules, meeting minutes, and reports) for the project. Use of "
            "deliverables for any other purpose, or after modification by others, is at the Owner’s sole risk.",
            "The Consultant retains all rights to its pre-existing and independently developed templates, forms, "
            "software, tools, processes, and know-how, including those used to prepare deliverables. Nothing in "
            "this Agreement transfers those rights to the Owner.",
            "The Consultant will keep records in enough detail to manage its services. During the term, the Owner "
            "may inspect and copy project billing records on reasonable notice. On the Owner’s written request at "
            "final completion, and subject to payment in full, the Consultant will provide an electronic copy of the "
            "project records at no additional charge.",
        ]]),
        ("14. Non-Solicitation", [
            "During the term of this Agreement and for twelve (12) months afterward, the Owner will not directly or "
            "indirectly solicit or hire any person employed or engaged by the Consultant to provide services of the "
            "type described in this Agreement. If the Owner does, the Owner will pay the Consultant a placement fee "
            f"equal to {_pct(v['placement_fee_pct'])}% of that person’s first-year compensation."]),
        ("15. Force Majeure", [
            "Neither party is liable for delay or failure to perform caused by events beyond its reasonable control, "
            "including acts of God, hurricanes and other severe weather, epidemics, government action, and labor "
            "disputes. Payment obligations are not excused by this section."]),
        ("16. Disputes", [
            "This Agreement is governed by Florida law. The parties will first try to resolve any dispute through "
            "mediation in Miami-Dade County, Florida, before filing suit. Any lawsuit will be brought exclusively in "
            "the state or federal courts located in Miami-Dade County. The prevailing party is entitled to recover "
            "its reasonable attorneys’ fees and costs, including on appeal. EACH PARTY WAIVES ITS RIGHT TO A JURY "
            "TRIAL IN ANY ACTION ARISING OUT OF OR RELATING TO THIS AGREEMENT."]),
        ("17. General", [
            "Neither party may assign this Agreement without the other’s written consent, except that the "
            "Consultant may assign its right to receive payment. This Agreement, including its attachments, is the "
            "entire agreement between the parties and may be amended only in writing. If documents conflict, these "
            "Terms and Conditions control over the proposal letter and Attachments A and B. If any provision is "
            "found unenforceable, the rest of the Agreement remains in effect. This Agreement may be signed "
            "electronically and in counterparts."]),
    ]


# ---------------------------------------------------------------------------
# Attachment A (plain-text, editable per proposal)
# ---------------------------------------------------------------------------
#
# Format (works in TextEdit, Telegram or chat):
#   ## HEADING                 section heading
#   - item                     bullet
#     - sub-item               indented bullet (2+ leading spaces)
#   any other line             plain paragraph
#   **bold text**              bold inside any line
#   {role}                     replaced with the client role (Owner, HOA, ...)


def default_attachment_a(role="Owner", project_scope="Owner’s Representative oversight of the "
                         "Pre-Construction and Construction phases of the project.", stages=None):
    lines = ["## PROJECT SPECIFIC SCOPE OF WORK", f"- **On behalf of the {role}:** {project_scope}", "",
             "## GENERAL"]
    lines += [f"- {item}" for item in GENERAL_SCOPE]
    if stages:
        lines += ["", "## PROJECT STAGES", "The project development will be covered within the following stages:"]
        lines += [f"- {item}" for item in stages]
    return "\n".join(lines)


def parse_attachment_a(text):
    out = []
    for raw in text.splitlines():
        if not raw.strip():
            continue
        stripped = raw.lstrip()
        indent = len(raw) - len(stripped)
        if stripped.startswith("#"):
            out.append(("heading", stripped.lstrip("#").strip()))
        elif stripped[:2] in ("- ", "* ", "• "):
            out.append(("subitem" if indent >= 2 else "item", stripped[2:].strip()))
        else:
            out.append(("para", stripped))
    if not out:
        raise ValueError("Attachment A is empty")
    return out


def _inline_bold(text):
    parts = re.split(r"(\*\*[^*]+\*\*)", text)
    runs = []
    for part in parts:
        if not part:
            continue
        if part.startswith("**") and part.endswith("**"):
            runs.append((part[2:-2], True))
        else:
            runs.append((part, False))
    return runs


def _indent(p, twips=720):
    """Push a bullet one level in (sub-item)."""
    ppr = p.find(qn("w:pPr"))
    ilvl = ppr.find(".//" + qn("w:ilvl")) if ppr is not None else None
    if ilvl is not None:
        ilvl.set(qn("w:val"), "1")
    else:
        ind = ppr.find(qn("w:ind")) if ppr is not None else None
        if ind is None and ppr is not None:
            ind = OxmlElement("w:ind")
            ppr.append(ind)
        if ind is not None:
            left = int(ind.get(qn("w:left")) or 0)
            ind.set(qn("w:left"), str(left + twips))
    return p


WAIVER_558 = ("18. THE OWNER AND CONSULTANT AGREE TO WAIVE ALL REQUIREMENTS UNDER CHAPTER 558, FLORIDA "
              "STATUTES, RELATIVE TO THE SERVICES PROVIDED UNDER THIS AGREEMENT.")

# ---------------------------------------------------------------------------
# Spec handling and fee math
# ---------------------------------------------------------------------------


def _money(x):
    return f"${x:,.0f}"


def compute_fees(phases):
    rows, total_months, total_fee = [], 0, 0
    for i, ph in enumerate(phases):
        months = ph["months"]
        fee = ph["monthly_fee"]
        if not isinstance(months, (int, float)) or months <= 0:
            raise ValueError(f"Phase {i + 1}: months must be a positive number")
        if not isinstance(fee, (int, float)) or fee <= 0:
            raise ValueError(f"Phase {i + 1}: monthly_fee must be a positive number")
        line = months * fee
        rows.append({**ph, "line_total": line})
        total_months += months
        total_fee += line
    blended = total_fee / total_months
    distinct = len({ph["monthly_fee"] for ph in phases}) > 1
    return {"rows": rows, "total_months": total_months, "total_fee": total_fee,
            "blended_monthly": blended, "mixed_rates": distinct}


def _date_parts(d):
    if isinstance(d, str):
        d = datetime.date.fromisoformat(d)
    day = d.day
    suffix = "th" if 11 <= day % 100 <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")
    return f"{d.strftime('%B')} {day}", suffix, f", {d.year}"


def proposal_filename(spec):
    name = f"Miami Coastline - {spec['project_name']} - Proposal"
    if spec.get("revision"):
        name += f" - Rev.{spec['revision']}"
    return name + ".docx"

# ---------------------------------------------------------------------------
# Low-level XML helpers
# ---------------------------------------------------------------------------


_RPR_ORDER = ["rStyle", "rFonts", "b", "bCs", "i", "iCs", "caps", "smallCaps", "strike", "dstrike", "outline",
              "shadow", "emboss", "imprint", "noProof", "snapToGrid", "vanish", "webHidden", "color", "spacing", "w",
              "kern", "position", "sz", "szCs", "highlight", "u", "effect", "bdr", "shd", "fitText", "vertAlign",
              "rtl", "cs", "em", "lang", "eastAsianLayout", "specVanish", "oMath"]


def _set_rpr(rpr, tag, el):
    """Insert/replace child `tag` in rPr respecting schema order (el=None removes it)."""
    old = rpr.find(qn("w:" + tag))
    if old is not None:
        rpr.remove(old)
    if el is None:
        return
    idx = _RPR_ORDER.index(tag)
    for pos, child in enumerate(rpr):
        name = child.tag.split("}")[1]
        if name in _RPR_ORDER and _RPR_ORDER.index(name) > idx:
            rpr.insert(pos, el)
            return
    rpr.append(el)


def _make_run(text, rpr_src, bold=None, sup=False):
    r = OxmlElement("w:r")
    rpr = copy.deepcopy(rpr_src) if rpr_src is not None else OxmlElement("w:rPr")
    if bold is not None:
        _set_rpr(rpr, "b", OxmlElement("w:b") if bold else None)
        _set_rpr(rpr, "bCs", OxmlElement("w:bCs") if bold else None)
    if sup:
        va = OxmlElement("w:vertAlign")
        va.set(qn("w:val"), "superscript")
        _set_rpr(rpr, "vertAlign", va)
    else:
        _set_rpr(rpr, "vertAlign", None)
    if len(rpr):
        r.append(rpr)
    t = OxmlElement("w:t")
    t.text = text
    t.set(qn("xml:space"), "preserve")
    r.append(t)
    return r


def _first_rpr(el):
    for r in el.iter(qn("w:r")):
        if r.find(qn("w:t")) is not None:
            return r.find(qn("w:rPr"))
    return None


def _para(proto, runs):
    """Clone paragraph `proto` (keeping its paragraph formatting and first-run font)
    and fill it with runs: list of (text, bold, superscript) or plain strings."""
    new = copy.deepcopy(proto)
    rpr_src = _first_rpr(proto)
    for child in list(new):
        if child.tag != qn("w:pPr"):
            new.remove(child)
    for run in runs:
        if isinstance(run, str):
            run = (run, None, False)
        text, bold, sup = (list(run) + [None, False])[:3]
        new.append(_make_run(text, rpr_src, bold, sup))
    return new


def _keep_next(p):
    """Keep a heading on the same page as the paragraph after it."""
    ppr = p.find(qn("w:pPr"))
    if ppr is None:
        ppr = OxmlElement("w:pPr")
        p.insert(0, ppr)
    if ppr.find(qn("w:keepNext")) is None:
        kn = OxmlElement("w:keepNext")
        anchor = ppr.find(qn("w:pStyle"))
        if anchor is not None:
            anchor.addnext(kn)
        else:
            ppr.insert(0, kn)
    return p


def _text(el):
    return "".join(t.text or "" for t in el.iter(qn("w:t"))).strip()


def _has_num(el):
    return el.find(".//" + qn("w:numPr")) is not None


def _set_cell(tc, text, rpr_src, bold):
    p = tc.find(qn("w:p"))
    for child in list(p):
        if child.tag != qn("w:pPr"):
            p.remove(child)
    if text:
        p.append(_make_run(text, rpr_src, bold))

# ---------------------------------------------------------------------------
# Template parsing
# ---------------------------------------------------------------------------

_MONTH_RE = re.compile(r"^(January|February|March|April|May|June|July|August|September|October|November|December)"
                       r"\s+\d")


class TemplateError(Exception):
    pass


def _find(children, pred, start=0, what="element"):
    for i in range(start, len(children)):
        if pred(children[i]):
            return i
    raise TemplateError(f"Template is missing the {what}")


def _parse_template(doc):
    body = doc.element.body
    ch = list(body.iterchildren())
    P, TBL = qn("w:p"), qn("w:tbl")
    is_p = lambda e: e.tag == P
    t = {}
    i_date = _find(ch, lambda e: is_p(e) and _MONTH_RE.match(_text(e)), what="proposal date line")
    t["date"] = ch[i_date]
    i_client = _find(ch, lambda e: is_p(e) and _text(e), i_date + 1, "client address line")
    t["client"] = ch[i_client]
    i_re = _find(ch, lambda e: is_p(e) and _text(e).startswith("Re:"), i_date, "'Re:' line")
    t["re"] = ch[i_re]
    i_dear = _find(ch, lambda e: is_p(e) and _text(e).startswith("Dear"), i_re, "salutation")
    t["dear"] = ch[i_dear]
    t["body"] = ch[_find(ch, lambda e: is_p(e) and _text(e), i_dear + 1, "letter body")]
    i_acc = _find(ch, lambda e: is_p(e) and _text(e).startswith("Acceptance of this Agreement"), i_dear,
                  "'Acceptance of this Agreement' heading")
    t["acc_head"] = ch[i_acc]
    t["doc_list"] = ch[_find(ch, lambda e: is_p(e) and _has_num(e), i_acc, "numbered document list")]
    t["sig_table"] = ch[_find(ch, lambda e: e.tag == TBL, i_acc, "signature table")]
    i_brk = _find(ch, lambda e: is_p(e) and 'w:type="page"' in e.xml if hasattr(e, "xml") else False, i_acc,
                  "page break")
    t["page_break"] = ch[i_brk]
    i_att = _find(ch, lambda e: is_p(e) and _text(e).upper() == "ATTACHMENT A", i_brk,
                  "'ATTACHMENT A' title")
    t["att_title"] = ch[i_att]
    t["att_sub"] = ch[_find(ch, lambda e: is_p(e) and _text(e), i_att + 1, "attachment subtitle")]
    i_sec = _find(ch, lambda e: is_p(e) and "PROJECT SPECIFIC" in _text(e).upper(), i_att, "scope section heading")
    t["sec_head"] = ch[i_sec]
    t["bullet"] = ch[_find(ch, lambda e: is_p(e) and _has_num(e), i_sec, "scope bullet")]
    t["fee_table"] = ch[_find(ch, lambda e: e.tag == TBL and "MONTHLY FEE" in _text(e).upper(), i_sec, "fee table")]
    i_tc = _find(ch, lambda e: is_p(e) and _text(e).upper() == "TERMS AND CONDITIONS", i_att, "terms title")
    i_th = _find(ch, lambda e: is_p(e) and _text(e), i_tc + 1, "terms heading")
    t["terms_head"] = ch[i_th]
    t["terms_body"] = ch[_find(ch, lambda e: is_p(e) and _text(e), i_th + 1, "terms paragraph")]
    t["caps"] = ch[_find(ch, lambda e: is_p(e) and _text(e).startswith("THE OWNER AND CONSULTANT"), i_th,
                         "Chapter 558 waiver")]
    t["_body"], t["_children"], t["_start"] = body, ch, i_date
    return t

# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------


def _fee_table(proto, fees, show_blended):
    tbl = copy.deepcopy(proto)
    rows = tbl.findall(qn("w:tr"))
    header, data_proto, total_proto = rows[0], rows[1], rows[-1]
    rpr_src = _first_rpr(data_proto)
    for r in rows[1:]:
        tbl.remove(r)
    for i, row in enumerate(fees["rows"]):
        tr = copy.deepcopy(data_proto)
        cells = tr.findall(qn("w:tc"))
        vals = [str(row.get("phase", i + 1)), row["description"], f"{row['months']:g}",
                _money(row["monthly_fee"]), _money(row["line_total"])]
        for tc, v in zip(cells, vals):
            _set_cell(tc, v, rpr_src, None)
        tbl.append(tr)
    tr = copy.deepcopy(total_proto)
    cells = tr.findall(qn("w:tc"))
    vals = ["TOTAL", "", f"{fees['total_months']:g}",
            _money(fees["blended_monthly"]) if show_blended else "", _money(fees["total_fee"])]
    for tc, v in zip(cells, vals):
        _set_cell(tc, v, rpr_src, True)
    tbl.append(tr)
    return tbl


def build_proposal(template_bytes, spec):
    """Return (docx_bytes, summary) for a proposal spec. See example_spec() for fields."""
    for key in ("project_name", "client_lines", "salutation_name", "project_address", "owner_signature_name",
                "attachment_a", "phases"):
        if not spec.get(key):
            raise ValueError(f"Spec is missing '{key}'")
    terms = {**TERMS_DEFAULTS, **spec.get("terms", {})}
    role = spec.get("client_role", "Owner")
    spec = {**spec, "attachment_a": spec["attachment_a"].replace("{role}", role)}
    fees = compute_fees(spec["phases"])
    show_blended = spec.get("show_blended_rate", fees["mixed_rates"])
    role = spec.get("client_role", "Owner")
    date = spec.get("date") or datetime.date.today().isoformat()
    d1, dsuf, d2 = _date_parts(date)

    doc = Document(io.BytesIO(template_bytes))
    t = _parse_template(doc)
    body, ch = t["_body"], t["_children"]
    sect = ch[-1] if ch[-1].tag == qn("w:sectPr") else None
    for el in ch[t["_start"]:]:
        if el is not sect:
            body.remove(el)

    out = []
    add = out.append
    # --- Letter
    add(_para(t["date"], [(d1, True), (dsuf, True, True), (d2, True)]))
    for line in spec["client_lines"]:
        add(_para(t["client"], [(line, True)]))
    add(_para(t["re"], [(f"Re: {spec.get('re_line', spec['project_name'] + ' – Owner’s Representative Services')}",
                         True)]))
    add(_para(t["dear"], [f"Dear {spec['salutation_name']},"]))
    services_for = spec.get("services_for", "the project")
    add(_para(t["body"], ["Miami Coastline Management (“Consultant”) is pleased to submit this proposal to provide "
                          f"Owner’s Representative services for {services_for} located at "
                          f"{spec['project_address']}."]))
    add(_para(t["body"], [f"As outlined herein, Miami Coastline will represent the {role}, and work on behalf of "
                          "their interests in accordance with the terms of this Agreement. The scope of services to "
                          "be performed under Base Services is further detailed in ", ("Attachment A", True), "."]))
    add(_para(t["body"], [f"We are enthusiastic about the opportunity to support the {role} in the successful "
                          "execution of the project and look forward to collaborating closely with your team."]))
    add(_para(t["acc_head"], [("Acceptance of this Agreement", True)]))
    add(_para(t["body"], ["This Agreement shall consist of and incorporate the following documents:"]))
    add(_para(t["doc_list"], ["This proposal letter, dated ", d1, (dsuf, None, True), d2]))
    for name, desc in (("Attachment A", "Scope of Services"), ("Attachment B", "Fee Proposal"),
                       ("Attachment C", "Terms and Conditions")):
        add(_para(t["doc_list"], [(name, True), f" – {desc}"]))
    add(_para(t["body"], ["By execution below, the parties agree to the terms of this Agreement as of the date "
                          "first written above."]))
    sig = copy.deepcopy(t["sig_table"])
    row1 = sig.findall(qn("w:tr"))[1]
    c0 = row1.findall(qn("w:tc"))[0]
    _set_cell(c0, spec["owner_signature_name"], _first_rpr(c0), None)
    add(sig)
    # --- Attachment A
    add(copy.deepcopy(t["page_break"]))
    add(_para(t["att_title"], [("ATTACHMENT A", True)]))
    add(_para(t["att_sub"], [("SCOPE OF SERVICES", True)]))
    for kind, text in parse_attachment_a(spec["attachment_a"]):
        runs = _inline_bold(text)
        if kind == "heading":
            add(_keep_next(_para(t["sec_head"], [(text.upper(), True)])))
        elif kind == "item":
            add(_para(t["bullet"], runs))
        elif kind == "subitem":
            add(_indent(_para(t["bullet"], runs)))
        else:
            add(_para(t["body"], runs))
    # --- Attachment B
    add(copy.deepcopy(t["page_break"]))
    add(_para(t["att_title"], [("ATTACHMENT B", True)]))
    add(_para(t["att_sub"], [("FEE PROPOSAL SUMMARY", True)]))
    add(_fee_table(t["fee_table"], fees, show_blended))
    for note in spec.get("fee_notes", []):
        add(_para(t["body"], [note]))
    # --- Attachment C
    add(copy.deepcopy(t["page_break"]))
    add(_para(t["att_title"], [("ATTACHMENT C", True)]))
    add(_para(t["att_sub"], [("TERMS AND CONDITIONS", True)]))
    for heading, paras in terms_sections(terms):
        add(_keep_next(_para(t["terms_head"], [(heading, True)])))
        for p in paras:
            if isinstance(p, list):
                for j, sub in enumerate(p):
                    label = f"({chr(97 + j)}) "
                    if sub.startswith(("Consultant’s indemnity.", "Owner’s indemnity.")):
                        lead, rest = sub.split(". ", 1)
                        add(_para(t["terms_body"], [label, (lead + ".", True), " " + rest]))
                    else:
                        add(_para(t["terms_body"], [label + sub]))
            else:
                add(_para(t["terms_body"], [p]))
    add(_para(t["caps"], [(WAIVER_558, True)]))

    for el in out:
        if sect is not None:
            sect.addprevious(el)
        else:
            body.append(el)

    buf = io.BytesIO()
    doc.save(buf)
    summary = {
        "filename": proposal_filename(spec),
        "total_fee": fees["total_fee"],
        "total_months": fees["total_months"],
        "blended_monthly": round(fees["blended_monthly"]),
        "phases": [{"description": r["description"], "months": r["months"], "monthly_fee": r["monthly_fee"],
                    "line_total": r["line_total"]} for r in fees["rows"]],
    }
    return buf.getvalue(), summary


def example_spec():
    return {
        "project_name": "Soundbath",
        "revision": None,
        "date": "2026-10-26",
        "client_lines": ["Celebration Ventures, Inc.", "3114 N Bay Road", "Miami, FL"],
        "salutation_name": "Vishal",
        "re_line": "Soundbath – Owner’s Representative Services",
        "project_address": "410 NE 29th Street",
        "owner_signature_name": "Vishal Sapra",
        "client_role": "Owner",
        "attachment_a": """## PROJECT SPECIFIC SCOPE OF WORK
- **On behalf of the {role}:** Owner’s Representative oversight of the Due Diligence, Pre-Construction, and Construction phases.

## GENERAL
- Through all stages report regularly to the client on the status of the project and obtain decisions needed.
- In conjunction with other consultants, initiate action if any aspect of the project appears likely to fail to achieve the client’s objectives, public obligations and program. Agree suitable corrective action and monitor its implementation.
- Advise the client on the statutory approvals and notifications required throughout the execution of the project.
- In conjunction with other consultants, coordinate quality assurance policies, plans and responsibilities for the project. Jobsite safety remains the responsibility of the contractor.
- Establish a regular meetings structure, specifying who shall attend and their responsibilities.
- Attend Client, Design, Project, and other meetings as provided under this appointment.
- Prepare regular / monthly reports.
- Advise Client of any decisions required and obtain authorization.

## PROJECT STAGES
The project development will be covered within the following stages:
- Due Diligence
- Pre-Construction
  - Design Management (Concept, Schematic, Design Development, Construction Documents)
  - Local Authority Planning & Approvals, including building permit filing
  - Management of the bidding process
- Construction Administration
- Handover""",
        "phases": [
            {"phase": "1", "description": "Due Diligence", "months": 4, "monthly_fee": 5000},
            {"phase": "2", "description": "Pre-Construction", "months": 12, "monthly_fee": 12000},
            {"phase": "3", "description": "Construction", "months": 14, "monthly_fee": 12000},
        ],
        "terms": {},
    }


if __name__ == "__main__":
    if len(sys.argv) != 4:
        sys.exit("usage: proposal_generator.py template.docx spec.json out.docx")
    tpl, spec_path, out_path = sys.argv[1:]
    spec = example_spec() if spec_path == "example" else json.load(open(spec_path))
    data, summary = build_proposal(open(tpl, "rb").read(), spec)
    open(out_path, "wb").write(data)
    print(json.dumps(summary, indent=2))
