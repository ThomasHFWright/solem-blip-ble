"""Controller ownership and bounded cleanup recovery."""

import asyncio
from unittest.mock import AsyncMock

from bleak.exc import BleakError
import pytest

from solem_blip_ble import client_v2 as module
from solem_blip_ble.client_v2 import StatelessSolemClient
from solem_blip_ble.exceptions import SolemConnectionError


class Radio:
    """Small connected-client stand-in."""

    def __init__(self, *_args, **_kwargs):
        self.is_connected = True

    async def disconnect(self) -> None:
        self.is_connected = False


async def test_failed_cleanup_recovers_before_next_connection(monkeypatch) -> None:
    monkeypatch.setattr(module, "DISCONNECT_CLEANUP_TIMEOUT", 0.01)
    address = "AA:BB:CC:DD:EE:02"
    stuck = Radio()
    stuck.disconnect = AsyncMock(side_effect=BleakError("busy"))
    first = StatelessSolemClient(address)
    first._connect = AsyncMock(return_value=stuck)

    await first._run_operation(AsyncMock())
    assert first.cleanup_pending

    replacement = StatelessSolemClient(address)
    replacement._connect = AsyncMock(return_value=Radio())
    with pytest.raises(SolemConnectionError, match="cleanup still pending"):
        await replacement._run_operation(AsyncMock())
    replacement._connect.assert_not_awaited()

    stuck.is_connected = False
    await replacement._run_operation(AsyncMock())
    replacement._connect.assert_awaited_once()
    assert not replacement.cleanup_pending


async def test_cancelled_connect_releases_partial_client(monkeypatch) -> None:
    made: list[Radio] = []
    connected = asyncio.Event()

    class PartialRadio(Radio):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            made.append(self)

    async def establish(client_class, device, **_kwargs):
        client_class(device)
        connected.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(module, "BleakClientWithServiceCache", PartialRadio)
    monkeypatch.setattr(module, "establish_connection", establish)
    client = StatelessSolemClient(
        "AA:BB:CC:DD:EE:03", ble_device_resolver=lambda: object()
    )
    task = asyncio.create_task(client.get_status())
    await connected.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert made and not made[0].is_connected
    assert client._active_client is None and client._connecting_client is None


async def test_clients_for_one_controller_are_serialized() -> None:
    address = "AA:BB:CC:DD:EE:04"
    entered = asyncio.Event()
    release = asyncio.Event()
    first = StatelessSolemClient(address)
    second = StatelessSolemClient(address)
    first._connect = AsyncMock(return_value=Radio())
    second._connect = AsyncMock(return_value=Radio())

    async def hold(_client):
        entered.set()
        await release.wait()

    first_task = asyncio.create_task(first._run_operation(hold))
    await entered.wait()
    second_task = asyncio.create_task(second._run_operation(AsyncMock()))
    await asyncio.sleep(0)
    second._connect.assert_not_awaited()
    release.set()
    await asyncio.gather(first_task, second_task)
