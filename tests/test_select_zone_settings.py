"""Tests for the per-zone cleaning-mode/strategy select entities.

Covers DysonRobotZoneCleanTypeSelect / DysonRobotZoneCleaningStrategySelect
(select.py) — state decoding from persistent-map-metadata zone settings, and
that async_select_option calls hass_dyson.set_zone_behaviour with the right
parameters. Companion to test_services_zone_clean.py, which covers the
service handler itself (the read-modify-write against the cloud).
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.hass_dyson.select import (
    DysonRobotZoneCleaningStrategySelect,
    DysonRobotZoneCleanTypeSelect,
    DysonRobotZoneMopPassesSelect,
    DysonRobotZoneUvScanSelect,
    _async_setup_zone_settings_selects,
    _ha_device_id_for_serial,
)

SERIAL = "7VS-EU-VFA2680A"
MAP_ID = "1790712695"


def _zone(zone_id: str, name: str, **settings_overrides) -> dict:
    settings = {
        "cleaningStrategy": "auto",
        "cleanType": "vacuum",
        "waterLevel": "low",
        "mopPasses": 1,
        "dryPasses": 1,
        "isUvScanOn": True,
    }
    settings.update(settings_overrides)
    return {"id": zone_id, "name": name, "settings": settings}


def _make_coordinator() -> MagicMock:
    coordinator = MagicMock()
    coordinator.serial_number = SERIAL
    coordinator.device = MagicMock()
    return coordinator


class TestZoneCleanTypeSelectStateDecoding:
    """Test state decoding from a zone's settings dict."""

    def test_vacuum_only(self):
        zone = _zone("13", "Gang", cleanType="vacuum", waterLevel="low")
        entity = DysonRobotZoneCleanTypeSelect(_make_coordinator(), MAP_ID, zone)
        assert entity._attr_current_option == "Alleen stofzuigen"

    def test_vacuum_and_mop_high_water(self):
        zone = _zone(
            "13", "Gang", cleanType="vacuumAndMop", waterLevel="high", mopPasses=2
        )
        entity = DysonRobotZoneCleanTypeSelect(_make_coordinator(), MAP_ID, zone)
        assert entity._attr_current_option == "Stofzuigen + dweilen (hoog water)"

    def test_very_low_water_decodes_to_the_new_option(self):
        zone = _zone("14", "Keuken", cleanType="vacuumAndMop", waterLevel="veryLow")
        entity = DysonRobotZoneCleanTypeSelect(_make_coordinator(), MAP_ID, zone)
        assert entity._attr_current_option == "Stofzuigen + dweilen (zeer laag water)"
        assert "Stofzuigen + dweilen (zeer laag water)" in entity._attr_options

    def test_unrecognised_combination_is_none(self):
        zone = _zone("13", "Gang", cleanType="vacuumAndMop", waterLevel="turbo")
        entity = DysonRobotZoneCleanTypeSelect(_make_coordinator(), MAP_ID, zone)
        assert entity._attr_current_option is None

    def test_name_falls_back_to_zone_id(self):
        zone = _zone("13", "")
        zone["name"] = None
        entity = DysonRobotZoneCleanTypeSelect(_make_coordinator(), MAP_ID, zone)
        assert entity.zone_name == "Zone 13"


class TestZoneCleaningStrategySelectStateDecoding:
    def test_known_strategy(self):
        zone = _zone("13", "Gang", cleaningStrategy="boost")
        entity = DysonRobotZoneCleaningStrategySelect(_make_coordinator(), MAP_ID, zone)
        assert entity._attr_current_option == "boost"

    def test_unknown_strategy_is_none(self):
        zone = _zone("13", "Gang", cleaningStrategy="turbo")
        entity = DysonRobotZoneCleaningStrategySelect(_make_coordinator(), MAP_ID, zone)
        assert entity._attr_current_option is None


class TestZoneCleanTypeSelectOption:
    @pytest.mark.asyncio
    async def test_select_very_low_water_sends_very_low(self):
        coordinator = _make_coordinator()
        entity = DysonRobotZoneCleanTypeSelect(
            coordinator, MAP_ID, _zone("14", "Keuken")
        )
        entity.hass = MagicMock()
        entity.hass.services.async_call = AsyncMock()
        entity.async_write_ha_state = MagicMock()
        with patch(
            "custom_components.hass_dyson.select._ha_device_id_for_serial",
            return_value="ha-device-id",
        ):
            await entity.async_select_option("Stofzuigen + dweilen (zeer laag water)")
        data = entity.hass.services.async_call.await_args.args[2]
        assert data["clean_type"] == "vacuumAndMop"
        assert data["water_level"] == "veryLow"

    @pytest.mark.asyncio
    async def test_select_vacuum_and_mop_passes_water_level(self):
        coordinator = _make_coordinator()
        zone = _zone("13", "Gang")
        entity = DysonRobotZoneCleanTypeSelect(coordinator, MAP_ID, zone)
        entity.hass = MagicMock()
        entity.hass.services.async_call = AsyncMock()
        entity.async_write_ha_state = MagicMock()

        with patch(
            "custom_components.hass_dyson.select._ha_device_id_for_serial",
            return_value="ha-device-id",
        ):
            await entity.async_select_option("Stofzuigen + dweilen (hoog water)")

        entity.hass.services.async_call.assert_awaited_once_with(
            "hass_dyson",
            "set_zone_behaviour",
            {
                "device_id": "ha-device-id",
                "zone": "13",
                "map": MAP_ID,
                "clean_type": "vacuumAndMop",
                "water_level": "high",
            },
            blocking=True,
        )
        assert entity._attr_current_option == "Stofzuigen + dweilen (hoog water)"

    @pytest.mark.asyncio
    async def test_select_vacuum_only_omits_water_level(self):
        coordinator = _make_coordinator()
        zone = _zone("13", "Gang")
        entity = DysonRobotZoneCleanTypeSelect(coordinator, MAP_ID, zone)
        entity.hass = MagicMock()
        entity.hass.services.async_call = AsyncMock()
        entity.async_write_ha_state = MagicMock()

        with patch(
            "custom_components.hass_dyson.select._ha_device_id_for_serial",
            return_value="ha-device-id",
        ):
            await entity.async_select_option("Alleen stofzuigen")

        call_data = entity.hass.services.async_call.await_args.args[2]
        assert call_data["clean_type"] == "vacuum"
        assert "water_level" not in call_data

    @pytest.mark.asyncio
    async def test_service_failure_does_not_update_state(self):
        coordinator = _make_coordinator()
        # Starts as "Alleen stofzuigen" (vacuum/low); the failed call below
        # attempts a different option, so a wrongly-optimistic update is
        # distinguishable from "state left alone".
        zone = _zone("13", "Gang", cleanType="vacuum", waterLevel="low")
        entity = DysonRobotZoneCleanTypeSelect(coordinator, MAP_ID, zone)
        assert entity._attr_current_option == "Alleen stofzuigen"
        entity.hass = MagicMock()
        entity.hass.services.async_call = AsyncMock(side_effect=RuntimeError("boom"))
        entity.async_write_ha_state = MagicMock()

        with patch(
            "custom_components.hass_dyson.select._ha_device_id_for_serial",
            return_value="ha-device-id",
        ):
            await entity.async_select_option("Stofzuigen + dweilen (hoog water)")

        assert entity._attr_current_option == "Alleen stofzuigen"
        entity.async_write_ha_state.assert_not_called()


class TestZoneCleaningStrategySelectOption:
    @pytest.mark.asyncio
    async def test_select_option_calls_service(self):
        coordinator = _make_coordinator()
        zone = _zone("13", "Gang")
        entity = DysonRobotZoneCleaningStrategySelect(coordinator, MAP_ID, zone)
        entity.hass = MagicMock()
        entity.hass.services.async_call = AsyncMock()
        entity.async_write_ha_state = MagicMock()

        with patch(
            "custom_components.hass_dyson.select._ha_device_id_for_serial",
            return_value="ha-device-id",
        ):
            await entity.async_select_option("quiet")

        entity.hass.services.async_call.assert_awaited_once_with(
            "hass_dyson",
            "set_zone_behaviour",
            {
                "device_id": "ha-device-id",
                "zone": "13",
                "map": MAP_ID,
                "cleaning_strategy": "quiet",
            },
            blocking=True,
        )
        assert entity._attr_current_option == "quiet"


class TestHaDeviceIdForSerial:
    def test_raises_when_device_not_found(self):
        from homeassistant.exceptions import HomeAssistantError

        hass = MagicMock()
        with patch("custom_components.hass_dyson.select.dr") as mock_dr:
            mock_dr.async_get.return_value.async_get_device.return_value = None
            with pytest.raises(HomeAssistantError, match="No HA device registry"):
                _ha_device_id_for_serial(hass, SERIAL)

    def test_returns_entry_id(self):
        hass = MagicMock()
        with patch("custom_components.hass_dyson.select.dr") as mock_dr:
            mock_dr.async_get.return_value.async_get_device.return_value.id = "dev-123"
            assert _ha_device_id_for_serial(hass, SERIAL) == "dev-123"


class TestSetupZoneSettingsSelects:
    """Test the discovery/refresh entry point in isolation from HA's full setup."""

    @pytest.mark.asyncio
    async def test_no_maps_adds_nothing(self):
        coordinator = _make_coordinator()
        add_entities = MagicMock()
        with patch(
            "custom_components.hass_dyson.services._fetch_zone_settings_for_current_map",
            AsyncMock(return_value=None),
        ):
            await _async_setup_zone_settings_selects(
                MagicMock(), MagicMock(), coordinator, add_entities
            )
        add_entities.assert_not_called()

    @pytest.mark.asyncio
    async def test_discovers_four_selects_per_zone(self):
        coordinator = _make_coordinator()
        add_entities = MagicMock()
        zones = [_zone("13", "Gang"), _zone("10", "Woonkamer")]
        with patch(
            "custom_components.hass_dyson.services._fetch_zone_settings_for_current_map",
            AsyncMock(return_value=(MAP_ID, zones)),
        ):
            await _async_setup_zone_settings_selects(
                MagicMock(), MagicMock(), coordinator, add_entities
            )
        add_entities.assert_called_once()
        added = add_entities.call_args.args[0]
        assert len(added) == 8  # 2 zones × 4 settings selects
        assert sum(isinstance(e, DysonRobotZoneCleanTypeSelect) for e in added) == 2
        assert (
            sum(isinstance(e, DysonRobotZoneCleaningStrategySelect) for e in added) == 2
        )
        assert sum(isinstance(e, DysonRobotZoneMopPassesSelect) for e in added) == 2


class TestZoneMopPassesSelect:
    def test_decodes_one_and_two(self):
        one = DysonRobotZoneMopPassesSelect(
            _make_coordinator(), MAP_ID, _zone("13", "Gang", mopPasses=1)
        )
        two = DysonRobotZoneMopPassesSelect(
            _make_coordinator(), MAP_ID, _zone("13", "Gang", mopPasses=2)
        )
        assert one._attr_current_option == "1x dweilen"
        assert two._attr_current_option == "2x dweilen"
        assert one._attr_name == "Gang aantal keer dweilen"

    def test_unknown_value_is_none(self):
        entity = DysonRobotZoneMopPassesSelect(
            _make_coordinator(), MAP_ID, _zone("13", "Gang", mopPasses=3)
        )
        assert entity._attr_current_option is None

    @pytest.mark.asyncio
    async def test_select_calls_set_zone_behaviour(self):
        entity = DysonRobotZoneMopPassesSelect(
            _make_coordinator(), MAP_ID, _zone("13", "Gang", mopPasses=1)
        )
        entity.hass = MagicMock()
        entity.hass.services.async_call = AsyncMock()
        entity.async_write_ha_state = MagicMock()
        with patch(
            "custom_components.hass_dyson.select._ha_device_id_for_serial",
            return_value="dev1",
        ):
            await entity.async_select_option("2x dweilen")
        entity.hass.services.async_call.assert_awaited_once_with(
            "hass_dyson",
            "set_zone_behaviour",
            {"device_id": "dev1", "zone": "13", "map": MAP_ID, "mop_passes": 2},
            blocking=True,
        )
        assert entity._attr_current_option == "2x dweilen"


class TestZoneUvScanSelect:
    def test_uv_scan_decodes_bool(self):
        on = DysonRobotZoneUvScanSelect(
            _make_coordinator(), MAP_ID, _zone("13", "Gang", isUvScanOn=True)
        )
        off = DysonRobotZoneUvScanSelect(
            _make_coordinator(), MAP_ID, _zone("13", "Gang", isUvScanOn=False)
        )
        assert on._attr_current_option == "aan"
        assert off._attr_current_option == "uit"

    def test_uv_scan_does_not_confuse_bool_with_int(self):
        entity = DysonRobotZoneUvScanSelect(
            _make_coordinator(), MAP_ID, _zone("13", "Gang", isUvScanOn=1)
        )
        assert entity._attr_current_option is None

    @pytest.mark.asyncio
    async def test_uv_scan_writes_boolean(self):
        entity = DysonRobotZoneUvScanSelect(
            _make_coordinator(), MAP_ID, _zone("13", "Gang", isUvScanOn=True)
        )
        entity.hass = MagicMock()
        entity.hass.services.async_call = AsyncMock()
        entity.async_write_ha_state = MagicMock()
        with patch(
            "custom_components.hass_dyson.select._ha_device_id_for_serial",
            return_value="dev1",
        ):
            await entity.async_select_option("uit")
        entity.hass.services.async_call.assert_awaited_once_with(
            "hass_dyson",
            "set_zone_behaviour",
            {"device_id": "dev1", "zone": "13", "map": MAP_ID, "uv_scan": False},
            blocking=True,
        )
