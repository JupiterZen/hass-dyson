"""Tests for the abort_dock_action / start_dock_action services.

Covers _handle_abort_dock_action and _handle_start_dock_action — the
HA-service wrappers around robot_abort_dock_action()/
robot_start_dock_action() (see dyson/robot-probe/README.md, "OPGELOST"
sections, for how these commands were reverse-engineered).
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

from custom_components.hass_dyson.services import (
    _handle_abort_dock_action,
    _handle_start_dock_action,
)

SERIAL = "7VS-EU-UNA6126A"


def _make_coordinator() -> MagicMock:
    coordinator = MagicMock()
    coordinator.serial_number = SERIAL
    coordinator.device = MagicMock()
    coordinator.device.robot_abort_dock_action = AsyncMock()
    coordinator.device.robot_start_dock_action = AsyncMock()
    return coordinator


def _call(data: dict) -> MagicMock:
    call = MagicMock()
    call.data = data
    return call


async def _run_abort(coordinator, data: dict) -> None:
    with patch(
        "custom_components.hass_dyson.services._get_coordinator_from_device_id",
        AsyncMock(return_value=coordinator),
    ):
        await _handle_abort_dock_action(MagicMock(), _call(data))


async def _run_start(coordinator, data: dict) -> None:
    with patch(
        "custom_components.hass_dyson.services._get_coordinator_from_device_id",
        AsyncMock(return_value=coordinator),
    ):
        await _handle_start_dock_action(MagicMock(), _call(data))


class TestAbortDockActionService:
    """Test _handle_abort_dock_action (dock 'Stop'/'Vertragen')."""

    @pytest.mark.asyncio
    async def test_no_delay_stops_immediately(self):
        coordinator = _make_coordinator()
        await _run_abort(coordinator, {"device_id": "dev"})

        coordinator.device.robot_abort_dock_action.assert_awaited_once_with(
            delay_minutes=None
        )

    @pytest.mark.asyncio
    async def test_with_delay_minutes(self):
        coordinator = _make_coordinator()
        await _run_abort(coordinator, {"device_id": "dev", "delay_minutes": 15})

        coordinator.device.robot_abort_dock_action.assert_awaited_once_with(
            delay_minutes=15
        )

    @pytest.mark.asyncio
    async def test_unknown_device_raises(self):
        with (
            patch(
                "custom_components.hass_dyson.services._get_coordinator_from_device_id",
                AsyncMock(return_value=None),
            ),
            pytest.raises(ServiceValidationError),
        ):
            await _handle_abort_dock_action(MagicMock(), _call({"device_id": "dev"}))

    @pytest.mark.asyncio
    async def test_device_error_wrapped(self):
        coordinator = _make_coordinator()
        coordinator.device.robot_abort_dock_action.side_effect = RuntimeError("boom")

        with pytest.raises(HomeAssistantError):
            await _run_abort(coordinator, {"device_id": "dev"})


class TestStartDockActionService:
    """Test _handle_start_dock_action (dock 'Leeg reservoir'/'Wassen en drogen')."""

    @pytest.mark.asyncio
    async def test_default_action_is_collect_dust(self):
        coordinator = _make_coordinator()
        await _run_start(coordinator, {"device_id": "dev", "action": "COLLECT_DUST"})

        coordinator.device.robot_start_dock_action.assert_awaited_once_with(
            action="COLLECT_DUST"
        )

    @pytest.mark.asyncio
    async def test_wash_mop_action(self):
        coordinator = _make_coordinator()
        await _run_start(coordinator, {"device_id": "dev", "action": "WASH_MOP"})

        coordinator.device.robot_start_dock_action.assert_awaited_once_with(
            action="WASH_MOP"
        )

    @pytest.mark.asyncio
    async def test_unknown_device_raises(self):
        with (
            patch(
                "custom_components.hass_dyson.services._get_coordinator_from_device_id",
                AsyncMock(return_value=None),
            ),
            pytest.raises(ServiceValidationError),
        ):
            await _handle_start_dock_action(
                MagicMock(), _call({"device_id": "dev", "action": "COLLECT_DUST"})
            )

    @pytest.mark.asyncio
    async def test_device_error_wrapped(self):
        coordinator = _make_coordinator()
        coordinator.device.robot_start_dock_action.side_effect = RuntimeError("boom")

        with pytest.raises(HomeAssistantError):
            await _run_start(coordinator, {"device_id": "dev", "action": "WASH_MOP"})
