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
        """Free-text spec pushed to Bring: '{qty} {unit} {note}'."""
        parts: list[str] = []
        if self.quantity:
            qty = int(self.quantity) if float(self.quantity).is_integer() else self.quantity
            parts.append(str(qty))
        if self.unit:
            parts.append(self.unit)
        if self.note:
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


def _to_item(raw: dict) -> MealieItem:
    food = raw.get("food") or {}
    unit = raw.get("unit") or {}
    display = raw.get("display") or food.get("name") or raw.get("note") or ""
    return MealieItem(
        id=raw["id"],
        display=display,
        quantity=raw.get("quantity"),
        unit=(unit.get("name") if isinstance(unit, dict) else unit) or None,
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

    async def add_item(self, *, note: str, quantity: float | None = None) -> str:
        """Create a plain note item on the list. Returns the new Mealie item id.

        The create endpoint returns a ``ShoppingListItemsCollectionOut``
        (``createdItems``/``updatedItems``/``deletedItems``), not a single item.
        """
        url = f"{_base()}/api/households/shopping/items"
        payload: dict = {
            "shoppingListId": settings.mealie_shopping_list_id,
            "note": note,
            "isFood": False,
            "checked": False,
        }
        if quantity is not None:
            payload["quantity"] = quantity
        resp = await self._client.post(url, headers=_headers(), json=payload, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        created = data.get("createdItems") or []
        if created:
            return created[0]["id"]
        return data.get("id", "")  # fallback for older Mealie versions

    async def set_checked(self, item_id: str, checked: bool) -> None:
        """Toggle an item's checked state.

        ``ShoppingListItemUpdate`` requires ``shoppingListId`` and resets unset
        fields to defaults, so read the current item and merge rather than
        sending a bare ``{checked}`` (which would clobber quantity/note).
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

    async def delete_item(self, item_id: str) -> None:
        url = f"{_base()}/api/households/shopping/items/{item_id}"
        resp = await self._client.delete(url, headers=_headers(), timeout=15)
        resp.raise_for_status()
