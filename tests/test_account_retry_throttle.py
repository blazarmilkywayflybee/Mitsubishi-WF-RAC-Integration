"""Tests for the account re-registration throttle in Device.update()."""

from datetime import datetime, timedelta

import pytest
from aiohttp import ClientConnectionError
from homeassistant.core import HomeAssistant

from custom_components.mitsubishi_wf_rac.wfrac import device as device_module
from custom_components.mitsubishi_wf_rac.wfrac import repository as repository_module
from custom_components.mitsubishi_wf_rac.wfrac.device import (
    _ACCOUNT_RETRY_COOLDOWN,
    Device,
)
from custom_components.mitsubishi_wf_rac.wfrac.models.aircon import Aircon, AirconStat
from custom_components.mitsubishi_wf_rac.wfrac.rac_parser import RacParser

HOST = "192.168.1.99"
PORT = 51443
BASE = f"http://{HOST}:{PORT}/beaver/command"
STAT_URL = f"{BASE}/getAirconStat"
ACCOUNT_URL = f"{BASE}/updateAccountInfo"

POLL_INTERVAL = 60.0


def _valid_aircon_stat() -> str:
    """A payload the real parser accepts, built with the real encoder."""
    return RacParser().to_base64(AirconStat(Aircon()))


def _stat_response() -> dict:
    return {
        "result": 1,
        "contents": {
            "numOfAccount": "1",
            "firmType": "WF-RAC",
            "mcu": {"firmVer": "200"},
            "wireless": {"firmVer": "025"},
            "airconStat": _valid_aircon_stat(),
        },
    }


class FakeClock:
    """Controllable stand-in for the module's monotonic source."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture(name="clock")
def clock_fixture(monkeypatch) -> FakeClock:
    """Patch only the device module's monotonic reference.

    Patching time.monotonic globally would also move the asyncio event
    loop's clock and deadlock the test.
    """
    clock = FakeClock()
    monkeypatch.setattr(device_module, "monotonic", clock)
    return clock


@pytest.fixture(name="no_request_spacing", autouse=True)
def no_request_spacing_fixture(monkeypatch) -> None:
    """Drop the inter-request delay so tests aren't paced in real seconds."""
    monkeypatch.setattr(
        repository_module, "_MIN_TIME_BETWEEN_REQUESTS", timedelta(0)
    )


@pytest.fixture(name="device")
async def device_fixture(hass: HomeAssistant, aioclient_mock) -> Device:
    """A Device pointed at a fixed host, with protocol discovery skipped."""
    device = Device(
        hass,
        "Test Airco",
        HOST,
        PORT,
        "device-id",
        "operator-id",
        "airco-id",
        availability_retry=False,
        availability_retry_limit=3,
        create_swing_mode_select=True,
    )
    device._api._method = "http"
    return device


def account_posts(aioclient_mock) -> int:
    """How many updateAccountInfo requests actually went out."""
    return sum(
        1 for call in aioclient_mock.mock_calls if "updateAccountInfo" in str(call[1])
    )


def set_unreachable(aioclient_mock) -> None:
    aioclient_mock.clear_requests()
    aioclient_mock.post(STAT_URL, exc=ClientConnectionError())
    aioclient_mock.post(ACCOUNT_URL, exc=ClientConnectionError())


def set_reachable(aioclient_mock) -> None:
    aioclient_mock.clear_requests()
    aioclient_mock.post(STAT_URL, json=_stat_response())
    aioclient_mock.post(ACCOUNT_URL, json={"result": 1})


async def test_sustained_outage_throttles_reregistration(
    device: Device, clock: FakeClock, aioclient_mock
) -> None:
    """A long outage must not re-register on every failed poll."""
    set_unreachable(aioclient_mock)

    polls = 10
    for poll in range(polls):
        if poll:
            clock.advance(POLL_INTERVAL)
        await device.update()

    last_poll_at = POLL_INTERVAL * (polls - 1)
    expected = 1 + int(last_poll_at // _ACCOUNT_RETRY_COOLDOWN)

    assert expected == 2
    assert account_posts(aioclient_mock) == expected
    assert device.available is False


async def test_healthy_poll_marks_available(
    device: Device, aioclient_mock
) -> None:
    """Baseline: a well-formed response parses and clears the cooldown."""
    set_reachable(aioclient_mock)

    await device.update()

    assert device.available is True
    assert device._last_account_retry is None


async def test_first_failure_after_success_reregisters_immediately(
    device: Device, clock: FakeClock, aioclient_mock
) -> None:
    """Eviction recovery must not have to wait out a stale cooldown."""
    set_unreachable(aioclient_mock)
    await device.update()
    assert account_posts(aioclient_mock) == 1

    set_reachable(aioclient_mock)
    clock.advance(POLL_INTERVAL)
    await device.update()
    assert device.available is True

    set_unreachable(aioclient_mock)
    clock.advance(POLL_INTERVAL)
    await device.update()

    assert account_posts(aioclient_mock) == 1, (
        "first failure after a healthy poll must re-register at once"
    )


async def test_reached_device_clears_cooldown_even_if_payload_unparseable(
    device: Device, clock: FakeClock, aioclient_mock
) -> None:
    """Reaching the device proves we aren't evicted, parse outcome aside."""
    set_unreachable(aioclient_mock)
    await device.update()
    assert device._last_account_retry is not None

    aioclient_mock.clear_requests()
    aioclient_mock.post(STAT_URL, json={"result": 1, "contents": {"junk": True}})
    clock.advance(POLL_INTERVAL)
    await device.update()

    assert device._last_account_retry is None
    assert device.available is False


async def test_cooldown_boundary(
    device: Device, clock: FakeClock, aioclient_mock
) -> None:
    """Suppressed right up to the cooldown, allowed exactly on it."""
    set_unreachable(aioclient_mock)

    await device.update()
    assert account_posts(aioclient_mock) == 1

    clock.advance(_ACCOUNT_RETRY_COOLDOWN - 1)
    await device.update()
    assert account_posts(aioclient_mock) == 1

    clock.advance(1)
    await device.update()
    assert account_posts(aioclient_mock) == 2


async def test_throttle_unaffected_by_wall_clock_going_backwards(
    device: Device, clock: FakeClock, aioclient_mock, freezer
) -> None:
    """A backwards wall-clock step must not stall re-registration.

    Regression guard: a naive datetime.now() comparison goes negative on an
    NTP step backwards or a DST fall-back, which suppresses re-registration
    for the length of the jump.

    Repository paces its own requests off datetime.now(), which is a separate
    concern from the throttle under test, so its next-request marker is
    re-based after each jump to keep this test focused.
    """
    freezer.move_to("2026-04-05T03:00:00+00:00")
    device._api._next_request_after = datetime.now()
    set_unreachable(aioclient_mock)

    await device.update()
    assert account_posts(aioclient_mock) == 1

    freezer.move_to("2026-04-05T02:00:00+00:00")
    device._api._next_request_after = datetime.now()
    clock.advance(_ACCOUNT_RETRY_COOLDOWN)
    await device.update()

    assert account_posts(aioclient_mock) == 2, (
        "elapsed time must come from a monotonic source, not the wall clock"
    )
