"""Unit tests for the Bring client mapping and BringItem hashing."""
from types import SimpleNamespace

from app.services.bring import BringItem, _to_items


class TestToItems:
    def test_maps_fields(self):
        raw = SimpleNamespace(uuid="u1", itemId="Milk", specification="2 l")
        (item,) = _to_items([raw], completed=False)
        assert item.uuid == "u1"
        assert item.name == "Milk"
        assert item.spec == "2 l"
        assert item.completed is False

    def test_completed_flag_applied(self):
        raw = SimpleNamespace(uuid="u2", itemId="Eggs", specification="10")
        (item,) = _to_items([raw], completed=True)
        assert item.completed is True

    def test_missing_attributes_default_to_empty(self):
        raw = SimpleNamespace()
        (item,) = _to_items([raw], completed=False)
        assert item.uuid == ""
        assert item.name == ""
        assert item.spec == ""


class TestBringItem:
    def test_norm_key(self):
        item = BringItem(uuid="u", name="  Café  ", spec="", completed=False)
        assert item.norm_key == "cafe"

    def test_content_hash_stable(self):
        a = BringItem(uuid="u", name="Milk", spec="2 l", completed=False)
        b = BringItem(uuid="other", name="Milk", spec="2 l", completed=False)
        # Hash covers completed + spec only, not identity fields.
        assert a.content_hash() == b.content_hash()

    def test_content_hash_changes_on_completion(self):
        active = BringItem(uuid="u", name="Milk", spec="2 l", completed=False)
        done = BringItem(uuid="u", name="Milk", spec="2 l", completed=True)
        assert active.content_hash() != done.content_hash()

    def test_content_hash_changes_on_spec(self):
        a = BringItem(uuid="u", name="Milk", spec="1 l", completed=False)
        b = BringItem(uuid="u", name="Milk", spec="2 l", completed=False)
        assert a.content_hash() != b.content_hash()
