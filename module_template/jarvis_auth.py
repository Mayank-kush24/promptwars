"""
Legacy import path for CDI auth (same as h2s_cdi_auth).

Some apps use ``from jarvis_auth import jarvis_auth_required``. Copy this file
next to ``h2s_cdi_auth.py`` so those imports work without renaming the whole app.
"""
from h2s_cdi_auth import (
    get_module_pages,
    get_portal_url,
    get_user,
    h2s_cdi_auth_required,
    register_h2s_cdi_auth,
    register_with_portal,
)

# Aliases expected by older module apps
jarvis_auth_required = h2s_cdi_auth_required
register_with_jarvis = register_with_portal
register_jarvis_auth = register_h2s_cdi_auth


def set_module_pages(*args, **kwargs):
    """
    No-op placeholder if legacy code calls this after changing MODULE_PAGES.
    Real apps should restart so register_with_jarvis runs again, or rebuild nav
    from get_module_pages(g.user).
    """
    return None


__all__ = [
    "jarvis_auth_required",
    "register_jarvis_auth",
    "register_with_jarvis",
    "register_with_portal",
    "register_h2s_cdi_auth",
    "h2s_cdi_auth_required",
    "get_portal_url",
    "get_module_pages",
    "get_user",
    "set_module_pages",
]
