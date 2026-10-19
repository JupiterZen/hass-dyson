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
    DysonRobotZoneWaterLevelSelect,
    _async_setup_zone_settings_selects,
    _ha_device_id_for_serial,
)

SERIAL = "7VS-EU-TST0001A"
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
    """The app's four-way "reinigingstype" picker."""

    @pytest.mark.parametrize(
        ("clean_type", "label"),
        [
            ("vacuum", "Stofzuigen"),
            ("vacuumAndMop", "Stofzuigen en dweilen"),
            ("mop", "Dweilen"),
            ("vacuumThenMop", "Stofzuigen, dan dweilen"),
        ],
    )
    def test_decodes_each_clean_type(self, clean_type, label):
        entity = DysonRobotZoneCleanTypeSelect(
            _make_coordinator(), MAP_ID, _zone("14", "Keuken", cleanType=clean_type)
        )
        assert entity._attr_current_option == label
        assert entity._attr_name == "Keuken reinigingstype"

    def test_has_exactly_the_four_app_options(self):
        entity = DysonRobotZoneCleanTypeSelect(
            _make_coordinator(), MAP_ID, _zone("14", "Keuken")
        )
        assert entity._attr_options == [
            "Stofzuigen en dweilen",
            "Stofzuigen",
            "Dweilen",
            "Stofzuigen, dan dweilen",
        ]

    def test_unknown_clean_type_is_none(self):
        entity = DysonRobotZoneCleanTypeSelect(
            _make_coordinator(), MAP_ID, _zone("14", "Keuken", cleanType="warp")
        )
        assert entity._attr_current_option is None

    def test_name_falls_back_to_zone_id(self):
        entity = DysonRobotZoneCleanTypeSelect(
            _make_coordinator(), MAP_ID, {"id": "99", "settings": {}}
        )
        assert entity._attr_name == "Zone 99 reinigingstype"

    def test_unique_id_unchanged_from_before_the_split(self):
        entity = DysonRobotZoneCleanTypeSelect(
            _make_coordinator(), MAP_ID, _zone("14", "Keuken")
        )
        assert entity._attr_unique_id == f"{SERIAL}_zone_clean_type_{MAP_ID}_14"


class TestZoneWaterLevelSelect:
    @pytest.mark.parametrize(
        ("level", "label"),
        [
            ("veryLow", "zeer laag"),
            ("low", "laag"),
            ("medium", "gemiddeld"),
            ("high", "hoog"),
        ],
    )
    def test_decodes_each_level(self, level, label):
        entity = DysonRobotZoneWaterLevelSelect(
            _make_coordinator(), MAP_ID, _zone("14", "Keuken", waterLevel=level)
        )
        assert entity._attr_current_option == label
        assert entity._attr_name == "Keuken waterniveau"

    @pytest.mark.asyncio
    async def test_select_sends_only_the_water_level(self):
        entity = DysonRobotZoneWaterLevelSelect(
            _make_coordinator(), MAP_ID, _zone("14", "Keuken")
        )
        entity.hass = MagicMock()
        entity.hass.services.async_call = AsyncMock()
        entity.async_write_ha_state = MagicMock()
        with patch(
            "custom_components.hass_dyson.select._ha_device_id_for_serial",
            return_value="ha-device-id",
        ):
            await entity.async_select_option("hoog")
        data = entity.hass.services.async_call.await_args.args[2]
        assert data == {
            "device_id": "ha-device-id",
            "zone": "14",
            "map": MAP_ID,
            "water_level": "high",
        }


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
    @pytest.mark.parametrize(
        ("label", "clean_type"),
        [
            ("Stofzuigen", "vacuum"),
            ("Dweilen", "mop"),
            ("Stofzuigen en dweilen", "vacuumAndMop"),
            ("Stofzuigen, dan dweilen", "vacuumThenMop"),
        ],
    )
    @pytest.mark.asyncio
    async def test_select_sends_only_the_clean_type(self, label, clean_type):
        entity = DysonRobotZoneCleanTypeSelect(
            _make_coordinator(), MAP_ID, _zone("13", "Gang")
        )
        entity.hass = MagicMock()
        entity.hass.services.async_call = AsyncMock()
        entity.async_write_ha_state = MagicMock()
        with patch(
            "custom_components.hass_dyson.select._ha_device_id_for_serial",
            return_value="ha-device-id",
        ):
            await entity.async_select_option(label)
        data = entity.hass.services.async_call.await_args.args[2]
        assert data == {
            "device_id": "ha-device-id",
            "zone": "13",
            "map": MAP_ID,
            "clean_type": clean_type,
        }
        assert entity._attr_current_option == label

    @pytest.mark.asyncio
    async def test_service_failure_does_not_update_state(self):
        entity = DysonRobotZoneCleanTypeSelect(
            _make_coordinator(), MAP_ID, _zone("13", "Gang")
        )
        entity.hass = MagicMock()
        entity.hass.services.async_call = AsyncMock(side_effect=RuntimeError("boom"))
        entity.async_write_ha_state = MagicMock()
        with patch(
            "custom_components.hass_dyson.select._ha_device_id_for_serial",
            return_value="ha-device-id",
        ):
            await entity.async_select_option("Dweilen")
        assert entity._attr_current_option == "Stofzuigen"
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
    async def test_discovers_five_selects_per_zone(self):
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
        assert len(added) == 10  # 2 zones × 5 settings selects
        assert sum(isinstance(e, DysonRobotZoneCleanTypeSelect) for e in added) == 2
        assert (
            sum(isinstance(e, DysonRobotZoneCleaningStrategySelect) for e in added) == 2
        )
        assert sum(isinstance(e, DysonRobotZoneMopPassesSelect) for e in added) == 2
        assert sum(isinstance(e, DysonRobotZoneWaterLevelSelect) for e in added) == 2


class TestZoneDiscoveryRetry:
    """A failed zone fetch at startup (e.g. a Dyson 504) is retried later."""

    @pytest.mark.asyncio
    async def test_failed_fetch_schedules_retry_and_retries(self):
        coordinator = _make_coordinator()
        add_entities = MagicMock()
        fetch = AsyncMock(side_effect=[None, (MAP_ID, [_zone("13", "Gang")])])
        with (
            patch(
                "custom_components.hass_dyson.services._fetch_zone_settings_for_current_map",
                fetch,
            ),
            patch("custom_components.hass_dyson.select.async_call_later") as later,
        ):
            await _async_setup_zone_settings_selects(
                MagicMock(), MagicMock(), coordinator, add_entities
            )
            add_entities.assert_not_called()
            later.assert_called_once()
            delay, callback = later.call_args.args[1], later.call_args.args[2]
            assert delay == 120.0

            await callback(None)

        assert fetch.await_count == 2
        assert len(add_entities.call_args.args[0]) == 5

    @pytest.mark.asyncio
    async def test_retries_are_bounded(self):
        coordinator = _make_coordinator()
        fetch = AsyncMock(return_value=None)
        with (
            patch(
                "custom_components.hass_dyson.services._fetch_zone_settings_for_current_map",
                fetch,
            ),
            patch("custom_components.hass_dyson.select.async_call_later") as later,
        ):
            await _async_setup_zone_settings_selects(
                MagicMock(), MagicMock(), coordinator, MagicMock()
            )
            for _ in range(40):
                if later.call_count == 0:
                    break
                callback = later.call_args.args[2]
                later.reset_mock()
                await callback(None)
        assert fetch.await_count == 16  # first attempt + 15 retries


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


class TestZoneSelectRefreshTriggers:
    """App-side changes (no manifest broadcast) still refresh the selects."""

    @pytest.mark.asyncio
    async def test_jdm_set_preference_triggers_refresh_and_state_follows(self):
        coordinator = _make_coordinator()
        hass = MagicMock()
        hass.loop.call_soon_threadsafe.side_effect = lambda fn, *a: fn(*a)
        entry = MagicMock()
        add_entities = MagicMock()
        fetch = AsyncMock(
            side_effect=[
                (MAP_ID, [_zone("14", "Keuken")]),
                (MAP_ID, [_zone("14", "Keuken", cleanType="mop")]),
            ]
        )
        with (
            patch(
                "custom_components.hass_dyson.services._fetch_zone_settings_for_current_map",
                fetch,
            ),
            patch("custom_components.hass_dyson.select.async_call_later") as later,
            patch("custom_components.hass_dyson.select.async_track_time_interval"),
        ):
            await _async_setup_zone_settings_selects(
                hass, entry, coordinator, add_entities
            )
            clean_type = add_entities.call_args.args[0][0]
            assert clean_type._attr_current_option == "Stofzuigen"

            on_message = coordinator.device.add_message_callback.call_args.args[0]
            on_message("RB05/7VS/status/jdm", {"method": "event.clean_record.post"})
            later.assert_not_called()  # unrelated jdm traffic is ignored

            on_message("RB05/7VS/command/jdm", {"method": "service.set_preference"})
            later.assert_called_once()
            await later.call_args.args[2](None)

        assert clean_type._attr_current_option == "Dweilen"
