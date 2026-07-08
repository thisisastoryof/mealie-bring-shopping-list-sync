"""Reconciliation engine + async poll loop (DESIGN.md §5).

Each cycle:
  1. Fetch the Mealie list and the Bring list (purchase + recently).
  2. Link the two sides by stable id (``mealie_id`` <-> ``bring_uuid``), falling
     back to normalized name to *merge* pre-existing items instead of duplicating.
  3. Heal one-sided mappings (create the missing counterpart) and apply only
     *transitions* against the stored per-side hash.
  4. Persist new hashes / mappings / tombstones.

Diffing against **stored** state (not the live snapshot) is what makes a
lingering completed/removed item act exactly once — the failure that made the
old Home-Assistant approach unfixable. Both hashes are written whenever a
mapping is created or linked, so an item never immediately round-trips back to
the side it came from.
"""
import asyncio
import time
import uuid
from collections import Counter

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session
from structlog.contextvars import bind_contextvars, clear_contextvars

from app.config import settings
from app.database import SessionLocal
from app.health import health
from app.logging_config import get_logger
from app.models import ItemMap
from app.services.bring import BringClient, BringItem
from app.services.mealie import MealieClient, MealieItem
from app.utils import is_quiet_now, normalize_name, parse_quantity, stable_hash, utcnow

log = get_logger(__name__)


def _mealie_hash(item: MealieItem) -> str:
    return stable_hash(
        {"checked": item.checked, "quantity": item.quantity, "unit": item.unit, "note": item.note}
    )


def _bring_hash(*, completed: bool, spec: str) -> str:
    return stable_hash({"completed": completed, "spec": spec})


def _split_unit(remainder: str, units: dict[str, str]) -> tuple[str | None, str]:
    """Resolve the first token of ``remainder`` against Mealie's units.

    Returns ``(unit_id, leftover_note)``. If the leading token is not a known
    Mealie unit, nothing is consumed and the remainder is returned unchanged.
    """
    if not remainder:
        return None, ""
    token, _, rest = remainder.partition(" ")
    unit_id = units.get(normalize_name(token))
    if unit_id:
        return unit_id, rest.strip()
    return None, remainder


class Reconciler:
    def __init__(self, mealie: MealieClient, bring: BringClient):
        self.mealie = mealie
        self.bring = bring
        self._stats: Counter = Counter()

    def _emit(self, bucket: str, event: str, *, destructive: bool = False, **fields) -> None:
        """Count an action for the per-cycle summary and log it at INFO.

        Every action the reconciler takes is intended, normal behaviour, so it
        logs at INFO — WARNING/ERROR stay reserved for genuine anomalies (see
        ``cycle.failed``). Irreversible deletes are tagged ``destructive=True``
        so they stay findable by field (``grep destructive=true``) without
        abusing the log level.
        """
        self._stats[bucket] += 1
        if destructive:
            fields["destructive"] = True
        log.info(event, **fields)

    # ── mapping helpers ─────────────────────────────────────────────
    @staticmethod
    def _by_mealie(db: Session, mealie_id: str) -> ItemMap | None:
        return db.scalar(select(ItemMap).where(ItemMap.mealie_id == mealie_id))

    @staticmethod
    def _by_bring(db: Session, bring_uuid: str) -> ItemMap | None:
        return db.scalar(select(ItemMap).where(ItemMap.bring_uuid == bring_uuid))

    @staticmethod
    def _by_norm(db: Session, norm_key: str) -> ItemMap | None:
        if not norm_key:
            return None
        return db.scalar(
            select(ItemMap).where(ItemMap.norm_key == norm_key, ItemMap.deleted_at.is_(None))
        )

    # ── single cycle ────────────────────────────────────────────────
    async def run_cycle(self) -> None:
        # Tag every log line in this cycle so the two interleaved directions can
        # be grouped by a single grep (structlog merge_contextvars picks it up).
        bind_contextvars(cycle_id=uuid.uuid4().hex[:8])
        self._stats = Counter()
        started = time.perf_counter()
        try:
            mealie_items = await self.mealie.fetch_items()
            bring_items = await self.bring.fetch_items()
            # Mealie unit vocabulary — only needed to resolve Bring specs to foods.
            units = await self.mealie.fetch_units() if settings.bring_to_mealie == "food" else {}

            db = SessionLocal()
            try:
                await self._reconcile_mealie_to_bring(db, mealie_items, bring_items)
                await self._reconcile_bring_to_mealie(db, bring_items, mealie_items, units)
                db.commit()
            finally:
                db.close()

            # Heartbeat: one line every cycle, even when idle, so the log itself
            # proves liveness and shows the net effect at a glance.
            log.info(
                "cycle.done",
                mealie_items=len(mealie_items),
                bring_items=len(bring_items),
                created=self._stats["created"],
                updated=self._stats["updated"],
                removed=self._stats["removed"],
                linked=self._stats["linked"],
                healed=self._stats["healed"],
                duration_ms=round((time.perf_counter() - started) * 1000),
            )
        finally:
            clear_contextvars()

    # ── Mealie → Bring ──────────────────────────────────────────────
    async def _create_in_bring(self, m: MealieItem) -> tuple[str, str]:
        """Create a Bring item mirroring ``m``. Returns ``(uuid, bring_hash)``."""
        spec = m.spec()
        uuid = await self.bring.add_item(name=m.food or m.note or m.display, spec=spec)
        return uuid, _bring_hash(completed=False, spec=spec)

    async def _reconcile_mealie_to_bring(
        self, db: Session, mealie_items: list[MealieItem], bring_items: list[BringItem]
    ) -> None:
        # Live Bring items by name, used to *merge* an existing Bring twin
        # instead of creating a duplicate (first-run / pre-existing items).
        bring_by_norm: dict[str, BringItem] = {}
        for b in bring_items:
            bring_by_norm.setdefault(b.norm_key, b)

        seen: set[str] = set()
        now = utcnow()

        for m in mealie_items:
            seen.add(m.id)

            # Freshness debounce — skip items edited mid-write.
            if m.updated_at is not None:
                age = (now - m.updated_at).total_seconds()
                if 0 <= age < settings.freshness_debounce_seconds:
                    continue

            new_hash = _mealie_hash(m)
            row = self._by_mealie(db, m.id) or self._by_norm(db, m.norm_key)

            if row is None:
                twin = bring_by_norm.get(m.norm_key)
                if twin is not None and self._by_bring(db, twin.uuid) is None:
                    # Same item already on both sides → link, no side effect.
                    db.add(ItemMap(
                        mealie_id=m.id, bring_uuid=twin.uuid, norm_key=m.norm_key,
                        mealie_hash=new_hash, bring_hash=twin.content_hash(),
                    ))
                    self._emit("linked", "link.mealie+bring", item=m.display)
                    continue
                uuid, bhash = await self._create_in_bring(m)
                db.add(ItemMap(
                    mealie_id=m.id, bring_uuid=uuid, norm_key=m.norm_key,
                    mealie_hash=new_hash, bring_hash=bhash,
                ))
                self._emit("created", "mealie.added->bring", item=m.display)
                continue

            if row.deleted_at is not None:
                # The mapping was already torn down (e.g. the Bring side was
                # removed and, with ON_COMPLETE=check, the Mealie item was only
                # checked — so it still shows up here). Leave the closed mapping
                # closed. Reviving it (deleted_at=None) makes the Bring→Mealie
                # pass re-detect the still-absent Bring item and re-log the exact
                # same removal on every cycle.
                continue

            row.mealie_id = m.id

            # Heal a mapping that lost/never had its Bring side (e.g. a legacy
            # one-sided seed row) so the item finally mirrors across.
            if row.bring_uuid is None:
                twin = bring_by_norm.get(m.norm_key)
                if twin is not None and self._by_bring(db, twin.uuid) is None:
                    row.bring_uuid = twin.uuid
                    row.bring_hash = twin.content_hash()
                    self._emit("healed", "heal.link_bring", item=m.display)
                else:
                    row.bring_uuid, row.bring_hash = await self._create_in_bring(m)
                    self._emit("healed", "heal.create_bring", item=m.display)
                row.mealie_hash = new_hash
                continue

            if row.mealie_hash == new_hash:
                continue  # no transition

            if m.checked:
                await self.bring.complete_item(name=m.food or m.display, item_uuid=row.bring_uuid)
                row.bring_hash = _bring_hash(completed=True, spec=m.spec())
                self._emit("updated", "mealie.checked->bring.complete", item=m.display)
            else:
                spec = m.spec()
                await self.bring.update_spec(name=m.food or m.display, spec=spec, item_uuid=row.bring_uuid)
                row.bring_hash = _bring_hash(completed=False, spec=spec)
                self._emit("updated", "mealie.changed->bring.spec", item=m.display)
            row.mealie_hash = new_hash

        # Mealie removed (had mapping, now absent) → propagate + tombstone.
        for row in db.scalars(select(ItemMap).where(ItemMap.mealie_id.is_not(None))).all():
            if row.mealie_id in seen or row.deleted_at is not None:
                continue
            if row.bring_uuid:
                await self.bring.remove_item(name=row.norm_key, item_uuid=row.bring_uuid)
            row.deleted_at = now
            # Always a hard delete on the Bring side — tag it as irreversible.
            self._emit("removed", "mealie.removed->bring.remove", item=row.norm_key, destructive=True)

    # ── Bring → Mealie ──────────────────────────────────────────────
    async def _create_in_mealie(self, b: BringItem, units: dict[str, str]) -> MealieItem:
        """Create a Mealie item mirroring a Bring item.

        The Bring ``spec`` is split into a structured ``quantity`` (so "10 Eier"
        becomes quantity 10, not the note "10 Eier" shown as "1 10 Eier"). With
        ``BRING_TO_MEALIE=food`` the name is resolved to a Mealie food and the
        leading unit token to a Mealie unit; otherwise a note item is created.
        """
        quantity, remainder = parse_quantity(b.spec)

        if settings.bring_to_mealie == "food":
            food_id = await self.mealie.resolve_food_id(b.name)
            if food_id:
                unit_id, note = _split_unit(remainder, units)
                log.debug("bring.resolved_food", item=b.name, food_id=food_id)
                return await self.mealie.create_item(
                    note=note, quantity=quantity, food_id=food_id, unit_id=unit_id
                )

        note = f"{remainder} {b.name}".strip() if remainder else b.name
        return await self.mealie.create_item(note=note, quantity=quantity)

    async def _reconcile_bring_to_mealie(
        self, db: Session, bring_items: list[BringItem], mealie_items: list[MealieItem], units: dict[str, str]
    ) -> None:
        mealie_by_norm: dict[str, MealieItem] = {}
        for m in mealie_items:
            mealie_by_norm.setdefault(m.norm_key, m)

        seen: set[str] = set()

        for b in bring_items:
            seen.add(b.uuid)
            new_hash = b.content_hash()
            row = self._by_bring(db, b.uuid) or self._by_norm(db, b.norm_key)

            if row is None:
                twin = mealie_by_norm.get(b.norm_key)
                if twin is not None and self._by_mealie(db, twin.id) is None:
                    db.add(ItemMap(
                        mealie_id=twin.id, bring_uuid=b.uuid, norm_key=b.norm_key,
                        mealie_hash=_mealie_hash(twin), bring_hash=new_hash,
                    ))
                    self._emit("linked", "link.bring+mealie", item=b.name)
                    continue
                if b.completed:
                    # Arrived already-completed with no history → just record it.
                    db.add(ItemMap(bring_uuid=b.uuid, norm_key=b.norm_key, bring_hash=new_hash))
                    continue
                created = await self._create_in_mealie(b, units)
                db.add(ItemMap(
                    mealie_id=created.id, bring_uuid=b.uuid, norm_key=b.norm_key,
                    mealie_hash=_mealie_hash(created), bring_hash=new_hash,
                ))
                self._emit("created", "bring.added->mealie", item=b.name)
                continue

            row.bring_uuid = b.uuid

            # Heal a mapping that lost/never had its Mealie side.
            if row.mealie_id is None and not b.completed:
                twin = mealie_by_norm.get(b.norm_key)
                if twin is not None and self._by_mealie(db, twin.id) is None:
                    row.mealie_id = twin.id
                    row.mealie_hash = _mealie_hash(twin)
                    self._emit("healed", "heal.link_mealie", item=b.name)
                else:
                    created = await self._create_in_mealie(b, units)
                    row.mealie_id = created.id
                    row.mealie_hash = _mealie_hash(created)
                    self._emit("healed", "heal.create_mealie", item=b.name)
                row.bring_hash = new_hash
                continue

            if row.bring_hash == new_hash:
                continue

            if b.completed and row.mealie_id:
                if settings.on_complete == "delete":
                    await self.mealie.delete_item(row.mealie_id)
                    row.deleted_at = utcnow()
                    self._emit("updated", "bring.completed->mealie.delete", item=b.name, destructive=True)
                else:
                    await self.mealie.set_checked(row.mealie_id, True)
                    self._emit("updated", "bring.completed->mealie.check", item=b.name)
            row.bring_hash = new_hash

        # Bring removed entirely (had mapping) → propagate + tombstone.
        for row in db.scalars(select(ItemMap).where(ItemMap.bring_uuid.is_not(None))).all():
            if row.bring_uuid in seen or row.deleted_at is not None:
                continue
            deleted = settings.on_complete == "delete"
            if row.mealie_id:
                if deleted:
                    await self.mealie.delete_item(row.mealie_id)
                else:
                    await self.mealie.set_checked(row.mealie_id, True)
            row.deleted_at = utcnow()
            # Tag as destructive only when it actually deletes in Mealie; a
            # check is reversible and part of normal completion.
            self._emit("removed", "bring.removed->mealie", item=row.norm_key, destructive=deleted)



async def poll_loop(stop: asyncio.Event) -> None:
    """Own the HTTP/Bring clients for the process lifetime and run cycles on an interval."""
    async with httpx.AsyncClient() as http_client:
        mealie = MealieClient(http_client)
        bring = BringClient()
        await bring.connect()
        reconciler = Reconciler(mealie, bring)

        try:
            while not stop.is_set():
                if is_quiet_now(settings.quiet_hours, settings.timezone):
                    log.debug("cycle.skipped_quiet_hours", window=settings.quiet_hours)
                else:
                    try:
                        await reconciler.run_cycle()
                        health.record_success()
                    except Exception as exc:  # keep the loop alive; surface via /health + logs
                        health.record_failure(str(exc))
                        log.error("cycle.failed", error=str(exc), exc_info=True)
                try:
                    await asyncio.wait_for(stop.wait(), timeout=settings.poll_interval)
                except asyncio.TimeoutError:
                    pass
        finally:
            await bring.close()
