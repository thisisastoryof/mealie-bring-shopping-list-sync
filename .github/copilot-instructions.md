# Copilot Instructions — mealie-bring-shopping-list-sync

## What this is
A **headless** Docker service that bidirectionally syncs a Mealie shopping list
with a Bring! list by talking to both APIs directly. No UI, no templates, no auth.
Replaces a prior Home Assistant approach. See `DESIGN.md`.

## Stack
- Python 3.14, `asyncio` (single poll loop — no APScheduler)
- FastAPI **only** for the `/health` endpoint + to own the event loop via lifespan
- `httpx.AsyncClient` for Mealie, `bring-api` (async, aiohttp) for Bring
- SQLAlchemy 2.0 + SQLite state store
- `structlog` for structured logging; `pydantic-settings` for config

## Core rules
- **Diff against stored state, not the live snapshot.** Act on *transitions* only.
  A lingering completed/removed item must be actioned exactly once.
- **Stable identity over string matching.** Track `mealie_id ⇄ bring_uuid`;
  `norm_key` is a fallback matcher only.
- **Deletes are final within a sync window** via `deleted_at` tombstones.
- **Update Bring `spec`, never rename** an item's `itemId` (bring-api quirk).
  A changed food *name* = delete + recreate.
- **Mealie is the composition authority** — mirror its already-computed list;
  never push raw recipe ingredients that bypass Mealie's aggregation.

## Conventions
- Config via env only (`app/config.py`); no web-editable settings.
- Keep the poll loop resilient: catch per-cycle exceptions, record to `health`,
  keep looping. Never let one bad cycle kill the process.
- New external calls go in `app/services/`; the engine lives in
  `app/services/reconciler.py`.
- No secrets in logs (never log `BRING_PASSWORD` or the Mealie token).

## Version cautions
- `bring-api` method/return shapes vary by major version — pin it and verify
  `app/services/bring.py` against the installed version.
- Mealie shopping-list endpoints vary by Mealie version — verify
  `app/services/mealie.py` paths against the deployed instance.

## Git
- Independent repo. User often amends and force-pushes — use `--force-with-lease`.
