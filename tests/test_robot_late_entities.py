"""Robot entities created when the first state arrives after platform setup."""

from unittest.mock import MagicMock

from custom_components.hass_dyson.robot_late_entities import (
    async_robot_state_entities,
)


def _entity(uid: str) -> MagicMock:
    entity = MagicMock()
    entity.unique_id = uid
    return entity


def _setup(data):
    coordinator = MagicMock()
    coordinator.data = data
    coordinator.serial_number = "7VS-EU-TEST"
    listeners = []
    unsubbed = []

    def add_listener(cb):
        listeners.append(cb)
        return lambda: unsubbed.append(cb)

    coordinator.async_add_listener.side_effect = add_listener
    entry = MagicMock()
    add_entities = MagicMock()
    return coordinator, entry, add_entities, listeners, unsubbed


def _builder(coordinator):
    def build():
        data = coordinator.data or {}
        found = []
        if "dockState" in data:
            found.append(_entity("dock"))
        if "volume" in data:
            found.append(_entity("volume"))
        return found

    return build


def test_state_already_present_returns_entities_without_listener():
    coordinator, entry, add, listeners, _ = _setup({"state": "X", "dockState": "IDLE"})
    result = async_robot_state_entities(entry, coordinator, add, _builder(coordinator))
    assert [e.unique_id for e in result] == ["dock"]
    assert listeners == []
    add.assert_not_called()


def test_late_state_adds_entities_once_and_unsubscribes():
    coordinator, entry, add, listeners, unsubbed = _setup({})
    result = async_robot_state_entities(entry, coordinator, add, _builder(coordinator))
    assert result == []
    assert len(listeners) == 1

    coordinator.data = {"jdm-properties": {}}  # update without the robot state
    listeners[0]()
    add.assert_not_called()
    assert unsubbed == []

    coordinator.data = {"state": "X", "dockState": "IDLE", "volume": 3}
    listeners[0]()
    add.assert_called_once()
    added = add.call_args.args[0]
    assert sorted(e.unique_id for e in added) == ["dock", "volume"]
    assert unsubbed == [listeners[0]]


def test_late_state_skips_entities_already_created():
    coordinator, entry, add, listeners, _ = _setup({"dockState": "IDLE"})
    result = async_robot_state_entities(entry, coordinator, add, _builder(coordinator))
    assert [e.unique_id for e in result] == ["dock"]

    coordinator.data = {"state": "X", "dockState": "IDLE", "volume": 3}
    listeners[0]()
    added = add.call_args.args[0]
    assert [e.unique_id for e in added] == ["volume"]


def test_listener_registration_failure_is_not_fatal():
    coordinator, entry, add, _, _ = _setup({})
    coordinator.async_add_listener.side_effect = RuntimeError("boom")
    result = async_robot_state_entities(entry, coordinator, add, _builder(coordinator))
    assert result == []


def test_unload_removes_listener():
    coordinator, entry, add, listeners, unsubbed = _setup({})
    async_robot_state_entities(entry, coordinator, add, _builder(coordinator))
    entry.async_on_unload.call_args.args[0]()
    assert unsubbed == [listeners[0]]
