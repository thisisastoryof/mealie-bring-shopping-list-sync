"""Reconciliation engine + async poll loop (DESIGN.md §5).

Each cycle:
  1. Fetch the Mealie list and the Bring list (purchase + recently).
  2. Classify each side against its stored hash.
  3. Apply only *transitions* — never re-derive actions from the raw snapshot.
  4. Persist new hashes / mappings / tombstones.

The whole point of diffing against **stored** state (not the live snapshot) is
that a lingering completed/removed item is actioned exactly once, which is what
made the old Home-Assistant approach unfixable.
"""
import asyncio

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.database import SessionLocal
from app.health import health
from app.logging_config import get_logger
from app.models import ItemMap
from app.services.bring import BringClient, BringItem
from app.services.mealie import MealieClient, MealieItem
from app.utils import is_quiet_now, stable_hash, utcnow

log = get_logger(__name__)


def _mealie_hash(item: MealieItem) -> str:
    return stable_hash(
        {"checked": item.checked, "quantity": item.quantity, "unit": item.unit, "note": item.note}
    )


class Reconciler:
    def __init__(self, mealie: MealieClient, bring: BringClient):
        self.mealie = mealie
        self.bring = bring

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
    async def run_cycle(self, *, seed: bool = False) -> None:
        mealie_items = await self.mealie.fetch_items()
        bring_items = await self.bring.fetch_items()

        db = SessionLocal()
        try:
            if seed:
                self._seed(db, mealie_items, bring_items)
            else:
                await self._reconcile_mealie_to_bring(db, mealie_items)
                await self._reconcile_bring_to_mealie(db, bring_items)
            db.commit()
        finally:
            db.close()

    # ── first run: mark everything known, create no side effects ────
    def _seed(self, db: Session, mealie_items: list[MealieItem], bring_items: list[BringItem]) -> None:
        log.info("seed.start", mealie=len(mealie_items), bring=len(bring_items))
        by_norm: dict[str, ItemMap] = {}
        for m in mealie_items:
            row = ItemMap(mealie_id=m.id, norm_key=m.norm_key, mealie_hash=_mealie_hash(m))
            db.add(row)
            by_norm[m.norm_key] = row
        for b in bring_items:
            row = by_norm.get(b.norm_key)
            if row is not None and row.bring_uuid is None:
                row.bring_uuid = b.uuid
                row.bring_hash = b.content_hash()
            else:
                db.add(ItemMap(bring_uuid=b.uuid, norm_key=b.norm_key, bring_hash=b.content_hash()))
        log.info("seed.done")

    # ── Mealie → Bring ──────────────────────────────────────────────
    async def _reconcile_mealie_to_bring(self, db: Session, mealie_items: list[MealieItem]) -> None:
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
                # Mealie added → create in Bring.
                uuid = await self.bring.add_item(name=m.food or m.note or m.display, spec=m.spec())
                db.add(ItemMap(mealie_id=m.id, bring_uuid=uuid, norm_key=m.norm_key, mealie_hash=new_hash))
                log.info("mealie.added->bring", item=m.display)
                continue

            row.mealie_id = m.id
            row.deleted_at = None
            if row.mealie_hash == new_hash:
                continue  # unchanged transition-wise

            # Determine which transition happened.
            if m.checked and row.bring_uuid:
                await self.bring.complete_item(name=m.food or m.display, item_uuid=row.bring_uuid)
                log.info("mealie.checked->bring.complete", item=m.display)
            elif row.bring_uuid:
                # quantity/unit/note changed → update spec only (never rename).
                await self.bring.update_spec(
                    name=m.food or m.display, spec=m.spec(), item_uuid=row.bring_uuid
                )
                log.info("mealie.changed->bring.spec", item=m.display)
            row.mealie_hash = new_hash

        # Mealie removed (had mapping, now absent) → propagate + tombstone.
        for row in db.scalars(select(ItemMap).where(ItemMap.mealie_id.is_not(None))).all():
            if row.mealie_id in seen or row.deleted_at is not None:
                continue
            if row.bring_uuid:
                await self.bring.remove_item(name=row.norm_key, item_uuid=row.bring_uuid)
            row.deleted_at = now
            log.info("mealie.removed->bring.remove", mealie_id=row.mealie_id)

    # ── Bring → Mealie ──────────────────────────────────────────────
    async def _create_in_mealie(self, b: BringItem, note: str) -> str:
        """Create a Bring-originated item in Mealie.

        ``BRING_TO_MEALIE=food`` resolves the Bring item name to a Mealie food so
        Mealie can aggregate it; on no match it falls back to a plain note item
        (DESIGN.md §5, §11). ``note`` mode always creates a note item.
        """
        if settings.bring_to_mealie == "food":
            food_id = await self.mealie.resolve_food_id(b.name)
            if food_id:
                log.info("bring.added->mealie.food", item=b.name)
                return await self.mealie.add_food_item(food_id=food_id, note=b.spec)
        return await self.mealie.add_item(note=note)

    async def _reconcile_bring_to_mealie(self, db: Session, bring_items: list[BringItem]) -> None:
        seen: set[str] = set()

        for b in bring_items:
            seen.add(b.uuid)
            new_hash = b.content_hash()
            row = self._by_bring(db, b.uuid) or self._by_norm(db, b.norm_key)

            if row is None:
                if b.completed:
                    # Ignore items that arrive already-completed with no history.
                    db.add(ItemMap(bring_uuid=b.uuid, norm_key=b.norm_key, bring_hash=new_hash))
                    continue
                # Bring added (no mapping) → create in Mealie.
                # DESIGN default: plain note item. BRING_TO_MEALIE=food resolves to a food.
                note = f"{b.spec} {b.name}".strip() if b.spec else b.name
                mealie_id = await self._create_in_mealie(b, note)
                db.add(
                    ItemMap(mealie_id=mealie_id, bring_uuid=b.uuid, norm_key=b.norm_key, bring_hash=new_hash)
                )
                log.info("bring.added->mealie", item=b.name)
                continue

            row.bring_uuid = b.uuid
            if row.bring_hash == new_hash:
                continue

            # Bring completed → check or delete the Mealie item.
            if b.completed and row.mealie_id:
                if settings.on_complete == "delete":
                    await self.mealie.delete_item(row.mealie_id)
                    row.deleted_at = utcnow()
                    log.info("bring.completed->mealie.delete", item=b.name)
                else:
                    await self.mealie.set_checked(row.mealie_id, True)
                    log.info("bring.completed->mealie.check", item=b.name)
            row.bring_hash = new_hash

        # Bring removed entirely (had mapping) → propagate + tombstone.
        for row in db.scalars(select(ItemMap).where(ItemMap.bring_uuid.is_not(None))).all():
            if row.bring_uuid in seen or row.deleted_at is not None:
                continue
            if row.mealie_id:
                if settings.on_complete == "delete":
                    await self.mealie.delete_item(row.mealie_id)
                else:
                    await self.mealie.set_checked(row.mealie_id, True)
            row.deleted_at = utcnow()
            log.info("bring.removed->mealie", bring_uuid=row.bring_uuid)


async def poll_loop(stop: asyncio.Event) -> None:
    """Own the HTTP/Bring clients for the process lifetime and run cycles on an interval."""
    async with httpx.AsyncClient() as http_client:
        mealie = MealieClient(http_client)
        bring = BringClient()
        await bring.connect()
        reconciler = Reconciler(mealie, bring)

        first = True
        try:
            while not stop.is_set():
                if is_quiet_now(settings.quiet_hours, settings.timezone):
                    log.debug("cycle.skipped_quiet_hours", window=settings.quiet_hours)
                else:
                    try:
                        await reconciler.run_cycle(seed=first)
                        health.record_success()
                        first = False
                    except Exception as exc:  # keep the loop alive; surface via /health + logs
                        health.record_failure(str(exc))
                        log.error("cycle.failed", error=str(exc), exc_info=True)
                try:
                    await asyncio.wait_for(stop.wait(), timeout=settings.poll_interval)
                except asyncio.TimeoutError:
                    pass
        finally:
            await bring.close()
