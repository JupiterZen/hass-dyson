"""Robot initial refresh waits for the robot's first state.

Robot sensors/selects/switches are only created for keys already present at
platform setup. After the Pi reboot of 16 Oct 2026 the first CURRENT-STATE
arrived after setup, so ~60 entities were never created.
"""

import logging
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest

from custom_components.hass_dyson.const import MQTT_CMD_REQUEST_CURRENT_STATE
from custom_components.hass_dyson.coordinator import DysonDataUpdateCoordinator


def _coordinator(category: list[str]) -> DysonDataUpdateCoordinator:
    with patch(
        "custom_components.hass_dyson.coordinator.DataUpdateCoordinator.__init__"
    ):
        coordinator = DysonDataUpdateCoordinator(MagicMock(), MagicMock())
    coordinator.hass = MagicMock()
    coordinator.config_entry = MagicMock()
    coordinator._serial_number = "RB05-EU-TST0000A"
    coordinator.device = MagicMock()
    coordinator.device.is_connected = True
    coordinator.device.send_command = AsyncMock()
    coordinator.device.get_state = AsyncMock(return_value={})
    coordinator.device.request_current_faults = AsyncMock()
    coordinator.device.get_environmental_data = MagicMock(return_value={})
    coordinator.async_update_listeners = MagicMock()
    coordinator.data = None
    coordinator._device_capabilities = []
    coordinator._device_category = category
    return coordinator


@pytest.mark.asyncio
async def test_returns_as_soon_as_state_arrives():
    coordinator = _coordinator(["robot"])
    type(coordinator.device).robot_state = PropertyMock(
        side_effect=[None, None, "INACTIVE_CHARGED", "INACTIVE_CHARGED"]
    )
    sleep = AsyncMock()
    with patch("custom_components.hass_dyson.coordinator.asyncio.sleep", sleep):
        await coordinator._async_wait_for_robot_first_state()

    coordinator.device.send_command.assert_awaited_once_with(
        MQTT_CMD_REQUEST_CURRENT_STATE
    )
    assert sleep.await_count == 2


@pytest.mark.asyncio
async def test_gives_up_after_timeout_and_warns(caplog):
    coordinator = _coordinator(["robot"])
    coordinator.device.robot_state = None
    with (
        patch(
            "custom_components.hass_dyson.coordinator.ROBOT_FIRST_STATE_TIMEOUT", 0.2
        ),
        patch(
            "custom_components.hass_dyson.coordinator.asyncio.sleep",
            AsyncMock(),
        ),
        patch(
            "custom_components.hass_dyson.coordinator.asyncio.get_running_loop"
        ) as get_loop,
        caplog.at_level(logging.WARNING),
    ):
        times = iter([0.0, 0.0, 0.1, 0.3])
        get_loop.return_value.time = lambda: next(times)
        await coordinator._async_wait_for_robot_first_state()

    assert "sent no state within" in caplog.text


@pytest.mark.asyncio
async def test_initial_refresh_waits_for_robots_only():
    robot = _coordinator(["robot"])
    purifier = _coordinator(["ec"])
    for coordinator in (robot, purifier):
        with (
            patch.object(
                coordinator,
                "_async_wait_for_robot_first_state",
                new_callable=AsyncMock,
            ) as wait,
            patch.object(
                coordinator, "ensure_device_services_registered", new_callable=AsyncMock
            ),
        ):
            await coordinator._async_update_data()
        assert wait.await_count == (1 if coordinator is robot else 0)
