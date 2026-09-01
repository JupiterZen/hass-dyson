"""Tests for the robot vacuum mop air-dry duration number entity.

airDryFrequency is misleadingly named — confirmed by the user against the
app's own UI ("Droogtijd met roterende dweilborstel: 3 uur") to be a
duration in hours, not a frequency. Write path VERIFIED 1 sep 2026 probe
capture (run-6-probe.log): values 3/4/5 sent via STATE-SET while the app
setting was changed, robot's next CURRENT-STATE reflected each change.
"""

from unittest.mock import AsyncMock, MagicMock, Mock

import pytest

from custom_components.hass_dyson.const import CONF_HOSTNAME
from custom_components.hass_dyson.number import (
    DysonRobotAirDryFrequencyNumber,
    DysonRobotVolumeNumber,
    async_setup_entry as number_setup_entry,
)


@pytest.fixture
def mock_coordinator():
    """Create a mock coordinator for an RB05 robot reporting airDryFrequency/volume."""
    coordinator = Mock()
    coordinator.serial_number = "RB05-EU-TST0000A"
    coordinator.device = Mock()
    coordinator.device.robot_air_dry_frequency = 4
    coordinator.device.robot_volume = 40
    coordinator.device_capabilities = []
    coordinator.device_category = ["robot"]
    coordinator.config_entry = Mock()
    coordinator.config_entry.data = {"device_name": "Spot+Scrub"}
    coordinator.data = {"airDryFrequency": 4, "volume": 40}
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


class TestAirDryFrequencyCreation:
    @pytest.mark.asyncio
    async def test_created_when_reported(
        self, mock_hass, mock_config_entry, mock_coordinator
    ):
        mock_add = MagicMock()
        await number_setup_entry(mock_hass, mock_config_entry, mock_add)
        entities = mock_add.call_args[0][0]
        assert any(isinstance(e, DysonRobotAirDryFrequencyNumber) for e in entities)

    @pytest.mark.asyncio
    async def test_not_created_when_absent(
        self, mock_hass, mock_config_entry, mock_coordinator
    ):
        mock_coordinator.data = {}
        mock_add = MagicMock()
        await number_setup_entry(mock_hass, mock_config_entry, mock_add)
        entities = mock_add.call_args[0][0]
        assert not any(isinstance(e, DysonRobotAirDryFrequencyNumber) for e in entities)

    @pytest.mark.asyncio
    async def test_not_created_for_non_robot_category(
        self, mock_hass, mock_config_entry, mock_coordinator
    ):
        mock_coordinator.device_category = ["ec"]
        mock_add = MagicMock()
        await number_setup_entry(mock_hass, mock_config_entry, mock_add)
        entities = mock_add.call_args[0][0]
        assert not any(isinstance(e, DysonRobotAirDryFrequencyNumber) for e in entities)


class TestAirDryFrequencyEntity:
    def test_unique_id(self, mock_coordinator):
        entity = DysonRobotAirDryFrequencyNumber(mock_coordinator)
        assert entity._attr_unique_id == "RB05-EU-TST0000A_robot_air_dry_frequency"

    def test_translation_key(self, mock_coordinator):
        entity = DysonRobotAirDryFrequencyNumber(mock_coordinator)
        assert entity._attr_translation_key == "robot_air_dry_frequency"

    def test_unit_is_hours(self, mock_coordinator):
        entity = DysonRobotAirDryFrequencyNumber(mock_coordinator)
        assert entity._attr_native_unit_of_measurement == "h"

    def test_native_value_reflects_device_state(self, mock_coordinator):
        entity = DysonRobotAirDryFrequencyNumber(mock_coordinator)
        assert entity._attr_native_value == 4

    def test_native_value_none_when_no_device(self, mock_coordinator):
        mock_coordinator.device = None
        entity = DysonRobotAirDryFrequencyNumber(mock_coordinator)
        assert entity._attr_native_value is None

    def test_handle_update_reflects_new_value(self, mock_coordinator):
        entity = DysonRobotAirDryFrequencyNumber(mock_coordinator)
        entity.async_write_ha_state = MagicMock()
        mock_coordinator.device.robot_air_dry_frequency = 5
        entity._handle_coordinator_update()
        assert entity._attr_native_value == 5

    @pytest.mark.asyncio
    async def test_set_native_value_calls_set_air_dry_frequency(self, mock_coordinator):
        mock_coordinator.device.set_robot_air_dry_frequency = AsyncMock()
        entity = DysonRobotAirDryFrequencyNumber(mock_coordinator)
        await entity.async_set_native_value(5)
        mock_coordinator.device.set_robot_air_dry_frequency.assert_awaited_once_with(5)

    @pytest.mark.asyncio
    async def test_no_command_when_device_none(self, mock_coordinator):
        mock_coordinator.device = None
        entity = DysonRobotAirDryFrequencyNumber(mock_coordinator)
        await entity.async_set_native_value(5)  # should not raise


class TestVolumeCreation:
    @pytest.mark.asyncio
    async def test_created_when_reported(
        self, mock_hass, mock_config_entry, mock_coordinator
    ):
        mock_add = MagicMock()
        await number_setup_entry(mock_hass, mock_config_entry, mock_add)
        entities = mock_add.call_args[0][0]
        assert any(isinstance(e, DysonRobotVolumeNumber) for e in entities)

    @pytest.mark.asyncio
    async def test_not_created_when_absent(
        self, mock_hass, mock_config_entry, mock_coordinator
    ):
        del mock_coordinator.data["volume"]
        mock_add = MagicMock()
        await number_setup_entry(mock_hass, mock_config_entry, mock_add)
        entities = mock_add.call_args[0][0]
        assert not any(isinstance(e, DysonRobotVolumeNumber) for e in entities)


class TestVolumeEntity:
    def test_unique_id(self, mock_coordinator):
        entity = DysonRobotVolumeNumber(mock_coordinator)
        assert entity._attr_unique_id == "RB05-EU-TST0000A_robot_volume"

    def test_translation_key(self, mock_coordinator):
        entity = DysonRobotVolumeNumber(mock_coordinator)
        assert entity._attr_translation_key == "robot_volume"

    def test_native_value_reflects_device_state(self, mock_coordinator):
        entity = DysonRobotVolumeNumber(mock_coordinator)
        assert entity._attr_native_value == 40

    def test_native_value_none_when_no_device(self, mock_coordinator):
        mock_coordinator.device = None
        entity = DysonRobotVolumeNumber(mock_coordinator)
        assert entity._attr_native_value is None

    def test_handle_update_reflects_new_value(self, mock_coordinator):
        entity = DysonRobotVolumeNumber(mock_coordinator)
        entity.async_write_ha_state = MagicMock()
        mock_coordinator.device.robot_volume = 60
        entity._handle_coordinator_update()
        assert entity._attr_native_value == 60

    @pytest.mark.asyncio
    async def test_set_native_value_calls_set_volume(self, mock_coordinator):
        mock_coordinator.device.set_robot_volume = AsyncMock()
        entity = DysonRobotVolumeNumber(mock_coordinator)
        await entity.async_set_native_value(60)
        mock_coordinator.device.set_robot_volume.assert_awaited_once_with(60)

    @pytest.mark.asyncio
    async def test_no_command_when_device_none(self, mock_coordinator):
        mock_coordinator.device = None
        entity = DysonRobotVolumeNumber(mock_coordinator)
        await entity.async_set_native_value(60)  # should not raise
