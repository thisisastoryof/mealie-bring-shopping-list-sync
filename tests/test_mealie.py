"""Unit tests for the Mealie client mapping and the set_checked read-back."""
import json

import httpx
import pytest

from app.services import mealie as mealie_mod
from app.services.mealie import MealieClient, MealieItem, _to_item, _unit_label


class TestToItem:
    def test_food_item_mapping(self):
        raw = {
            "id": "abc",
            "display": "2 cups Flour",
            "quantity": 2,
            "unit": {"name": "cup", "pluralName": "cups"},
            "note": "sifted",
            "food": {"name": "Flour"},
            "checked": False,
            "updatedAt": "2026-01-01T10:00:00Z",
        }
        item = _to_item(raw)
        assert item.id == "abc"
        assert item.food == "Flour"
        assert item.note == "sifted"
        assert item.unit == "cups"  # plural because quantity > 1
        assert item.checked is False
        assert item.updated_at is not None

    def test_note_item_has_no_food(self):
        item = _to_item({"id": "x", "note": "Paper towels", "quantity": 1, "checked": True})
        assert item.food is None
        assert item.norm_key == "paper towels"
        assert item.checked is True


class TestUnitLabel:
    def test_plural_when_quantity_gt_one(self):
        unit = {"name": "cup", "pluralName": "cups"}
        assert _unit_label(unit, 2) == "cups"
        assert _unit_label(unit, 1) == "cup"

    def test_abbreviation_preferred(self):
        unit = {"name": "gram", "abbreviation": "g", "useAbbreviation": True}
        assert _unit_label(unit, 100) == "g"

    def test_plain_string_unit(self):
        assert _unit_label("pinch", 1) == "pinch"


class TestSpec:
    def test_note_item_spec_omits_name(self):
        # A note item's note IS its name, so the spec carries only quantity/unit.
        item = MealieItem(id="1", display="Eier", quantity=10, unit=None,
                          note="Eier", food=None, checked=False, updated_at=None)
        assert item.spec() == "10"

    def test_food_item_spec_includes_note(self):
        item = MealieItem(id="1", display="Flour", quantity=2, unit="cups",
                          note="sifted", food="Flour", checked=False, updated_at=None)
        assert item.spec() == "2 cups sifted"

    def test_integer_quantity_has_no_decimal(self):
        item = MealieItem(id="1", display="x", quantity=3.0, unit=None,
                          note=None, food="x", checked=False, updated_at=None)
        assert item.spec() == "3"


class _MealieServer:
    """Minimal stateful Mealie stand-in for MockTransport."""

    def __init__(self, item):
        self.item = item
        self.puts = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path.endswith("/items/x"):
            return httpx.Response(200, json=self.item)
        if request.method == "PUT" and path.endswith("/items/x"):
            self.puts += 1
            self.item = {**self.item, **json.loads(request.content)}
            return httpx.Response(200, json=self.item)
        if request.method == "POST" and path.endswith("/shopping/items"):
            return httpx.Response(
                201, json={"createdItems": [], "updatedItems": [self.item]}
            )
        return httpx.Response(404, json={})


@pytest.fixture
def mealie_server():
    return _MealieServer(
        {
            "id": "x",
            "shoppingListId": "list-1",
            "checked": False,
            "quantity": 2,
            "note": "Milk",
            "foodId": None,
            "unitId": None,
            "labelId": None,
            "position": 0,
            "display": "Milk",
        }
    )


async def _client(server):
    transport = httpx.MockTransport(server.handler)
    return httpx.AsyncClient(transport=transport)


class TestSetChecked:
    async def test_reads_back_persisted_state(self, mealie_server):
        async with await _client(mealie_server) as http:
            client = MealieClient(http)
            result = await client.set_checked("x", True)
        assert mealie_server.puts == 1
        assert isinstance(result, MealieItem)
        assert result.checked is True  # observed from the read-back GET

    async def test_uncheck_reads_back(self, mealie_server):
        mealie_server.item["checked"] = True
        async with await _client(mealie_server) as http:
            client = MealieClient(http)
            result = await client.set_checked("x", False)
        assert result.checked is False


class TestCreateItemMerge:
    async def test_uses_updated_items_when_created_empty(self, mealie_server):
        # Mealie dedupes on create and returns the merge under updatedItems.
        async with await _client(mealie_server) as http:
            client = MealieClient(http)
            item = await client.create_item(note="Milk", quantity=2)
        assert item.id == "x"
        assert item.note == "Milk"
