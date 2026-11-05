"""
charts.py — the dashboard's charts, drawn on the server as plain SVG.

No chart library and nothing extra for the phone to download: each function
takes plain numbers and returns an <svg> string that scales to its box.

  ring(pct)                        progress ring with the % in the middle
  area_chart(labels, series)       smooth stacked area (the big workload chart)
  bar_chart(labels, values)        gradient bars (items closed per day)
  line_chart(labels, series)       lines with dots (lead pipeline)
"""
import html

PINK = "#FF4F8B"
VIOLET = "#9B4DEB"
LILAC = "#B45CF0"
CYAN = "#4FD1E8"
ORANGE = "#FF9F6B"
TRACK = "#352F78"
GRID = "#2C2670"
MUTED = "#847FC0"
TEXT = "#E6E3FF"

_uid = [0]


def _id(prefix: str) -> str:
    _uid[0] += 1
    return f"{prefix}{_uid[0]}"


def _smooth(points) -> str:
    """Path through the points with horizontal-midpoint curves (never overshoots)."""
    if not points:
        return ""
    d = f"M{points[0][0]:.1f},{points[0][1]:.1f}"
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        mx = (x0 + x1) / 2
        d += f" C{mx:.1f},{y0:.1f} {mx:.1f},{y1:.1f} {x1:.1f},{y1:.1f}"
    return d


def ring(pct: float, size: int = 64, stroke: int = 7) -> str:
    pct = max(0.0, min(100.0, pct or 0))
    r = (size - stroke) / 2
    c = size / 2
    circ = 2 * 3.14159265 * r
    on = circ * pct / 100
    gid = _id("rg")
    return f"""<svg class='ring' width='{size}' height='{size}' viewBox='0 0 {size} {size}' aria-hidden='true'>
<defs><linearGradient id='{gid}' x1='0' y1='0' x2='1' y2='1'><stop offset='0' stop-color='{PINK}'/><stop offset='1' stop-color='{VIOLET}'/></linearGradient></defs>
<circle cx='{c}' cy='{c}' r='{r:.1f}' fill='none' stroke='{TRACK}' stroke-width='{stroke}'/>
<circle cx='{c}' cy='{c}' r='{r:.1f}' fill='none' stroke='url(#{gid})' stroke-width='{stroke}' stroke-linecap='round'
 stroke-dasharray='{on:.1f} {circ:.1f}' transform='rotate(-90 {c} {c})'/>
<text x='{c}' y='{c + 4.5}' text-anchor='middle' font-size='13' font-weight='600' fill='{TEXT}'>{round(pct)}%</text></svg>"""


def area_chart(labels, series, highlight: int = None, w: int = 640, h: int = 260, label_step: int = None) -> str:
    """series: [(name, [values...], color)], stacked bottom-up in the order given."""
    pad_l, pad_r, pad_t, pad_b = 30, 10, 14, 26
    n = len(labels)
    totals = [sum(s[1][i] for s in series) for i in range(n)]
    top = max(totals + [4])
    top = top + (-top % 4)              # round up to a multiple of 4 for even gridlines
    iw, ih = w - pad_l - pad_r, h - pad_t - pad_b

    def x(i):
        return pad_l + (iw * i / (n - 1) if n > 1 else iw / 2)

    def y(v):
        return pad_t + ih - ih * v / top

    out = [f"<svg viewBox='0 0 {w} {h}' width='100%' class='chart' role='img'>", "<defs>"]
    for idx, (_, _, color) in enumerate(series):
        out.append(f"<linearGradient id='ag{idx}{_uid[0]}' x1='0' y1='0' x2='0' y2='1'>"
                   f"<stop offset='0' stop-color='{color}' stop-opacity='0.95'/>"
                   f"<stop offset='1' stop-color='{color}' stop-opacity='0.08'/></linearGradient>")
    out.append("</defs>")
    for k in range(5):                  # gridlines + y labels
        v = top * k / 4
        out.append(f"<line x1='{pad_l}' x2='{w - pad_r}' y1='{y(v):.1f}' y2='{y(v):.1f}' stroke='{GRID}' stroke-width='1'/>")
        out.append(f"<text x='{pad_l - 8}' y='{y(v) + 4:.1f}' text-anchor='end' font-size='11' fill='{MUTED}'>{int(v)}</text>")
    if highlight is not None and 0 <= highlight < n:
        hx = x(highlight)
        out.append(f"<rect x='{hx - iw / (2 * max(n - 1, 1)):.1f}' y='{pad_t}' width='{iw / max(n - 1, 1):.1f}' "
                   f"height='{ih}' fill='#FFFFFF' opacity='0.06'/>")
    base = [0] * n
    layers = []
    for idx, (name, vals, color) in enumerate(series):
        upper = [base[i] + vals[i] for i in range(n)]
        top_pts = [(x(i), y(upper[i])) for i in range(n)]
        bot_pts = [(x(i), y(base[i])) for i in reversed(range(n))]
        path = _smooth(top_pts) + " L" + _smooth(bot_pts)[1:] + " Z"
        layers.append(f"<path d='{path}' fill='url(#ag{idx}{_uid[0]})'/>"
                      f"<path d='{_smooth(top_pts)}' fill='none' stroke='{color}' stroke-width='2'/>")
        base = upper
    out.extend(layers)
    step = label_step or max(1, n // 7)
    for i, lab in enumerate(labels):
        if (i % step == 0 and n - 1 - i >= step / 2) or i == n - 1:
            out.append(f"<text x='{x(i):.1f}' y='{h - 6}' text-anchor='middle' font-size='11' fill='{MUTED}'>{html.escape(lab)}</text>")
    if highlight is not None and 0 <= highlight < n and totals[highlight]:
        hx, hy = x(highlight), y(totals[highlight])
        anchor = "end" if hx > w * 0.75 else "start"
        dx = -8 if anchor == "end" else 8
        out.append(f"<line x1='{hx:.1f}' x2='{hx:.1f}' y1='{pad_t}' y2='{pad_t + ih}' stroke='#FFD1E6' stroke-width='1' stroke-dasharray='3 3'/>")
        out.append(f"<circle cx='{hx:.1f}' cy='{hy:.1f}' r='4' fill='#FFD1E6'/>")
        out.append(f"<text x='{hx + dx:.1f}' y='{max(hy - 8, pad_t + 10):.1f}' text-anchor='{anchor}' font-size='12' "
                   f"font-weight='600' fill='#FFD1E6'>{html.escape(labels[highlight])}: {totals[highlight]} due</text>")
    _uid[0] += 1
    out.append("</svg>")
    return "".join(out)


def bar_chart(labels, values, w: int = 320, h: int = 130) -> str:
    pad_b, pad_t = 20, 16
    n = max(len(values), 1)
    top = max(values + [1])
    slot = w / n
    bw = slot * 0.55
    gid = _id("bg")
    out = [f"<svg viewBox='0 0 {w} {h}' width='100%' class='chart' role='img'>",
           f"<defs><linearGradient id='{gid}' x1='0' y1='0' x2='0' y2='1'><stop offset='0' stop-color='{PINK}'/>"
           f"<stop offset='1' stop-color='{VIOLET}' stop-opacity='0.55'/></linearGradient></defs>"]
    ih = h - pad_b - pad_t
    for i, v in enumerate(values):
        bh = max(ih * v / top, 2 if v else 1)
        bx = i * slot + (slot - bw) / 2
        fill = f"url(#{gid})" if v else TRACK
        out.append(f"<rect x='{bx:.1f}' y='{pad_t + ih - bh:.1f}' width='{bw:.1f}' height='{bh:.1f}' rx='2' fill='{fill}'>"
                   f"<title>{html.escape(labels[i])}: {v}</title></rect>")
        if v:
            out.append(f"<text x='{bx + bw / 2:.1f}' y='{pad_t + ih - bh - 4:.1f}' text-anchor='middle' font-size='9' fill='{MUTED}'>{v}</text>")
    for i, lab in enumerate(labels):
        if i % 2 == 0 or i == len(labels) - 1:
            out.append(f"<text x='{i * slot + slot / 2:.1f}' y='{h - 5}' text-anchor='middle' font-size='9' fill='{MUTED}'>{html.escape(lab)}</text>")
    out.append("</svg>")
    return "".join(out)


def line_chart(labels, series, money: bool = True, w: int = 320, h: int = 130) -> str:
    """series: [(name, [values...], color)]."""
    pad_l, pad_r, pad_t, pad_b = 8, 8, 16, 20
    n = len(labels)
    top = max([v for _, vals, _ in series for v in vals] + [1])
    iw, ih = w - pad_l - pad_r, h - pad_t - pad_b

    def x(i):
        return pad_l + (iw * i / (n - 1) if n > 1 else iw / 2)

    def y(v):
        return pad_t + ih - ih * v / top

    out = [f"<svg viewBox='0 0 {w} {h}' width='100%' class='chart' role='img'>"]
    for k in range(4):
        gy = pad_t + ih * k / 3
        out.append(f"<line x1='{pad_l}' x2='{w - pad_r}' y1='{gy:.1f}' y2='{gy:.1f}' stroke='{GRID}' stroke-width='1'/>")
    for name, vals, color in series:
        pts = [(x(i), y(v)) for i, v in enumerate(vals)]
        out.append(f"<path d='{_smooth(pts)}' fill='none' stroke='{color}' stroke-width='2.2'/>")
        for i, (px, py) in enumerate(pts):
            tip = f"${vals[i]:,.0f}" if money else str(vals[i])
            out.append(f"<circle cx='{px:.1f}' cy='{py:.1f}' r='3.2' fill='#1B1747' stroke='{color}' stroke-width='2'>"
                       f"<title>{html.escape(name)}, {html.escape(labels[i])}: {tip}</title></circle>")
        last = vals[-1] if vals else 0
        if last:
            tip = money_short(last) if money else str(last)
            out.append(f"<text x='{pts[-1][0] - 6:.1f}' y='{pts[-1][1] - 8:.1f}' text-anchor='end' font-size='10' "
                       f"font-weight='600' fill='{color}'>{tip}</text>")
    for i, lab in enumerate(labels):
        out.append(f"<text x='{x(i):.1f}' y='{h - 5}' text-anchor='middle' font-size='9' fill='{MUTED}'>{html.escape(lab)}</text>")
    out.append("</svg>")
    return "".join(out)


def money_short(v) -> str:
    v = float(v or 0)
    if v >= 1_000_000:
        return f"${v / 1_000_000:.1f}M"
    if v >= 1_000:
        return f"${v / 1_000:.0f}K"
    return f"${v:,.0f}"
