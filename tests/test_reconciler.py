"""Integration-style unit tests for the single-pass reconciliation engine.

Each test drives the engine through :func:`tests.fakes.run_cycle` against the
in-memory Mealie/Bring fakes and asserts both the side effects (client calls,
resulting list contents) and the stored shadow (``ItemMap`` rows).
"""
import pytest
from sqlalchemy import select

from app.config import settings
from app.models import ItemMap
from app.utils import utcnow


def rows(db):
    return db.scalars(select(ItemMap)).all()


def active_rows(db):
    return [r for r in rows(db) if r.deleted_at is None]


class TestCreation:
    async def test_new_mealie_item_created_in_bring(self, cycle, db, mealie, bring):
        m = mealie.seed(note="Milk", quantity=2)
        await cycle()

        assert bring.count("add_item") == 1
        (row,) = active_rows(db)
        assert row.mealie_id == m.id
        assert row.bring_uuid is not None
        assert row.mealie_hash and row.bring_hash
        (b,) = list(bring.items.values())
        assert b.name == "Milk"
        assert b.spec == "2"
        assert b.completed is False

    async def test_new_active_bring_item_created_in_mealie(self, cycle, db, mealie, bring):
        b = bring.seed(name="Eggs", spec="10")
        await cycle()

        assert mealie.count("create_item") == 1
        (row,) = active_rows(db)
        assert row.bring_uuid == b.uuid
        assert row.mealie_id is not None
        (m,) = list(mealie.items.values())
        assert m.note == "Eggs"
        assert m.quantity == 10.0

    async def test_completed_bring_item_recorded_without_mealie(self, cycle, db, mealie, bring):
        bring.seed(name="Old", spec="1", completed=True)
        await cycle()

        assert mealie.count("create_item") == 0
        (row,) = rows(db)
        assert row.mealie_id is None
        assert row.bring_uuid is not None


class TestLinking:
    async def test_link_preexisting_by_name(self, cycle, db, mealie, bring):
        mealie.seed(note="Milk", quantity=1)
        bring.seed(name="Milk", spec="1")
        await cycle()

        assert bring.count("add_item") == 0
        assert mealie.count("create_item") == 0
        (row,) = active_rows(db)
        assert row.mealie_id is not None
        assert row.bring_uuid is not None

    async def test_link_unchecks_mealie_when_bring_active(self, cycle, db, mealie, bring):
        m = mealie.seed(note="Milk", quantity=1, checked=True)
        bring.seed(name="Milk", spec="1")
        await cycle()

        assert mealie.count("set_checked") == 1
        assert mealie.items[m.id].checked is False


class TestMealieToBring:
    async def test_check_completes_bring(self, cycle, db, mealie, bring):
        m = mealie.seed(note="Milk", quantity=1)
        await cycle()
        mealie.check(m.id)
        await cycle()

        assert bring.count("complete_item") == 1
        (b,) = list(bring.items.values())
        assert b.completed is True

    async def test_quantity_change_updates_spec_without_recreate(self, cycle, db, mealie, bring):
        m = mealie.seed(note="Milk", quantity=1)
        await cycle()
        mealie.set_quantity(m.id, 3)
        await cycle()

        assert bring.count("update_spec") == 1
        assert bring.count("add_item") == 1  # only the initial create
        assert bring.count("remove_item") == 0
        (b,) = list(bring.items.values())
        assert b.spec == "3"
        assert b.completed is False

    async def test_note_item_quantity_change_keeps_bring_name(self, cycle, db, mealie, bring):
        """Regression: a note item's display carries the quantity ("12 Eier").

        A spec-only change must update the Bring spec in place and never rename
        the itemId to the display string.
        """
        from dataclasses import replace

        m = mealie.seed(note="Eier", quantity=10)
        await cycle()
        (b,) = list(bring.items.values())
        assert b.name == "Eier"
        assert b.spec == "10"

        # Real Mealie recomputes display to include the quantity.
        mealie.items[m.id] = replace(mealie.items[m.id], quantity=12, display="12 Eier")
        await cycle()

        assert bring.count("update_spec") == 1
        assert bring.count("add_item") == 1  # spec update, not a recreate
        assert bring.count("remove_item") == 0
        (b2,) = list(bring.items.values())
        assert b2.name == "Eier"  # itemId preserved, not renamed to "12 Eier"
        assert b2.spec == "12"

    async def test_rename_recreates_bring_item(self, cycle, db, mealie, bring):
        m = mealie.seed(note="Milk", quantity=1)
        await cycle()
        old_uuid = active_rows(db)[0].bring_uuid

        mealie.rename(m.id, note="Oat Milk")
        await cycle()

        assert bring.count("remove_item") == 1
        assert bring.count("add_item") == 2  # initial + recreate
        assert old_uuid not in bring.items
        (b,) = list(bring.items.values())
        assert b.name == "Oat Milk"
        (row,) = active_rows(db)
        assert row.bring_uuid != old_uuid
        assert row.norm_key == "oat milk"

    async def test_rename_is_stable_on_next_cycle(self, cycle, db, mealie, bring):
        m = mealie.seed(note="Milk", quantity=1)
        await cycle()
        mealie.rename(m.id, note="Oat Milk")
        await cycle()
        removes, adds = bring.count("remove_item"), bring.count("add_item")

        await cycle()  # no further churn expected

        assert bring.count("remove_item") == removes
        assert bring.count("add_item") == adds
        assert len(bring.items) == 1


class TestBringCompletion:
    async def test_complete_checks_mealie(self, cycle, db, mealie, bring):
        m = mealie.seed(note="Milk", quantity=1)
        await cycle()
        uuid = active_rows(db)[0].bring_uuid

        bring.complete(uuid)
        await cycle()

        assert mealie.items[m.id].checked is True

    async def test_complete_is_stable_no_resurrection(self, cycle, db, mealie, bring):
        """Regression: a Bring-completed item must not reappear or ping-pong.

        The shadow is written from the read-back of set_checked, so subsequent
        cycles detect no transition and take no action.
        """
        m = mealie.seed(note="Milk", quantity=1)
        await cycle()
        uuid = active_rows(db)[0].bring_uuid
        bring.complete(uuid)
        await cycle()

        for _ in range(3):
            await cycle()

        assert bring.count("add_item") == 1  # only the very first create
        assert bring.count("update_spec") == 0
        assert bring.count("remove_item") == 0
        assert bring.items[uuid].completed is True  # stays done
        assert mealie.items[m.id].checked is True  # stays checked

    async def test_reactivate_unchecks_mealie(self, cycle, db, mealie, bring):
        m = mealie.seed(note="Milk", quantity=1)
        await cycle()
        uuid = active_rows(db)[0].bring_uuid
        bring.complete(uuid)
        await cycle()
        assert mealie.items[m.id].checked is True

        bring.reactivate(uuid)
        await cycle()

        assert mealie.items[m.id].checked is False

    async def test_complete_deletes_in_delete_mode(self, cycle, db, mealie, bring, monkeypatch):
        monkeypatch.setattr(settings, "on_complete", "delete")
        m = mealie.seed(note="Milk", quantity=1)
        await cycle()
        uuid = active_rows(db)[0].bring_uuid

        bring.complete(uuid)
        await cycle()

        assert mealie.count("delete_item") == 1
        assert m.id not in mealie.items
        (row,) = rows(db)
        assert row.deleted_at is not None


class TestRemoval:
    async def test_mealie_removed_removes_bring(self, cycle, db, mealie, bring):
        m = mealie.seed(note="Milk", quantity=1)
        await cycle()

        mealie.remove(m.id)
        await cycle()

        assert bring.count("remove_item") == 1
        assert bring.items == {}
        (row,) = rows(db)
        assert row.deleted_at is not None

    async def test_bring_removed_checks_mealie_and_tombstones(self, cycle, db, mealie, bring):
        m = mealie.seed(note="Milk", quantity=1)
        await cycle()
        uuid = active_rows(db)[0].bring_uuid

        bring.remove(uuid)
        await cycle()

        assert mealie.items[m.id].checked is True
        (row,) = rows(db)
        assert row.deleted_at is not None

    async def test_tombstone_not_resurrected(self, cycle, db, mealie, bring):
        m = mealie.seed(note="Milk", quantity=1)
        await cycle()
        uuid = active_rows(db)[0].bring_uuid
        bring.remove(uuid)
        await cycle()  # → mealie checked + tombstone

        # The checked Mealie item is still present; it must not be re-mirrored.
        for _ in range(3):
            await cycle()

        assert bring.count("add_item") == 1  # only the original create
        assert bring.items == {}


class TestFreshnessDebounce:
    async def test_recent_item_is_skipped_then_synced(self, cycle, db, mealie, bring):
        from dataclasses import replace

        m = mealie.seed(note="Milk", quantity=1)
        mealie.items[m.id] = replace(m, updated_at=utcnow())  # just edited
        await cycle()

        assert bring.count("add_item") == 0
        assert rows(db) == []

        mealie.items[m.id] = replace(mealie.items[m.id], updated_at=None)
        await cycle()

        assert bring.count("add_item") == 1
        assert len(active_rows(db)) == 1
