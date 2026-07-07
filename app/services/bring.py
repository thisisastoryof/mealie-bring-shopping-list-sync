"""Async Bring! client — thin wrapper around the unofficial ``bring-api`` library.

``bring-api`` is the same reverse-engineered client used by the Home Assistant
Bring integration (DESIGN.md §3). Bring has no persistent "completed" state:
completing an item moves it from the ``purchase`` bucket to ``recently``; the
reconciler treats that bucket transition as a completion.

NOTE: ``bring-api`` method/return shapes vary between major versions. This
wrapper is written against the 1.x API; if you pin a different version, adjust
the attribute access in ``_to_items`` and the ``batch_update_list`` calls.
"""
from dataclasses import dataclass
from uuid import uuid4

import aiohttp
from bring_api import Bring

from app.config import settings
from app.logging_config import get_logger
from app.utils import normalize_name, stable_hash

log = get_logger(__name__)


@dataclass
class BringItem:
    uuid: str
    name: str            # Bring itemId (the food name)
    spec: str            # free-text qty/detail
    completed: bool      # True if currently in the 'recently' bucket

    @property
    def norm_key(self) -> str:
        return normalize_name(self.name)

    def content_hash(self) -> str:
        return stable_hash({"completed": self.completed, "spec": self.spec})


class BringClient:
    def __init__(self) -> None:
        self._session: aiohttp.ClientSession | None = None
        self._bring: Bring | None = None
        self._list_uuid: str | None = None

    async def connect(self) -> None:
        self._session = aiohttp.ClientSession()
        self._bring = Bring(self._session, settings.bring_email, settings.bring_password)
        await self._bring.login()
        await self._resolve_list()
        log.info("bring.connected", list_name=settings.bring_list_name, list_uuid=self._list_uuid)

    async def close(self) -> None:
        if self._session:
            await self._session.close()

    async def _resolve_list(self) -> None:
        assert self._bring is not None
        lists = await self._bring.load_lists()
        for lst in lists.lists:
            if lst.name == settings.bring_list_name:
                self._list_uuid = lst.listUuid
                return
        raise RuntimeError(f"Bring list '{settings.bring_list_name}' not found")

    async def fetch_items(self) -> list[BringItem]:
        assert self._bring is not None and self._list_uuid is not None
        data = await self._bring.get_list(self._list_uuid)
        items: list[BringItem] = []
        items.extend(_to_items(data.items.purchase, completed=False))
        items.extend(_to_items(data.items.recently, completed=True))
        return items

    async def add_item(self, *, name: str, spec: str) -> str:
        """Create an item with a client-generated uuid. Returns the uuid."""
        assert self._bring is not None and self._list_uuid is not None
        item_uuid = str(uuid4())
        await self._bring.save_item(self._list_uuid, name, spec, item_uuid)
        return item_uuid

    async def update_spec(self, *, name: str, spec: str, item_uuid: str) -> None:
        """Update an existing item's spec only — never rename (DESIGN.md §6)."""
        assert self._bring is not None and self._list_uuid is not None
        await self._bring.save_item(self._list_uuid, name, spec, item_uuid)

    async def complete_item(self, *, name: str, item_uuid: str) -> None:
        assert self._bring is not None and self._list_uuid is not None
        await self._bring.complete_item(self._list_uuid, name, item_uuid=item_uuid)

    async def remove_item(self, *, name: str, item_uuid: str) -> None:
        assert self._bring is not None and self._list_uuid is not None
        await self._bring.remove_item(self._list_uuid, name, item_uuid=item_uuid)


def _to_items(bucket, *, completed: bool) -> list[BringItem]:
    result: list[BringItem] = []
    for raw in bucket:
        result.append(
            BringItem(
                uuid=getattr(raw, "uuid", "") or "",
                name=getattr(raw, "itemId", "") or "",
                spec=getattr(raw, "specification", "") or "",
                completed=completed,
            )
        )
    return result
