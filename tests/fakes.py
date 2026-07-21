"""In-memory doubles for the Mealie and Bring clients.

These behave like tiny stateful servers: mutations are persisted so that a
subsequent ``fetch_items`` reflects them, and every write is recorded in
``calls`` for assertions. ``FakeMealie.set_checked`` returns the *persisted*
item, mirroring the real client's read-back — the property the reconciler
relies on to avoid optimistic shadow writes.
"""
from dataclasses import replace
from itertools import count

from app.services.bring import BringItem
from app.services.mealie import MealieItem
from app.services.reconciler import Reconciler


class FakeMealie:
    def __init__(self):
        self.items: dict[str, MealieItem] = {}
        self.calls: list[tuple] = []
        self._ids = count(1)

    # ── seeding helper (not part of the client API) ─────────────────
    def seed(self, *, note=None, quantity=None, food=None, unit=None, checked=False) -> MealieItem:
        item_id = f"m{next(self._ids)}"
        display = food or note or ""
        item = MealieItem(
            id=item_id, display=display, quantity=quantity, unit=unit,
            note=note, food=food, checked=checked, updated_at=None,
        )
        self.items[item_id] = item
        return item

    # ── client API used by the reconciler ───────────────────────────
    async def fetch_items(self) -> list[MealieItem]:
        return list(self.items.values())

    async def fetch_units(self) -> dict[str, str]:
        return {}

    async def resolve_food_id(self, name: str):
        return None

    async def create_item(self, *, note="", quantity=None, food_id=None, unit_id=None) -> MealieItem:
        self.calls.append(("create_item", note, quantity, food_id, unit_id))
        item = self.seed(note=note or None, quantity=quantity)
        return item

    async def set_checked(self, item_id: str, checked: bool) -> MealieItem:
        self.calls.append(("set_checked", item_id, checked))
        item = replace(self.items[item_id], checked=checked)
        self.items[item_id] = item
        return item  # read-back: the persisted item

    async def delete_item(self, item_id: str) -> None:
        self.calls.append(("delete_item", item_id))
        self.items.pop(item_id, None)

    # ── test conveniences ───────────────────────────────────────────
    def rename(self, item_id: str, *, note=None, food=None) -> None:
        cur = self.items[item_id]
        display = food or note or cur.display
        self.items[item_id] = replace(cur, note=note, food=food, display=display)

    def set_quantity(self, item_id: str, quantity) -> None:
        self.items[item_id] = replace(self.items[item_id], quantity=quantity)

    def check(self, item_id: str) -> None:
        """Simulate a user checking an item off in the Mealie UI."""
        self.items[item_id] = replace(self.items[item_id], checked=True)

    def uncheck(self, item_id: str) -> None:
        self.items[item_id] = replace(self.items[item_id], checked=False)

    def remove(self, item_id: str) -> None:
        self.items.pop(item_id, None)

    def count(self, name):
        return sum(1 for c in self.calls if c[0] == name)


class FakeBring:
    def __init__(self):
        self.items: dict[str, BringItem] = {}
        self.calls: list[tuple] = []
        self._ids = count(1)

    # ── seeding helper ──────────────────────────────────────────────
    def seed(self, *, name, spec="", completed=False) -> BringItem:
        uuid = f"b{next(self._ids)}"
        item = BringItem(uuid=uuid, name=name, spec=spec, completed=completed)
        self.items[uuid] = item
        return item

    # ── client API used by the reconciler ───────────────────────────
    async def fetch_items(self) -> list[BringItem]:
        return list(self.items.values())

    async def add_item(self, *, name: str, spec: str) -> str:
        uuid = f"b{next(self._ids)}"
        self.calls.append(("add_item", name, spec, uuid))
        self.items[uuid] = BringItem(uuid=uuid, name=name, spec=spec, completed=False)
        return uuid

    async def update_spec(self, *, name: str, spec: str, item_uuid: str) -> None:
        self.calls.append(("update_spec", name, spec, item_uuid))
        cur = self.items[item_uuid]
        # save_item re-activates an item, matching real Bring behaviour.
        self.items[item_uuid] = replace(cur, spec=spec, completed=False)

    async def complete_item(self, *, name: str, item_uuid: str) -> None:
        self.calls.append(("complete_item", name, item_uuid))
        self.items[item_uuid] = replace(self.items[item_uuid], completed=True)

    async def remove_item(self, *, name: str, item_uuid: str) -> None:
        self.calls.append(("remove_item", name, item_uuid))
        self.items.pop(item_uuid, None)

    # ── test conveniences ───────────────────────────────────────────
    def complete(self, item_uuid: str) -> None:
        """Simulate a user completing an item in the Bring app."""
        self.items[item_uuid] = replace(self.items[item_uuid], completed=True)

    def reactivate(self, item_uuid: str) -> None:
        self.items[item_uuid] = replace(self.items[item_uuid], completed=False)

    def remove(self, item_uuid: str) -> None:
        self.items.pop(item_uuid, None)

    def count(self, name):
        return sum(1 for c in self.calls if c[0] == name)


def make_reconciler(mealie: FakeMealie, bring: FakeBring) -> Reconciler:
    return Reconciler(mealie, bring)  # type: ignore[arg-type]


async def run_cycle(reconciler, db, mealie: FakeMealie, bring: FakeBring, units=None) -> None:
    mealie_items = await mealie.fetch_items()
    bring_items = await bring.fetch_items()
    await reconciler._reconcile(db, mealie_items, bring_items, units or {})
    db.commit()
