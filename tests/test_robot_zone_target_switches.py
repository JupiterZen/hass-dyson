"""Tests for the multi-room "target zone" switches and their discovery.

DysonRobotZoneTargetSwitch is purely local UI selection state — no MQTT or
cloud write. One per zone on the robot's *current* map, discovered via the
same persistent-map metadata as button.py's zone-clean buttons and kept in
sync on the same PERSISTENT-MAP-MANIFEST-UPDATED broadcast, but without
button.py's retry-backoff or rename/retirement tracking (see
switch._async_setup_zone_target_switches's docstring for why).
"""

from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest
from libdyson_rest.models import PersistentMapMeta, ZoneMeta

from custom_components.hass_dyson.const import CONF_HOSTNAME, DOMAIN
from custom_components.hass_dyson.switch import (
    DysonRobotZoneTargetSwitch,
    async_setup_entry as switch_setup_entry,
)

_FAKE_PMAP = PersistentMapMeta(
    id="map-1",
    name="LEGO Huis",
    zones_definition_last_updated_date=None,
    zones=[
        ZoneMeta(id="10", name="Living room", icon=None, area=None),
        ZoneMeta(id="12", name="Kinderkamer", icon=None, area=None),
    ],
)


@pytest.fixture
def mock_coordinator():
    """Create a mock coordinator for an RB05 robot with a current map."""
    coordinator = Mock()
    coordinator.serial_number = "RB05-EU-TST0000A"
    coordinator.device = Mock()
    coordinator.device.robot_child_lock = False
    coordinator.device.robot_wash_mop_before_clean = True
    coordinator.device.robot_do_not_disturb = None
    coordinator.device.robot_hot_water_switch = None
    coordinator.device.robot_alarm = None
    coordinator.device.robot_detergent = None
    coordinator.device.robot_hot_water_mop = None
    coordinator.device.robot_collect_dust_on_self_clean = None
    coordinator.device.add_message_callback = Mock()
    coordinator.device.remove_message_callback = Mock()
    coordinator.device_capabilities = []
    coordinator.device_category = ["robot"]
    coordinator.config_entry = Mock()
    coordinator.config_entry.data = {"device_name": "Spot+Scrub"}
    coordinator.config_entry.async_on_unload = Mock()
    coordinator.data = {"childLock": False}
    return coordinator


@pytest.fixture
def mock_hass(mock_coordinator):
    hass = Mock()
    hass.data = {DOMAIN: {mock_coordinator.serial_number: mock_coordinator}}
    hass.loop = Mock()
    hass.async_create_task = Mock()
    return hass


@pytest.fixture
def mock_config_entry(mock_coordinator):
    entry = Mock()
    entry.data = {CONF_HOSTNAME: "192.168.1.50", "connection_type": "local"}
    entry.unique_id = mock_coordinator.serial_number
    entry.entry_id = mock_coordinator.serial_number
    entry.async_on_unload = Mock()
    return entry


class TestZoneTargetSwitchDiscovery:
    @pytest.mark.asyncio
    async def test_creates_one_switch_per_zone_on_current_map(
        self, mock_hass, mock_config_entry, mock_coordinator
    ):
        mock_add = MagicMock()
        with (
            patch(
                "custom_components.hass_dyson.services._fetch_persistent_map_metadata",
                AsyncMock(return_value=[_FAKE_PMAP]),
            ),
            patch(
                "custom_components.hass_dyson.services._effective_current_map",
                return_value=_FAKE_PMAP,
            ),
        ):
            await switch_setup_entry(mock_hass, mock_config_entry, mock_add)

        # First call: the unconditional/gated switches; a later call carries
        # the zone-target switches.
        all_added = [e for call in mock_add.call_args_list for e in call[0][0]]
        zone_switches = [
            e for e in all_added if isinstance(e, DysonRobotZoneTargetSwitch)
        ]
        assert len(zone_switches) == 2
        assert {s.zone_name for s in zone_switches} == {
            "Living room",
            "Kinderkamer",
        }

    @pytest.mark.asyncio
    async def test_no_zone_switches_for_non_robot(
        self, mock_hass, mock_config_entry, mock_coordinator
    ):
        mock_coordinator.device_category = ["ec"]
        mock_add = MagicMock()
        with patch(
            "custom_components.hass_dyson.services._fetch_persistent_map_metadata",
            AsyncMock(return_value=[_FAKE_PMAP]),
        ):
            await switch_setup_entry(mock_hass, mock_config_entry, mock_add)

        all_added = [e for call in mock_add.call_args_list for e in call[0][0]]
        assert not any(isinstance(e, DysonRobotZoneTargetSwitch) for e in all_added)

    @pytest.mark.asyncio
    async def test_empty_maps_creates_no_zone_switches(
        self, mock_hass, mock_config_entry, mock_coordinator
    ):
        mock_add = MagicMock()
        with patch(
            "custom_components.hass_dyson.services._fetch_persistent_map_metadata",
            AsyncMock(return_value=[]),
        ):
            await switch_setup_entry(mock_hass, mock_config_entry, mock_add)

        all_added = [e for call in mock_add.call_args_list for e in call[0][0]]
        assert not any(isinstance(e, DysonRobotZoneTargetSwitch) for e in all_added)

    @pytest.mark.asyncio
    async def test_fetch_failure_does_not_raise(
        self, mock_hass, mock_config_entry, mock_coordinator
    ):
        """A cloud fetch failure at setup must not block the rest of the platform."""
        mock_add = MagicMock()
        with patch(
            "custom_components.hass_dyson.services._fetch_persistent_map_metadata",
            AsyncMock(side_effect=Exception("network error")),
        ):
            await switch_setup_entry(
                mock_hass, mock_config_entry, mock_add
            )  # should not raise


class TestZoneTargetSwitchEntity:
    def test_unique_id_is_map_qualified(self, mock_coordinator):
        zone = _FAKE_PMAP.zones[1]
        entity = DysonRobotZoneTargetSwitch(mock_coordinator, _FAKE_PMAP, zone)
        assert entity._attr_unique_id == "RB05-EU-TST0000A_zone_target_map-1_12"

    def test_name_and_zone_name(self, mock_coordinator):
        zone = _FAKE_PMAP.zones[1]
        entity = DysonRobotZoneTargetSwitch(mock_coordinator, _FAKE_PMAP, zone)
        assert entity.zone_name == "Kinderkamer"
        assert entity._attr_name == "Target Kinderkamer"

    def test_extra_state_attributes_expose_zone_name(self, mock_coordinator):
        zone = _FAKE_PMAP.zones[1]
        entity = DysonRobotZoneTargetSwitch(mock_coordinator, _FAKE_PMAP, zone)
        assert entity._attr_extra_state_attributes == {"zone_name": "Kinderkamer"}

    def test_defaults_to_off(self, mock_coordinator):
        zone = _FAKE_PMAP.zones[0]
        entity = DysonRobotZoneTargetSwitch(mock_coordinator, _FAKE_PMAP, zone)
        assert entity._attr_is_on is False

    @pytest.mark.asyncio
    async def test_turn_on_sets_local_state_only(self, mock_coordinator):
        zone = _FAKE_PMAP.zones[0]
        entity = DysonRobotZoneTargetSwitch(mock_coordinator, _FAKE_PMAP, zone)
        entity.async_write_ha_state = MagicMock()
        await entity.async_turn_on()
        assert entity._attr_is_on is True
        # No device/cloud call — purely local.
        mock_coordinator.device.assert_not_called()

    @pytest.mark.asyncio
    async def test_turn_off_sets_local_state_only(self, mock_coordinator):
        zone = _FAKE_PMAP.zones[0]
        entity = DysonRobotZoneTargetSwitch(mock_coordinator, _FAKE_PMAP, zone)
        entity._attr_is_on = True
        entity.async_write_ha_state = MagicMock()
        await entity.async_turn_off()
        assert entity._attr_is_on is False

    def test_update_zone_meta_renames(self, mock_coordinator):
        zone = _FAKE_PMAP.zones[1]
        entity = DysonRobotZoneTargetSwitch(mock_coordinator, _FAKE_PMAP, zone)
        renamed_zone = ZoneMeta(id="12", name="Kids Room", icon=None, area=None)
        entity.async_update_zone_meta(_FAKE_PMAP, renamed_zone)
        assert entity.zone_name == "Kids Room"
        assert entity._attr_name == "Target Kids Room"

    @pytest.mark.asyncio
    async def test_restores_last_selection_state(self, mock_coordinator):
        zone = _FAKE_PMAP.zones[0]
        entity = DysonRobotZoneTargetSwitch(mock_coordinator, _FAKE_PMAP, zone)
        last_state = Mock()
        last_state.state = "on"
        entity.async_get_last_state = AsyncMock(return_value=last_state)
        # Bypass RestoreEntity/Entity base async_added_to_hass machinery —
        # only exercising the restore logic itself here.
        with patch(
            "homeassistant.helpers.restore_state.RestoreEntity.async_added_to_hass",
            AsyncMock(),
        ):
            await entity.async_added_to_hass()
        assert entity._attr_is_on is True
