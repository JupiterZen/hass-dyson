"""Switch platform for Dyson integration."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CAPABILITY_ENVIRONMENTAL_DATA, DEVICE_CATEGORY_ROBOT, DOMAIN
from .coordinator import DysonBLEDataUpdateCoordinator, DysonDataUpdateCoordinator
from .device_utils import mask_serial
from .entity import DysonBLEEntity, DysonEntity

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

    async_add_entities(entities, True)
    return True


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
