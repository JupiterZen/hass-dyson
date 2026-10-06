"""Tests for the separate "jdm" MQTT protocol (RB05 robots only).

Discovered 30 sep 2026 via full MQTT payload logging against a live
toggle of the voice-notifications setting in the MyDyson app — see
dyson/robot-probe/onderzoek/2026-09-30-jdm-protocol-ontdekt-en-spraak-
alarm.md. Covers the write path (device._send_robot_jdm_command /
set_robot_voice_notifications), the read/state path
(coordinator._handle_jdm_status_message), and the switch entity built
on top of both (DysonRobotVoiceNotificationsSwitch).
"""

import json
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest

from custom_components.hass_dyson.coordinator import DysonDataUpdateCoordinator
from custom_components.hass_dyson.device import DysonDevice
from custom_components.hass_dyson.switch import DysonRobotVoiceNotificationsSwitch

# ---------------------------------------------------------------------------
# device._send_robot_jdm_command / set_robot_voice_notifications
# ---------------------------------------------------------------------------


class TestSendRobotJdmCommand:
    """Test the low-level jdm command publish path."""

    @pytest.fixture
    def mock_hass(self):
        hass = MagicMock()
        hass.async_add_executor_job = AsyncMock()
        return hass

    @pytest.fixture
    def mock_mqtt_client(self):
        client = MagicMock()
        client.is_connected = MagicMock(return_value=True)
        client.publish = MagicMock()
        return client

    @pytest.fixture
    def connected_device(self, mock_hass, mock_mqtt_client):
        device = DysonDevice(
            hass=mock_hass,
            serial_number="RB05-EU-TST0000A",
            host="192.168.1.100",
            credential="test_cred",
            mqtt_prefix="RB05",
        )
        device._connected = True
        device._mqtt_client = mock_mqtt_client
        return device

    @pytest.mark.asyncio
    async def test_publishes_to_jdm_command_topic(
        self, connected_device, mock_mqtt_client
    ):
        await connected_device._send_robot_jdm_command("prop.set", {"alarm": 1})

        assert mock_mqtt_client.publish.call_count == 1
        topic, message = mock_mqtt_client.publish.call_args[0]
        assert topic == "RB05/RB05-EU-TST0000A/command/jdm"

        payload = json.loads(message)
        assert payload["method"] == "prop.set"
        assert payload["params"] == {"alarm": 1}
        assert payload["version"] == "1.0.1"
        assert "msgId" in payload
        assert "time" in payload

    @pytest.mark.asyncio
    async def test_raises_when_not_connected(self, mock_hass):
        device = DysonDevice(
            hass=mock_hass,
            serial_number="RB05-EU-TST0000A",
            host="192.168.1.100",
            credential="test_cred",
            mqtt_prefix="RB05",
        )
        device._connected = False
        device._mqtt_client = None

        with pytest.raises(RuntimeError):
            await device._send_robot_jdm_command("prop.set", {"alarm": 1})


class TestSetRobotVoiceNotifications:
    """Test the public write method used by the switch entity."""

    @pytest.fixture
    def device_with_jdm(self):
        device = DysonDevice(
            hass=MagicMock(),
            serial_number="RB05-EU-TST0000A",
            host="192.168.1.100",
            credential="test_cred",
        )
        device._send_robot_jdm_command = AsyncMock()
        return device

    @pytest.mark.asyncio
    async def test_enable_sends_alarm_1(self, device_with_jdm):
        await device_with_jdm.set_robot_voice_notifications(True)
        device_with_jdm._send_robot_jdm_command.assert_awaited_once_with(
            "prop.set", {"alarm": 1}
        )

    @pytest.mark.asyncio
    async def test_disable_sends_alarm_0(self, device_with_jdm):
        await device_with_jdm.set_robot_voice_notifications(False)
        device_with_jdm._send_robot_jdm_command.assert_awaited_once_with(
            "prop.set", {"alarm": 0}
        )


# ---------------------------------------------------------------------------
# coordinator._handle_jdm_status_message
# ---------------------------------------------------------------------------


class TestHandleJdmStatusMessage:
    """Test the coordinator's parsing of status/jdm broadcasts."""

    @pytest.fixture
    def coordinator(self):
        coord = Mock(spec=DysonDataUpdateCoordinator)
        coord.data = {}
        coord.hass = Mock()
        coord.hass.loop = Mock()
        coord.hass.loop.call_soon_threadsafe = Mock(side_effect=lambda fn: fn())
        coord.async_set_updated_data = Mock()
        # Bind the real method under test to the mock instance.
        coord._handle_jdm_status_message = (
            DysonDataUpdateCoordinator._handle_jdm_status_message.__get__(coord)
        )
        return coord

    def test_prop_post_with_alarm_true_updates_state(self, coordinator):
        coordinator._handle_jdm_status_message(
            {"method": "prop.post", "params": {"alarm": 1}}
        )
        assert coordinator.data["jdm-properties"]["alarm"] is True
        coordinator.async_set_updated_data.assert_called_once_with(coordinator.data)

    def test_prop_post_with_alarm_false_updates_state(self, coordinator):
        coordinator._handle_jdm_status_message(
            {"method": "prop.post", "params": {"alarm": 0}}
        )
        assert coordinator.data["jdm-properties"]["alarm"] is False

    def test_prop_set_ack_is_ignored(self, coordinator):
        """The write-ack shape carries no value, only a per-property success code."""
        coordinator._handle_jdm_status_message(
            {
                "code": 0,
                "method": "prop.set",
                "data": {"property": [{"prop": "alarm", "code": 0}]},
            }
        )
        assert "jdm-properties" not in coordinator.data
        coordinator.async_set_updated_data.assert_not_called()

    def test_unrelated_prop_post_is_ignored(self, coordinator):
        coordinator._handle_jdm_status_message(
            {"method": "prop.post", "params": {"someOtherField": 5}}
        )
        assert "jdm-properties" not in coordinator.data
        coordinator.async_set_updated_data.assert_not_called()

    def test_malformed_message_does_not_raise(self, coordinator):
        coordinator._handle_jdm_status_message({"method": "prop.post", "params": None})
        coordinator.async_set_updated_data.assert_not_called()

    def test_preserves_existing_jdm_properties(self, coordinator):
        coordinator.data = {"jdm-properties": {"otherField": "keep-me"}}
        coordinator._handle_jdm_status_message(
            {"method": "prop.post", "params": {"alarm": 1}}
        )
        assert coordinator.data["jdm-properties"]["otherField"] == "keep-me"
        assert coordinator.data["jdm-properties"]["alarm"] is True


# ---------------------------------------------------------------------------
# switch.DysonRobotVoiceNotificationsSwitch
# ---------------------------------------------------------------------------


class TestDysonRobotVoiceNotificationsSwitch:
    """Test the switch entity built on top of jdm-properties state."""

    @pytest.fixture
    def mock_coordinator(self):
        coordinator = Mock()
        coordinator.serial_number = "RB05-EU-TST0000A"
        coordinator.device_name = "Spot+Scrub Ai"
        coordinator.device = Mock()
        coordinator.device.set_robot_voice_notifications = AsyncMock()
        coordinator.config_entry = Mock()
        coordinator.config_entry.data = {"device_name": "Spot+Scrub"}
        coordinator.data = {"jdm-properties": {"alarm": True}}
        return coordinator

    def test_unique_id(self, mock_coordinator):
        entity = DysonRobotVoiceNotificationsSwitch(mock_coordinator)
        assert entity._attr_unique_id == "RB05-EU-TST0000A_robot_voice_notifications"

    def test_translation_key(self, mock_coordinator):
        entity = DysonRobotVoiceNotificationsSwitch(mock_coordinator)
        assert entity._attr_translation_key == "robot_voice_notifications"

    def test_is_on_reflects_jdm_properties(self, mock_coordinator):
        entity = DysonRobotVoiceNotificationsSwitch(mock_coordinator)
        assert entity._attr_is_on is True

    def test_is_on_false(self, mock_coordinator):
        mock_coordinator.data = {"jdm-properties": {"alarm": False}}
        entity = DysonRobotVoiceNotificationsSwitch(mock_coordinator)
        assert entity._attr_is_on is False

    def test_is_on_unknown_when_never_broadcast(self, mock_coordinator):
        """No jdm-properties key at all yet -> unknown state, not False."""
        mock_coordinator.data = {}
        entity = DysonRobotVoiceNotificationsSwitch(mock_coordinator)
        assert entity._attr_is_on is None

    def test_handle_coordinator_update_reflects_new_state(self, mock_coordinator):
        entity = DysonRobotVoiceNotificationsSwitch(mock_coordinator)
        entity.async_write_ha_state = MagicMock()
        assert entity._attr_is_on is True

        mock_coordinator.data = {"jdm-properties": {"alarm": False}}
        entity._handle_coordinator_update()
        assert entity._attr_is_on is False

    @pytest.mark.asyncio
    async def test_turn_on_calls_device_method_true(self, mock_coordinator):
        entity = DysonRobotVoiceNotificationsSwitch(mock_coordinator)
        await entity.async_turn_on()
        mock_coordinator.device.set_robot_voice_notifications.assert_awaited_once_with(
            True
        )

    @pytest.mark.asyncio
    async def test_turn_off_calls_device_method_false(self, mock_coordinator):
        entity = DysonRobotVoiceNotificationsSwitch(mock_coordinator)
        await entity.async_turn_off()
        mock_coordinator.device.set_robot_voice_notifications.assert_awaited_once_with(
            False
        )

    @pytest.mark.asyncio
    async def test_turn_on_noop_when_no_device(self, mock_coordinator):
        """No device on the coordinator -> silently do nothing, no exception."""
        mock_coordinator.device = None
        entity = DysonRobotVoiceNotificationsSwitch(mock_coordinator)
        await entity.async_turn_on()  # Should not raise

    @pytest.mark.asyncio
    async def test_turn_on_handles_communication_error(self, mock_coordinator):
        mock_coordinator.device.set_robot_voice_notifications.side_effect = (
            ConnectionError("mqtt down")
        )
        entity = DysonRobotVoiceNotificationsSwitch(mock_coordinator)
        await entity.async_turn_on()  # Should not raise, just log


class TestRequestRobotRoomPreferences:
    @pytest.mark.asyncio
    async def test_sends_get_preference_with_int_map_id(self):
        device = DysonDevice(
            hass=MagicMock(),
            serial_number="RB05-EU-TST0000A",
            host="h",
            credential="c",
            mqtt_prefix="RB05",
        )
        device._send_robot_jdm_command = AsyncMock()
        await device.request_robot_room_preferences("1790712695")
        device._send_robot_jdm_command.assert_awaited_once_with(
            "service.get_preference", {"map_id": 1790712695}
        )

    @pytest.mark.asyncio
    async def test_asks_map_list_without_map_id(self):
        device = DysonDevice(
            hass=MagicMock(),
            serial_number="RB05-EU-TST0000A",
            host="h",
            credential="c",
            mqtt_prefix="RB05",
        )
        device._send_robot_jdm_command = AsyncMock()
        await device.request_robot_room_preferences(None)
        device._send_robot_jdm_command.assert_awaited_once_with(
            "service.get_map_list", {}
        )
