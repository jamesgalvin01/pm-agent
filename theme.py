"""
theme.py — Rowan's look, in one place.

Palette (deep indigo + pink/violet gradients):
  page #14113A · header #1B1747 · panel #221D58 · border #352F78
  text #E6E3FF · muted #847FC0 · pink #FF4F8B · violet #9B4DEB / #B45CF0

THEME_CSS goes into every page's <head> through app_shell.APP_HEAD. Its
selectors start with "html body" so they win over each page's own styles
without !important. To restyle the app, start here.
"""

THEME_CSS = """
        html body {
            background-color: #14113A;
            background-image:
                radial-gradient(900px 500px at 85% -10%, rgba(155,77,235,0.28), transparent 60%),
                radial-gradient(700px 420px at -10% 110%, rgba(255,79,139,0.16), transparent 60%);
            background-attachment: fixed;
        }
        html body .header { background: rgba(27,23,71,0.82); -webkit-backdrop-filter: blur(14px); backdrop-filter: blur(14px); }
        html body .header nav a.active { background: linear-gradient(90deg, rgba(255,79,139,0.22), rgba(155,77,235,0.22)); color: #FFFFFF; }
        html body .filter-btn.active,
        html body .add-task summary,
        html body .add-form button,
        html body .new-btn,
        html body .composer button:not(:disabled),
        html body .msg.user .bubble,
        html body .chip.on,
        html body .b-blue {
            background: linear-gradient(90deg, #FF4F8B, #9B4DEB); border-color: transparent; color: #FFFFFF;
        }
        html body .add-task[open] summary { background: #352F78; }
        html body .card, html body .stat-card, html body table, html body .add-form, html body .tcard {
            background-color: rgba(34,29,88,0.72); border-radius: 16px;
        }
        html body .tabbar a.active { color: #FF8FC4; }
        html body ::selection { background: rgba(255,79,139,0.35); }
"""
