"""Tests for the robot vacuum voice language select entity.

Changing the voice language is an async download/install flow, not an
immediate write — VERIFIED 1 sep 2026 (run-6-probe.log): SET-VOICE-LANGUAGE
starts a background download, REQUEST-VOICE-DOWNLOAD-STATUS polls it, and
the robot pushes VOICE-DOWNLOAD-STATUS messages until install_complete,
at which point CURRENT-STATE's voiceLanguage updates. Only ja-JP and en-US
have ever been observed — the options list is deliberately incomplete.
"""

from unittest.mock import AsyncMock, MagicMock, Mock

import pytest

from custom_components.hass_dyson.const import CONF_HOSTNAME
from custom_components.hass_dyson.select import (
    DysonRobotVoiceLanguageSelect,
    async_setup_entry as select_setup_entry,
)


@pytest.fixture
def mock_coordinator():
    """Create a mock coordinator for an RB05 robot reporting voiceLanguage."""
    coordinator = Mock()
    coordinator.serial_number = "RB05-EU-TST0000A"
    coordinator.device = Mock()
    coordinator.device.robot_voice_language = "ja-JP"
    coordinator.device_capabilities = []
    coordinator.device_category = ["robot"]
    coordinator.config_entry = Mock()
    coordinator.config_entry.data = {"device_name": "Spot+Scrub"}
    coordinator.data = {"voiceLanguage": "ja-JP"}
    return coordinator


@pytest.fixture
def mock_hass(mock_coordinator):
    hass = Mock()
    hass.data = {"hass_dyson": {mock_coordinator.serial_number: mock_coordinator}}
    return hass


@pytest.fixture
def mock_config_entry(mock_coordinator):
    entry = Mock()
    entry.data = {CONF_HOSTNAME: "192.168.1.50", "connection_type": "local"}
    entry.unique_id = mock_coordinator.serial_number
    entry.entry_id = mock_coordinator.serial_number
    return entry


class TestVoiceLanguageCreation:
    @pytest.mark.asyncio
    async def test_created_when_reported(
        self, mock_hass, mock_config_entry, mock_coordinator
    ):
        mock_add = MagicMock()
        await select_setup_entry(mock_hass, mock_config_entry, mock_add)
        entities = mock_add.call_args[0][0]
        assert any(isinstance(e, DysonRobotVoiceLanguageSelect) for e in entities)

    @pytest.mark.asyncio
    async def test_not_created_when_absent(
        self, mock_hass, mock_config_entry, mock_coordinator
    ):
        mock_coordinator.data = {}
        mock_add = MagicMock()
        await select_setup_entry(mock_hass, mock_config_entry, mock_add)
        entities = mock_add.call_args[0][0]
        assert not any(isinstance(e, DysonRobotVoiceLanguageSelect) for e in entities)

    @pytest.mark.asyncio
    async def test_not_created_for_non_robot_category(
        self, mock_hass, mock_config_entry, mock_coordinator
    ):
        mock_coordinator.device_category = ["ec"]
        mock_add = MagicMock()
        await select_setup_entry(mock_hass, mock_config_entry, mock_add)
        entities = mock_add.call_args[0][0]
        assert not any(isinstance(e, DysonRobotVoiceLanguageSelect) for e in entities)


class TestVoiceLanguageEntity:
    def test_unique_id(self, mock_coordinator):
        entity = DysonRobotVoiceLanguageSelect(mock_coordinator)
        assert entity._attr_unique_id == "RB05-EU-TST0000A_robot_voice_language"

    def test_translation_key(self, mock_coordinator):
        entity = DysonRobotVoiceLanguageSelect(mock_coordinator)
        assert entity._attr_translation_key == "robot_voice_language"

    def test_options_include_known_languages(self, mock_coordinator):
        entity = DysonRobotVoiceLanguageSelect(mock_coordinator)
        assert "ja-JP" in entity._attr_options
        assert "en-US" in entity._attr_options

    def test_current_option_reflects_device_state(self, mock_coordinator):
        entity = DysonRobotVoiceLanguageSelect(mock_coordinator)
        assert entity._attr_current_option == "ja-JP"

    def test_current_option_none_when_no_device(self, mock_coordinator):
        mock_coordinator.device = None
        entity = DysonRobotVoiceLanguageSelect(mock_coordinator)
        assert entity._attr_current_option is None

    def test_unknown_current_language_is_added_to_options(self, mock_coordinator):
        """A language outside the known list must still appear in options,
        so HA never shows a current_option that isn't selectable."""
        mock_coordinator.device.robot_voice_language = "de-DE"
        entity = DysonRobotVoiceLanguageSelect(mock_coordinator)
        assert "de-DE" in entity._attr_options
        assert entity._attr_current_option == "de-DE"

    def test_handle_update_reflects_new_language(self, mock_coordinator):
        entity = DysonRobotVoiceLanguageSelect(mock_coordinator)
        entity.async_write_ha_state = MagicMock()
        mock_coordinator.device.robot_voice_language = "en-US"
        entity._handle_coordinator_update()
        assert entity._attr_current_option == "en-US"

    @pytest.mark.asyncio
    async def test_select_option_calls_set_voice_language(self, mock_coordinator):
        mock_coordinator.device.set_robot_voice_language = AsyncMock()
        entity = DysonRobotVoiceLanguageSelect(mock_coordinator)
        await entity.async_select_option("en-US")
        mock_coordinator.device.set_robot_voice_language.assert_awaited_once_with(
            "en-US"
        )

    @pytest.mark.asyncio
    async def test_no_command_when_device_none(self, mock_coordinator):
        mock_coordinator.device = None
        entity = DysonRobotVoiceLanguageSelect(mock_coordinator)
        await entity.async_select_option("en-US")  # should not raise
