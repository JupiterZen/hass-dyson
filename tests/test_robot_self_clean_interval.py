"""Tests for the robot vacuum self-clean interval select entity.

Maps the MyDyson app's single "Zelfreinigingsinterval" setting (4 labeled
options) onto the underlying backWashType/backWashTime MQTT fields.
VERIFIED 1 sep 2026: cycling through every app option during a probe
capture (run-6-probe.log) confirmed both fields are always sent together
in one STATE-SET, and the robot's next CURRENT-STATE reflected each
change immediately.
"""

from unittest.mock import AsyncMock, MagicMock, Mock

import pytest

from custom_components.hass_dyson.const import CONF_HOSTNAME
from custom_components.hass_dyson.select import (
    DysonRobotSelfCleanIntervalSelect,
    async_setup_entry as select_setup_entry,
)


@pytest.fixture
def mock_coordinator():
    """Create a mock coordinator for an RB05 robot reporting backWashType."""
    coordinator = Mock()
    coordinator.serial_number = "RB05-EU-TST0000A"
    coordinator.device = Mock()
    coordinator.device.robot_self_clean_interval = "Elke 15 min"
    coordinator.device_capabilities = []
    coordinator.device_category = ["robot"]
    coordinator.config_entry = Mock()
    coordinator.config_entry.data = {"device_name": "Spot+Scrub"}
    coordinator.data = {"backWashType": "TIME", "backWashTime": 15}
    return coordinator


@pytest.fixture
def mock_hass(mock_coordinator):
    hass = Mock()
    hass.data = {"hass_dyson": {mock_coordinator.serial_number: mock_coordinator}}
    return hass


@pytest.fixture
def mock_config_entry(mock_coordinator):
    entry = Mock()
    entry.data = {CONF_HOSTNAME: "192.168.1.50", "connection_type": "local"}
    entry.unique_id = mock_coordinator.serial_number
    entry.entry_id = mock_coordinator.serial_number
    return entry


class TestSelfCleanIntervalCreation:
    @pytest.mark.asyncio
    async def test_created_when_back_wash_type_reported(
        self, mock_hass, mock_config_entry, mock_coordinator
    ):
        mock_add = MagicMock()
        await select_setup_entry(mock_hass, mock_config_entry, mock_add)
        entities = mock_add.call_args[0][0]
        assert any(isinstance(e, DysonRobotSelfCleanIntervalSelect) for e in entities)

    @pytest.mark.asyncio
    async def test_not_created_when_back_wash_type_absent(
        self, mock_hass, mock_config_entry, mock_coordinator
    ):
        mock_coordinator.data = {}
        mock_add = MagicMock()
        await select_setup_entry(mock_hass, mock_config_entry, mock_add)
        entities = mock_add.call_args[0][0]
        assert not any(
            isinstance(e, DysonRobotSelfCleanIntervalSelect) for e in entities
        )

    @pytest.mark.asyncio
    async def test_not_created_for_non_robot_category(
        self, mock_hass, mock_config_entry, mock_coordinator
    ):
        mock_coordinator.device_category = ["ec"]
        mock_add = MagicMock()
        await select_setup_entry(mock_hass, mock_config_entry, mock_add)
        entities = mock_add.call_args[0][0]
        assert not any(
            isinstance(e, DysonRobotSelfCleanIntervalSelect) for e in entities
        )


class TestSelfCleanIntervalEntity:
    def test_unique_id(self, mock_coordinator):
        entity = DysonRobotSelfCleanIntervalSelect(mock_coordinator)
        assert entity._attr_unique_id == "RB05-EU-TST0000A_robot_self_clean_interval"

    def test_translation_key(self, mock_coordinator):
        entity = DysonRobotSelfCleanIntervalSelect(mock_coordinator)
        assert entity._attr_translation_key == "robot_self_clean_interval"

    def test_options_match_the_four_app_labels(self, mock_coordinator):
        entity = DysonRobotSelfCleanIntervalSelect(mock_coordinator)
        assert entity._attr_options == [
            "Na elke kamer",
            "Elke 15 min",
            "Elke 30 min",
            "Alleen indien nodig",
        ]

    def test_current_option_reflects_device_state(self, mock_coordinator):
        entity = DysonRobotSelfCleanIntervalSelect(mock_coordinator)
        assert entity._attr_current_option == "Elke 15 min"

    def test_current_option_none_when_no_device(self, mock_coordinator):
        mock_coordinator.device = None
        entity = DysonRobotSelfCleanIntervalSelect(mock_coordinator)
        assert entity._attr_current_option is None

    def test_handle_update_reflects_new_state(self, mock_coordinator):
        entity = DysonRobotSelfCleanIntervalSelect(mock_coordinator)
        entity.async_write_ha_state = MagicMock()
        mock_coordinator.device.robot_self_clean_interval = "Na elke kamer"
        entity._handle_coordinator_update()
        assert entity._attr_current_option == "Na elke kamer"

    @pytest.mark.asyncio
    async def test_select_option_calls_set_self_clean_interval(self, mock_coordinator):
        mock_coordinator.device.set_robot_self_clean_interval = AsyncMock()
        entity = DysonRobotSelfCleanIntervalSelect(mock_coordinator)
        await entity.async_select_option("Elke 30 min")
        mock_coordinator.device.set_robot_self_clean_interval.assert_awaited_once_with(
            "Elke 30 min"
        )

    @pytest.mark.asyncio
    async def test_no_command_when_device_none(self, mock_coordinator):
        mock_coordinator.device = None
        entity = DysonRobotSelfCleanIntervalSelect(mock_coordinator)
        await entity.async_select_option("Elke 30 min")  # should not raise

    @pytest.mark.asyncio
    async def test_value_error_logged_not_raised(self, mock_coordinator):
        mock_coordinator.device.set_robot_self_clean_interval = AsyncMock(
            side_effect=ValueError("Unknown self-clean interval option: 'x'")
        )
        entity = DysonRobotSelfCleanIntervalSelect(mock_coordinator)
        await entity.async_select_option("x")  # should not raise
