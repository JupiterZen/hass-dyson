"""Create robot entities that depend on the robot's first state, whenever it arrives.

Robot sensors, selects, switches and numbers are only created for fields the
robot has reported (``dockState``, ``consumables``, ``airDryFrequency``, ...),
so a robot on a plain dock does not get permanently-unknown entities. That
gate is evaluated once at platform setup. After a Pi reboot on 17 Oct 2026 the
robot (also without power) sent its first state six minutes after HA had
started, so none of those entities were ever created and they stayed
``unavailable`` until the entry was reloaded by hand.

``async_robot_state_entities`` keeps the gate but re-evaluates it on coordinator
updates until the robot's state has arrived, adding whatever is new.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any, TypeVar

from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.entity import Entity

from .coordinator import DysonDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)

_E = TypeVar("_E", bound=Entity)

# Key of the robot's CURRENT-STATE payload; absent until the first state.
ROBOT_STATE_MARKER = "state"


def _state_received(coordinator: DysonDataUpdateCoordinator) -> bool:
    data = coordinator.data
    return isinstance(data, dict) and ROBOT_STATE_MARKER in data


def async_robot_state_entities(
    config_entry: ConfigEntry,
    coordinator: DysonDataUpdateCoordinator,
    async_add_entities: Callable[..., Any],
    build: Callable[[], list[_E]],
) -> list[_E]:
    """Return the entities ``build`` yields now; add the rest when they appear.

    ``build`` must be a pure function of ``coordinator.data`` that creates only
    the entities whose gating keys are present. If the robot's state has not
    arrived yet, a coordinator listener calls it on every update and adds the
    entities whose unique id was not created before, then removes itself once
    the state is in.
    """
    initial = build()
    if _state_received(coordinator):
        return initial

    created = {e.unique_id for e in initial}
    unsub: Callable[[], None] | None = None

    def _on_update() -> None:
        nonlocal unsub
        if not _state_received(coordinator):
            return
        new = [e for e in build() if e.unique_id not in created]
        created.update(e.unique_id for e in new)
        if new:
            _LOGGER.info(
                "Robot %s sent its state after setup; adding %d late entities",
                coordinator.serial_number,
                len(new),
            )
            async_add_entities(new, True)
        if unsub is not None:
            unsub()
            unsub = None

    try:
        unsub = coordinator.async_add_listener(_on_update)
        config_entry.async_on_unload(lambda: unsub() if unsub else None)
    except Exception:  # noqa: BLE001 - never let this break platform setup
        _LOGGER.debug("Could not register late-entity listener", exc_info=True)
    return initial
