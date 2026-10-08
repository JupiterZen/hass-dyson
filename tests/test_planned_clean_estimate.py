"""Planned-clean duration: app-shaped clean-estimation request and its sensor."""

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import httpx
import pytest
from libdyson_rest.exceptions import DysonAPIError, DysonAuthError

from custom_components.hass_dyson.sensor import DysonRobotPlannedCleanEstimateSensor
from custom_components.hass_dyson.services import _post_clean_estimate

SETTINGS = {"cleanType": "vacuum", "cleaningStrategy": "auto", "waterLevel": "high"}


def _zone(zid, name, order, area=3.0):
    return {"id": zid, "name": name, "order": order, "area": area, "settings": SETTINGS}


def _client(response=None, error=None):
    http = MagicMock()
    if error is not None:
        http.post = AsyncMock(side_effect=error)
    else:
        http.post = AsyncMock(return_value=response)
    client = MagicMock()
    client.country = "NL"
    client._get_client = AsyncMock(return_value=http)
    return client, http


def _ok(payload):
    r = MagicMock()
    r.raise_for_status = Mock()
    r.json = Mock(return_value=payload)
    return r


class TestPostCleanEstimate:
    @pytest.mark.asyncio
    async def test_body_is_app_shaped_and_ordered(self):
        client, http = _client(_ok({"duration": 25, "charges": 0}))
        zones = [_zone("13", "Gang", 3), _zone("14", "Keuken", 2)]
        answer = await _post_clean_estimate(client, "S", "1790712695", zones)
        assert answer == {"duration": 25, "charges": 0}
        url = http.post.call_args[0][0]
        assert url.endswith("/v2/app/S/persistent-maps/1790712695/clean-estimation")
        body = http.post.call_args[1]["json"]
        assert [z["id"] for z in body["zones"]] == ["14", "13"]
        assert body["zones"][0] == {"id": "14", "area": 3.0, "settings": SETTINGS}

    @pytest.mark.asyncio
    async def test_gateway_timeout_becomes_api_error(self):
        resp = httpx.Response(504, request=httpx.Request("POST", "https://x"))
        err = httpx.HTTPStatusError("504", request=resp.request, response=resp)
        client, _ = _client(error=err)
        with pytest.raises(DysonAPIError):
            await _post_clean_estimate(client, "S", "1", [_zone("14", "Keuken", 2)])

    @pytest.mark.asyncio
    async def test_401_becomes_auth_error(self):
        resp = httpx.Response(401, request=httpx.Request("POST", "https://x"))
        err = httpx.HTTPStatusError("401", request=resp.request, response=resp)
        client, _ = _client(error=err)
        with pytest.raises(DysonAuthError):
            await _post_clean_estimate(client, "S", "1", [_zone("14", "Keuken", 2)])


@pytest.fixture
def coordinator():
    coord = MagicMock()
    coord.serial_number = "7VS-EU-VFA2680A"
    coord.config_entry.entry_id = "entry"

    @asynccontextmanager
    async def cloud():
        yield MagicMock()

    coord.async_cloud_client = cloud
    return coord


ZONES = [
    _zone("14", "Keuken", 2),
    _zone("13", "Gang", 3),
    _zone("15", "Wasruimte", 5),
    _zone("11", "Slaapkamer", 6),
]


def _patches(selected, answer=None, error=None, zones=ZONES):
    post = (
        AsyncMock(return_value=answer)
        if error is None
        else AsyncMock(side_effect=error)
    )
    return (
        patch.object(
            DysonRobotPlannedCleanEstimateSensor,
            "_selected_zone_names",
            return_value=selected,
        ),
        patch(
            "custom_components.hass_dyson.services._fetch_zone_settings_for_current_map",
            AsyncMock(return_value=("1790712695", zones)),
        ),
        patch("custom_components.hass_dyson.services._post_clean_estimate", post),
        post,
    )


class TestSensor:
    @pytest.mark.asyncio
    async def test_duration_charges_and_room_order(self, coordinator):
        sensor = DysonRobotPlannedCleanEstimateSensor(coordinator)
        p1, p2, p3, post = _patches(
            ["Slaapkamer", "Keuken", "Gang", "Wasruimte"],
            {"duration": 25, "charges": 0},
        )
        with p1, p2, p3:
            await sensor.async_update()
        assert sensor.native_value == 25
        assert sensor.extra_state_attributes["kamers"] == [
            "Keuken",
            "Gang",
            "Wasruimte",
            "Slaapkamer",
        ]
        assert sensor.extra_state_attributes["laadstops"] == 0
        assert sensor.extra_state_attributes["fout"] is None

    @pytest.mark.asyncio
    async def test_cached_until_selection_changes(self, coordinator):
        sensor = DysonRobotPlannedCleanEstimateSensor(coordinator)
        p1, p2, p3, post = _patches(["Keuken"], {"duration": 6, "charges": 0})
        with p1, p2, p3:
            await sensor.async_update()
            await sensor.async_update()
        assert post.await_count == 1
        p1, p2, p3, post2 = _patches(["Keuken", "Gang"], {"duration": 12, "charges": 0})
        with p1, p2, p3:
            await sensor.async_update()
        assert post2.await_count == 1
        assert sensor.native_value == 12

    @pytest.mark.asyncio
    async def test_failure_keeps_last_value_and_sets_error(self, coordinator):
        sensor = DysonRobotPlannedCleanEstimateSensor(coordinator)
        p1, p2, p3, _ = _patches(["Keuken"], {"duration": 6, "charges": 0})
        with p1, p2, p3:
            await sensor.async_update()
        p1, p2, p3, _ = _patches(["Keuken", "Gang"], error=DysonAPIError("504"))
        with p1, p2, p3:
            await sensor.async_update()
        assert sensor.native_value == 6
        assert "504" in sensor.extra_state_attributes["fout"]

    @pytest.mark.asyncio
    async def test_no_selection_gives_no_duration(self, coordinator):
        sensor = DysonRobotPlannedCleanEstimateSensor(coordinator)
        p1, p2, p3, post = _patches([])
        with p1, p2, p3:
            await sensor.async_update()
        assert sensor.native_value is None
        post.assert_not_awaited()


class TestSelectedZoneNames:
    def _sensor(self):
        coordinator = MagicMock()
        coordinator.serial_number = "S"
        sensor = DysonRobotPlannedCleanEstimateSensor(coordinator)
        sensor.hass = MagicMock()
        return sensor

    def test_open_rooms_from_the_day_status_sensor_win(self):
        sensor = self._sensor()
        registry = MagicMock()
        registry.async_get_entity_id.return_value = "sensor.x_dagstatus"
        sensor.hass.states.get.return_value = MagicMock(
            attributes={"open": ["Kinderkamer", "Zithoek"]}
        )
        with patch(
            "homeassistant.helpers.entity_registry.async_get", return_value=registry
        ):
            assert sensor._selected_zone_names() == ["Kinderkamer", "Zithoek"]
        registry.async_get_entity_id.assert_called_once_with(
            "sensor", "hass_dyson", "S_day_status"
        )

    def test_falls_back_to_the_target_switches_without_a_day_status(self):
        sensor = self._sensor()
        registry = MagicMock()
        registry.async_get_entity_id.return_value = None
        with (
            patch(
                "homeassistant.helpers.entity_registry.async_get",
                return_value=registry,
            ),
            patch(
                "homeassistant.helpers.entity_registry.async_entries_for_config_entry",
                return_value=[],
            ),
        ):
            assert sensor._selected_zone_names() == []
