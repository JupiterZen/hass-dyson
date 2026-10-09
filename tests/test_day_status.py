"""Daily house-clean bookkeeping: rules agreed 8 okt 2026."""

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from custom_components.hass_dyson import day_status as ds

AMS = ZoneInfo("Europe/Amsterdam")
ROOMS = [
    "Keuken",
    "Gang",
    "Kinderkamer",
    "Wasruimte",
    "Slaapkamer",
    "Zithoek",
    "Woonkamer",
]


def at(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 10, day, hour, minute, tzinfo=AMS)


def zones(**statuses: str) -> list[dict]:
    return [
        {"name": n.replace("_", " "), "cleanStatus": s} for n, s in statuses.items()
    ]


class TestDay:
    def test_day_runs_from_four_to_four(self):
        assert ds.day_of(at(8, 3, 59)).isoformat() == "2026-10-07"
        assert ds.day_of(at(8, 4, 0)).isoformat() == "2026-10-08"
        assert ds.day_of(at(8, 23, 59)).isoformat() == "2026-10-08"

    def test_a_run_ending_after_midnight_belongs_to_the_previous_day(self):
        state = ds.new_state()
        ds.ingest_record(state, "a", at(8, 0, 9), zones(Keuken=ds.COMPLETE))
        assert ds.evaluate(state, at(7, 23), ROOMS)["kamers"]["Keuken"]["klaar_vandaag"]
        assert not ds.evaluate(state, at(8, 10), ROOMS)["kamers"]["Keuken"][
            "klaar_vandaag"
        ]


class TestIngest:
    def test_only_complete_rooms_count_as_done(self):
        state = ds.new_state()
        ds.ingest_record(
            state,
            "r1",
            at(8, 14, 55),
            zones(
                Woonkamer="CLEAN_IN_PROGRESS", Keuken=ds.COMPLETE, Gang=ds.CANT_CLEAN
            ),
        )
        result = ds.evaluate(state, at(8, 15), ROOMS)
        assert result["klaar"] == ["Keuken"]
        assert result["cant_clean_vandaag"] == ["Gang"]
        assert result["kamers"]["Woonkamer"]["laatste_status"] == "CLEAN_IN_PROGRESS"
        assert result["kamers"]["Woonkamer"]["pogingen_vandaag"] == 1

    def test_not_requested_rooms_are_ignored(self):
        state = ds.new_state()
        ds.ingest_record(
            state,
            "r1",
            at(8, 12),
            zones(Slaapkamer=ds.COMPLETE, Keuken=ds.NOT_REQUESTED),
        )
        assert "Keuken" not in state["zones"]

    def test_a_record_is_only_counted_once(self):
        state = ds.new_state()
        assert ds.ingest_record(state, "r1", at(8, 12), zones(Gang="CLEAN_IN_PROGRESS"))
        assert not ds.ingest_record(
            state, "r1", at(8, 12), zones(Gang="CLEAN_IN_PROGRESS")
        )
        assert state["zones"]["Gang"]["attempts"] == 1

    def test_attempts_reset_on_a_new_day(self):
        state = ds.new_state()
        ds.ingest_record(state, "r1", at(7, 12), zones(Gang=ds.CANT_CLEAN))
        ds.ingest_record(state, "r2", at(8, 12), zones(Gang=ds.CANT_CLEAN))
        result = ds.evaluate(state, at(8, 13), ROOMS)
        assert result["kamers"]["Gang"]["pogingen_vandaag"] == 1

    def test_processed_list_is_bounded(self):
        state = ds.new_state()
        for i in range(100):
            ds.ingest_record(state, f"id{i}", at(8, 12), zones(Gang=ds.COMPLETE))
        assert len(state["processed"]) == 60

    def test_a_later_incomplete_attempt_does_not_undo_a_completion(self):
        state = ds.new_state()
        ds.ingest_record(state, "r1", at(8, 11), zones(Gang=ds.COMPLETE))
        ds.ingest_record(state, "r2", at(8, 15), zones(Gang="CLEAN_IN_PROGRESS"))
        result = ds.evaluate(state, at(8, 16), ROOMS)
        assert result["kamers"]["Gang"]["klaar_vandaag"]
        assert result["kamers"]["Gang"]["laatste_status"] == "CLEAN_IN_PROGRESS"


class TestOrder:
    def test_never_cleaned_rooms_come_first_in_the_fixed_order(self):
        result = ds.evaluate(ds.new_state(), at(8, 12), ROOMS)
        assert result["open"] == ROOMS
        assert not result["alles_klaar"]

    def test_oldest_completion_first_then_the_fixed_list(self):
        state = ds.new_state()
        ds.ingest_record(
            state, "a", at(6, 12), zones(Woonkamer=ds.COMPLETE, Keuken=ds.COMPLETE)
        )
        ds.ingest_record(state, "b", at(7, 12), zones(Gang=ds.COMPLETE))
        result = ds.evaluate(state, at(8, 12), ROOMS)
        # never cleaned: Kinderkamer, Wasruimte, Slaapkamer, Zithoek (fixed list),
        # then 6 okt: Keuken before Woonkamer (fixed list), then 7 okt: Gang
        assert result["open"] == [
            "Kinderkamer",
            "Wasruimte",
            "Slaapkamer",
            "Zithoek",
            "Keuken",
            "Woonkamer",
            "Gang",
        ]

    def test_same_day_completions_ignore_the_hour(self):
        state = ds.new_state()
        ds.ingest_record(state, "a", at(7, 20), zones(Keuken=ds.COMPLETE))
        ds.ingest_record(state, "b", at(7, 9), zones(Woonkamer=ds.COMPLETE))
        result = ds.evaluate(state, at(8, 12), ["Keuken", "Woonkamer"])
        assert result["open"] == ["Keuken", "Woonkamer"]

    def test_unknown_rooms_go_last_alphabetically(self):
        result = ds.evaluate(ds.new_state(), at(8, 12), ["Zolder", "Keuken", "Attic"])
        assert result["open"] == ["Keuken", "Attic", "Zolder"]

    def test_everything_done_today(self):
        state = ds.new_state()
        ds.ingest_record(
            state, "a", at(8, 12), zones(**dict.fromkeys(ROOMS, ds.COMPLETE))
        )
        result = ds.evaluate(state, at(8, 13), ROOMS)
        assert result["alles_klaar"]
        assert result["open"] == []

    def test_an_interrupted_room_returns_first_the_next_day(self):
        """Interrupted rooms keep their old completion date and so come up first."""
        state = ds.new_state()
        ds.ingest_record(
            state,
            "a",
            at(7, 12),
            zones(**dict.fromkeys(ROOMS, ds.COMPLETE)),
        )
        ds.ingest_record(
            state,
            "b",
            at(8, 15),
            zones(Keuken=ds.COMPLETE, Woonkamer="CLEAN_IN_PROGRESS"),
        )
        result = ds.evaluate(state, at(8, 16), ROOMS)
        assert (
            result["open"][0] == "Gang"
        )  # oldest completion day among open rooms: 7 okt, fixed list first
        assert "Woonkamer" in result["open"]


def test_replay_of_8_okt_2026():
    """Real day: Wasruimte 11:32 and Slaapkamer 12:01 by hand, Woonkamer 14:21 interrupted."""
    state = ds.new_state()
    ds.ingest_record(state, "wasruimte", at(8, 11, 50), zones(Wasruimte=ds.COMPLETE))
    ds.ingest_record(state, "slaapkamer", at(8, 12, 20), zones(Slaapkamer=ds.COMPLETE))
    ds.ingest_record(
        state, "woonkamer", at(8, 14, 55), zones(Woonkamer="CLEAN_IN_PROGRESS")
    )
    result = ds.evaluate(state, at(8, 18), ROOMS)
    assert result["klaar"] == ["Wasruimte", "Slaapkamer"]
    assert "Woonkamer" in result["open"]
    assert result["open"] == ["Keuken", "Gang", "Kinderkamer", "Zithoek", "Woonkamer"]


def _sensor():
    from custom_components.hass_dyson.sensor import DysonRobotDayStatusSensor

    coordinator = MagicMock()
    coordinator.serial_number = "7VS-EU-VFA2680A"
    coordinator.data = {}
    sensor = DysonRobotDayStatusSensor(coordinator)
    sensor.hass = MagicMock()
    return sensor, coordinator


class TestSensor:
    @pytest.mark.asyncio
    async def test_ingests_cloud_records_oldest_first(self):
        sensor, coordinator = _sensor()
        rec = lambda i, t: MagicMock(
            clean_id=i, start_time_epoch=t, end_time_epoch=t + 600
        )  # noqa: E731
        records = [rec("new", 1_791_470_000), rec("old", 1_791_460_000)]
        data = {
            "old": {"zones": zones(Wasruimte=ds.COMPLETE)},
            "new": {
                "zones": zones(Wasruimte="CLEAN_IN_PROGRESS", Slaapkamer=ds.COMPLETE)
            },
        }
        fetch = AsyncMock(side_effect=lambda c, cid: data[cid])
        with (
            patch(
                "custom_components.hass_dyson.sensor.fetch_clean_maps",
                AsyncMock(return_value=records),
            ),
            patch(
                "custom_components.hass_dyson.image._fetch_v2_floor_plan_data", fetch
            ),
            patch(
                "custom_components.hass_dyson.sensor._is_cleaning", return_value=False
            ),
            patch(
                "custom_components.hass_dyson.sensor._cloud_zone_names_for",
                return_value={},
            ),
        ):
            await sensor.async_update()
        assert sensor._state["processed"] == ["old", "new"]
        assert sensor._state["zones"]["Wasruimte"]["last_status"] == "CLEAN_IN_PROGRESS"
        assert sensor._attr_extra_state_attributes["processed_ids"] == ["old", "new"]

    @pytest.mark.asyncio
    async def test_waits_while_the_robot_is_cleaning(self):
        sensor, _ = _sensor()
        fetch = AsyncMock()
        with (
            patch("custom_components.hass_dyson.sensor.fetch_clean_maps", fetch),
            patch(
                "custom_components.hass_dyson.sensor._is_cleaning", return_value=True
            ),
            patch(
                "custom_components.hass_dyson.sensor._cloud_zone_names_for",
                return_value={},
            ),
        ):
            await sensor.async_update()
        fetch.assert_not_awaited()
        assert sensor._pending


class TestZoneUniverse:
    """9 okt 2026: an empty map cache must not drop never-cleaned rooms."""

    def test_empty_cache_keeps_the_last_known_rooms(self):
        sensor, _ = _sensor()
        sensor._state["zones"] = {"Woonkamer": {}}  # only one room has a record
        with patch(
            "custom_components.hass_dyson.sensor._cloud_zone_names_for"
        ) as names:
            names.return_value = {
                "10": "Woonkamer",
                "12": "Kinderkamer",
                "16": "Zithoek",
            }
            assert sensor._zone_names() == ["Woonkamer", "Kinderkamer", "Zithoek"]
            names.return_value = {}  # cache wiped by a setting change
            assert sensor._zone_names() == ["Woonkamer", "Kinderkamer", "Zithoek"]

    def test_without_any_known_rooms_the_fixed_list_is_used(self):
        sensor, _ = _sensor()
        sensor._state["zones"] = {"Woonkamer": {}}
        with patch(
            "custom_components.hass_dyson.sensor._cloud_zone_names_for", return_value={}
        ):
            names = sensor._zone_names()
        assert names == [*ds.TIEBREAK]
