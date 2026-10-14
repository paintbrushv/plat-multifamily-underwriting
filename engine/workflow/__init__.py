"""The analyst workflow: a persistent deal workspace over the deterministic engine.

``engine.workflow.service`` implements the eight-step flow an analyst (or an
AI assistant on their behalf) follows to underwrite a deal: create a deal,
add its documents, describe it, ingest, review and edit the assumptions, run
it and solve a price. Every function returns a JSON-serializable dict with a
short ``summary`` and a ``next_step`` hint, and errors say what failed and
how to fix it. ``engine.mcp_server`` exposes these as MCP tools; the browser
app (``plat-uw serve``, ``engine.ui.server``) calls the same functions.

The service only orchestrates: ingestion, assumption resolution, validation,
the engine run and the price search are the existing engine functions.
"""
