"""Per-clean obstacle/dirt/spot-zone tally sensors.

GET /v1/app/{serial}/live-maps/cleaning (see _fetch_live_map_cleaning in
image.py) returns the *complete* current obstacles/dirt/spotZones arrays on
every poll, not a delta — DysonRobotObstaclesSensor, DysonRobotDirtSensor and
DysonRobotSpotZonesSensor turn that into a running per-clean tally by
deduping already-seen points.
"""

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.hass_dyson.image import _live_map_cache
from custom_components.hass_dyson.sensor import (
    DysonRobotDirtSensor,
    DysonRobotObstaclesSensor,
    DysonRobotSpotZonesSensor,
)


@pytest.fixture(autouse=True)
def _clear_shared_fetch_cache():
    """The three sensors share image.py's live-map fetch cache — isolate tests from it."""
    _live_map_cache._store.clear()
    yield
    _live_map_cache._store.clear()


@pytest.fixture
def mock_robot_coordinator():
    """Robot coordinator whose device reports an active clean by default."""
    coordinator = MagicMock()
    coordinator.serial_number = "7VS-EU-UNA6126A"
    coordinator.device_name = "Robot"
    coordinator.device = MagicMock()
    coordinator.device.robot_state = "FULL_CLEAN_RUNNING"
    coordinator.device.robot_clean_id = "clean-1"
    coordinator.config_entry = MagicMock()
    coordinator.config_entry.data = {"auth_token": "tok"}
    return coordinator


def _live_map(obstacles=None, dirt=None, spot_zones=None):
    return {
        "obstacles": obstacles or [],
        "dirt": dirt or [],
        "spotZones": spot_zones or [],
    }


def _client_returning(payload):
    fake_client = AsyncMock()
    fake_client.get_live_map_cleaning = AsyncMock(return_value=payload)

    @asynccontextmanager
    async def make_client():
        yield fake_client

    return make_client


class TestObstaclesSensor:
    @pytest.mark.asyncio
    async def test_first_poll_counts_all_points(self, mock_robot_coordinator):
        mock_robot_coordinator.async_cloud_client = _client_returning(
            _live_map(obstacles=[{"x": 1.0, "y": 2.0}, {"x": 3.0, "y": 4.0}])
        )
        sensor = DysonRobotObstaclesSensor(mock_robot_coordinator)

        await sensor.async_update()

        assert sensor._attr_native_value == 2
        points = sensor._attr_extra_state_attributes["points"]
        assert {"x": 1.0, "y": 2.0} in [{"x": p["x"], "y": p["y"]} for p in points]

    @pytest.mark.asyncio
    async def test_same_point_across_polls_not_double_counted(
        self, mock_robot_coordinator
    ):
        mock_robot_coordinator.async_cloud_client = _client_returning(
            _live_map(obstacles=[{"x": 1.0, "y": 2.0}])
        )
        sensor = DysonRobotObstaclesSensor(mock_robot_coordinator)

        await sensor.async_update()
        await sensor.async_update()
        await sensor.async_update()

        assert sensor._attr_native_value == 1

    @pytest.mark.asyncio
    async def test_nearby_point_within_tolerance_not_double_counted(
        self, mock_robot_coordinator
    ):
        # Second poll's point is 5cm away — within the dedup radius, should
        # still count as the same physical obstacle (drift between polls).
        sensor = DysonRobotObstaclesSensor(mock_robot_coordinator)

        mock_robot_coordinator.async_cloud_client = _client_returning(
            _live_map(obstacles=[{"x": 1.00, "y": 2.00}])
        )
        await sensor.async_update()

        _live_map_cache._store.clear()
        mock_robot_coordinator.async_cloud_client = _client_returning(
            _live_map(obstacles=[{"x": 1.03, "y": 2.04}])
        )
        await sensor.async_update()

        assert sensor._attr_native_value == 1

    @pytest.mark.asyncio
    async def test_point_beyond_tolerance_counts_as_new(self, mock_robot_coordinator):
        sensor = DysonRobotObstaclesSensor(mock_robot_coordinator)

        mock_robot_coordinator.async_cloud_client = _client_returning(
            _live_map(obstacles=[{"x": 1.0, "y": 2.0}])
        )
        await sensor.async_update()

        _live_map_cache._store.clear()
        mock_robot_coordinator.async_cloud_client = _client_returning(
            _live_map(obstacles=[{"x": 1.0, "y": 2.0}, {"x": 5.0, "y": 5.0}])
        )
        await sensor.async_update()

        assert sensor._attr_native_value == 2

    @pytest.mark.asyncio
    async def test_new_clean_resets_tally(self, mock_robot_coordinator):
        sensor = DysonRobotObstaclesSensor(mock_robot_coordinator)

        mock_robot_coordinator.async_cloud_client = _client_returning(
            _live_map(obstacles=[{"x": 1.0, "y": 2.0}])
        )
        await sensor.async_update()
        assert sensor._attr_native_value == 1

        mock_robot_coordinator.device.robot_clean_id = "clean-2"
        _live_map_cache._store.clear()
        mock_robot_coordinator.async_cloud_client = _client_returning(
            _live_map(obstacles=[{"x": 9.0, "y": 9.0}])
        )
        await sensor.async_update()

        assert sensor._attr_native_value == 1
        assert sensor._attr_extra_state_attributes["clean_id"] == "clean-2"

    @pytest.mark.asyncio
    async def test_not_cleaning_keeps_last_tally(self, mock_robot_coordinator):
        sensor = DysonRobotObstaclesSensor(mock_robot_coordinator)

        mock_robot_coordinator.async_cloud_client = _client_returning(
            _live_map(obstacles=[{"x": 1.0, "y": 2.0}])
        )
        await sensor.async_update()
        assert sensor._attr_native_value == 1

        mock_robot_coordinator.device.robot_state = "INACTIVE_CHARGING"
        await sensor.async_update()

        assert sensor._attr_native_value == 1

    @pytest.mark.asyncio
    async def test_endpoint_404_keeps_last_tally(self, mock_robot_coordinator):
        sensor = DysonRobotObstaclesSensor(mock_robot_coordinator)

        mock_robot_coordinator.async_cloud_client = _client_returning(
            _live_map(obstacles=[{"x": 1.0, "y": 2.0}])
        )
        await sensor.async_update()
        assert sensor._attr_native_value == 1

        _live_map_cache._store.clear()

        @asynccontextmanager
        async def null_client():
            yield None

        mock_robot_coordinator.async_cloud_client = null_client
        await sensor.async_update()

        assert sensor._attr_native_value == 1

    @pytest.mark.asyncio
    async def test_no_device_defaults_to_zero(self, mock_robot_coordinator):
        mock_robot_coordinator.device = None
        sensor = DysonRobotObstaclesSensor(mock_robot_coordinator)

        await sensor.async_update()

        assert sensor._attr_native_value == 0


class TestDirtSensor:
    @pytest.mark.asyncio
    async def test_describes_type_and_uv_scan(self, mock_robot_coordinator):
        mock_robot_coordinator.async_cloud_client = _client_returning(
            _live_map(
                dirt=[
                    {"x": -0.88, "y": 2.97, "type": "solid", "isUvScanOn": False},
                ]
            )
        )
        sensor = DysonRobotDirtSensor(mock_robot_coordinator)

        await sensor.async_update()

        assert sensor._attr_native_value == 1
        point = sensor._attr_extra_state_attributes["points"][0]
        assert point["type"] == "solid"
        assert point["is_uv_scan_on"] is False
        assert "first_seen" in point

    @pytest.mark.asyncio
    async def test_dedup_independent_of_obstacles_sensor(self, mock_robot_coordinator):
        # Sanity check the two sensors don't share dedup state, only the
        # underlying fetch cache.
        mock_robot_coordinator.async_cloud_client = _client_returning(
            _live_map(
                obstacles=[{"x": 1.0, "y": 1.0}],
                dirt=[{"x": 1.0, "y": 1.0, "type": "solid", "isUvScanOn": False}],
            )
        )
        obstacles = DysonRobotObstaclesSensor(mock_robot_coordinator)
        dirt = DysonRobotDirtSensor(mock_robot_coordinator)

        await obstacles.async_update()
        await dirt.async_update()

        assert obstacles._attr_native_value == 1
        assert dirt._attr_native_value == 1


class TestSpotZonesSensor:
    @pytest.mark.asyncio
    async def test_structural_dedup_for_unknown_schema(self, mock_robot_coordinator):
        mock_robot_coordinator.async_cloud_client = _client_returning(
            _live_map(spot_zones=[{"foo": "bar", "n": 1}])
        )
        sensor = DysonRobotSpotZonesSensor(mock_robot_coordinator)

        await sensor.async_update()
        _live_map_cache._store.clear()
        mock_robot_coordinator.async_cloud_client = _client_returning(
            _live_map(spot_zones=[{"foo": "bar", "n": 1}])
        )
        await sensor.async_update()

        assert sensor._attr_native_value == 1

    @pytest.mark.asyncio
    async def test_different_entry_counts_as_new(self, mock_robot_coordinator):
        mock_robot_coordinator.async_cloud_client = _client_returning(
            _live_map(spot_zones=[{"foo": "bar"}, {"foo": "baz"}])
        )
        sensor = DysonRobotSpotZonesSensor(mock_robot_coordinator)

        await sensor.async_update()

        assert sensor._attr_native_value == 2

    @pytest.mark.asyncio
    async def test_empty_spot_zones_stays_zero(self, mock_robot_coordinator):
        mock_robot_coordinator.async_cloud_client = _client_returning(_live_map())
        sensor = DysonRobotSpotZonesSensor(mock_robot_coordinator)

        await sensor.async_update()

        assert sensor._attr_native_value == 0


class TestSharedFetchCache:
    @pytest.mark.asyncio
    async def test_second_sensor_reuses_cached_fetch_within_ttl(
        self, mock_robot_coordinator
    ):
        fake_client = AsyncMock()
        fake_client.get_live_map_cleaning = AsyncMock(
            return_value=_live_map(obstacles=[{"x": 1.0, "y": 1.0}])
        )

        @asynccontextmanager
        async def make_client():
            yield fake_client

        mock_robot_coordinator.async_cloud_client = make_client

        obstacles = DysonRobotObstaclesSensor(mock_robot_coordinator)
        dirt = DysonRobotDirtSensor(mock_robot_coordinator)

        await obstacles.async_update()
        await dirt.async_update()

        fake_client.get_live_map_cleaning.assert_called_once()


class TestRestoreAcrossRestart:
    @pytest.mark.asyncio
    async def test_restores_points_for_same_clean_id(self, mock_robot_coordinator):
        sensor = DysonRobotObstaclesSensor(mock_robot_coordinator)
        last_state = MagicMock()
        last_state.state = "1"
        last_state.attributes = {
            "clean_id": "clean-1",
            "points": [{"x": 1.0, "y": 2.0, "first_seen": "2026-09-05T10:00:00+00:00"}],
        }
        sensor.async_get_last_state = AsyncMock(return_value=last_state)

        with patch(
            "custom_components.hass_dyson.sensor.RestoreEntity.async_added_to_hass",
            new=AsyncMock(),
        ):
            await sensor.async_added_to_hass()

        # Restored point should still count as "already seen" on the next poll.
        mock_robot_coordinator.async_cloud_client = _client_returning(
            _live_map(obstacles=[{"x": 1.0, "y": 2.0}])
        )
        await sensor.async_update()

        assert sensor._attr_native_value == 1

    @pytest.mark.asyncio
    async def test_ignores_restored_state_from_different_clean(
        self, mock_robot_coordinator
    ):
        sensor = DysonRobotObstaclesSensor(mock_robot_coordinator)
        last_state = MagicMock()
        last_state.state = "unknown"
        last_state.attributes = {}
        sensor.async_get_last_state = AsyncMock(return_value=last_state)

        with patch(
            "custom_components.hass_dyson.sensor.RestoreEntity.async_added_to_hass",
            new=AsyncMock(),
        ):
            await sensor.async_added_to_hass()

        assert sensor._seen_points == []
        assert sensor._tracked_clean_id is None


class TestCleanIdViaCloudFallback:
    """device.robot_clean_id is confirmed absent for some devices/firmware —
    restore and reset must still work via the cloud clean-history endpoint.
    """

    @pytest.mark.asyncio
    async def test_restore_uses_cloud_clean_id_when_mqtt_absent(
        self, mock_robot_coordinator
    ):
        mock_robot_coordinator.device.robot_clean_id = None
        sensor = DysonRobotObstaclesSensor(mock_robot_coordinator)
        last_state = MagicMock()
        last_state.state = "1"
        last_state.attributes = {
            "clean_id": None,
            "points": [{"x": 1.0, "y": 2.0, "first_seen": "2026-09-05T10:00:00+00:00"}],
        }
        sensor.async_get_last_state = AsyncMock(return_value=last_state)

        cloud_clean = MagicMock()
        cloud_clean.clean_id = "cloud-clean-1"

        with (
            patch(
                "custom_components.hass_dyson.sensor.RestoreEntity.async_added_to_hass",
                new=AsyncMock(),
            ),
            patch(
                "custom_components.hass_dyson.sensor.fetch_clean_maps",
                new=AsyncMock(return_value=[cloud_clean]),
            ),
        ):
            await sensor.async_added_to_hass()

        assert sensor._tracked_clean_id == "cloud-clean-1"

        # Restored point still counts as "already seen" on the next poll —
        # the persisted attrs had no clean_id, but the cloud fallback gave
        # _restore_tally something to key the restore off of.
        mock_robot_coordinator.async_cloud_client = _client_returning(
            _live_map(obstacles=[{"x": 1.0, "y": 2.0}])
        )
        await sensor.async_update()
        assert sensor._attr_native_value == 1

    @pytest.mark.asyncio
    async def test_reset_on_new_clean_still_works_via_task_begin_time(
        self, mock_robot_coordinator
    ):
        """Even with clean_id permanently unavailable, a real new clean must
        still reset the tally — via the live-map response's own
        taskBeginTime, which is refetched every poll regardless of MQTT.
        """
        mock_robot_coordinator.device.robot_clean_id = None
        sensor = DysonRobotObstaclesSensor(mock_robot_coordinator)

        payload_clean_1 = _live_map(obstacles=[{"x": 1.0, "y": 1.0}])
        payload_clean_1["taskBeginTime"] = 1000
        mock_robot_coordinator.async_cloud_client = _client_returning(payload_clean_1)
        await sensor.async_update()
        assert sensor._attr_native_value == 1

        # A new clean starts: different taskBeginTime, unrelated obstacle.
        _live_map_cache._store.clear()
        payload_clean_2 = _live_map(obstacles=[{"x": 9.0, "y": 9.0}])
        payload_clean_2["taskBeginTime"] = 2000
        mock_robot_coordinator.async_cloud_client = _client_returning(payload_clean_2)
        await sensor.async_update()

        assert sensor._attr_native_value == 1
        assert sensor._attr_extra_state_attributes["points"][0]["x"] == 9.0

    @pytest.mark.asyncio
    async def test_restore_falls_back_to_old_behaviour_when_cloud_empty(
        self, mock_robot_coordinator
    ):
        """fetch_clean_maps returning nothing must not crash — just skip
        restore, same as before this fix (no usable identity to key off).
        """
        mock_robot_coordinator.device.robot_clean_id = None
        sensor = DysonRobotObstaclesSensor(mock_robot_coordinator)
        last_state = MagicMock()
        last_state.state = "1"
        last_state.attributes = {
            "clean_id": None,
            "points": [{"x": 1.0, "y": 2.0, "first_seen": "2026-09-05T10:00:00+00:00"}],
        }
        sensor.async_get_last_state = AsyncMock(return_value=last_state)

        with (
            patch(
                "custom_components.hass_dyson.sensor.RestoreEntity.async_added_to_hass",
                new=AsyncMock(),
            ),
            patch(
                "custom_components.hass_dyson.sensor.fetch_clean_maps",
                new=AsyncMock(return_value=[]),
            ),
        ):
            await sensor.async_added_to_hass()

        assert sensor._seen_points == []
        assert sensor._tracked_clean_id is None
