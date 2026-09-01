"""Tests for robot vacuum switch entities (child lock, wash-mop-before-clean,
do-not-disturb, hot-water self-clean).

childLock/washMopBeforeClean's write path is still unverified — neither
probe capture ever recorded an app-initiated write to them, only to
doNotDisturbMode/backWashType/hotWaterSwitch/airDryFrequency (see
device.py docstrings for the 1 sep 2026 verification details). The
STATE-SET envelope shape carries over from the verified fields, but not
field-specific confirmation for those two.
"""

from unittest.mock import AsyncMock, MagicMock, Mock

import pytest

from custom_components.hass_dyson.const import CONF_HOSTNAME
from custom_components.hass_dyson.switch import (
    DysonRobotChildLockSwitch,
    DysonRobotDoNotDisturbSwitch,
    DysonRobotHotWaterSwitchSwitch,
    DysonRobotWashMopBeforeCleanSwitch,
    async_setup_entry as switch_setup_entry,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_coordinator():
    """Create a mock coordinator for an RB05 robot with all fields reported."""
    coordinator = Mock()
    coordinator.serial_number = "RB05-EU-TST0000A"
    coordinator.device_name = "Spot+Scrub Ai"
    coordinator.device = Mock()
    coordinator.device.robot_child_lock = False
    coordinator.device.robot_wash_mop_before_clean = True
    coordinator.device.robot_do_not_disturb = {
        "isOn": False,
        "startTime": "22:00",
        "endTime": "8:00",
    }
    coordinator.device.robot_hot_water_switch = True
    coordinator.device_capabilities = []
    coordinator.device_category = ["robot"]
    coordinator.config_entry = Mock()
    coordinator.config_entry.data = {"device_name": "Spot+Scrub"}
    coordinator.data = {
        "childLock": False,
        "washMopBeforeClean": True,
        "doNotDisturbMode": {"isOn": False, "startTime": "22:00", "endTime": "8:00"},
        "hotWaterSwitch": True,
    }
    return coordinator


@pytest.fixture
def mock_hass(mock_coordinator):
    """Create a mock Home Assistant instance."""
    hass = Mock()
    hass.data = {"hass_dyson": {mock_coordinator.serial_number: mock_coordinator}}
    return hass


@pytest.fixture
def mock_config_entry(mock_coordinator):
    """Create a mock config entry."""
    entry = Mock()
    entry.data = {CONF_HOSTNAME: "192.168.1.50", "connection_type": "local"}
    entry.unique_id = mock_coordinator.serial_number
    entry.entry_id = mock_coordinator.serial_number
    return entry


# ---------------------------------------------------------------------------
# Entity creation gating
# ---------------------------------------------------------------------------


class TestRobotSwitchCreation:
    """Tests for conditional switch entity creation."""

    @pytest.mark.asyncio
    async def test_all_created_when_fields_reported(
        self, mock_hass, mock_config_entry, mock_coordinator
    ):
        mock_add = MagicMock()
        await switch_setup_entry(mock_hass, mock_config_entry, mock_add)
        entities = mock_add.call_args[0][0]
        assert any(isinstance(e, DysonRobotChildLockSwitch) for e in entities)
        assert any(isinstance(e, DysonRobotWashMopBeforeCleanSwitch) for e in entities)
        assert any(isinstance(e, DysonRobotDoNotDisturbSwitch) for e in entities)
        assert any(isinstance(e, DysonRobotHotWaterSwitchSwitch) for e in entities)

    @pytest.mark.asyncio
    async def test_none_created_for_non_robot_category(
        self, mock_hass, mock_config_entry, mock_coordinator
    ):
        """A non-robot device (e.g. air purifier) never gets robot switches."""
        mock_coordinator.device_category = ["ec"]
        mock_add = MagicMock()
        await switch_setup_entry(mock_hass, mock_config_entry, mock_add)
        entities = mock_add.call_args[0][0]
        assert not any(isinstance(e, DysonRobotChildLockSwitch) for e in entities)
        assert not any(
            isinstance(e, DysonRobotWashMopBeforeCleanSwitch) for e in entities
        )
        assert not any(isinstance(e, DysonRobotDoNotDisturbSwitch) for e in entities)
        assert not any(isinstance(e, DysonRobotHotWaterSwitchSwitch) for e in entities)

    @pytest.mark.asyncio
    async def test_hot_water_switch_not_created_when_absent(
        self, mock_hass, mock_config_entry, mock_coordinator
    ):
        del mock_coordinator.data["hotWaterSwitch"]
        mock_add = MagicMock()
        await switch_setup_entry(mock_hass, mock_config_entry, mock_add)
        entities = mock_add.call_args[0][0]
        assert not any(isinstance(e, DysonRobotHotWaterSwitchSwitch) for e in entities)

    @pytest.mark.asyncio
    async def test_child_lock_not_created_when_absent(
        self, mock_hass, mock_config_entry, mock_coordinator
    ):
        """A robot without child-lock hardware never sends childLock — skip it."""
        del mock_coordinator.data["childLock"]
        mock_add = MagicMock()
        await switch_setup_entry(mock_hass, mock_config_entry, mock_add)
        entities = mock_add.call_args[0][0]
        assert not any(isinstance(e, DysonRobotChildLockSwitch) for e in entities)
        # The other two are unaffected
        assert any(isinstance(e, DysonRobotWashMopBeforeCleanSwitch) for e in entities)
        assert any(isinstance(e, DysonRobotDoNotDisturbSwitch) for e in entities)

    @pytest.mark.asyncio
    async def test_wash_mop_not_created_when_absent(
        self, mock_hass, mock_config_entry, mock_coordinator
    ):
        """A robot without a wash/dry dock never sends washMopBeforeClean."""
        del mock_coordinator.data["washMopBeforeClean"]
        mock_add = MagicMock()
        await switch_setup_entry(mock_hass, mock_config_entry, mock_add)
        entities = mock_add.call_args[0][0]
        assert not any(
            isinstance(e, DysonRobotWashMopBeforeCleanSwitch) for e in entities
        )

    @pytest.mark.asyncio
    async def test_do_not_disturb_not_created_when_absent(
        self, mock_hass, mock_config_entry, mock_coordinator
    ):
        del mock_coordinator.data["doNotDisturbMode"]
        mock_add = MagicMock()
        await switch_setup_entry(mock_hass, mock_config_entry, mock_add)
        entities = mock_add.call_args[0][0]
        assert not any(isinstance(e, DysonRobotDoNotDisturbSwitch) for e in entities)

    @pytest.mark.asyncio
    async def test_none_created_when_coordinator_data_is_none(
        self, mock_hass, mock_config_entry, mock_coordinator
    ):
        mock_coordinator.data = None
        mock_add = MagicMock()
        await switch_setup_entry(mock_hass, mock_config_entry, mock_add)
        entities = mock_add.call_args[0][0]
        assert not any(isinstance(e, DysonRobotChildLockSwitch) for e in entities)
        assert not any(
            isinstance(e, DysonRobotWashMopBeforeCleanSwitch) for e in entities
        )
        assert not any(isinstance(e, DysonRobotDoNotDisturbSwitch) for e in entities)
        assert not any(isinstance(e, DysonRobotHotWaterSwitchSwitch) for e in entities)


# ---------------------------------------------------------------------------
# Child lock switch
# ---------------------------------------------------------------------------


class TestRobotChildLockSwitch:
    def test_unique_id(self, mock_coordinator):
        entity = DysonRobotChildLockSwitch(mock_coordinator)
        assert entity._attr_unique_id == "RB05-EU-TST0000A_robot_child_lock"

    def test_translation_key(self, mock_coordinator):
        entity = DysonRobotChildLockSwitch(mock_coordinator)
        assert entity._attr_translation_key == "robot_child_lock"

    def test_is_on_reflects_device_state(self, mock_coordinator):
        mock_coordinator.device.robot_child_lock = True
        entity = DysonRobotChildLockSwitch(mock_coordinator)
        assert entity._attr_is_on is True

    def test_handle_update_reflects_new_state(self, mock_coordinator):
        entity = DysonRobotChildLockSwitch(mock_coordinator)
        entity.async_write_ha_state = MagicMock()
        mock_coordinator.device.robot_child_lock = True
        entity._handle_coordinator_update()
        assert entity._attr_is_on is True

    def test_is_none_when_no_device(self, mock_coordinator):
        mock_coordinator.device = None
        entity = DysonRobotChildLockSwitch(mock_coordinator)
        assert entity._attr_is_on is None

    @pytest.mark.asyncio
    async def test_turn_on_calls_set_robot_child_lock_true(self, mock_coordinator):
        mock_coordinator.device.set_robot_child_lock = AsyncMock()
        entity = DysonRobotChildLockSwitch(mock_coordinator)
        await entity.async_turn_on()
        mock_coordinator.device.set_robot_child_lock.assert_awaited_once_with(True)

    @pytest.mark.asyncio
    async def test_turn_off_calls_set_robot_child_lock_false(self, mock_coordinator):
        mock_coordinator.device.set_robot_child_lock = AsyncMock()
        entity = DysonRobotChildLockSwitch(mock_coordinator)
        await entity.async_turn_off()
        mock_coordinator.device.set_robot_child_lock.assert_awaited_once_with(False)

    @pytest.mark.asyncio
    async def test_no_command_when_device_none(self, mock_coordinator):
        mock_coordinator.device = None
        entity = DysonRobotChildLockSwitch(mock_coordinator)
        await entity.async_turn_on()  # should not raise

    @pytest.mark.asyncio
    async def test_connection_error_logged_not_raised(self, mock_coordinator):
        mock_coordinator.device.set_robot_child_lock = AsyncMock(
            side_effect=ConnectionError()
        )
        entity = DysonRobotChildLockSwitch(mock_coordinator)
        await entity.async_turn_on()

    @pytest.mark.asyncio
    async def test_unexpected_error_logged_not_raised(self, mock_coordinator):
        mock_coordinator.device.set_robot_child_lock = AsyncMock(
            side_effect=RuntimeError()
        )
        entity = DysonRobotChildLockSwitch(mock_coordinator)
        await entity.async_turn_off()


# ---------------------------------------------------------------------------
# Wash-mop-before-clean switch
# ---------------------------------------------------------------------------


class TestRobotWashMopBeforeCleanSwitch:
    def test_unique_id(self, mock_coordinator):
        entity = DysonRobotWashMopBeforeCleanSwitch(mock_coordinator)
        assert entity._attr_unique_id == "RB05-EU-TST0000A_robot_wash_mop_before_clean"

    def test_translation_key(self, mock_coordinator):
        entity = DysonRobotWashMopBeforeCleanSwitch(mock_coordinator)
        assert entity._attr_translation_key == "robot_wash_mop_before_clean"

    def test_is_on_reflects_device_state(self, mock_coordinator):
        entity = DysonRobotWashMopBeforeCleanSwitch(mock_coordinator)
        assert entity._attr_is_on is True

    def test_handle_update_reflects_new_state(self, mock_coordinator):
        entity = DysonRobotWashMopBeforeCleanSwitch(mock_coordinator)
        entity.async_write_ha_state = MagicMock()
        mock_coordinator.device.robot_wash_mop_before_clean = False
        entity._handle_coordinator_update()
        assert entity._attr_is_on is False

    def test_is_none_when_no_device(self, mock_coordinator):
        mock_coordinator.device = None
        entity = DysonRobotWashMopBeforeCleanSwitch(mock_coordinator)
        assert entity._attr_is_on is None

    @pytest.mark.asyncio
    async def test_turn_on_calls_set_wash_mop_true(self, mock_coordinator):
        mock_coordinator.device.set_robot_wash_mop_before_clean = AsyncMock()
        entity = DysonRobotWashMopBeforeCleanSwitch(mock_coordinator)
        await entity.async_turn_on()
        mock_coordinator.device.set_robot_wash_mop_before_clean.assert_awaited_once_with(
            True
        )

    @pytest.mark.asyncio
    async def test_turn_off_calls_set_wash_mop_false(self, mock_coordinator):
        mock_coordinator.device.set_robot_wash_mop_before_clean = AsyncMock()
        entity = DysonRobotWashMopBeforeCleanSwitch(mock_coordinator)
        await entity.async_turn_off()
        mock_coordinator.device.set_robot_wash_mop_before_clean.assert_awaited_once_with(
            False
        )

    @pytest.mark.asyncio
    async def test_no_command_when_device_none(self, mock_coordinator):
        mock_coordinator.device = None
        entity = DysonRobotWashMopBeforeCleanSwitch(mock_coordinator)
        await entity.async_turn_on()  # should not raise


# ---------------------------------------------------------------------------
# Do-not-disturb switch
# ---------------------------------------------------------------------------


class TestRobotDoNotDisturbSwitch:
    def test_unique_id(self, mock_coordinator):
        entity = DysonRobotDoNotDisturbSwitch(mock_coordinator)
        assert entity._attr_unique_id == "RB05-EU-TST0000A_robot_do_not_disturb"

    def test_translation_key(self, mock_coordinator):
        entity = DysonRobotDoNotDisturbSwitch(mock_coordinator)
        assert entity._attr_translation_key == "robot_do_not_disturb"

    def test_is_on_reflects_isOn(self, mock_coordinator):
        mock_coordinator.device.robot_do_not_disturb = {
            "isOn": True,
            "startTime": "22:00",
            "endTime": "8:00",
        }
        entity = DysonRobotDoNotDisturbSwitch(mock_coordinator)
        assert entity._attr_is_on is True

    def test_is_none_when_no_device(self, mock_coordinator):
        mock_coordinator.device = None
        entity = DysonRobotDoNotDisturbSwitch(mock_coordinator)
        assert entity._attr_is_on is None

    def test_is_none_when_property_returns_none(self, mock_coordinator):
        mock_coordinator.device.robot_do_not_disturb = None
        entity = DysonRobotDoNotDisturbSwitch(mock_coordinator)
        assert entity._attr_is_on is None

    def test_extra_state_attributes_exposes_schedule(self, mock_coordinator):
        entity = DysonRobotDoNotDisturbSwitch(mock_coordinator)
        attrs = entity.extra_state_attributes
        assert attrs == {"start_time": "22:00", "end_time": "8:00"}

    def test_extra_state_attributes_none_when_no_device(self, mock_coordinator):
        mock_coordinator.device = None
        entity = DysonRobotDoNotDisturbSwitch(mock_coordinator)
        assert entity.extra_state_attributes is None

    @pytest.mark.asyncio
    async def test_turn_on_calls_set_do_not_disturb_true(self, mock_coordinator):
        mock_coordinator.device.set_robot_do_not_disturb = AsyncMock()
        entity = DysonRobotDoNotDisturbSwitch(mock_coordinator)
        await entity.async_turn_on()
        mock_coordinator.device.set_robot_do_not_disturb.assert_awaited_once_with(True)

    @pytest.mark.asyncio
    async def test_turn_off_calls_set_do_not_disturb_false(self, mock_coordinator):
        mock_coordinator.device.set_robot_do_not_disturb = AsyncMock()
        entity = DysonRobotDoNotDisturbSwitch(mock_coordinator)
        await entity.async_turn_off()
        mock_coordinator.device.set_robot_do_not_disturb.assert_awaited_once_with(False)

    @pytest.mark.asyncio
    async def test_no_command_when_device_none(self, mock_coordinator):
        mock_coordinator.device = None
        entity = DysonRobotDoNotDisturbSwitch(mock_coordinator)
        await entity.async_turn_on()  # should not raise


# ---------------------------------------------------------------------------
# Hot-water self-clean switch
# ---------------------------------------------------------------------------


class TestRobotHotWaterSwitchSwitch:
    def test_unique_id(self, mock_coordinator):
        entity = DysonRobotHotWaterSwitchSwitch(mock_coordinator)
        assert entity._attr_unique_id == "RB05-EU-TST0000A_robot_hot_water_switch"

    def test_translation_key(self, mock_coordinator):
        entity = DysonRobotHotWaterSwitchSwitch(mock_coordinator)
        assert entity._attr_translation_key == "robot_hot_water_switch"

    def test_is_on_reflects_device_state(self, mock_coordinator):
        entity = DysonRobotHotWaterSwitchSwitch(mock_coordinator)
        assert entity._attr_is_on is True

    def test_handle_update_reflects_new_state(self, mock_coordinator):
        entity = DysonRobotHotWaterSwitchSwitch(mock_coordinator)
        entity.async_write_ha_state = MagicMock()
        mock_coordinator.device.robot_hot_water_switch = False
        entity._handle_coordinator_update()
        assert entity._attr_is_on is False

    def test_is_none_when_no_device(self, mock_coordinator):
        mock_coordinator.device = None
        entity = DysonRobotHotWaterSwitchSwitch(mock_coordinator)
        assert entity._attr_is_on is None

    @pytest.mark.asyncio
    async def test_turn_on_calls_set_hot_water_switch_true(self, mock_coordinator):
        mock_coordinator.device.set_robot_hot_water_switch = AsyncMock()
        entity = DysonRobotHotWaterSwitchSwitch(mock_coordinator)
        await entity.async_turn_on()
        mock_coordinator.device.set_robot_hot_water_switch.assert_awaited_once_with(
            True
        )

    @pytest.mark.asyncio
    async def test_turn_off_calls_set_hot_water_switch_false(self, mock_coordinator):
        mock_coordinator.device.set_robot_hot_water_switch = AsyncMock()
        entity = DysonRobotHotWaterSwitchSwitch(mock_coordinator)
        await entity.async_turn_off()
        mock_coordinator.device.set_robot_hot_water_switch.assert_awaited_once_with(
            False
        )

    @pytest.mark.asyncio
    async def test_no_command_when_device_none(self, mock_coordinator):
        mock_coordinator.device = None
        entity = DysonRobotHotWaterSwitchSwitch(mock_coordinator)
        await entity.async_turn_on()  # should not raise
