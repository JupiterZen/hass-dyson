"""ROBOT_STATE_TO_HA_STATE: states that must not claim the robot is on the dock."""

from homeassistant.components.vacuum import VacuumActivity

from custom_components.hass_dyson.const import ROBOT_STATE_TO_HA_STATE


def test_inactive_discharging_is_not_docked():
    # 3 okt 2026: robot paused 4.5 m from the dock, battery flat for 6 h,
    # reported INACTIVE_DISCHARGING and HA showed "docked".
    assert ROBOT_STATE_TO_HA_STATE["INACTIVE_DISCHARGING"] == VacuumActivity.IDLE


def test_on_dock_states_still_docked():
    for state in ("INACTIVE_CHARGED", "INACTIVE_CHARGING", "FULL_CLEAN_CHARGING"):
        assert ROBOT_STATE_TO_HA_STATE[state] == VacuumActivity.DOCKED
