"""Async Mealie shopping-list client.

Talks to the official Mealie REST API with a Bearer token. Only the shopping
list endpoints are used; items expose stable UUIDs plus structured
``quantity`` / ``unit`` / ``food`` / ``note`` / ``checked`` fields, so no regex
parsing is needed (DESIGN.md §2, §3).
"""
from dataclasses import dataclass
from datetime import datetime

import httpx

from app.config import settings
from app.logging_config import get_logger
from app.utils import normalize_name

log = get_logger(__name__)


@dataclass
class MealieItem:
    id: str
    display: str          # human label used for Bring spec / matching
    quantity: float | None
    unit: str | None
    note: str | None
    food: str | None
    checked: bool
    updated_at: datetime | None

    @property
    def norm_key(self) -> str:
        return normalize_name(self.food or self.note or self.display)

    def spec(self) -> str:
        """Free-text spec pushed to Bring.

        For a **note** item (``food`` is ``None``) the note *is* the item name,
        so the spec carries only quantity/unit — otherwise the name would be
        duplicated on every round-trip ("Eier" -> "10 Eier" -> ...). For a
        **food** item the note is a real annotation and is included.
        """
        parts: list[str] = []
        if self.quantity:
            qty = int(self.quantity) if float(self.quantity).is_integer() else self.quantity
            parts.append(str(qty))
        if self.unit:
            parts.append(self.unit)
        if self.food and self.note:
            parts.append(self.note)
        return " ".join(parts).strip()


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {settings.mealie_api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _base() -> str:
    return settings.mealie_base_url.rstrip("/")


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _unit_label(unit, quantity) -> str | None:
    """Pick the unit string Mealie itself would show.

    Mealie uses the plural form when ``quantity > 1`` and prefers the
    abbreviation when the unit's ``useAbbreviation`` flag is set. Replicating
    this keeps the spec pushed to Bring consistent with the Mealie UI (e.g.
    "2 cups", not "2 cup").
    """
    if not isinstance(unit, dict):
        return unit or None
    plural = quantity is not None and float(quantity) > 1
    if unit.get("useAbbreviation"):
        label = (
            (unit.get("pluralAbbreviation") if plural else None)
            or unit.get("abbreviation")
            or (unit.get("pluralName") if plural else None)
            or unit.get("name")
        )
    else:
        label = (unit.get("pluralName") if plural else None) or unit.get("name")
    return label or None


def _to_item(raw: dict) -> MealieItem:
    food = raw.get("food") or {}
    unit = raw.get("unit") or {}
    display = raw.get("display") or food.get("name") or raw.get("note") or ""
    return MealieItem(
        id=raw["id"],
        display=display,
        quantity=raw.get("quantity"),
        unit=_unit_label(unit, raw.get("quantity")),
        note=raw.get("note") or None,
        food=(food.get("name") if isinstance(food, dict) else food) or None,
        checked=bool(raw.get("checked")),
        updated_at=_parse_dt(raw.get("updatedAt")),
    )


class MealieClient:
    def __init__(self, client: httpx.AsyncClient):
        self._client = client

    async def check_connectivity(self) -> bool:
        try:
            resp = await self._client.get(f"{_base()}/api/app/about", headers=_headers(), timeout=5)
            return resp.status_code == 200
        except httpx.HTTPError:
            return False

    async def fetch_items(self) -> list[MealieItem]:
        """Return all items on the configured shopping list."""
        url = f"{_base()}/api/households/shopping/lists/{settings.mealie_shopping_list_id}"
        resp = await self._client.get(url, headers=_headers(), timeout=30)
        resp.raise_for_status()
        data = resp.json()
        raw_items = data.get("listItems") or data.get("items") or []
        return [_to_item(i) for i in raw_items if i.get("id")]

    async def fetch_units(self) -> dict[str, str]:
        """Map normalized unit token -> ``unitId`` from Mealie's own unit list.

        Lets the reconciler resolve a Bring spec's unit (e.g. ``cup``, ``g``)
        to a real Mealie unit instead of guessing with a hardcoded list.
        """
        url = f"{_base()}/api/units"
        resp = await self._client.get(url, headers=_headers(), params={"perPage": 200}, timeout=15)
        resp.raise_for_status()
        mapping: dict[str, str] = {}
        for u in resp.json().get("items") or []:
            uid = u.get("id")
            if not uid:
                continue
            for label in (u.get("name"), u.get("pluralName"), u.get("abbreviation"), u.get("pluralAbbreviation")):
                key = normalize_name(label)
                if key:
                    mapping.setdefault(key, uid)
        return mapping

    async def create_item(
        self,
        *,
        note: str = "",
        quantity: float | None = None,
        food_id: str | None = None,
        unit_id: str | None = None,
    ) -> MealieItem:
        """Create a shopping-list item and return it as a :class:`MealieItem`.

        Returns the *server-resolved* item (built from the create response) so
        the caller can store an exact ``mealie_hash`` and avoid a spurious
        change being detected on the next cycle (round-trip prevention).
        """
        url = f"{_base()}/api/households/shopping/items"
        payload: dict = {
            "shoppingListId": settings.mealie_shopping_list_id,
            "note": note,
            "quantity": quantity if quantity is not None else 1,
            "checked": False,
        }
        if food_id:
            payload["foodId"] = food_id
        if unit_id:
            payload["unitId"] = unit_id
        resp = await self._client.post(url, headers=_headers(), json=payload, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        # Mealie dedupes on create: a posted item that matches an existing one is
        # *merged* and comes back under ``updatedItems`` (with ``createdItems``
        # empty). Map to that existing item instead of falling through to the
        # envelope, which has no ``id`` (KeyError that would kill the cycle).
        created = data.get("createdItems") or data.get("updatedItems") or []
        raw = created[0] if created else data  # fallback for older Mealie versions
        return _to_item(raw)

    async def resolve_food_id(self, name: str) -> str | None:
        """Look up a Mealie food by name; return its id or ``None`` if no exact match.

        Used by ``BRING_TO_MEALIE=food`` (DESIGN.md §5, §11). Matching is on the
        normalized name so it stays a deterministic, exact resolution — never a
        fuzzy guess.
        """
        target = normalize_name(name)
        if not target:
            return None
        url = f"{_base()}/api/foods"
        resp = await self._client.get(
            url, headers=_headers(), params={"search": name, "perPage": 50}, timeout=15
        )
        resp.raise_for_status()
        for food in resp.json().get("items") or []:
            if normalize_name(food.get("name")) == target:
                return food.get("id")
        return None

    async def set_checked(self, item_id: str, checked: bool) -> MealieItem:
        """Toggle an item's checked state and return the *persisted* item.

        ``ShoppingListItemUpdate`` requires ``shoppingListId`` and resets unset
        fields to defaults, so read the current item and merge rather than
        sending a bare ``{checked}`` (which would clobber quantity/note).

        The item is re-read after the write and returned so the reconciler can
        hash **observed** state instead of an optimistic guess — the invariant
        that stops a completed item resurrecting and the checked-state ping-pong.
        """
        url = f"{_base()}/api/households/shopping/items/{item_id}"
        current = (await self._client.get(url, headers=_headers(), timeout=15)).json()
        payload = {
            "id": item_id,
            "shoppingListId": current.get("shoppingListId", settings.mealie_shopping_list_id),
            "checked": checked,
            "quantity": current.get("quantity", 1),
            "note": current.get("note") or "",
            "isFood": current.get("foodId") is not None,
            "foodId": current.get("foodId"),
            "unitId": current.get("unitId"),
            "labelId": current.get("labelId"),
            "position": current.get("position", 0),
        }
        resp = await self._client.put(url, headers=_headers(), json=payload, timeout=15)
        resp.raise_for_status()
        fresh = await self._client.get(url, headers=_headers(), timeout=15)
        fresh.raise_for_status()
        return _to_item(fresh.json())

    async def delete_item(self, item_id: str) -> None:
        url = f"{_base()}/api/households/shopping/items/{item_id}"
        resp = await self._client.delete(url, headers=_headers(), timeout=15)
        resp.raise_for_status()
