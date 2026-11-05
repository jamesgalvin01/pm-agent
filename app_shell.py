"""
app_shell.py — what makes the Rowan dashboard install and behave like a phone app.

  - APP_HEAD:   viewport, web-app manifest, home-screen icon, full-screen mode,
                status-bar and theme colors (goes in every page's <head>)
  - MOBILE_CSS: phone layout below 760px wide — compact header, a bottom tab
                bar, task cards instead of tables, swipeable filter chips, and
                room for the iPhone notch and home bar
  - tab_bar():  the bottom tab bar
  - router:     /manifest.webmanifest and the icons (public: the phone fetches
                them before you're signed in)

Shared by dashboard.py and chat.py, so it imports neither.
"""
import os

from theme import THEME_CSS

from fastapi import APIRouter
from fastapi.responses import FileResponse, JSONResponse

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
THEME = "#14113A"

router = APIRouter()


APP_HEAD = f"""
        <meta name='viewport' content='width=device-width, initial-scale=1, viewport-fit=cover'>
        <meta name='theme-color' content='{THEME}'>
        <meta name='apple-mobile-web-app-capable' content='yes'>
        <meta name='mobile-web-app-capable' content='yes'>
        <meta name='apple-mobile-web-app-title' content='Rowan'>
        <meta name='apple-mobile-web-app-status-bar-style' content='black-translucent'>
        <link rel='manifest' href='/manifest.webmanifest'>
        <link rel='apple-touch-icon' href='/static/apple-touch-icon.png?v=2'>
        <link rel='icon' type='image/png' sizes='32x32' href='/static/favicon-32.png?v=2'>
        <link rel='icon' type='image/png' sizes='192x192' href='/static/icon-192.png?v=2'>
        <style>
            .logo.brand-logo {{ background: none !important; overflow: hidden; padding: 0;
                               box-shadow: 0 0 18px rgba(255,79,139,0.3) !important; }}
            .logo.brand-logo img {{ width: 100%; height: 100%; display: block; }}
            {THEME_CSS}
        </style>"""


TABS = (
    ("dashboard", "/", "ti-list-check", "Tasks"),
    ("leads", "/leads", "ti-target-arrow", "Leads"),
    ("emails", "/emails", "ti-mail", "Email"),
    ("chat", "/chat", "ti-message-circle", "Chat"),
    ("field", "/field", "ti-crane", "Field"),
)


def tab_bar(active: str) -> str:
    links = "".join(
        f"<a href='{href}' class='{'active' if key == active else ''}'>"
        f"<i class='ti {icon}'></i><span>{label}</span></a>"
        for key, href, icon, label in TABS
    )
    return f"<nav class='tabbar'>{links}</nav>"


MOBILE_CSS = """
        html { -webkit-text-size-adjust: 100%; }
        .tabbar { display: none; }
        .m-only { display: none; }
        .signout-icon { display: none; }

        @media (max-width: 760px) {
            body { padding-bottom: calc(68px + env(safe-area-inset-bottom)); }
            .header {
                padding: calc(10px + env(safe-area-inset-top)) 16px 10px;
                position: sticky; top: 0; z-index: 20;
            }
            .header nav, .header .user span, .header .user .signout-text { display: none; }
            .header .signout-icon { display: inline; font-size: 20px; }
            .header .sub { display: none; }
            .container { padding: 16px 14px; }
            .section-title { margin: 22px 0 10px; }

            .tabbar {
                display: flex; position: fixed; left: 0; right: 0; bottom: 0; z-index: 30;
                background: rgba(27,23,71,0.96); border-top: 1px solid #352F78;
                padding: 6px 4px calc(6px + env(safe-area-inset-bottom));
                -webkit-backdrop-filter: blur(12px); backdrop-filter: blur(12px);
            }
            .tabbar a {
                flex: 1; display: flex; flex-direction: column; align-items: center; gap: 2px;
                color: #9993D0; text-decoration: none; font-size: 11px; padding: 6px 0;
                -webkit-tap-highlight-color: transparent;
            }
            .tabbar a i { font-size: 23px; }
            .tabbar a.active { color: #B45CF0; }

            /* project cards become a swipeable strip */
            .cards {
                display: flex; overflow-x: auto; gap: 10px; padding-bottom: 6px;
                scroll-snap-type: x mandatory; -webkit-overflow-scrolling: touch;
            }
            .cards > * { flex: 0 0 150px; scroll-snap-align: start; }
            .cards::-webkit-scrollbar, .filters::-webkit-scrollbar { display: none; }

            /* filter chips scroll sideways instead of wrapping */
            .filters { flex-wrap: nowrap; overflow-x: auto; margin: 0 -14px 14px; padding: 0 14px; }
            .filter-btn { white-space: nowrap; padding: 9px 15px; flex-shrink: 0; }
            .filter-gap { display: none; }

            /* the task table is replaced by cards on phones */
            .d-only { display: none !important; }
            .m-only { display: block; }

            /* other tables scroll sideways rather than squash */
            table { display: block; overflow-x: auto; white-space: nowrap; }
            td, th { padding: 12px 14px; }
            .add-form { padding: 14px; }
            .add-form > div, .add-form input, .add-form select, .add-form button { width: 100%; }
            input, select, textarea { font-size: 16px !important; }   /* stops iOS zooming in */
        }

        /* task cards (phone) */
        .tcards { display: flex; flex-direction: column; gap: 10px; }
        .tcard {
            background: #221D58; border: 1px solid #352F78; border-left: 3px solid #ABA6DD;
            border-radius: 14px; padding: 14px 14px 12px;
        }
        .tcard .t-top { display: flex; justify-content: space-between; gap: 10px; align-items: flex-start; }
        .tcard .t-desc { font-size: 15px; line-height: 1.4; color: #F3F1FF; font-weight: 500; }
        .tcard .t-id { font-size: 12px; color: #847FC0; flex-shrink: 0; }
        .tcard .t-meta { font-size: 13px; color: #ABA6DD; margin-top: 6px; line-height: 1.5; }
        .tcard .t-tier { font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.6px; margin-top: 8px; }
        .tcard .t-actions { display: flex; gap: 8px; margin-top: 12px; }
        .tcard .t-actions form { flex: 1; margin: 0; }
        .tcard .t-actions button {
            width: 100%; min-height: 44px; border: none; border-radius: 10px; font-family: inherit;
            font-size: 14px; font-weight: 600; display: flex; align-items: center; justify-content: center; gap: 6px;
            -webkit-tap-highlight-color: transparent;
        }
        .tcard .t-done { background: rgba(43,212,160,0.14); color: #2bd4a0; }
        .tcard .t-snooze { background: rgba(171,166,221,0.14); color: #CBC7F0; }
        .tcard .t-open { background: rgba(180,92,240,0.14); color: #E0B0FF; }
        .tcard .t-actions a.t-log {
            flex: 1; min-height: 44px; border-radius: 10px; font-size: 14px; font-weight: 600; text-decoration: none;
            display: flex; align-items: center; justify-content: center; gap: 6px; -webkit-tap-highlight-color: transparent;
        }
        .tcard.complete .t-desc { color: #847FC0; text-decoration: line-through; }
"""


# ============================================================
# PUBLIC ROUTES: manifest + icons
# ============================================================

@router.get("/manifest.webmanifest")
def manifest():
    return JSONResponse(
        {
            "name": "Rowan",
            "short_name": "Rowan",
            "description": "Miami Coastline Management action items",
            "start_url": "/",
            "scope": "/",
            "display": "standalone",
            "background_color": THEME,
            "theme_color": THEME,
            "icons": [
                {"src": "/static/icon-192.png?v=2", "sizes": "192x192", "type": "image/png"},
                {"src": "/static/icon-512.png?v=2", "sizes": "512x512", "type": "image/png"},
                {"src": "/static/icon-512.png?v=2", "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
            ],
        },
        media_type="application/manifest+json",
    )


_ICONS = {"apple-touch-icon.png", "icon-192.png", "icon-512.png", "favicon-32.png"}


@router.get("/static/{name}")
def static_icon(name: str):
    if name not in _ICONS:
        return JSONResponse({"error": "not found"}, status_code=404)
    return FileResponse(os.path.join(STATIC_DIR, name), media_type="image/png",
                        headers={"Cache-Control": "public, max-age=604800"})


@router.get("/apple-touch-icon.png")
def apple_icon_root():
    """iOS also looks here before reading the page."""
    return static_icon("apple-touch-icon.png")


@router.get("/favicon.ico")
def favicon():
    return static_icon("favicon-32.png")
