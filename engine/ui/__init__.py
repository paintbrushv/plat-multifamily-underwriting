"""Analyst screens for the underwriting workflow.

Four screens (deal intake, check and assumptions, results, pricing) built from the plain files in
``engine/ui/static/``. They ship two ways with the same code:

- as **MCP Apps** (``ui://plat-underwriting/<screen>`` resources the MCP server registers, opened by
  the tools that link to them through ``_meta.ui.resourceUri``), see ``engine.ui.screens``;
- as a **browser app** (``plat-uw serve``), see ``engine.ui.server``.
"""

from engine.ui.screens import (
    APP_MIME_TYPE,
    SCREENS,
    TOOL_SCREENS,
    URI_PREFIX,
    app_html,
    apps_extension,
    screen_html,
    script_hashes,
    tool_meta,
)

__all__ = [
    "APP_MIME_TYPE", "SCREENS", "TOOL_SCREENS", "URI_PREFIX", "app_html", "apps_extension", "screen_html",
    "script_hashes", "tool_meta",
]
