"""Switch platform for Dyson integration."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.restore_state import RestoreEntity
from libdyson_rest.models import PersistentMapMeta, ZoneMeta

from .const import (
    CAPABILITY_ENVIRONMENTAL_DATA,
    DEVICE_CATEGORY_ROBOT,
    DOMAIN,
    ROBOT_MSG_MAP_MANIFEST_UPDATED,
)
from .coordinator import DysonBLEDataUpdateCoordinator, DysonDataUpdateCoordinator
from .device_utils import mask_serial
from .entity import DysonBLEEntity, DysonEntity

# Coalescing delay (seconds) between the robot's PERSISTENT-MAP-MANIFEST-
# UPDATED broadcast and the zone-target-switch metadata re-fetch it triggers.
# Same value as button.py's zone-button discovery — both react to the same
# broadcast, no reason for the debounce windows to differ.
_ZONE_SWITCH_MANIFEST_REFRESH_DEBOUNCE: float = 5.0

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> bool:
    """Set up Dyson switch platform."""
    entry_data = hass.data[DOMAIN][config_entry.entry_id]

    # BLE-only light devices (Lightcycle Morph CF06/CD06)
    if isinstance(entry_data, dict) and entry_data.get("is_ble"):
        ble_coordinator: DysonBLEDataUpdateCoordinator = entry_data["ble_coordinator"]
        async_add_entities([DysonDaylightModeSwitch(ble_coordinator)], True)
        return True

    # MQTT / cloud-connected devices
    coordinator: DysonDataUpdateCoordinator = entry_data

    entities: list[SwitchEntity] = []

    # Basic switches for all devices
    entities.append(DysonNightModeSwitch(coordinator))

    # Auto mode switch removed - now handled by fan platform preset modes

    # Add firmware auto-update switch for cloud-discovered devices only
    from .const import CONF_DISCOVERY_METHOD, DISCOVERY_CLOUD

    if config_entry.data.get(CONF_DISCOVERY_METHOD) == DISCOVERY_CLOUD:
        entities.append(DysonFirmwareAutoUpdateSwitch(coordinator))
        _LOGGER.debug(
            "Adding firmware auto-update switch for cloud device %s",
            coordinator.serial_number,
        )

    # Add additional switches based on capabilities
    device_capabilities = coordinator.device_capabilities

    # Note: Oscillation is now handled natively by the fan platform via FanEntityFeature.OSCILLATE
    # Advanced oscillation modes are available through the oscillation mode select entity

    # Note: Heating functionality is now integrated into the fan entity's HVAC modes
    # No separate heating switch needed

    if CAPABILITY_ENVIRONMENTAL_DATA in device_capabilities:
        entities.append(DysonContinuousMonitoringSwitch(coordinator))

    # Add Find+Follow switch for devices that report the 'soon' state key.
    # No dedicated capability flag exists; presence of 'soon' in product-state is
    # the sole gating criterion (same pattern as 'oton' for tilt oscillation).
    ff_product_state: dict = {}
    if coordinator.data:
        raw_ps = coordinator.data.get("product-state", {})
        if isinstance(raw_ps, dict):
            ff_product_state = raw_ps
    if "soon" in ff_product_state:
        entities.append(DysonFindFollowSwitch(coordinator))

    # Robot vacuum switches — only for devices that have reported the
    # corresponding CURRENT-STATE field at least once, matching the
    # dockState sensor's gate: a robot without a wash/dry dock or without
    # child-lock hardware never sends these fields, so skip rather than
    # create a permanently-unknown entity.
    device_category = coordinator.device_category or []
    if (
        any(cat == DEVICE_CATEGORY_ROBOT for cat in device_category)
        and coordinator.data
    ):
        if "childLock" in coordinator.data:
            entities.append(DysonRobotChildLockSwitch(coordinator))
        if "washMopBeforeClean" in coordinator.data:
            entities.append(DysonRobotWashMopBeforeCleanSwitch(coordinator))
        if "doNotDisturbMode" in coordinator.data:
            entities.append(DysonRobotDoNotDisturbSwitch(coordinator))
        if "hotWaterSwitch" in coordinator.data:
            entities.append(DysonRobotHotWaterSwitchSwitch(coordinator))
        if "alarm" in coordinator.data:
            entities.append(DysonRobotAlarmSwitch(coordinator))
        if "detergent" in coordinator.data:
            entities.append(DysonRobotDetergentSwitch(coordinator))
        if "hotWaterMop" in coordinator.data:
            entities.append(DysonRobotHotWaterMopSwitch(coordinator))
        if "collectDustOnSelfClean" in coordinator.data:
            entities.append(DysonRobotCollectDustOnSelfCleanSwitch(coordinator))

        # Per-zone "include in next multi-room clean" toggles — local
        # selection state only, no MQTT/cloud write. Discovered from the
        # same persistent-map metadata as the zone clean buttons in
        # button.py, and refreshed on the same PERSISTENT-MAP-MANIFEST-
        # UPDATED broadcast, but with a simpler lifecycle: no retry-backoff
        # (button.py's Refresh Zone List button already covers recovering
        # a failed initial fetch for the whole zone list) and no rename/
        # retirement tracking (a switch is disposable local UI state, not
        # a command target whose staleness could mis-clean a room).
        await _async_setup_zone_target_switches(
            hass, config_entry, coordinator, async_add_entities
        )

    async_add_entities(entities, True)
    return True


async def _async_setup_zone_target_switches(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    coordinator: DysonDataUpdateCoordinator,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Discover/refresh per-zone target switches for the robot's current map.

    Scoped to the robot's *current* map only (unlike button.py's zone-clean
    buttons, which cover every stored map) — the multi-select clean this
    feeds targets one map per run, so switches for zones on a map the robot
    isn't on would be misleading to show as selectable.
    """
    from .services import _effective_current_map, _fetch_persistent_map_metadata

    known_switches: dict[str, DysonRobotZoneTargetSwitch] = {}

    async def _async_discover() -> None:
        try:
            maps = await _fetch_persistent_map_metadata(coordinator)
        except Exception as err:  # noqa: BLE001 — next broadcast/refresh retries
            _LOGGER.debug(
                "Zone-target-switch discovery failed for %s: %s",
                coordinator.serial_number,
                err,
            )
            return
        if not maps:
            return
        pmap = _effective_current_map(maps, coordinator) or maps[0]

        new_switches: list[DysonRobotZoneTargetSwitch] = []
        fresh_ids: set[str] = set()
        for zone in pmap.zones:
            if not zone.id:
                continue
            fresh_ids.add(zone.id)
            existing = known_switches.get(zone.id)
            if existing is not None:
                existing.async_update_zone_meta(pmap, zone)
                continue
            switch = DysonRobotZoneTargetSwitch(coordinator, pmap, zone)
            known_switches[zone.id] = switch
            new_switches.append(switch)

        # A zone no longer on the current map (deleted, or the robot moved
        # to a different map) — retire the switch. Unlike the zone-clean
        # buttons, dropping it outright (rather than marking unavailable)
        # is fine: it carries no command history worth preserving, and a
        # returning zone just gets a fresh switch defaulting to unselected.
        stale_ids = set(known_switches) - fresh_ids
        for zone_id in stale_ids:
            switch = known_switches.pop(zone_id)
            if switch.hass is not None:
                hass.async_create_task(switch.async_remove())

        if new_switches:
            async_add_entities(new_switches, True)

    manifest_refresh_unsub: CALLBACK_TYPE | None = None
    manifest_listener_removed = False

    async def _async_manifest_refresh(_now) -> None:
        nonlocal manifest_refresh_unsub
        manifest_refresh_unsub = None
        from .services import _persistent_map_cache

        _persistent_map_cache.invalidate(coordinator.serial_number)
        await _async_discover()

    def _schedule_manifest_refresh() -> None:
        nonlocal manifest_refresh_unsub
        if manifest_listener_removed:
            return
        if manifest_refresh_unsub is not None:
            manifest_refresh_unsub()
        manifest_refresh_unsub = async_call_later(
            hass, _ZONE_SWITCH_MANIFEST_REFRESH_DEBOUNCE, _async_manifest_refresh
        )

    def _on_device_message(topic: str, data: dict[str, Any]) -> None:
        if data.get("msg") != ROBOT_MSG_MAP_MANIFEST_UPDATED:
            return
        hass.loop.call_soon_threadsafe(_schedule_manifest_refresh)

    device = coordinator.device
    if device is not None:
        device.add_message_callback(_on_device_message)

        def _remove_manifest_listener() -> None:
            nonlocal manifest_listener_removed
            manifest_listener_removed = True
            device.remove_message_callback(_on_device_message)
            if manifest_refresh_unsub is not None:
                manifest_refresh_unsub()

        config_entry.async_on_unload(_remove_manifest_listener)

    await _async_discover()


class DysonAutoModeSwitch(DysonEntity, SwitchEntity):
    """Switch for auto mode."""

    coordinator: DysonDataUpdateCoordinator

    def __init__(self, coordinator: DysonDataUpdateCoordinator) -> None:
        """Initialize the auto mode switch."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.serial_number}_auto_mode"
        self._attr_translation_key = "auto_mode"
        self._attr_icon = "mdi:auto-mode"
        if coordinator.data and coordinator.device:
            product_state = coordinator.data.get("product-state", {})
            self._attr_is_on = (
                coordinator.device.get_state_value(product_state, "auto", "OFF") == "ON"
            )
        else:
            self._attr_is_on = False

    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        if self.coordinator.device:
            # Get auto mode from device state (auto)
            product_state = self.coordinator.data.get("product-state", {})
            auto_mode = self.coordinator.device.get_state_value(
                product_state, "auto", "OFF"
            )
            self._attr_is_on = auto_mode == "ON"
        else:
            self._attr_is_on = None
        super()._handle_coordinator_update()

    async def async_turn_on(self, **kwargs) -> None:
        """Turn on auto mode."""
        if not self.coordinator.device:
            return

        try:
            await self.coordinator.device.set_auto_mode(True)
            # No need to refresh - MQTT provides real-time updates
            _LOGGER.debug(
                "Turned on auto mode for %s",
                mask_serial(self.coordinator.serial_number),
            )
        except (ConnectionError, TimeoutError) as err:
            _LOGGER.error(
                "Communication error enabling auto mode for %s: %s",
                self.coordinator.serial_number,
                err,
            )
        except AttributeError as err:
            _LOGGER.error(
                "Device method not available for auto mode on %s: %s",
                self.coordinator.serial_number,
                err,
            )
        except Exception as err:
            _LOGGER.error(
                "Unexpected error enabling auto mode for %s: %s",
                self.coordinator.serial_number,
                err,
            )

    async def async_turn_off(self, **kwargs) -> None:
        """Turn off auto mode."""
        if not self.coordinator.device:
            return

        try:
            await self.coordinator.device.set_auto_mode(False)
            # No need to refresh - MQTT provides real-time updates
            _LOGGER.debug(
                "Turned off auto mode for %s",
                mask_serial(self.coordinator.serial_number),
            )
        except (ConnectionError, TimeoutError) as err:
            _LOGGER.error(
                "Communication error disabling auto mode for %s: %s",
                self.coordinator.serial_number,
                err,
            )
        except AttributeError as err:
            _LOGGER.error(
                "Device method not available for auto mode on %s: %s",
                self.coordinator.serial_number,
                err,
            )
        except Exception as err:
            _LOGGER.error(
                "Unexpected error disabling auto mode for %s: %s",
                self.coordinator.serial_number,
                err,
            )


class DysonNightModeSwitch(DysonEntity, SwitchEntity):
    """Switch for night mode."""

    coordinator: DysonDataUpdateCoordinator

    def __init__(self, coordinator: DysonDataUpdateCoordinator) -> None:
        """Initialize the night mode switch."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.serial_number}_night_mode"
        self._attr_translation_key = "night_mode"
        self._attr_icon = "mdi:weather-night"
        if coordinator.data and coordinator.device:
            product_state = coordinator.data.get("product-state", {})
            self._attr_is_on = (
                coordinator.device.get_state_value(product_state, "nmod", "OFF") == "ON"
            )
        else:
            self._attr_is_on = False

    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        if self.coordinator.device:
            # Get night mode from device state (nmod)
            product_state = self.coordinator.data.get("product-state", {})
            night_mode = self.coordinator.device.get_state_value(
                product_state, "nmod", "OFF"
            )
            self._attr_is_on = night_mode == "ON"
        else:
            self._attr_is_on = None
        super()._handle_coordinator_update()

    async def async_turn_on(self, **kwargs) -> None:
        """Turn on night mode."""
        if not self.coordinator.device:
            return

        try:
            await self.coordinator.device.set_night_mode(True)
            # No need to refresh - MQTT provides real-time updates
            _LOGGER.debug(
                "Turned on night mode for %s",
                mask_serial(self.coordinator.serial_number),
            )
        except (ConnectionError, TimeoutError) as err:
            _LOGGER.error(
                "Communication error enabling night mode for %s: %s",
                self.coordinator.serial_number,
                err,
            )
        except AttributeError as err:
            _LOGGER.error(
                "Device method not available for night mode on %s: %s",
                self.coordinator.serial_number,
                err,
            )
        except Exception as err:
            _LOGGER.error(
                "Unexpected error enabling night mode for %s: %s",
                self.coordinator.serial_number,
                err,
            )

    async def async_turn_off(self, **kwargs) -> None:
        """Turn off night mode."""
        if not self.coordinator.device:
            return

        try:
            await self.coordinator.device.set_night_mode(False)
            # No need to refresh - MQTT provides real-time updates
            _LOGGER.debug(
                "Turned off night mode for %s", self.coordinator.serial_number
            )
        except (ConnectionError, TimeoutError) as err:
            _LOGGER.error(
                "Communication error disabling night mode for %s: %s",
                self.coordinator.serial_number,
                err,
            )
        except AttributeError as err:
            _LOGGER.error(
                "Device method not available for night mode on %s: %s",
                self.coordinator.serial_number,
                err,
            )
        except Exception as err:
            _LOGGER.error(
                "Unexpected error disabling night mode for %s: %s",
                self.coordinator.serial_number,
                err,
            )


# DysonOscillationSwitch class removed - oscillation is now handled natively by the fan platform
# via FanEntityFeature.OSCILLATE and the fan.oscillate service


class DysonHeatingSwitch(DysonEntity, SwitchEntity):
    """Switch for heating mode."""

    coordinator: DysonDataUpdateCoordinator

    def __init__(self, coordinator: DysonDataUpdateCoordinator) -> None:
        """Initialize the heating switch."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.serial_number}_heating"
        self._attr_translation_key = "heating"
        self._attr_icon = "mdi:radiator"
        if coordinator.data and coordinator.device:
            product_state = coordinator.data.get("product-state", {})
            self._attr_is_on = (
                coordinator.device.get_state_value(product_state, "hmod", "OFF")
                != "OFF"
            )
        else:
            self._attr_is_on = False

    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        if self.coordinator.device:
            # Get heating from device state (hmod)
            product_state = self.coordinator.data.get("product-state", {})
            hmod = self.coordinator.device.get_state_value(product_state, "hmod", "OFF")
            self._attr_is_on = hmod != "OFF"
        else:
            self._attr_is_on = None
        super()._handle_coordinator_update()

    async def async_turn_on(self, **kwargs) -> None:
        """Turn on heating."""
        if not self.coordinator.device:
            return

        try:
            await self.coordinator.device.set_heating_mode("HEAT")
            _LOGGER.debug(
                "Turned on heating for %s", mask_serial(self.coordinator.serial_number)
            )
        except (ConnectionError, TimeoutError) as err:
            _LOGGER.error(
                "Communication error enabling heating for %s: %s",
                self.coordinator.serial_number,
                err,
            )
        except AttributeError as err:
            _LOGGER.error(
                "Device method not available for heating on %s: %s",
                self.coordinator.serial_number,
                err,
            )
        except Exception as err:
            _LOGGER.error(
                "Unexpected error enabling heating for %s: %s",
                self.coordinator.serial_number,
                err,
            )

    async def async_turn_off(self, **kwargs) -> None:
        """Turn off heating."""
        if not self.coordinator.device:
            return

        try:
            await self.coordinator.device.set_heating_mode("OFF")
            _LOGGER.debug(
                "Turned off heating for %s", mask_serial(self.coordinator.serial_number)
            )
        except (ConnectionError, TimeoutError) as err:
            _LOGGER.error(
                "Communication error disabling heating for %s: %s",
                self.coordinator.serial_number,
                err,
            )
        except AttributeError as err:
            _LOGGER.error(
                "Device method not available for heating on %s: %s",
                self.coordinator.serial_number,
                err,
            )
        except Exception as err:
            _LOGGER.error(
                "Unexpected error disabling heating for %s: %s",
                self.coordinator.serial_number,
                err,
            )

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return heating-specific state attributes for scene support."""
        if not self.coordinator.device:
            return None

        attributes: dict[str, Any] = {}
        product_state = self.coordinator.data.get("product-state", {})

        # Heating mode state for scene support
        hmod = self.coordinator.device.get_state_value(product_state, "hmod", "OFF")
        attributes["heating_mode"] = hmod
        heating_enabled: bool = hmod != "OFF"
        attributes["heating_enabled"] = heating_enabled  # type: ignore[assignment]

        # Include related heating properties if available
        try:
            # Target temperature in Kelvin
            hmax = self.coordinator.device.get_state_value(
                product_state, "hmax", "2980"
            )
            temp_kelvin: float = int(hmax) / 10  # Device reports in 0.1K increments
            target_celsius: float = temp_kelvin - 273.15
            attributes["target_temperature"] = round(target_celsius, 1)  # type: ignore[assignment]
            attributes["target_temperature_kelvin"] = hmax
        except (ValueError, TypeError):
            pass

        return attributes


class DysonContinuousMonitoringSwitch(DysonEntity, SwitchEntity):
    """Switch for continuous monitoring."""

    coordinator: DysonDataUpdateCoordinator

    def __init__(self, coordinator: DysonDataUpdateCoordinator) -> None:
        """Initialize the continuous monitoring switch."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.serial_number}_continuous_monitoring"
        self._attr_translation_key = "continuous_monitoring"
        self._attr_icon = "mdi:monitor-eye"
        from homeassistant.const import EntityCategory

        self._attr_entity_category = EntityCategory.CONFIG
        if coordinator.data and coordinator.device:
            product_state = coordinator.data.get("product-state", {})
            self._attr_is_on = (
                coordinator.device.get_state_value(product_state, "rhtm", "OFF") == "ON"
            )
        else:
            self._attr_is_on = False

    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        if self.coordinator.device:
            # Get monitoring from device state (rhtm)
            product_state = self.coordinator.data.get("product-state", {})
            rhtm = self.coordinator.device.get_state_value(product_state, "rhtm", "OFF")
            self._attr_is_on = rhtm == "ON"
        else:
            self._attr_is_on = None
        super()._handle_coordinator_update()

    async def async_turn_on(self, **kwargs) -> None:
        """Turn on continuous monitoring."""
        if not self.coordinator.device:
            return

        try:
            await self.coordinator.device.set_continuous_monitoring(True)
            _LOGGER.debug(
                "Turned on continuous monitoring for %s", self.coordinator.serial_number
            )
        except (ConnectionError, TimeoutError) as err:
            _LOGGER.error(
                "Communication error enabling continuous monitoring for %s: %s",
                self.coordinator.serial_number,
                err,
            )
        except AttributeError as err:
            _LOGGER.error(
                "Device method not available for continuous monitoring on %s: %s",
                self.coordinator.serial_number,
                err,
            )
        except Exception as err:
            _LOGGER.error(
                "Unexpected error enabling continuous monitoring for %s: %s",
                self.coordinator.serial_number,
                err,
            )

    async def async_turn_off(self, **kwargs) -> None:
        """Turn off continuous monitoring."""
        if not self.coordinator.device:
            return

        try:
            await self.coordinator.device.set_continuous_monitoring(False)
            _LOGGER.debug(
                "Turned off continuous monitoring for %s",
                self.coordinator.serial_number,
            )
        except (ConnectionError, TimeoutError) as err:
            _LOGGER.error(
                "Communication error disabling continuous monitoring for %s: %s",
                self.coordinator.serial_number,
                err,
            )
        except AttributeError as err:
            _LOGGER.error(
                "Device method not available for continuous monitoring on %s: %s",
                self.coordinator.serial_number,
                err,
            )
        except Exception as err:
            _LOGGER.error(
                "Unexpected error disabling continuous monitoring for %s: %s",
                self.coordinator.serial_number,
                err,
            )

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:  # type: ignore[return]
        """Return continuous monitoring state attributes for scene support."""
        if not self.coordinator.device:
            return None

        attributes: dict[str, Any] = {}
        product_state = self.coordinator.data.get("product-state", {})

        # Continuous monitoring state for scene support
        rhtm = self.coordinator.device.get_state_value(product_state, "rhtm", "OFF")
        continuous_monitoring: bool = rhtm == "ON"
        attributes["continuous_monitoring"] = continuous_monitoring  # type: ignore[assignment]
        attributes["monitoring_mode"] = rhtm

        return attributes


class DysonFirmwareAutoUpdateSwitch(DysonEntity, SwitchEntity):
    """Switch to control firmware auto-update setting."""

    coordinator: DysonDataUpdateCoordinator

    def __init__(self, coordinator: DysonDataUpdateCoordinator) -> None:
        """Initialize the firmware auto-update switch."""
        super().__init__(coordinator)

        self._attr_unique_id = f"{coordinator.serial_number}_firmware_auto_update"
        self._attr_translation_key = "firmware_auto_update"
        self._attr_icon = "mdi:cloud-sync"
        from homeassistant.const import EntityCategory

        self._attr_entity_category = EntityCategory.CONFIG

    @property
    def is_on(self) -> bool | None:
        """Return True if firmware auto-update is enabled."""
        return self.coordinator.firmware_auto_update_enabled

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return the state attributes."""
        attrs = dict(super().extra_state_attributes or {})
        attrs.update(
            {
                "current_firmware_version": self.coordinator.firmware_version,
            }
        )
        return attrs

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn on firmware auto-update."""
        success = await self.coordinator.async_set_firmware_auto_update(True)
        if success:
            _LOGGER.info(
                "Enabled firmware auto-update for %s", self.coordinator.serial_number
            )
        else:
            _LOGGER.error(
                "Failed to enable firmware auto-update for %s",
                self.coordinator.serial_number,
            )

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn off firmware auto-update."""
        success = await self.coordinator.async_set_firmware_auto_update(False)
        if success:
            _LOGGER.info(
                "Disabled firmware auto-update for %s", self.coordinator.serial_number
            )
        else:
            _LOGGER.error(
                "Failed to disable firmware auto-update for %s",
                self.coordinator.serial_number,
            )

    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        _LOGGER.debug(
            "Firmware auto-update switch updated for %s: enabled=%s, version=%s",
            self.coordinator.serial_number,
            self.coordinator.firmware_auto_update_enabled,
            self.coordinator.firmware_version,
        )
        super()._handle_coordinator_update()


class DysonFindFollowSwitch(DysonEntity, SwitchEntity):
    """Switch for Find+Follow mode.

    Find+Follow uses the device camera to identify and track people in the
    room, directing airflow toward them.  The switch is detected at runtime
    by the presence of the ``soon`` state key in the device's product-state.
    """

    coordinator: DysonDataUpdateCoordinator

    def __init__(self, coordinator: DysonDataUpdateCoordinator) -> None:
        """Initialize the Find+Follow switch."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.serial_number}_find_follow"
        self._attr_translation_key = "find_follow"
        self._attr_icon = "mdi:account-eye"
        if coordinator.data and coordinator.device:
            product_state = coordinator.data.get("product-state", {})
            soon = coordinator.device.get_state_value(product_state, "soon", "OFF")
            self._attr_is_on = soon in ("ON", "SCAN")
        else:
            self._attr_is_on = False

    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        if self.coordinator.device:
            product_state = self.coordinator.data.get("product-state", {})
            soon = self.coordinator.device.get_state_value(product_state, "soon", "OFF")
            # ON when actively tracking (ON) or scanning (SCAN);
            # scanning always transitions to ON after the scan completes.
            self._attr_is_on = soon in ("ON", "SCAN")
        else:
            self._attr_is_on = None
        super()._handle_coordinator_update()

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return Find+Follow state attributes for diagnostics and automations."""
        if not self.coordinator.device:
            return None

        product_state = self.coordinator.data.get("product-state", {})
        soon = self.coordinator.device.get_state_value(product_state, "soon", "OFF")
        sost = self.coordinator.device.get_state_value(product_state, "sost", "OFF")

        return {
            "find_follow_active": soon in ("ON", "SCAN"),
            "find_follow_command": soon,
            "find_follow_engine_status": sost,
        }

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Enable Find+Follow mode."""
        if not self.coordinator.device:
            return
        try:
            await self.coordinator.device.set_find_follow("ON")
            _LOGGER.debug(
                "Enabled Find+Follow for %s",
                mask_serial(self.coordinator.serial_number),
            )
        except (ConnectionError, TimeoutError) as err:
            _LOGGER.error(
                "Communication error enabling Find+Follow for %s: %s",
                self.coordinator.serial_number,
                err,
            )
        except Exception as err:
            _LOGGER.error(
                "Unexpected error enabling Find+Follow for %s: %s",
                self.coordinator.serial_number,
                err,
            )

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable Find+Follow mode."""
        if not self.coordinator.device:
            return
        try:
            await self.coordinator.device.set_find_follow("OFF")
            _LOGGER.debug(
                "Disabled Find+Follow for %s",
                mask_serial(self.coordinator.serial_number),
            )
        except (ConnectionError, TimeoutError) as err:
            _LOGGER.error(
                "Communication error disabling Find+Follow for %s: %s",
                self.coordinator.serial_number,
                err,
            )
        except Exception as err:
            _LOGGER.error(
                "Unexpected error disabling Find+Follow for %s: %s",
                self.coordinator.serial_number,
                err,
            )


class DysonRobotChildLockSwitch(DysonEntity, SwitchEntity):
    """Switch for a robot vacuum's child lock.

    VERIFIED write path (1 sep 2026, live against a real RB05) — see
    :meth:`DysonDevice.set_robot_child_lock`.
    """

    coordinator: DysonDataUpdateCoordinator

    def __init__(self, coordinator: DysonDataUpdateCoordinator) -> None:
        """Initialize the robot child lock switch."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.serial_number}_robot_child_lock"
        self._attr_translation_key = "robot_child_lock"
        self._attr_icon = "mdi:lock"
        self._attr_is_on = (
            coordinator.device.robot_child_lock if coordinator.device else None
        )

    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self._attr_is_on = (
            self.coordinator.device.robot_child_lock
            if self.coordinator.device
            else None
        )
        super()._handle_coordinator_update()

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Enable the robot's child lock."""
        if not self.coordinator.device:
            return
        try:
            await self.coordinator.device.set_robot_child_lock(True)
            _LOGGER.debug(
                "Enabled child lock for %s",
                mask_serial(self.coordinator.serial_number),
            )
        except (ConnectionError, TimeoutError) as err:
            _LOGGER.error(
                "Communication error enabling child lock for %s: %s",
                self.coordinator.serial_number,
                err,
            )
        except Exception as err:
            _LOGGER.error(
                "Unexpected error enabling child lock for %s: %s",
                self.coordinator.serial_number,
                err,
            )

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable the robot's child lock."""
        if not self.coordinator.device:
            return
        try:
            await self.coordinator.device.set_robot_child_lock(False)
            _LOGGER.debug(
                "Disabled child lock for %s",
                mask_serial(self.coordinator.serial_number),
            )
        except (ConnectionError, TimeoutError) as err:
            _LOGGER.error(
                "Communication error disabling child lock for %s: %s",
                self.coordinator.serial_number,
                err,
            )
        except Exception as err:
            _LOGGER.error(
                "Unexpected error disabling child lock for %s: %s",
                self.coordinator.serial_number,
                err,
            )


class DysonRobotWashMopBeforeCleanSwitch(DysonEntity, SwitchEntity):
    """Switch for whether the dock washes the mop before the robot departs.

    VERIFIED write path (1 sep 2026, live against a real RB05) — see
    :meth:`DysonDevice.set_robot_wash_mop_before_clean`.
    """

    coordinator: DysonDataUpdateCoordinator

    def __init__(self, coordinator: DysonDataUpdateCoordinator) -> None:
        """Initialize the wash-mop-before-clean switch."""
        super().__init__(coordinator)
        self._attr_unique_id = (
            f"{coordinator.serial_number}_robot_wash_mop_before_clean"
        )
        self._attr_translation_key = "robot_wash_mop_before_clean"
        self._attr_icon = "mdi:water-pump"
        self._attr_is_on = (
            coordinator.device.robot_wash_mop_before_clean
            if coordinator.device
            else None
        )

    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self._attr_is_on = (
            self.coordinator.device.robot_wash_mop_before_clean
            if self.coordinator.device
            else None
        )
        super()._handle_coordinator_update()

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Enable washing the mop before the robot departs."""
        if not self.coordinator.device:
            return
        try:
            await self.coordinator.device.set_robot_wash_mop_before_clean(True)
            _LOGGER.debug(
                "Enabled wash-mop-before-clean for %s",
                mask_serial(self.coordinator.serial_number),
            )
        except (ConnectionError, TimeoutError) as err:
            _LOGGER.error(
                "Communication error enabling wash-mop-before-clean for %s: %s",
                self.coordinator.serial_number,
                err,
            )
        except Exception as err:
            _LOGGER.error(
                "Unexpected error enabling wash-mop-before-clean for %s: %s",
                self.coordinator.serial_number,
                err,
            )

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable washing the mop before the robot departs."""
        if not self.coordinator.device:
            return
        try:
            await self.coordinator.device.set_robot_wash_mop_before_clean(False)
            _LOGGER.debug(
                "Disabled wash-mop-before-clean for %s",
                mask_serial(self.coordinator.serial_number),
            )
        except (ConnectionError, TimeoutError) as err:
            _LOGGER.error(
                "Communication error disabling wash-mop-before-clean for %s: %s",
                self.coordinator.serial_number,
                err,
            )
        except Exception as err:
            _LOGGER.error(
                "Unexpected error disabling wash-mop-before-clean for %s: %s",
                self.coordinator.serial_number,
                err,
            )


class DysonRobotDoNotDisturbSwitch(DysonEntity, SwitchEntity):
    """Switch for a robot vacuum's do-not-disturb schedule.

    ``doNotDisturbMode`` is an object (``isOn``/``startTime``/``endTime``),
    unlike the plain boolean fields on the sibling robot switches — the
    schedule times are exposed as extra state attributes rather than a
    separate entity, since HA has no built-in "switch with a time range"
    entity type.

    VERIFIED write path (1 sep 2026, live against a real RB05) — see
    :meth:`DysonDevice.set_robot_do_not_disturb`.
    """

    coordinator: DysonDataUpdateCoordinator

    def __init__(self, coordinator: DysonDataUpdateCoordinator) -> None:
        """Initialize the do-not-disturb switch."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.serial_number}_robot_do_not_disturb"
        self._attr_translation_key = "robot_do_not_disturb"
        self._attr_icon = "mdi:sleep"
        dnd = coordinator.device.robot_do_not_disturb if coordinator.device else None
        self._attr_is_on = dnd.get("isOn") if dnd else None

    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        dnd = (
            self.coordinator.device.robot_do_not_disturb
            if self.coordinator.device
            else None
        )
        self._attr_is_on = dnd.get("isOn") if dnd else None
        super()._handle_coordinator_update()

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return the do-not-disturb schedule's start/end times."""
        dnd = (
            self.coordinator.device.robot_do_not_disturb
            if self.coordinator.device
            else None
        )
        if not dnd:
            return None
        return {
            "start_time": dnd.get("startTime"),
            "end_time": dnd.get("endTime"),
        }

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Enable do-not-disturb, keeping the last-known schedule times."""
        if not self.coordinator.device:
            return
        try:
            await self.coordinator.device.set_robot_do_not_disturb(True)
            _LOGGER.debug(
                "Enabled do-not-disturb for %s",
                mask_serial(self.coordinator.serial_number),
            )
        except (ConnectionError, TimeoutError) as err:
            _LOGGER.error(
                "Communication error enabling do-not-disturb for %s: %s",
                self.coordinator.serial_number,
                err,
            )
        except Exception as err:
            _LOGGER.error(
                "Unexpected error enabling do-not-disturb for %s: %s",
                self.coordinator.serial_number,
                err,
            )

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable do-not-disturb, keeping the last-known schedule times."""
        if not self.coordinator.device:
            return
        try:
            await self.coordinator.device.set_robot_do_not_disturb(False)
            _LOGGER.debug(
                "Disabled do-not-disturb for %s",
                mask_serial(self.coordinator.serial_number),
            )
        except (ConnectionError, TimeoutError) as err:
            _LOGGER.error(
                "Communication error disabling do-not-disturb for %s: %s",
                self.coordinator.serial_number,
                err,
            )
        except Exception as err:
            _LOGGER.error(
                "Unexpected error disabling do-not-disturb for %s: %s",
                self.coordinator.serial_number,
                err,
            )


class DysonRobotHotWaterSwitchSwitch(DysonEntity, SwitchEntity):
    """Switch for the dock's "Zelfreinigend met heet water" (hot-water self-clean) toggle.

    VERIFIED write path (1 sep 2026 probe) — see
    :meth:`DysonDevice.set_robot_hot_water_switch`.
    """

    coordinator: DysonDataUpdateCoordinator

    def __init__(self, coordinator: DysonDataUpdateCoordinator) -> None:
        """Initialize the hot-water self-clean switch."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.serial_number}_robot_hot_water_switch"
        self._attr_translation_key = "robot_hot_water_switch"
        self._attr_icon = "mdi:water-thermometer"
        self._attr_is_on = (
            coordinator.device.robot_hot_water_switch if coordinator.device else None
        )

    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self._attr_is_on = (
            self.coordinator.device.robot_hot_water_switch
            if self.coordinator.device
            else None
        )
        super()._handle_coordinator_update()

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Enable hot-water self-clean."""
        if not self.coordinator.device:
            return
        try:
            await self.coordinator.device.set_robot_hot_water_switch(True)
            _LOGGER.debug(
                "Enabled hot-water self-clean for %s",
                mask_serial(self.coordinator.serial_number),
            )
        except (ConnectionError, TimeoutError) as err:
            _LOGGER.error(
                "Communication error enabling hot-water self-clean for %s: %s",
                self.coordinator.serial_number,
                err,
            )
        except Exception as err:
            _LOGGER.error(
                "Unexpected error enabling hot-water self-clean for %s: %s",
                self.coordinator.serial_number,
                err,
            )

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable hot-water self-clean."""
        if not self.coordinator.device:
            return
        try:
            await self.coordinator.device.set_robot_hot_water_switch(False)
            _LOGGER.debug(
                "Disabled hot-water self-clean for %s",
                mask_serial(self.coordinator.serial_number),
            )
        except (ConnectionError, TimeoutError) as err:
            _LOGGER.error(
                "Communication error disabling hot-water self-clean for %s: %s",
                self.coordinator.serial_number,
                err,
            )
        except Exception as err:
            _LOGGER.error(
                "Unexpected error disabling hot-water self-clean for %s: %s",
                self.coordinator.serial_number,
                err,
            )


class _DysonRobotBooleanSwitch(DysonEntity, SwitchEntity):
    """Shared base for robot switches on a single plain boolean CURRENT-STATE field.

    Subclasses set the four class attributes below; the read/update/
    turn_on/turn_off logic is identical across all of them (device
    property getter -> _attr_is_on, device method -> turn_on/turn_off).
    All four fields (alarm, detergent, hotWaterMop,
    collectDustOnSelfClean) have a VERIFIED write path — see each
    concrete subclass's docstring.
    """

    coordinator: DysonDataUpdateCoordinator

    #: Suffix for the unique_id and the translation_key (e.g. "alarm").
    _KEY: str = ""
    #: mdi icon name.
    _ICON: str = ""
    #: Attribute name on DysonDevice to read the current state from.
    _GETTER: str = ""
    #: Method name on DysonDevice to call with the new bool value.
    _SETTER: str = ""
    #: Human-readable label used only in log messages.
    _LABEL: str = ""

    def __init__(self, coordinator: DysonDataUpdateCoordinator) -> None:
        """Initialize the switch."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.serial_number}_robot_{self._KEY}"
        self._attr_translation_key = f"robot_{self._KEY}"
        self._attr_icon = self._ICON
        self._attr_is_on = (
            getattr(coordinator.device, self._GETTER) if coordinator.device else None
        )

    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self._attr_is_on = (
            getattr(self.coordinator.device, self._GETTER)
            if self.coordinator.device
            else None
        )
        super()._handle_coordinator_update()

    async def _set(self, enabled: bool) -> None:
        if not self.coordinator.device:
            return
        try:
            await getattr(self.coordinator.device, self._SETTER)(enabled)
            _LOGGER.debug(
                "%s %s for %s",
                "Enabled" if enabled else "Disabled",
                self._LABEL,
                mask_serial(self.coordinator.serial_number),
            )
        except (ConnectionError, TimeoutError) as err:
            _LOGGER.error(
                "Communication error setting %s to %s for %s: %s",
                self._LABEL,
                enabled,
                self.coordinator.serial_number,
                err,
            )
        except Exception as err:
            _LOGGER.error(
                "Unexpected error setting %s to %s for %s: %s",
                self._LABEL,
                enabled,
                self.coordinator.serial_number,
                err,
            )

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the switch on."""
        await self._set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the switch off."""
        await self._set(False)


class DysonRobotAlarmSwitch(_DysonRobotBooleanSwitch):
    """Switch for the robot's "find my robot" alarm/chime.

    VERIFIED write path (1 sep 2026 probe) — see
    :meth:`DysonDevice.set_robot_alarm`.
    """

    _KEY = "alarm"
    _ICON = "mdi:bell-ring"
    _GETTER = "robot_alarm"
    _SETTER = "set_robot_alarm"
    _LABEL = "alarm"


class DysonRobotDetergentSwitch(_DysonRobotBooleanSwitch):
    """Switch for the robot's detergent-use setting.

    VERIFIED write path (1 sep 2026, live against a real RB05) — see
    :meth:`DysonDevice.set_robot_detergent`.
    """

    _KEY = "detergent"
    _ICON = "mdi:spray-bottle"
    _GETTER = "robot_detergent"
    _SETTER = "set_robot_detergent"
    _LABEL = "detergent"


class DysonRobotHotWaterMopSwitch(_DysonRobotBooleanSwitch):
    """Switch for the robot's hot-water-mop setting.

    VERIFIED write path (1 sep 2026, live against a real RB05) — see
    :meth:`DysonDevice.set_robot_hot_water_mop`. Distinct from
    :class:`DysonRobotHotWaterSwitchSwitch` — see
    :attr:`DysonDevice.robot_hot_water_mop`'s docstring for how the two
    relate (still unconfirmed).
    """

    _KEY = "hot_water_mop"
    _ICON = "mdi:water-thermometer-outline"
    _GETTER = "robot_hot_water_mop"
    _SETTER = "set_robot_hot_water_mop"
    _LABEL = "hot-water mop"


class DysonRobotCollectDustOnSelfCleanSwitch(_DysonRobotBooleanSwitch):
    """Switch for the robot's collect-dust-on-self-clean setting.

    VERIFIED write path (1 sep 2026, live against a real RB05) — see
    :meth:`DysonDevice.set_robot_collect_dust_on_self_clean`. Confirmed
    distinct from "empty bin on dock" behavior (see
    :attr:`DysonDevice.robot_collect_dust_on_self_clean`'s docstring).
    """

    _KEY = "collect_dust_on_self_clean"
    _ICON = "mdi:vacuum"
    _GETTER = "robot_collect_dust_on_self_clean"
    _SETTER = "set_robot_collect_dust_on_self_clean"
    _LABEL = "collect-dust-on-self-clean"


class DysonRobotZoneTargetSwitch(DysonEntity, RestoreEntity, SwitchEntity):
    """ "Include this room in the next multi-room clean" toggle.

    Purely local UI selection state — turning this on/off sends nothing to
    the device or the cloud. button.py's "Start Selected Zones" button
    reads every switch's ``is_on`` at press time and starts one
    ``hass_dyson.start_zone_clean`` call covering whichever rooms are
    checked, mirroring the room-picker step of the MyDyson app's zone-clean
    flow (minus the per-room cleaning-type/power options — see
    dyson/CLAUDE.md's "Kamer-knoppen" section for why those aren't wired up
    here: the app persists them to the Dyson cloud via an endpoint that
    currently 500s, tracked as cmgrayb/libdyson-rest#226).

    State survives restarts via RestoreEntity. One entity per zone on the
    robot's *current* map — see
    ``switch._async_setup_zone_target_switches`` for the discovery/refresh
    lifecycle.
    """

    coordinator: DysonDataUpdateCoordinator

    def __init__(
        self,
        coordinator: DysonDataUpdateCoordinator,
        pmap: PersistentMapMeta,
        zone: ZoneMeta,
    ) -> None:
        """Initialize the zone target switch."""
        super().__init__(coordinator)
        self._zone_id: str = zone.id
        # unique_id is map-qualified, matching DysonZoneCleanButton — zone
        # ids restart from 1 on every map.
        self._attr_unique_id = (
            f"{coordinator.serial_number}_zone_target_{pmap.id}_{self._zone_id}"
        )
        self._attr_entity_registry_enabled_default = True
        self._attr_is_on = False
        self._apply_zone_meta(pmap, zone)

    def _apply_zone_meta(self, pmap: PersistentMapMeta, zone: ZoneMeta) -> None:
        self.zone_name: str = str(zone.name or f"Zone {self._zone_id}")
        self._attr_name = f"Target {self.zone_name}"
        self._attr_icon = "mdi:checkbox-marked-circle-outline"
        # Exposed as an attribute (not just the friendly name) so
        # DysonStartSelectedZonesButton can read the exact zone name to
        # pass to start_zone_clean without parsing "Target <name>".
        self._attr_extra_state_attributes = {"zone_name": self.zone_name}

    @callback
    def async_update_zone_meta(self, pmap: PersistentMapMeta, zone: ZoneMeta) -> None:
        """Refresh the zone name after a metadata re-fetch (e.g. app rename)."""
        old_name = self._attr_name
        self._apply_zone_meta(pmap, zone)
        if (
            self.hass is not None
            and self.entity_id is not None
            and (self._attr_name != old_name)
        ):
            self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        """Restore the last selection state across restarts."""
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if last_state is not None:
            self._attr_is_on = last_state.state == "on"

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Mark this zone selected for the next multi-room clean."""
        self._attr_is_on = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Unmark this zone."""
        self._attr_is_on = False
        self.async_write_ha_state()


class DysonDaylightModeSwitch(DysonBLEEntity, SwitchEntity):
    """Switch to enable/disable Dyson Lightcycle Morph daylight (auto) mode.

    When **on**, the lamp controls its own brightness and colour temperature
    automatically based on the time of day, ambient light, and the user's age
    profile (Dyson Solarcycle algorithm).  Manual brightness and colour-
    temperature changes from Home Assistant are ignored while this mode is
    active.

    When **off** (manual mode) the lamp accepts explicit brightness and colour-
    temperature writes from Home Assistant.

    This entity appears for BLE-only lights (CF06/CD06 Lightcycle Morph) that
    carry the ``Daylight`` or ``PersonalDaylight`` capability.
    """

    coordinator: DysonBLEDataUpdateCoordinator

    _attr_icon = "mdi:theme-light-dark"
    _attr_translation_key = "daylight_mode"

    def __init__(self, coordinator: DysonBLEDataUpdateCoordinator) -> None:
        """Initialise the daylight mode switch.

        Args:
            coordinator: BLE coordinator for this device.
        """
        super().__init__(coordinator)
        serial = coordinator.serial_number
        self._attr_unique_id = f"{serial}_daylight_mode"

    @property
    def is_on(self) -> bool | None:
        """Return True when daylight mode is active."""
        data = self.coordinator.data
        if data is None:
            return None
        val = data.get("daylight_mode")
        if val is None:
            return None
        return bool(val)

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Enable daylight (auto) mode on the lamp."""
        dev = self.coordinator.ble_device
        if dev is None:
            _LOGGER.warning(
                "No BLE device available for %s", self.coordinator.serial_number
            )
            return
        try:
            await dev.set_daylight_mode(enabled=True)
        except RuntimeError as err:
            _LOGGER.error(
                "Failed to enable daylight mode for %s: %s",
                self.coordinator.serial_number,
                err,
            )

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable daylight mode (switch to manual control)."""
        dev = self.coordinator.ble_device
        if dev is None:
            _LOGGER.warning(
                "No BLE device available for %s", self.coordinator.serial_number
            )
            return
        try:
            await dev.set_daylight_mode(enabled=False)
        except RuntimeError as err:
            _LOGGER.error(
                "Failed to disable daylight mode for %s: %s",
                self.coordinator.serial_number,
                err,
            )
