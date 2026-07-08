# Mealie ⇄ Bring Shopping List Sync

A small, self-hosted Docker service that keeps a **Mealie** shopping list and a
**Bring!** shopping list in true **bidirectional sync** — going directly to both
underlying APIs instead of routing through Home Assistant `todo` entities.

> This replaces an earlier Home Assistant automation/script approach. HA flattened
> every item to `{ summary, status }`, forcing fuzzy name-matching and regex
> parsing, which produced a recurring class of bugs (most visibly items
> disappearing from Bring). Talking to the APIs directly gives us stable IDs,
> structured data, and a state store we own. See [`DESIGN.md`](_project-input/DESIGN.md).

## How it works

- **Mealie** — official REST API, Bearer token. Items expose stable UUIDs plus
  structured `quantity` / `unit` / `food` / `note` / `checked`.
- **Bring!** — [`miaucl/bring-api`](https://github.com/miaucl/bring-api), the same
  unofficial client the Home Assistant Bring integration uses.
- **Engine** — one async poll loop diffs each list against a SQLite state store
  and applies only **transitions** (add / change / check / uncheck / remove),
  tracked by `mealie_id ⇄ bring_uuid` with tombstones so deletes don't
  resurrect. Items already on both lists are **merged by name** on first sight
  (never duplicated); one-sided items are mirrored across.
- **Quantities & units** — a Bring `spec` like `500 g` is parsed into Mealie's
  structured `quantity`/`unit`; on the way back, units use Mealie's own
  singular/plural forms (`2 cups`, not `2 cup`).

```
Mealie shopping list  ⇄  [ sync service + SQLite state ]  ⇄  Bring! list
        REST/Bearer                 asyncio                    bring-api
```

Runs **headless**: no UI, structured logs, and a `/health` endpoint for the
Docker healthcheck. Every cycle emits a `cycle.done` heartbeat (item counts +
duration) so the log proves liveness even when idle; irreversible deletes are
tagged `destructive=true` (find them with `grep destructive=true`), and each
line carries a `cycle_id` so a single cycle can be grouped with one `grep`.

## Quick start

```bash
cp .env.example .env
# Edit MEALIE_BASE_URL, MEALIE_API_KEY, MEALIE_SHOPPING_LIST_ID,
#      BRING_EMAIL, BRING_PASSWORD, BRING_LIST_NAME

cp docker-compose.yml.example docker-compose.yml
# Uncomment the `networks:` block to share Mealie's Docker network.

docker compose up -d
docker compose logs -f
```

Local dev:

```bash
python -m venv .venv && . .venv/Scripts/activate   # PowerShell: .venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

> Runtime: Python 3.14 (matches the Docker image). If `bring-api`/`aiohttp`
> wheels are unavailable for 3.14 on your platform, pin the Dockerfile to
> `python:3.12-slim`.

## Configuration

| Env var                      | Default         | Purpose                                                     |
| ---------------------------- | --------------- | ----------------------------------------------------------- |
| `MEALIE_BASE_URL`            | —               | e.g. `http://mealie:9000` (internal network).               |
| `MEALIE_API_KEY`             | —               | Mealie Bearer token.                                        |
| `MEALIE_SHOPPING_LIST_ID`    | —               | Target shopping list UUID.                                  |
| `BRING_EMAIL` / `_PASSWORD`  | —               | Bring credentials.                                          |
| `BRING_LIST_NAME`            | `Shopping`      | Target Bring list (resolved to a UUID on startup).          |
| `POLL_INTERVAL`              | `60`            | Seconds between cycles.                                     |
| `ON_COMPLETE`                | `check`         | On Bring completion: `check` (keep history) or `delete`.    |
| `BRING_TO_MEALIE`            | `note`          | Bring-originated items → `note` or resolve to `food`.       |
| `FRESHNESS_DEBOUNCE_SECONDS` | `5`             | Skip items edited within this window (mid-edit).            |
| `QUIET_HOURS`                | _(off)_         | Suspend polling in a local-time window, e.g. `23:00-07:00`. |
| `DB_PATH`                    | `/data/sync.db` | SQLite state store path.                                    |
| `LOG_LEVEL`                  | `INFO`          | Log verbosity. `DEBUG` also traces every HTTP request (outbound Mealie/Bring calls + inbound `/health`). |

> **Compatibility:** validated against live Mealie and Bring instances. On a
> different setup, sanity-check `bring-api` method signatures against the pinned
> version and Mealie's shopping-list endpoints against your Mealie version —
> both vary between releases.

## License

GPL-3.0 — see [`LICENSE`](LICENSE).
