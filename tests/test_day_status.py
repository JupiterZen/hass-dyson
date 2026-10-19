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
    coordinator.serial_number = "7VS-EU-TST0001A"
    coordinator.data = {}
    sensor = DysonRobotDayStatusSensor(coordinator)
    sensor.hass = MagicMock()
    sensor.hass.async_add_executor_job = AsyncMock(side_effect=lambda f, *a: f(*a))
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


def test_live_mark_counts_for_today_but_the_record_has_the_final_say():
    state = ds.new_state()
    assert ds.mark_complete(state, "Keuken", at(9, 9, 10))
    assert not ds.mark_complete(state, "Keuken", at(9, 9, 5))
    assert ds.evaluate(state, at(9, 10), ROOMS)["kamers"]["Keuken"]["klaar_vandaag"]
    # the finished record then says the room was only thinly covered
    ds.ingest_record(
        state, "r1", at(9, 10, 20), zones(Keuken=ds.COMPLETE), coverage={"Keuken": 0.2}
    )
    result = ds.evaluate(state, at(9, 11), ROOMS)
    assert not result["kamers"]["Keuken"]["klaar_vandaag"]
    assert result["te_weinig_gedekt_vandaag"] == ["Keuken"]


def test_a_live_mark_expires_when_no_record_ever_confirms_it():
    state = ds.new_state()
    ds.mark_complete(state, "Keuken", at(9, 5))
    assert ds.evaluate(state, at(9, 10), ROOMS)["kamers"]["Keuken"]["klaar_vandaag"]
    assert not ds.evaluate(state, at(9, 12), ROOMS)["kamers"]["Keuken"]["klaar_vandaag"]


class TestCoverage:
    SQUARE = [(0.0, 0.0), (2.0, 0.0), (2.0, 2.0), (0.0, 2.0)]

    def test_full_sweep(self):
        path = [(x / 10, y / 4) for x in range(0, 21) for y in range(0, 9)]
        assert ds.coverage_fraction(self.SQUARE, path) > 0.95

    def test_a_corner_only_is_thin(self):
        path = [(0.1 + i * 0.05, 0.1) for i in range(10)]
        assert ds.coverage_fraction(self.SQUARE, path) < 0.1

    def test_repeated_passes_do_not_inflate_the_share(self):
        one = [(0.1 + i * 0.05, 0.1) for i in range(10)]
        assert ds.coverage_fraction(self.SQUARE, one * 20) == ds.coverage_fraction(
            self.SQUARE, one
        )

    def test_unmeasurable_gives_none(self):
        assert ds.coverage_fraction([(0, 0), (1, 1)], [(0, 0)]) is None


class TestThinRooms:
    def test_complete_but_thin_is_not_done_and_is_flagged(self):
        state = ds.new_state()
        ds.ingest_record(
            state,
            "r1",
            at(9, 10, 20),
            zones(Kinderkamer=ds.COMPLETE, Gang=ds.COMPLETE),
            coverage={"Kinderkamer": 0.28, "Gang": 0.70},
        )
        result = ds.evaluate(state, at(9, 11), ROOMS)
        assert result["klaar"] == ["Gang"]
        assert result["te_weinig_gedekt_vandaag"] == ["Kinderkamer"]
        assert result["kamers"]["Kinderkamer"]["dekking"] == 0.28
        assert "Kinderkamer" in result["open"]

    def test_unknown_coverage_trusts_the_robot(self):
        state = ds.new_state()
        ds.ingest_record(state, "r1", at(9, 10), zones(Gang=ds.COMPLETE), coverage={})
        assert ds.evaluate(state, at(9, 11), ROOMS)["klaar"] == ["Gang"]

    def test_replay_of_9_okt_run(self):
        """Measured on the real record: Slaapkamer 6,5 %, Kinderkamer 28 %."""
        state = ds.new_state()
        ds.ingest_record(
            state,
            "run",
            at(9, 10, 20),
            zones(**dict.fromkeys(ROOMS, ds.COMPLETE)),
            coverage={
                "Woonkamer": 0.818,
                "Slaapkamer": 0.065,
                "Zithoek": 0.884,
                "Kinderkamer": 0.28,
                "Gang": 0.701,
                "Wasruimte": 0.738,
                "Keuken": 0.847,
            },
        )
        result = ds.evaluate(state, at(9, 11), ROOMS)
        assert sorted(result["open"]) == ["Kinderkamer", "Slaapkamer"]
        assert not result["alles_klaar"]


class TestLiveCompletions:
    @pytest.mark.asyncio
    async def test_rooms_finished_during_a_run_count_straight_away(self):
        sensor, _ = _sensor()
        tally = MagicMock(
            attributes={
                "zone_names": ["Kinderkamer", "Zithoek"],
                "last_reset_date": ds.day_of(at(9, 12)).isoformat(),
            }
        )
        registry = MagicMock()
        registry.async_get_entity_id.return_value = "sensor.ronde"
        sensor.hass.states.get.return_value = tally
        with (
            patch(
                "homeassistant.helpers.entity_registry.async_get", return_value=registry
            ),
            patch(
                "custom_components.hass_dyson.sensor._is_cleaning", return_value=True
            ),
            patch(
                "custom_components.hass_dyson.sensor._cloud_zone_names_for",
                return_value={},
            ),
            patch(
                "custom_components.hass_dyson.sensor.dt_util.now",
                return_value=at(9, 9, 20),
            ),
        ):
            await sensor.async_update()
        assert sensor._attr_extra_state_attributes["klaar"] == [
            "Kinderkamer",
            "Zithoek",
        ]

    @pytest.mark.asyncio
    async def test_a_stale_tally_from_another_day_is_ignored(self):
        sensor, _ = _sensor()
        tally = MagicMock(
            attributes={"zone_names": ["Keuken"], "last_reset_date": "2026-10-08"}
        )
        registry = MagicMock()
        registry.async_get_entity_id.return_value = "sensor.ronde"
        sensor.hass.states.get.return_value = tally
        with (
            patch(
                "homeassistant.helpers.entity_registry.async_get", return_value=registry
            ),
            patch(
                "custom_components.hass_dyson.sensor._is_cleaning", return_value=True
            ),
            patch(
                "custom_components.hass_dyson.sensor._cloud_zone_names_for",
                return_value={},
            ),
            patch(
                "custom_components.hass_dyson.sensor.dt_util.now",
                return_value=at(9, 9, 20),
            ),
        ):
            await sensor.async_update()
        assert sensor._attr_extra_state_attributes["klaar"] == []


class TestGivingUp:
    def test_two_failed_attempts_give_a_room_up_for_the_day(self):
        state = ds.new_state()
        ds.ingest_record(
            state, "a", at(9, 10), zones(Slaapkamer=ds.COMPLETE), {"Slaapkamer": 0.06}
        )
        one = ds.evaluate(state, at(9, 11), ROOMS)
        assert "Slaapkamer" in one["open"] and one["opgegeven_vandaag"] == []
        ds.ingest_record(
            state, "b", at(9, 18), zones(Slaapkamer=ds.COMPLETE), {"Slaapkamer": 0.065}
        )
        two = ds.evaluate(state, at(9, 19), ROOMS)
        assert two["opgegeven_vandaag"] == ["Slaapkamer"]
        assert "Slaapkamer" not in two["open"]
        assert not two["alles_klaar"]

    def test_the_next_day_starts_fresh(self):
        state = ds.new_state()
        for i, hour in enumerate((10, 18)):
            ds.ingest_record(
                state, f"r{i}", at(9, hour), zones(Slaapkamer=ds.CANT_CLEAN)
            )
        assert ds.evaluate(state, at(9, 19), ROOMS)["opgegeven_vandaag"] == [
            "Slaapkamer"
        ]
        tomorrow = ds.evaluate(state, at(10, 9), ROOMS)
        assert tomorrow["opgegeven_vandaag"] == []
        assert "Slaapkamer" in tomorrow["open"]

    def test_a_completed_manual_run_clears_it(self):
        state = ds.new_state()
        for i, hour in enumerate((10, 18)):
            ds.ingest_record(
                state, f"r{i}", at(9, hour), zones(Slaapkamer=ds.CANT_CLEAN)
            )
        ds.ingest_record(
            state, "hand", at(9, 20), zones(Slaapkamer=ds.COMPLETE), {"Slaapkamer": 0.8}
        )
        result = ds.evaluate(state, at(9, 21), ROOMS)
        assert "Slaapkamer" in result["klaar"]
        assert result["opgegeven_vandaag"] == []

    def test_interrupted_rooms_are_never_given_up(self):
        """Stopping because someone came home is not a failure."""
        state = ds.new_state()
        for i, hour in enumerate((10, 12, 14)):
            ds.ingest_record(
                state, f"r{i}", at(9, hour), zones(Woonkamer="CLEAN_IN_PROGRESS")
            )
        result = ds.evaluate(state, at(9, 15), ROOMS)
        assert "Woonkamer" in result["open"]
        assert result["opgegeven_vandaag"] == []

    def test_rooms_the_robot_never_reached_are_not_attempts(self):
        state = ds.new_state()
        ds.ingest_record(
            state, "a", at(9, 10), zones(Gang=ds.COMPLETE, Slaapkamer="CLEAN_PENDING")
        )
        assert "Slaapkamer" not in state["zones"]

    def test_replay_of_9_okt_evening(self):
        state = ds.new_state()
        ds.ingest_record(
            state,
            "morgen",
            at(9, 10, 20),
            zones(**dict.fromkeys(ROOMS, ds.COMPLETE)),
            coverage={
                "Woonkamer": 0.82,
                "Slaapkamer": 0.065,
                "Zithoek": 0.88,
                "Kinderkamer": 0.28,
                "Gang": 0.70,
                "Wasruimte": 0.74,
                "Keuken": 0.85,
            },
        )
        ds.ingest_record(
            state,
            "avond",
            at(9, 18, 4),
            zones(Kinderkamer=ds.COMPLETE, Slaapkamer=ds.COMPLETE),
            coverage={"Kinderkamer": 0.45, "Slaapkamer": 0.064},
        )
        result = ds.evaluate(state, at(9, 18, 30), ROOMS)
        assert result["open"] == []
        assert result["opgegeven_vandaag"] == ["Slaapkamer"]
        assert "Kinderkamer" in result["klaar"]
        assert not result["alles_klaar"]


def typed(cleanType: str, **statuses: str) -> list[dict]:
    """Zone entries carrying the record's own cleanType setting."""
    return [
        {
            "name": n.replace("_", " "),
            "cleanStatus": s,
            "settings": {"cleanType": cleanType},
        }
        for n, s in statuses.items()
    ]


class TestCleaningTypeComponents:
    """A room is done when what it wants now has been done today (19 okt 2026)."""

    def test_components_per_type(self):
        assert ds.components("vacuum") == {ds.VACUUM}
        assert ds.components("mop") == {ds.MOP}
        assert ds.components("vacuumAndMop") == {ds.VACUUM, ds.MOP}
        assert ds.components("vacuumThenMop") == {ds.VACUUM, ds.MOP}
        assert ds.components(None) == {ds.VACUUM, ds.MOP}

    def test_vacuumed_room_becomes_open_when_switched_to_mopping(self):
        state = ds.new_state()
        ds.ingest_record(state, "a", at(8, 9), typed("vacuum", Keuken=ds.COMPLETE))
        wanted_vacuum = ds.evaluate(state, at(8, 10), ROOMS, {"Keuken": "vacuum"})
        assert wanted_vacuum["kamers"]["Keuken"]["klaar_vandaag"]
        for wanted in ("mop", "vacuumAndMop", "vacuumThenMop"):
            result = ds.evaluate(state, at(8, 10), ROOMS, {"Keuken": wanted})
            room = result["kamers"]["Keuken"]
            assert not room["klaar_vandaag"], wanted
            assert "Keuken" in result["open"]
            assert "Keuken" in result["onvolledig_vandaag"]
            assert room["gedaan_vandaag"] == [ds.VACUUM]
        # mop-only still needs only the mop part
        mop = ds.evaluate(state, at(8, 10), ROOMS, {"Keuken": "mop"})
        assert mop["kamers"]["Keuken"]["ontbreekt"] == [ds.MOP]

    def test_mopped_run_covers_vacuum_only_wish(self):
        state = ds.new_state()
        ds.ingest_record(
            state, "a", at(8, 9), typed("vacuumAndMop", Keuken=ds.COMPLETE)
        )
        for wanted in ("vacuum", "mop", "vacuumAndMop"):
            result = ds.evaluate(state, at(8, 10), ROOMS, {"Keuken": wanted})
            assert result["kamers"]["Keuken"]["klaar_vandaag"], wanted

    def test_vacuum_then_mop_runs_complete_both(self):
        state = ds.new_state()
        ds.ingest_record(
            state, "a", at(8, 9), typed("vacuumThenMop", Keuken=ds.COMPLETE)
        )
        result = ds.evaluate(state, at(8, 10), ROOMS, {"Keuken": "vacuumAndMop"})
        assert result["kamers"]["Keuken"]["klaar_vandaag"]

    def test_two_runs_add_up(self):
        state = ds.new_state()
        ds.ingest_record(state, "a", at(8, 7), typed("vacuum", Keuken=ds.COMPLETE))
        ds.ingest_record(state, "b", at(8, 9), typed("mop", Keuken=ds.COMPLETE))
        result = ds.evaluate(state, at(8, 10), ROOMS, {"Keuken": "vacuumAndMop"})
        assert result["kamers"]["Keuken"]["klaar_vandaag"]

    def test_switching_back_to_vacuum_makes_it_done_again(self):
        state = ds.new_state()
        ds.ingest_record(state, "a", at(8, 9), typed("vacuum", Keuken=ds.COMPLETE))
        assert not ds.evaluate(state, at(8, 10), ROOMS, {"Keuken": "mop"})["kamers"][
            "Keuken"
        ]["klaar_vandaag"]
        assert ds.evaluate(state, at(8, 11), ROOMS, {"Keuken": "vacuum"})["kamers"][
            "Keuken"
        ]["klaar_vandaag"]

    def test_yesterdays_mop_does_not_count_today(self):
        state = ds.new_state()
        ds.ingest_record(
            state, "a", at(7, 9), typed("vacuumAndMop", Keuken=ds.COMPLETE)
        )
        ds.ingest_record(state, "b", at(8, 9), typed("vacuum", Keuken=ds.COMPLETE))
        result = ds.evaluate(state, at(8, 10), ROOMS, {"Keuken": "vacuumAndMop"})
        assert not result["kamers"]["Keuken"]["klaar_vandaag"]

    def test_run_snapshot_narrows_a_lagging_record(self):
        """The record said vacuumAndMop 14 s after the user chose vacuum only."""
        state = ds.new_state()
        ds.ingest_record(
            state,
            "a",
            at(8, 9),
            typed("vacuumAndMop", Keuken=ds.COMPLETE),
            run_components={"Keuken": ds.components("vacuum")},
        )
        result = ds.evaluate(state, at(8, 10), ROOMS, {"Keuken": "mop"})
        assert not result["kamers"]["Keuken"]["klaar_vandaag"]
        assert result["kamers"]["Keuken"]["gedaan_vandaag"] == [ds.VACUUM]

    def test_unknown_wish_keeps_old_behaviour(self):
        state = ds.new_state()
        ds.ingest_record(state, "a", at(8, 9), typed("vacuum", Keuken=ds.COMPLETE))
        result = ds.evaluate(state, at(8, 10), ROOMS)
        assert result["kamers"]["Keuken"]["klaar_vandaag"]
        assert result["kamers"]["Keuken"]["gewenst"] is None

    def test_state_saved_before_the_split_counts_as_both_parts(self):
        state = {
            "version": ds.STATE_VERSION,
            "zones": {
                "Keuken": {
                    "last_completed": at(8, 9).isoformat(),
                    "live_completed": None,
                    "last_coverage": 0.9,
                    "last_status": ds.COMPLETE,
                    "last_attempt": at(8, 9).isoformat(),
                    "attempt_day": "2026-10-08",
                    "attempts": 1,
                }
            },
            "processed": ["old"],
        }
        result = ds.evaluate(state, at(8, 10), ROOMS, {"Keuken": "vacuumAndMop"})
        assert result["kamers"]["Keuken"]["klaar_vandaag"]
        # a later vacuum-only run must not turn the legacy completion into a mop
        ds.ingest_record(state, "n", at(9, 9), typed("vacuum", Keuken=ds.COMPLETE))
        again = ds.evaluate(state, at(9, 10), ROOMS, {"Keuken": "vacuumAndMop"})
        assert not again["kamers"]["Keuken"]["klaar_vandaag"]


class TestWantedTypesAndRunSnapshots:
    def test_wanted_types_read_from_the_type_selects(self):
        sensor, coordinator = _sensor()
        coordinator.config_entry.entry_id = "entry1"
        make = lambda uid, name, eid: MagicMock(  # noqa: E731
            domain="select", unique_id=uid, original_name=name, entity_id=eid
        )
        entries = [
            make(
                "7VS-EU-TST0001A_zone_clean_type_1_14",
                "Keuken reinigingstype",
                "select.k",
            ),
            make(
                "7VS-EU-TST0001A_zone_clean_type_1_13",
                "Gang reinigingstype",
                "select.g",
            ),
            make(
                "7VS-EU-TST0001A_zone_water_level_1_14",
                "Keuken waterniveau",
                "select.w",
            ),
        ]
        states = {
            "select.k": MagicMock(state="Dweilen"),
            "select.g": MagicMock(state="unavailable"),
            "select.w": MagicMock(state="laag"),
        }
        sensor.hass.states.get.side_effect = states.get
        with (
            patch(
                "homeassistant.helpers.entity_registry.async_entries_for_config_entry",
                return_value=entries,
            ),
            patch("homeassistant.helpers.entity_registry.async_get"),
        ):
            assert sensor._wanted_types() == {"Keuken": "mop"}

    def test_snapshot_narrows_the_record_of_that_run(self):
        sensor, _ = _sensor()
        sensor._run_snapshots = [
            {"t": 1_000.0, "wanted": {"Keuken": "vacuum", "Gang": "vacuumAndMop"}},
            {"t": 9_000.0, "wanted": {"Keuken": "mop"}},
        ]
        assert sensor._run_components(1_020.0) == {
            "Keuken": ds.components("vacuum"),
            "Gang": ds.components("vacuumAndMop"),
        }
        assert sensor._run_components(5_000.0) == {}  # no snapshot near that start
        assert sensor._run_components(None) == {}

    @pytest.mark.asyncio
    async def test_snapshot_is_taken_once_per_run(self):
        sensor, _ = _sensor()
        with (
            patch.object(sensor, "_wanted_types", return_value={"Keuken": "vacuum"}),
            patch.object(sensor, "_mark_live_completions"),
            patch(
                "custom_components.hass_dyson.sensor._is_cleaning", return_value=True
            ),
            patch(
                "custom_components.hass_dyson.sensor._cloud_zone_names_for",
                return_value={},
            ),
        ):
            await sensor.async_update()
            await sensor.async_update()
        assert len(sensor._run_snapshots) == 1
        assert sensor._attr_extra_state_attributes["run_snapshots"]

    @pytest.mark.asyncio
    async def test_lagging_record_is_narrowed_by_the_snapshot(self):
        sensor, _ = _sensor()
        start = 1_791_470_000
        sensor._run_snapshots = [{"t": start + 15.0, "wanted": {"Keuken": "vacuum"}}]
        rec = MagicMock(
            clean_id="r", start_time_epoch=start, end_time_epoch=start + 300
        )
        data = {"r": {"zones": typed("vacuumAndMop", Keuken=ds.COMPLETE)}}
        with (
            patch(
                "custom_components.hass_dyson.sensor.fetch_clean_maps",
                AsyncMock(return_value=[rec]),
            ),
            patch(
                "custom_components.hass_dyson.image._fetch_v2_floor_plan_data",
                AsyncMock(side_effect=lambda c, cid: data[cid]),
            ),
            patch(
                "custom_components.hass_dyson.sensor._is_cleaning", return_value=False
            ),
            patch.object(sensor, "_wanted_types", return_value={"Keuken": "mop"}),
            patch(
                "custom_components.hass_dyson.sensor._cloud_zone_names_for",
                return_value={},
            ),
        ):
            await sensor.async_update()
        zone = sensor._state["zones"]["Keuken"]
        assert zone["last_vacuumed"] is not None
        assert zone["last_mopped"] is None  # the lagging record said mop; it was not
