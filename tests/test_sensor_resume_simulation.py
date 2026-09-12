"""Resume-simulation tally sensor (DysonRobotResumeSimulationSensor).

vacuum.stop/return_to_base are the only ways to end a clean early (both are
aliases for the same ABORT command — see vacuum.py), so there is no
server-side "pause, resume later". This sensor tracks, via the same
live-maps/cleaning endpoint the detection sensors use (see
test_sensor_robot_detections.py), which zone names reached CLEAN_COMPLETE
during the clean in progress, so a departure automation can skip
already-finished rooms on the next early-abort-then-restart cycle.
"""

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.hass_dyson.image import _live_map_cache
from custom_components.hass_dyson.sensor import DysonRobotResumeSimulationSensor


@pytest.fixture(autouse=True)
def _clear_shared_fetch_cache():
    _live_map_cache._store.clear()
    yield
    _live_map_cache._store.clear()


@pytest.fixture
def mock_robot_coordinator():
    coordinator = MagicMock()
    coordinator.serial_number = "7VS-EU-UNA6126A"
    coordinator.device_name = "Robot"
    coordinator.device = MagicMock()
    coordinator.device.robot_state = "FULL_CLEAN_RUNNING"
    coordinator.device.robot_clean_id = "clean-1"
    coordinator.config_entry = MagicMock()
    coordinator.config_entry.data = {"auth_token": "tok"}
    return coordinator


def _live_map(zones=None, task_begin_time=None):
    data = {"zones": zones or []}
    if task_begin_time is not None:
        data["taskBeginTime"] = task_begin_time
    return data


def _zone(name, status):
    return {"id": name.lower(), "name": name, "cleanStatus": status}


def _client_returning(payload):
    fake_client = AsyncMock()
    fake_client.get_live_map_cleaning = AsyncMock(return_value=payload)

    @asynccontextmanager
    async def make_client():
        yield fake_client

    return make_client


def _sensor_with_hass(coordinator, reset_toggle_state: str | None = "on"):
    """Build the sensor with a hass whose input_boolean toggle returns a fixed state.

    reset_toggle_state=None simulates the helper entity not existing yet
    (hass.states.get returns None) — the sensor's documented fallback.
    """
    sensor = DysonRobotResumeSimulationSensor(coordinator)
    fake_hass = MagicMock()
    if reset_toggle_state is None:
        fake_hass.states.get = MagicMock(return_value=None)
    else:
        fake_state = MagicMock()
        fake_state.state = reset_toggle_state
        fake_hass.states.get = MagicMock(return_value=fake_state)
    sensor.hass = fake_hass
    return sensor


class TestTallyDuringActiveClean:
    @pytest.mark.asyncio
    async def test_tracks_complete_zones_only(self, mock_robot_coordinator):
        sensor = _sensor_with_hass(mock_robot_coordinator)
        mock_robot_coordinator.async_cloud_client = _client_returning(
            _live_map(
                zones=[
                    _zone("Living Room", "CLEAN_COMPLETE"),
                    _zone("Bedroom", "CLEAN_IN_PROGRESS"),
                    _zone("Kitchen", "CLEAN_PENDING"),
                ]
            )
        )

        await sensor.async_update()

        assert sensor._attr_native_value == 1
        assert sensor._attr_extra_state_attributes["zone_names"] == ["Living Room"]

    @pytest.mark.asyncio
    async def test_accumulates_across_polls(self, mock_robot_coordinator):
        # toggle off: isolates "does the tally accumulate" from the
        # separate fully-resolved-run reset behaviour covered in
        # TestResetAfterCompleteRunToggle.
        sensor = _sensor_with_hass(mock_robot_coordinator, reset_toggle_state="off")
        mock_robot_coordinator.async_cloud_client = _client_returning(
            _live_map(
                zones=[
                    _zone("Living Room", "CLEAN_COMPLETE"),
                    _zone("Bedroom", "CLEAN_IN_PROGRESS"),
                ]
            )
        )
        await sensor.async_update()
        assert sensor._attr_native_value == 1

        _live_map_cache._store.clear()
        mock_robot_coordinator.async_cloud_client = _client_returning(
            _live_map(
                zones=[
                    _zone("Living Room", "CLEAN_COMPLETE"),
                    _zone("Bedroom", "CLEAN_COMPLETE"),
                ]
            )
        )
        await sensor.async_update()

        assert sensor._attr_native_value == 2
        assert sorted(sensor._attr_extra_state_attributes["zone_names"]) == [
            "Bedroom",
            "Living Room",
        ]

    @pytest.mark.asyncio
    async def test_cant_clean_zone_not_counted_as_complete(
        self, mock_robot_coordinator
    ):
        sensor = _sensor_with_hass(mock_robot_coordinator)
        mock_robot_coordinator.async_cloud_client = _client_returning(
            _live_map(zones=[_zone("Wasruimte", "CANT_CLEAN")])
        )

        await sensor.async_update()

        assert sensor._attr_native_value == 0

    @pytest.mark.asyncio
    async def test_early_abort_keeps_partial_tally(self, mock_robot_coordinator):
        """An early vacuum.stop makes the endpoint 404 (None) — the partial
        tally from before the abort must survive so the departure automation
        can still see which rooms were already done.
        """
        sensor = _sensor_with_hass(mock_robot_coordinator, reset_toggle_state="off")
        mock_robot_coordinator.async_cloud_client = _client_returning(
            _live_map(
                zones=[
                    _zone("Living Room", "CLEAN_COMPLETE"),
                    _zone("Bedroom", "CLEAN_PENDING"),
                ]
            )
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


class TestNewCleanReset:
    @pytest.mark.asyncio
    async def test_reset_on_new_clean_id(self, mock_robot_coordinator):
        sensor = _sensor_with_hass(mock_robot_coordinator, reset_toggle_state="off")
        mock_robot_coordinator.async_cloud_client = _client_returning(
            _live_map(
                zones=[
                    _zone("Living Room", "CLEAN_COMPLETE"),
                    _zone("Bedroom", "CLEAN_PENDING"),
                ]
            )
        )
        await sensor.async_update()
        assert sensor._attr_native_value == 1

        mock_robot_coordinator.device.robot_clean_id = "clean-2"
        _live_map_cache._store.clear()
        mock_robot_coordinator.async_cloud_client = _client_returning(
            _live_map(zones=[_zone("Bedroom", "CLEAN_IN_PROGRESS")])
        )
        await sensor.async_update()

        assert sensor._attr_native_value == 0
        assert sensor._attr_extra_state_attributes["clean_id"] == "clean-2"

    @pytest.mark.asyncio
    async def test_reset_via_task_begin_time_when_clean_id_absent(
        self, mock_robot_coordinator
    ):
        """clean_id is confirmed structurally None on this device/firmware —
        taskBeginTime (refetched every poll) must still catch a new clean.
        """
        mock_robot_coordinator.device.robot_clean_id = None
        sensor = _sensor_with_hass(mock_robot_coordinator, reset_toggle_state="off")

        mock_robot_coordinator.async_cloud_client = _client_returning(
            _live_map(
                zones=[
                    _zone("Living Room", "CLEAN_COMPLETE"),
                    _zone("Bedroom", "CLEAN_PENDING"),
                ],
                task_begin_time=1000,
            )
        )
        await sensor.async_update()
        assert sensor._attr_native_value == 1

        _live_map_cache._store.clear()
        mock_robot_coordinator.async_cloud_client = _client_returning(
            _live_map(
                zones=[_zone("Bedroom", "CLEAN_IN_PROGRESS")], task_begin_time=2000
            )
        )
        await sensor.async_update()

        assert sensor._attr_native_value == 0


class TestRestoreAcrossRestart:
    @pytest.mark.asyncio
    async def test_restores_zone_names_and_clean_id(self, mock_robot_coordinator):
        sensor = _sensor_with_hass(mock_robot_coordinator)
        last_state = MagicMock()
        last_state.state = "1"
        last_state.attributes = {
            "zone_names": ["Living Room"],
            "clean_id": "clean-1",
            "last_reset_date": "2026-09-08",
        }
        sensor.async_get_last_state = AsyncMock(return_value=last_state)

        with patch(
            "custom_components.hass_dyson.sensor.RestoreEntity.async_added_to_hass",
            new=AsyncMock(),
        ):
            await sensor.async_added_to_hass()

        assert sensor._completed_zone_names == {"Living Room"}
        assert sensor._tracked_clean_id == "clean-1"
        assert sensor._attr_native_value == 1

    @pytest.mark.asyncio
    async def test_restored_tally_survives_same_clean_poll(
        self, mock_robot_coordinator
    ):
        sensor = _sensor_with_hass(mock_robot_coordinator)
        last_state = MagicMock()
        last_state.state = "1"
        last_state.attributes = {
            "zone_names": ["Living Room"],
            "clean_id": "clean-1",
            "last_reset_date": None,
        }
        sensor.async_get_last_state = AsyncMock(return_value=last_state)

        with patch(
            "custom_components.hass_dyson.sensor.RestoreEntity.async_added_to_hass",
            new=AsyncMock(),
        ):
            await sensor.async_added_to_hass()

        mock_robot_coordinator.async_cloud_client = _client_returning(
            _live_map(
                zones=[
                    _zone("Living Room", "CLEAN_COMPLETE"),
                    _zone("Bedroom", "CLEAN_IN_PROGRESS"),
                ]
            )
        )
        await sensor.async_update()

        assert sensor._attr_native_value == 1

    @pytest.mark.asyncio
    async def test_ignores_unknown_last_state(self, mock_robot_coordinator):
        sensor = _sensor_with_hass(mock_robot_coordinator)
        last_state = MagicMock()
        last_state.state = "unknown"
        last_state.attributes = {}
        sensor.async_get_last_state = AsyncMock(return_value=last_state)

        with patch(
            "custom_components.hass_dyson.sensor.RestoreEntity.async_added_to_hass",
            new=AsyncMock(),
        ):
            await sensor.async_added_to_hass()

        assert sensor._completed_zone_names == set()
        assert sensor._tracked_clean_id is None

    @pytest.mark.asyncio
    async def test_restore_uses_cloud_clean_id_when_mqtt_absent(
        self, mock_robot_coordinator
    ):
        mock_robot_coordinator.device.robot_clean_id = None
        sensor = _sensor_with_hass(mock_robot_coordinator)
        last_state = MagicMock()
        last_state.state = "1"
        last_state.attributes = {
            "zone_names": ["Living Room"],
            "clean_id": None,
            "last_reset_date": None,
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


class TestResetAfterCompleteRunToggle:
    @pytest.mark.asyncio
    async def test_toggle_on_clears_tally_when_run_fully_resolved(
        self, mock_robot_coordinator
    ):
        sensor = _sensor_with_hass(mock_robot_coordinator, reset_toggle_state="on")
        mock_robot_coordinator.async_cloud_client = _client_returning(
            _live_map(
                zones=[
                    _zone("Living Room", "CLEAN_COMPLETE"),
                    _zone("Bedroom", "CLEAN_COMPLETE"),
                ]
            )
        )

        await sensor.async_update()

        assert sensor._attr_native_value == 0
        assert sensor._completed_zone_names == set()

    @pytest.mark.asyncio
    async def test_toggle_off_keeps_tally_when_run_fully_resolved(
        self, mock_robot_coordinator
    ):
        sensor = _sensor_with_hass(mock_robot_coordinator, reset_toggle_state="off")
        mock_robot_coordinator.async_cloud_client = _client_returning(
            _live_map(
                zones=[
                    _zone("Living Room", "CLEAN_COMPLETE"),
                    _zone("Bedroom", "CLEAN_COMPLETE"),
                ]
            )
        )

        await sensor.async_update()

        assert sensor._attr_native_value == 2
        assert sensor._completed_zone_names == {"Living Room", "Bedroom"}

    @pytest.mark.asyncio
    async def test_missing_helper_entity_defaults_to_reset(
        self, mock_robot_coordinator
    ):
        """Toggle entity not created yet: hass.states.get returns None. Must
        default to resetting (matches the input_boolean's own initial: true)
        rather than silently accumulating stale completion state forever.
        """
        sensor = _sensor_with_hass(mock_robot_coordinator, reset_toggle_state=None)
        mock_robot_coordinator.async_cloud_client = _client_returning(
            _live_map(zones=[_zone("Living Room", "CLEAN_COMPLETE")])
        )

        await sensor.async_update()

        assert sensor._attr_native_value == 0

    @pytest.mark.asyncio
    async def test_not_fully_resolved_run_never_triggers_toggle_reset(
        self, mock_robot_coordinator
    ):
        sensor = _sensor_with_hass(mock_robot_coordinator, reset_toggle_state="on")
        mock_robot_coordinator.async_cloud_client = _client_returning(
            _live_map(
                zones=[
                    _zone("Living Room", "CLEAN_COMPLETE"),
                    _zone("Bedroom", "CLEAN_IN_PROGRESS"),
                ]
            )
        )

        await sensor.async_update()

        assert sensor._attr_native_value == 1


class TestDayBoundaryReset:
    @pytest.mark.asyncio
    async def test_idle_new_day_resets_tally_when_not_cleaning(
        self, mock_robot_coordinator
    ):
        mock_robot_coordinator.device.robot_state = "INACTIVE_CHARGED"
        sensor = _sensor_with_hass(mock_robot_coordinator)
        sensor._completed_zone_names = {"Living Room"}
        sensor._tracked_clean_id = "clean-1"
        sensor._last_reset_date = "2026-09-01"

        with patch("custom_components.hass_dyson.sensor.dt_util.now") as mock_now:
            mock_now.return_value.date.return_value.isoformat.return_value = (
                "2026-09-02"
            )
            await sensor.async_update()

        assert sensor._completed_zone_names == set()
        assert sensor._attr_native_value == 0

    @pytest.mark.asyncio
    async def test_idle_same_day_keeps_tally(self, mock_robot_coordinator):
        mock_robot_coordinator.device.robot_state = "INACTIVE_CHARGED"
        sensor = _sensor_with_hass(mock_robot_coordinator)
        sensor._completed_zone_names = {"Living Room"}
        sensor._last_reset_date = "2026-09-02"

        with patch("custom_components.hass_dyson.sensor.dt_util.now") as mock_now:
            mock_now.return_value.date.return_value.isoformat.return_value = (
                "2026-09-02"
            )
            await sensor.async_update()

        assert sensor._completed_zone_names == {"Living Room"}
