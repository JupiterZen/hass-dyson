"""Tests for the Dutch robot message sensor."""

from unittest.mock import MagicMock

import pytest

from custom_components.hass_dyson.sensor import DysonRobotMessageSensor


def _sensor(faults):
    coordinator = MagicMock()
    coordinator.serial_number = "7VS-EU-TST0001A"
    coordinator.device.robot_active_faults = faults
    sensor = DysonRobotMessageSensor(coordinator)
    sensor.async_write_ha_state = MagicMock()
    sensor._handle_coordinator_update()
    return sensor


def test_no_fault():
    sensor = _sensor([])
    assert sensor.native_value == "Geen"
    assert sensor.extra_state_attributes == {"actie_nodig": False}


def test_water_tank_empty_needs_action():
    sensor = _sensor([{"faultCode": 581, "nextActionRequired": "USER_CONTINUE"}])
    assert sensor.native_value == "Schoonwatertank leeg"
    attrs = sensor.extra_state_attributes
    assert attrs["actie_nodig"] is True
    assert attrs["code"] == "581"
    assert "schoonwatertank" in attrs["wat_te_doen"].lower()


def test_informational_needs_no_action():
    sensor = _sensor([{"faultCode": "2105", "nextActionRequired": "LOG_ONLY"}])
    assert sensor.native_value == "Dock is aan het drogen"
    assert sensor.extra_state_attributes["actie_nodig"] is False


@pytest.mark.parametrize("code", [9999, "abc"])
def test_unknown_code_falls_back(code):
    sensor = _sensor([{"faultCode": code, "nextActionRequired": "USER_CONTINUE"}])
    assert sensor.native_value == f"Onbekende melding (code {code})"
    assert sensor.extra_state_attributes["actie_nodig"] is True


def test_unknown_informational_code_is_not_alarming():
    sensor = _sensor([{"faultCode": "2103", "nextActionRequired": "LOG_ONLY"}])
    assert sensor.native_value == "Informatief (code 2103)"
    assert sensor.extra_state_attributes["actie_nodig"] is False


def test_lifted_robot_codes_have_dutch_messages():
    lifted = _sensor([{"faultCode": 501, "nextActionRequired": "WAIT_TO_CLEAR"}])
    assert lifted.native_value == "Robot is opgetild (wiel hangt)"
    cont = _sensor([{"faultCode": 2509, "nextActionRequired": "USER_CONTINUE"}])
    assert cont.extra_state_attributes["actie_nodig"] is True
    assert "hervatten" in cont.native_value
