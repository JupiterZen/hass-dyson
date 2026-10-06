"""jdm get_preference: stored by the coordinator, decoded by the sensor.

The sample is the real answer the robot gave on 5 okt 2026 (paused mid-clean).
"""

from unittest.mock import AsyncMock, MagicMock, Mock

import pytest

from custom_components.hass_dyson.coordinator import DysonDataUpdateCoordinator
from custom_components.hass_dyson.sensor import DysonRobotRoomPreferencesSensor

REAL_ANSWER = {
    "msgId": "1260797905",
    "code": 0,
    "method": "service.get_preference",
    "data": {
        "room": [
            [10, "Woonkamer", 0, 1, 0, 2, 1, 0, 1, 0, 1, 0],
            [11, "Slaapkamer", 0, 0, 0, 2, 1, 0, 1, 0, 6, 0],
            [12, "Kinderkamer", 0, 1, 0, 2, 1, 0, 1, 0, 4, 0],
            [13, "Gang", 0, 0, 1, 2, 1, 0, 1, 0, 3, 0],
            [15, "Wasruimte", 0, 0, 0, 2, 1, 0, 1, 0, 5, 0],
            [14, "Keuken", 0, 0, 0, 2, 1, 0, 1, 0, 2, 0],
        ],
        "material": [],
        "uv_switch": [[10, 1], [11, 1]],
        "prefer_on": 0,
    },
}


@pytest.fixture
def coordinator():
    coord = Mock(spec=DysonDataUpdateCoordinator)
    coord.data = {}
    coord.hass = Mock()
    coord.hass.loop = Mock()
    coord.hass.loop.call_soon_threadsafe = Mock(side_effect=lambda fn: fn())
    coord.async_set_updated_data = Mock()
    coord.serial_number = "7VS-EU-VFA2680A"
    coord._store_jdm_service_response = (
        DysonDataUpdateCoordinator._store_jdm_service_response.__get__(coord)
    )
    coord._handle_jdm_status_message = (
        DysonDataUpdateCoordinator._handle_jdm_status_message.__get__(coord)
    )
    return coord


class TestCoordinatorStores:
    def test_get_preference_stored(self, coordinator):
        coordinator._handle_jdm_status_message(REAL_ANSWER)
        pref = coordinator.data["jdm-properties"]["preference"]
        assert pref["data"]["prefer_on"] == 0
        assert pref["received"]
        coordinator.async_set_updated_data.assert_called_once()

    def test_error_code_ignored(self, coordinator):
        coordinator._handle_jdm_status_message({**REAL_ANSWER, "code": 7})
        assert "preference" not in coordinator.data.get("jdm-properties", {})

    def test_map_list_gives_current_map_id(self, coordinator):
        coordinator._handle_jdm_status_message(
            {
                "code": 0,
                "method": "service.get_map_list",
                "data": {
                    "map_list": [
                        {"name": "Old", "id": 1, "cur": False},
                        {"name": "Map1", "id": 1790712695, "cur": True},
                    ]
                },
            }
        )
        assert coordinator.data["jdm-properties"]["map_id"] == "1790712695"

    def test_alarm_prop_post_still_works(self, coordinator):
        coordinator._handle_jdm_status_message(
            {"method": "prop.post", "params": {"alarm": 1}}
        )
        assert coordinator.data["jdm-properties"]["alarm"] is True


@pytest.fixture
def sensor(coordinator):
    coordinator.device = MagicMock()
    coordinator.device.robot_current_map_id = "1790712695"
    coordinator.device.request_robot_room_preferences = AsyncMock()
    return DysonRobotRoomPreferencesSensor(coordinator)


class TestSensor:
    def test_unknown_until_answer(self, sensor):
        assert sensor.native_value is None
        assert sensor.extra_state_attributes == {}

    def test_prefer_on_and_decoded_rooms(self, sensor, coordinator):
        coordinator._handle_jdm_status_message(REAL_ANSWER)
        assert sensor.native_value == "uit"
        rooms = {r["naam"]: r for r in sensor.extra_state_attributes["kamers"]}
        assert rooms["Keuken"]["cleanType"] == "vacuum"
        assert rooms["Keuken"]["volgorde"] == 2
        assert rooms["Gang"]["strategie"] == "boost"
        assert rooms["Woonkamer"]["cleanType"] == "vacuumAndMop"
        assert rooms["Woonkamer"]["water"] == "high"
        assert sensor.extra_state_attributes["prefer_on"] == 0

    def test_rooms_sorted_by_order(self, sensor, coordinator):
        coordinator._handle_jdm_status_message(REAL_ANSWER)
        order = [r["naam"] for r in sensor.extra_state_attributes["kamers"]]
        assert order == [
            "Woonkamer",
            "Keuken",
            "Gang",
            "Kinderkamer",
            "Wasruimte",
            "Slaapkamer",
        ]

    def test_unknown_values_not_guessed(self, sensor, coordinator):
        row = [10, "Woonkamer", 0, 0, 3, 9, 0, 0, 0, 0, 1, 0]
        coordinator._handle_jdm_status_message(
            {
                "code": 0,
                "method": "service.get_preference",
                "data": {"room": [row], "prefer_on": 1},
            }
        )
        room = sensor.extra_state_attributes["kamers"][0]
        assert room["strategie"] == "onbekend(3)"
        assert room["water"] == "onbekend(9)"
        assert sensor.native_value == "aan"

    @pytest.mark.asyncio
    async def test_requests_at_most_every_five_minutes(self, sensor, coordinator):
        await sensor.async_update()
        await sensor.async_update()
        coordinator.device.request_robot_room_preferences.assert_awaited_once_with(
            "1790712695"
        )

    @pytest.mark.asyncio
    async def test_falls_back_to_map_list_id_then_none(self, sensor, coordinator):
        coordinator.device.robot_current_map_id = None
        await sensor.async_update()
        coordinator.device.request_robot_room_preferences.assert_awaited_with(None)
        sensor._last_request = None
        coordinator.data = {"jdm-properties": {"map_id": "42"}}
        await sensor.async_update()
        coordinator.device.request_robot_room_preferences.assert_awaited_with("42")


def test_json_room_name_is_unpacked(sensor, coordinator):
    row = [10, '{"type":"livingRoom","name":"Woonkamer"}', 2, 1, 0, 2, 1, 0, 1, 0, 1, 0]
    coordinator._handle_jdm_status_message(
        {
            "code": 0,
            "method": "service.get_preference",
            "data": {"room": [row], "prefer_on": 0},
        }
    )
    room = sensor.extra_state_attributes["kamers"][0]
    assert room["naam"] == "Woonkamer"
    assert room["type"] == "livingRoom"
    assert room["cleanType"] == "vacuumAndMop"
    assert room["raw"][0] == 2


def test_water_level_99_is_very_low(sensor, coordinator):
    row = [14, "Keuken", 0, 1, 0, 99, 0, 0, 1, 0, 2, 0]
    coordinator._handle_jdm_status_message(
        {
            "code": 0,
            "method": "service.get_preference",
            "data": {"room": [row], "prefer_on": 1},
        }
    )
    assert sensor.extra_state_attributes["kamers"][0]["water"] == "veryLow"
