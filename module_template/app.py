"""
Module App — Hack2skill Central Data Intelligence integrated Flask app.

Quickstart:
  1. Copy this folder, rename it to your app name.
  2. Fill in .env  (copy .env.example → .env, edit the 3 marked values).
  3. Edit the two CUSTOMIZE sections below (pages + routes).
  4. In the portal → Nginx Config: add service (slug, port, host) → Reload Nginx.
  5. python app.py
"""
import os

from dotenv import load_dotenv
from flask import Flask, g, redirect, render_template, request, url_for
from werkzeug.middleware.proxy_fix import ProxyFix

from h2s_cdi_auth import (
    get_portal_url,
    get_module_pages,
    register_h2s_cdi_auth,
    register_with_portal,
)

load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "change-me-in-production")

# Required so url_for() and redirects use the public URL when behind nginx.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

APPLICATION_ROOT = (os.environ.get("APPLICATION_ROOT") or "").strip()
if APPLICATION_ROOT and not APPLICATION_ROOT.startswith("/"):
    APPLICATION_ROOT = "/" + APPLICATION_ROOT


@app.before_request
def _set_script_name():
    prefix = request.environ.get("HTTP_X_FORWARDED_PREFIX", "").strip().rstrip("/")
    if prefix and not prefix.startswith("/"):
        prefix = "/" + prefix
    if not prefix and APPLICATION_ROOT:
        prefix = APPLICATION_ROOT.rstrip("/")
    if prefix:
        request.environ["SCRIPT_NAME"] = prefix


# =============================================================================
# Portal auth — one registration covers all routes (see path_page_rules below)
# =============================================================================
register_h2s_cdi_auth(
    app,
    public_paths=("/static", "/favicon.ico", "/health", "/login", "/logout"),
    path_page_rules=[
        ("/home", "home"),
    ],
    default_page=None,
)


# =============================================================================
# CUSTOMIZE #1 — Define your pages
# =============================================================================
# Each entry becomes a nav link and a permission unit in the portal.
# "pageId" must exactly match:
#   - the path_page_rules prefix target (e.g. ("/home", "home"))
#   - the route's URL path                       ("/home")
#
# Add or remove entries freely; keep path_page_rules in sync. The portal
# auto-discovers pages on startup.
# =============================================================================

MODULE_PAGES = [
    {"pageId": "home",    "label": "Home",    "path": "/home"},
    # {"pageId": "reports", "label": "Reports", "path": "/reports"},
    # {"pageId": "settings","label": "Settings","path": "/settings"},
]


# ---------------------------------------------------------------------------
# Nav context — injected into every template automatically (don't touch)
# ---------------------------------------------------------------------------
@app.context_processor
def _inject_nav():
    user = getattr(g, "user", None)
    portal_url = get_portal_url()
    script_root = (request.environ.get("SCRIPT_NAME") or "").rstrip("/")

    if not user:
        return {
            "nav_pages": [],
            "current_user": None,
            "portal_url": portal_url,
            "script_root": script_root,
        }

    if user.get("isAdmin"):
        visible = MODULE_PAGES
    else:
        pages = get_module_pages(user)
        if pages is None:
            visible = MODULE_PAGES
        else:
            allowed = set(pages)
            visible = [p for p in MODULE_PAGES if p["pageId"] in allowed]

    return {
        "nav_pages": visible,
        "current_user": user,
        "portal_url": portal_url,
        "script_root": script_root,
    }


# ---------------------------------------------------------------------------
# Standard routes — login/logout/health/root (don't touch)
# ---------------------------------------------------------------------------

@app.route("/health")
def health():
    """Public health check — used to verify the app is reachable."""
    return {
        "status": "ok",
        "app": os.environ.get("H2S_CDI_MODULE_ID", os.environ.get("JARVIS_MODULE_ID", "module")),
    }, 200


@app.route("/")
def index():
    """Root: redirect to first allowed page, or ask for access."""
    user = g.user
    if user.get("isAdmin"):
        return redirect(url_for(MODULE_PAGES[0]["pageId"]))
    pages = get_module_pages(user)
    if pages is None or pages:
        first = pages[0] if pages else MODULE_PAGES[0]["pageId"]
        return redirect(url_for(first))
    return redirect(f"{get_portal_url()}/invite-only")


@app.route("/login")
def login():
    return redirect(f"{get_portal_url()}/login")


@app.route("/logout")
def logout():
    return redirect(f"{get_portal_url()}/dashboard")


# =============================================================================
# CUSTOMIZE #2 — Add your routes
# =============================================================================
# One route per pageId in MODULE_PAGES. Add each path to path_page_rules above.
# Access g.user inside the function for the logged-in user's info:
#   g.user["email"], g.user["name"], g.user["isAdmin"]
# =============================================================================

@app.route("/home")
def home():
    return render_template("home.html")


# Example additional pages — uncomment MODULE_PAGES, path_page_rules, and routes:

# @app.route("/reports")
# def reports():
#     return render_template("reports.html")

# @app.route("/settings")
# def settings():
#     return render_template("settings.html")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    module_name = os.environ.get("MODULE_NAME", "My Module")
    base_url = os.environ.get("BASE_URL", f"http://localhost:{port}")

    register_with_portal(MODULE_PAGES, module_name=module_name, base_url=base_url)

    app.run(host="0.0.0.0", port=port, debug=os.environ.get("DEBUG", "false").lower() == "true")
