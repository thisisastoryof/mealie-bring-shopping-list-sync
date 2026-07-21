"""Reconciliation engine + async poll loop (DESIGN.md §5).

Each cycle runs a single-pass **three-way merge**: every logical item is
compared on *both* sides against its stored shadow (``mealie_hash`` /
``bring_hash``), and only *transitions* are acted on.

  1. Fetch the Mealie list and the Bring list (purchase + recently).
  2. Join the two sides by stable id (``mealie_id`` <-> ``bring_uuid``), falling
     back to normalized name to *merge* pre-existing items instead of duplicating.
  3. For each mapping decide from the (mealie_changed, bring_changed) matrix;
     heal one-sided mappings; propagate removals with tombstones.

CORE INVARIANT: the shadow is **only ever written from observed state** — API
responses or read-backs — never from an optimistic guess about what the other
side now looks like. That is what stops a Bring-completed item from resurrecting
and the checked-state ping-pong. Tombstoned rows still claim their ids so a
still-present live item cannot be re-imported as brand-new.
"""
import asyncio
import time
import uuid
from collections import Counter, defaultdict

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
    # ``name`` (normalized) is part of the signature so a corrected/changed food
    # name registers as a transition — otherwise a rename is invisible to the
    # engine and never reaches Bring.
    return stable_hash(
        {
            "name": item.norm_key,
            "checked": item.checked,
            "quantity": item.quantity,
            "unit": item.unit,
            "note": item.note,
        }
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
                await self._reconcile(db, mealie_items, bring_items, units)
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

    @staticmethod
    def _too_fresh(m: MealieItem, now) -> bool:
        """True if ``m`` was edited within the debounce window (skip mid-edit)."""
        if m.updated_at is None:
            return False
        age = (now - m.updated_at).total_seconds()
        return 0 <= age < settings.freshness_debounce_seconds

    async def _reconcile(
        self,
        db: Session,
        mealie_items: list[MealieItem],
        bring_items: list[BringItem],
        units: dict[str, str],
    ) -> None:
        """Single-pass three-way merge over every logical item.

        Each item is diffed on both sides against its stored shadow; only
        transitions are acted on, and the shadow is always rewritten from
        observed state (see the module docstring's core invariant).
        """
        now = utcnow()
        mealie_by_id = {m.id: m for m in mealie_items}
        bring_by_uuid = {b.uuid: b for b in bring_items}

        # Claim every id owned by a mapping — tombstoned ones included, so a
        # still-present live item can't be resurrected as brand-new.
        all_rows = db.scalars(select(ItemMap)).all()
        active_rows = [r for r in all_rows if r.deleted_at is None]
        claimed_m: set[str] = {r.mealie_id for r in all_rows if r.mealie_id}
        claimed_b: set[str] = {r.bring_uuid for r in all_rows if r.bring_uuid}

        # Unclaimed live items (no stored mapping) are the only candidates for
        # pairing the two sides by normalized name.
        unclaimed_mealie: dict[str, list[MealieItem]] = defaultdict(list)
        for m in mealie_items:
            if m.id not in claimed_m:
                unclaimed_mealie[m.norm_key].append(m)
        unclaimed_bring: dict[str, list[BringItem]] = defaultdict(list)
        for b in bring_items:
            if b.uuid not in claimed_b:
                unclaimed_bring[b.norm_key].append(b)

        def pop_bring(norm: str) -> BringItem | None:
            lst = unclaimed_bring.get(norm)
            if not lst:
                return None
            chosen: BringItem | None = None
            for x in lst:  # prefer an active twin over a completed one
                if x.uuid in claimed_b:
                    continue
                if chosen is None or (not x.completed and chosen.completed):
                    chosen = x
            if chosen is None:
                return None
            lst.remove(chosen)
            claimed_b.add(chosen.uuid)
            return chosen

        def pop_mealie(norm: str) -> MealieItem | None:
            lst = unclaimed_mealie.get(norm)
            if not lst:
                return None
            chosen: MealieItem | None = None
            for x in lst:  # prefer an active (unchecked) twin
                if x.id in claimed_m:
                    continue
                if chosen is None or (not x.checked and chosen.checked):
                    chosen = x
            if chosen is None:
                return None
            lst.remove(chosen)
            claimed_m.add(chosen.id)
            return chosen

        # ── Stage 1: existing mappings (removal · heal · merge) ──────
        for row in active_rows:
            m = mealie_by_id.get(row.mealie_id) if row.mealie_id else None
            b = bring_by_uuid.get(row.bring_uuid) if row.bring_uuid else None
            mealie_gone = row.mealie_id is not None and m is None
            bring_gone = row.bring_uuid is not None and b is None

            # A mapped side vanished → propagate the removal once, then tombstone.
            if mealie_gone or bring_gone:
                if mealie_gone and b is not None:
                    await self.bring.remove_item(name=row.norm_key, item_uuid=row.bring_uuid)
                    self._emit("removed", "mealie.removed->bring.remove", item=row.norm_key, destructive=True)
                elif bring_gone and m is not None:
                    deleted = settings.on_complete == "delete"
                    if deleted:
                        await self.mealie.delete_item(row.mealie_id)
                    else:
                        await self.mealie.set_checked(row.mealie_id, True)
                    self._emit("removed", "bring.removed->mealie", item=row.norm_key, destructive=deleted)
                row.deleted_at = now
                continue

            # Heal a one-sided mapping by linking a live twin or creating one.
            if row.bring_uuid is None:
                if m is None:
                    continue
                twin = pop_bring(m.norm_key)
                if twin is not None:
                    row.bring_uuid = twin.uuid
                    row.bring_hash = twin.content_hash()
                    self._emit("healed", "heal.link_bring", item=m.display)
                else:
                    new_uuid, bhash = await self._create_in_bring(m)
                    row.bring_uuid = new_uuid
                    row.bring_hash = bhash
                    self._emit("healed", "heal.create_bring", item=m.display)
                row.mealie_hash = _mealie_hash(m)
                continue
            if row.mealie_id is None:
                if b is None:
                    continue
                if b.completed:
                    row.bring_hash = b.content_hash()
                    continue
                twin = pop_mealie(b.norm_key)
                if twin is not None:
                    row.mealie_id = twin.id
                    if twin.checked:
                        updated = await self.mealie.set_checked(twin.id, False)
                        row.mealie_hash = _mealie_hash(updated)
                        self._emit("updated", "bring.active->mealie.uncheck", item=b.name)
                    else:
                        row.mealie_hash = _mealie_hash(twin)
                    self._emit("healed", "heal.link_mealie", item=b.name)
                else:
                    created = await self._create_in_mealie(b, units)
                    row.mealie_id = created.id
                    row.mealie_hash = _mealie_hash(created)
                    self._emit("healed", "heal.create_mealie", item=b.name)
                row.bring_hash = b.content_hash()
                continue

            # Both sides live → three-way merge on transitions only.
            if self._too_fresh(m, now):
                continue  # edited mid-write; pick it up next cycle
            if row.norm_key != m.norm_key:
                row.norm_key = m.norm_key
            mealie_changed = _mealie_hash(m) != row.mealie_hash
            bring_changed = b.content_hash() != row.bring_hash
            if not mealie_changed and not bring_changed:
                continue
            if bring_changed and not mealie_changed:
                await self._converge_bring_to_mealie(row, m, b)
            elif mealie_changed and not bring_changed:
                await self._converge_mealie_to_bring(db, row, m, b, claimed_b)
            else:
                # Both changed (rare). Honor an explicit Bring completion — the
                # shopper physically checked it — otherwise Mealie is authority.
                if b.completed:
                    await self._converge_bring_to_mealie(row, m, b)
                else:
                    await self._converge_mealie_to_bring(db, row, m, b, claimed_b)

        # ── Stage 2a: unmapped Mealie items → link or create in Bring ─
        for m in mealie_items:
            if m.id in claimed_m or self._too_fresh(m, now):
                continue
            twin = pop_bring(m.norm_key)
            if twin is not None:
                mhash = _mealie_hash(m)
                # Pre-existing pair whose Mealie side is checked-off but Bring
                # side is active → uncheck so Mealie mirrors the live Bring list.
                if not twin.completed and m.checked:
                    updated = await self.mealie.set_checked(m.id, False)
                    mhash = _mealie_hash(updated)
                    self._emit("updated", "bring.active->mealie.uncheck", item=m.display)
                db.add(ItemMap(
                    mealie_id=m.id, bring_uuid=twin.uuid, norm_key=m.norm_key,
                    mealie_hash=mhash, bring_hash=twin.content_hash(),
                ))
                self._emit("linked", "link.mealie+bring", item=m.display)
            else:
                new_uuid, bhash = await self._create_in_bring(m)
                db.add(ItemMap(
                    mealie_id=m.id, bring_uuid=new_uuid, norm_key=m.norm_key,
                    mealie_hash=_mealie_hash(m), bring_hash=bhash,
                ))
                self._emit("created", "mealie.added->bring", item=m.display)
            claimed_m.add(m.id)

        # ── Stage 2b: unmapped Bring items → link or create in Mealie ─
        for b in bring_items:
            if b.uuid in claimed_b:
                continue
            twin = pop_mealie(b.norm_key)
            if twin is not None:
                mhash = _mealie_hash(twin)
                # Bring wants this item active but Mealie has it checked-off →
                # uncheck so the mirror matches the live Bring list.
                if not b.completed and twin.checked:
                    updated = await self.mealie.set_checked(twin.id, False)
                    mhash = _mealie_hash(updated)
                    self._emit("updated", "bring.active->mealie.uncheck", item=b.name)
                db.add(ItemMap(
                    mealie_id=twin.id, bring_uuid=b.uuid, norm_key=b.norm_key,
                    mealie_hash=mhash, bring_hash=b.content_hash(),
                ))
                self._emit("linked", "link.bring+mealie", item=b.name)
            elif b.completed:
                # Arrived already-completed with no history → just record it.
                db.add(ItemMap(bring_uuid=b.uuid, norm_key=b.norm_key, bring_hash=b.content_hash()))
            else:
                created = await self._create_in_mealie(b, units)
                db.add(ItemMap(
                    mealie_id=created.id, bring_uuid=b.uuid, norm_key=b.norm_key,
                    mealie_hash=_mealie_hash(created), bring_hash=b.content_hash(),
                ))
                self._emit("created", "bring.added->mealie", item=b.name)
            claimed_b.add(b.uuid)

    async def _converge_mealie_to_bring(
        self, db: Session, row: ItemMap, m: MealieItem, b: BringItem, claimed_b: set[str]
    ) -> None:
        """Push Mealie's state onto its live Bring twin; write both shadow hashes."""
        new_mhash = _mealie_hash(m)
        if m.checked:
            # Ensure the Bring twin is completed (idempotent — skip if already).
            if not b.completed:
                await self.bring.complete_item(name=m.food or m.display, item_uuid=row.bring_uuid)
            # Completing doesn't change Bring's spec, so hash the item's *actual*
            # spec, not m.spec() (which may render differently).
            row.bring_hash = _bring_hash(completed=True, spec=b.spec)
            self._emit("updated", "mealie.checked->bring.complete", item=m.display)
        elif b.norm_key != m.norm_key:
            # On Bring the item *name* is its identity (itemId) and can't be
            # renamed in place, so a changed name — a food rename or a corrected
            # note — is mirrored as delete + recreate. A mere quantity change
            # keeps ``norm_key`` stable and never lands here.
            await self.bring.remove_item(name=b.name, item_uuid=row.bring_uuid)
            new_uuid, bhash = await self._create_in_bring(m)
            claimed_b.add(new_uuid)  # keep the fresh uuid out of the new-item stage
            row.bring_uuid = new_uuid
            row.bring_hash = bhash
            row.mealie_hash = new_mhash
            # Recreate is the one non-idempotent action (a real delete + add on
            # Bring). Persist immediately so a later crash can't roll it back and
            # make the next cycle recreate the item again.
            db.commit()
            self._emit(
                "updated", "mealie.renamed->bring.recreate",
                item=m.display, old=b.name, destructive=True,
            )
            return
        else:
            spec = m.spec()
            await self.bring.update_spec(name=m.food or m.display, spec=spec, item_uuid=row.bring_uuid)
            row.bring_hash = _bring_hash(completed=False, spec=spec)
            self._emit("updated", "mealie.changed->bring.spec", item=m.display)
        row.mealie_hash = new_mhash

    async def _converge_bring_to_mealie(self, row: ItemMap, m: MealieItem, b: BringItem) -> None:
        """Push Bring's state onto its Mealie twin.

        The shadow is written from the item Mealie *actually persisted* (the
        ``set_checked`` read-back), never an optimistic guess — the invariant
        that stops the completed→resurrect and checked-state ping-pong loops.
        """
        if b.completed:
            if settings.on_complete == "delete":
                await self.mealie.delete_item(row.mealie_id)
                row.deleted_at = utcnow()
                self._emit("updated", "bring.completed->mealie.delete", item=b.name, destructive=True)
            else:
                updated = await self.mealie.set_checked(row.mealie_id, True)
                row.mealie_hash = _mealie_hash(updated)
                self._emit("updated", "bring.completed->mealie.check", item=b.name)
        elif m.checked:
            # Bring item is active but Mealie shows it checked → uncheck so
            # Mealie mirrors the live Bring list.
            updated = await self.mealie.set_checked(row.mealie_id, False)
            row.mealie_hash = _mealie_hash(updated)
            self._emit("updated", "bring.active->mealie.uncheck", item=b.name)
        row.bring_hash = b.content_hash()

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
