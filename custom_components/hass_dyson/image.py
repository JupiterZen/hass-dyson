"""Image platform for Dyson integration.

Currently provides:
 - DysonDustMapImage:   the dust-density heatmap for the most recent clean
                        (rendered as PNG with purple→white gradient over the
                        floor plan), re-rendered whenever the record's dust
                        data or the persistent map changes. Dyson updates the
                        clean record IN PLACE while a clean is running (same
                        cleanId, growing dust blob) and re-versions the
                        persistent map afterwards, so a cleanId comparison
                        would freeze the first (usually mid-clean) render.
 - DysonFloorPlanImage: the persistent-map presentation image (the static
                        floor plan with zone boundaries and dock location).

v1 devices: dust map blob embedded in CleanRecord; floor plan from
  GET /v2/app/{serial}/persistent-maps/{id} (presentation_map_data field).
v2 devices (e.g. RB05 Spot+Scrub):
  Dust map strategy (priority order):
    1. Map Visualizer API: GET /v1/mapvisualizer/devices/{serial}/map/{cleanId}
       (Vis Nav only; returns 404 for RB05/804A — result cached for TTL).
    2. Clean Map Data API: GET /v2/{serial}/clean-maps-data/{cleanId}
       Returns JSON with {dimensions, dustMap, cleanPath, dockLocation, …}.
       Rendered client-side by _render_v2_map_png (purple→white heatmap +
       blue robot path + green dock icon).
  Floor plan strategy (priority order):
    0. While actively cleaning: GET /v1/app/{serial}/live-maps/cleaning
       Per-zone cleanStatus, furniture, restriction zones, live robot
       position. Rendered client-side by _render_live_map_png. Session-
       bound (404s once docked/idle) — only attempted while cleaning.
    1. Presentation bitmap in GET /v2/app/{serial}/persistent-maps/{id}
       (v1 Vis Nav only — not present for RB05).
    2. Map Visualizer API: GET /v1/mapvisualizer/devices/{serial}/map/{pmapId}
       (returns 404 for RB05).
    3. Zone boundary lines from GET /v2/{serial}/clean-maps-data/{cleanId}
       Rendered client-side by _render_v2_floor_plan_png (white background +
       dark zone boundary line segments + green dock icon).

Bitmap rendering ported from thoukydides/matterbridge-dyson-robot
(src/dyson-bitmap-octet.ts + src/dyson-device-360-map.ts).
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import logging
import zlib
from datetime import datetime, timezone
from functools import partial

from homeassistant.components.image import ImageEntity
from homeassistant.components.vacuum import VacuumActivity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DEVICE_CATEGORY_ROBOT, DOMAIN, ROBOT_STATE_TO_HA_STATE
from .coordinator import DysonDataUpdateCoordinator, TTLCache
from .entity import DysonEntity
from .vacuum import fetch_clean_maps

_LOGGER = logging.getLogger(__name__)


# Dust-density colour gradient ported from matterbridge-dyson-robot
# (DUST_COLOURS, ANSI 256 IDs translated to RGB hex). Purple → orange → white.
_DUST_GRADIENT_RGB: list[tuple[int, int, int]] = [
    (0x5F, 0x00, 0x87),  # 54  deep purple
    (0x87, 0x00, 0xAF),  # 89  purple
    (0xAF, 0x00, 0xD7),  # 124 magenta
    (0xD7, 0x5F, 0x00),  # 166 burnt orange
    (0xFF, 0x87, 0x00),  # 208 orange
    (0xFF, 0xAF, 0x00),  # 214 amber
    (0xFF, 0xD7, 0x00),  # 220 yellow
    (0xFF, 0xFF, 0x00),  # 226 bright yellow
    (0xFF, 0xFF, 0x5F),  # 227 pale yellow
    (0xFF, 0xFF, 0x87),
    (0xFF, 0xFF, 0xAF),
    (0xFF, 0xFF, 0xD7),
    (0xFF, 0xFF, 0xFF),  # 231 white
]


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Dyson image platform."""
    entry_data = hass.data[DOMAIN][config_entry.entry_id]
    if isinstance(entry_data, dict) and entry_data.get("is_ble"):
        return
    coordinator: DysonDataUpdateCoordinator = entry_data

    # Only for robot devices with a cloud auth token.
    is_robot = any(
        cat == DEVICE_CATEGORY_ROBOT for cat in (coordinator.device_category or [])
    )
    has_token = bool(coordinator.config_entry.data.get("auth_token"))
    if not (is_robot and has_token):
        return

    entities: list[ImageEntity] = [
        DysonDustMapImage(hass, coordinator),
        DysonFloorPlanImage(hass, coordinator),
    ]
    async_add_entities(entities, True)
    _LOGGER.info(
        "Created dust-map + floor-plan image entities for %s",
        coordinator.serial_number,
    )


# ----------------------------------------------------------------------------
# Cloud fetch helpers.
#
# Recent cleaning runs (the clean-maps endpoint) are fetched via the SHARED
# `fetch_clean_maps` in vacuum.py — the cleaning-history sensors in sensor.py
# read the same blob, so one cache covers both consumers.
#
# The persistent-map endpoint (persistent-maps/{id}) is only consumed here, so
# the cache stays local. Persistent maps change rarely → generous TTL.
# ----------------------------------------------------------------------------

_persist_map_cache = TTLCache(6 * 3600)
_map_image_cache = TTLCache(10 * 60)
_floor_plan_cache = TTLCache(6 * 3600)


async def _fetch_map_image(
    coordinator: DysonDataUpdateCoordinator, map_id: str
) -> bytes | None:
    """Fetch a server-rendered map image from the Dyson Map Visualizer API.

    Uses the ``get_map_image(serial, map_id)`` method added in libdyson-rest
    v0.15.0b5.  ``map_id`` can be either a clean session UUID (for a dust map)
    or a persistent-map integer ID string (for a floor plan).

    Successful responses are cached for 10 minutes.  A ``b""`` sentinel is
    stored for known-missing results (e.g. 404 from the Map Visualizer for v2
    devices) so the endpoint is not retried on every poll cycle.
    Returns ``None`` on any API or network error.
    """
    from libdyson_rest.exceptions import DysonAPIError, DysonAuthError

    # "mv:" prefix — _fetch_map_image and _fetch_clean_map_data_image are
    # two independent API strategies tried against the same clean_id (see
    # DysonDustMapImage._build's v2 path), but shared the same bare
    # f"{serial}:{clean_id}" key before this fix. Strategy 1 (this
    # function, always a 404 on RB05/v2 devices) would cache a b""
    # sentinel that strategy 2 then read as "already tried", skipping its
    # own — possibly successful — API call entirely. Discovered 1 sep
    # 2026: the dust map was permanently broken for RB05 because of this,
    # not because clean-maps-data genuinely has no data.
    key = f"mv:{coordinator.serial_number}:{map_id}"
    cached = _map_image_cache.get(key)
    if cached is not None:
        # b"" sentinel means a previous call confirmed no image is available.
        return cached if cached else None

    async with coordinator.async_cloud_client() as client:
        if client is None:
            return None
        try:
            image_bytes = await client.get_map_image(coordinator.serial_number, map_id)
        except (DysonAPIError, DysonAuthError) as err:
            _LOGGER.debug(
                "Map Visualizer fetch failed for %s map_id=%s: %s",
                coordinator.serial_number,
                map_id,
                err,
            )
            # Cache the miss so we don't retry on every poll cycle.
            _map_image_cache.set(key, b"")
            return None

    if image_bytes:
        _map_image_cache.set(key, image_bytes)
    else:
        _map_image_cache.set(key, b"")
    return image_bytes or None


async def _fetch_clean_map_data_image(
    coordinator: DysonDataUpdateCoordinator, clean_id: str
) -> bytes | None:
    """Try to fetch/render a dust map image via the v2 clean-maps-data endpoint.

    Calls ``GET /v2/{serial}/clean-maps-data/{clean_id}`` (libdyson-rest
    ``get_clean_map_data``).  The response structure is logged at DEBUG level
    for diagnostics.  If the payload contains a renderable dust-map grid
    (``width``, ``height``, ``dustData`` keys) it is rendered to a PNG with
    the existing ``_render_dust_map_png`` helper.

    Results are cached in ``_map_image_cache`` under a ``"cmd:"``-prefixed
    key — see ``_fetch_map_image``'s docstring for why this must not share
    a bare key with that function's ``"mv:"`` cache entries.
    """
    from libdyson_rest.exceptions import DysonAPIError, DysonAuthError

    key = f"cmd:{coordinator.serial_number}:{clean_id}"
    # If a previous attempt already succeeded (or confirmed no image), reuse it.
    cached = _map_image_cache.get(key)
    if cached is not None:
        return cached if cached else None

    async with coordinator.async_cloud_client() as client:
        if client is None:
            return None
        try:
            data = await client.get_clean_map_data(coordinator.serial_number, clean_id)
        except (DysonAPIError, DysonAuthError) as err:
            _LOGGER.debug(
                "clean_map_data fetch failed for %s clean_id=%s: %s",
                coordinator.serial_number,
                clean_id,
                err,
            )
            _map_image_cache.set(key, b"")
            return None

    if not data:
        _LOGGER.debug(
            "clean_map_data empty for %s clean_id=%s",
            coordinator.serial_number,
            clean_id,
        )
        _map_image_cache.set(key, b"")
        return None

    _LOGGER.debug(
        "clean_map_data keys for %s clean_id=%s: %s  (first 300 chars: %s)",
        coordinator.serial_number,
        clean_id,
        sorted(data.keys()),
        str(data)[:300],
    )

    # Strategy 1: v2 format — {dimensions, dustMap, cleanPath, …}
    # (returned by GET /v2/{serial}/clean-maps-data/{cleanId} for RB05/804A)
    if "dimensions" in data and "dustMap" in data:
        rotation = int(data.get("orientation") or 0)
        png = _render_v2_map_png(data, rotation)
        if png:
            _map_image_cache.set(key, png)
            return png

    # Strategy 2: v1 format — {width, height, dustData}
    if "width" in data and "height" in data and "dustData" in data:
        png = _render_dust_map_png(data, None, None)
        if png:
            _map_image_cache.set(key, png)
            return png

    # Nothing renderable — cache the miss.
    _map_image_cache.set(key, b"")
    return None


async def _fetch_v2_floor_plan_data(
    coordinator: DysonDataUpdateCoordinator, clean_id: str
) -> dict | None:
    """Fetch the v2 clean-maps-data response used to render a floor plan.

    Calls ``GET /v2/{serial}/clean-maps-data/{cleanId}``. Results are cached
    in ``_floor_plan_cache`` with a 6-hour TTL (the zone geometry and dock
    location change infrequently). A ``b""`` sentinel is stored on failure to
    suppress repeated API calls.

    Deliberately returns the raw response rather than a rendered PNG — the
    caller (``DysonFloorPlanImage._build``) overlays the robot's live
    position on top of this data on every call, so the *rendered image*
    must never be cached: caching the PNG here would freeze the robot dot
    at wherever it was on the first render for the full 6-hour TTL.
    """
    from libdyson_rest.exceptions import DysonAPIError, DysonAuthError

    key = f"{coordinator.serial_number}:fp:{clean_id}"
    cached = _floor_plan_cache.get(key)
    if cached is not None:
        return json.loads(cached) if cached else None

    async with coordinator.async_cloud_client() as client:
        if client is None:
            return None
        try:
            data = await client.get_clean_map_data(coordinator.serial_number, clean_id)
        except (DysonAPIError, DysonAuthError) as err:
            _LOGGER.debug(
                "v2 floor plan fetch failed for %s clean_id=%s: %s",
                coordinator.serial_number,
                clean_id,
                err,
            )
            _floor_plan_cache.set(key, b"")
            return None

    if not data:
        _floor_plan_cache.set(key, b"")
        return None

    _floor_plan_cache.set(key, json.dumps(data).encode())
    return data


async def _fetch_live_map_cleaning(
    coordinator: DysonDataUpdateCoordinator,
) -> dict | None:
    """Fetch the live in-progress map via ``GET /v1/app/{serial}/live-maps/cleaning``.

    Unlike ``_fetch_v2_floor_plan_data`` (the *last completed* clean's
    zone geometry), this endpoint reflects the *currently running* clean —
    each zone carries a live ``cleanStatus`` (``CLEAN_NOT_REQUESTED`` /
    ``CLEAN_PENDING`` / ``CLEAN_IN_PROGRESS`` / ``CLEAN_COMPLETE`` /
    ``CANT_CLEAN``), plus ``furniture``, ``restrictions`` and a live
    ``dirt`` array. Confirmed via a live probe (3 sep 2026, see
    ``dyson/notes/06-...md`` in the smarthome repo) that this is the
    correct per-zone status source — better than inferring progress from
    MQTT ``FULL_CLEAN_DISCOVERING`` transitions.

    Deliberately uncached: the endpoint itself is session-bound (returns
    HTTP 404 once the robot is docked/no active clean session exists), so
    there's nothing stable to cache — every call either reflects the
    current live state or fails outright, and a stale cached frame would
    be actively misleading here (unlike the 6-hour zone-geometry cache
    used elsewhere, which is safe because geometry rarely changes).
    """
    from libdyson_rest.exceptions import DysonAPIError, DysonAuthError

    async with coordinator.async_cloud_client() as client:
        if client is None:
            return None
        try:
            return await client.get_live_map_cleaning(coordinator.serial_number)
        except (DysonAPIError, DysonAuthError) as err:
            _LOGGER.debug(
                "Live map (cleaning) fetch failed for %s: %s",
                coordinator.serial_number,
                err,
            )
            return None


async def _fetch_persist_map(coordinator: DysonDataUpdateCoordinator, map_id: str):
    """Fetch a persistent map via libdyson-rest (cached 6 h).

    Returns a ``PersistentMap`` object or the stale cached value on failure.
    """
    from libdyson_rest.exceptions import DysonAPIError, DysonAuthError

    key = f"{coordinator.serial_number}:{map_id}"
    fresh = _persist_map_cache.get(key)
    if fresh is not None:
        return fresh

    async with coordinator.async_cloud_client() as client:
        if client is None:
            return _persist_map_cache.get_stale(key)
        try:
            pmap = await client.get_persistent_map(
                coordinator.serial_number,
                map_id,
                api_version=await coordinator.async_discover_map_api_version(client),
            )
        except (DysonAPIError, DysonAuthError) as err:
            _LOGGER.debug(
                "Failed to fetch persistent map %s for %s: %s",
                map_id,
                coordinator.serial_number,
                err,
            )
            return _persist_map_cache.get_stale(key)

    _persist_map_cache.set(key, pmap)
    return pmap


def _pmap_fingerprint(pmap) -> tuple | None:
    """Content fingerprint of a persistent map for render-cache keys.

    The robot re-versions the persistent map after cleans (new world offset,
    dimensions and presentation bitmap). The model does not expose the map's
    version number, so fingerprint the fields that change with it.
    """
    if not pmap:
        return None
    return (
        pmap.offset_x,
        pmap.offset_y,
        pmap.display_orientation,
        hashlib.sha1((pmap.presentation_map_data or "").encode()).hexdigest(),
    )


# ----------------------------------------------------------------------------
# Rendering helpers
# ----------------------------------------------------------------------------


def _apply_orientation(img, rotation_deg: int):
    """Apply Y-invert + rotation to match MyDyson app orientation.

    Matches matterbridge-dyson-robot rendering:
      renderer.invertY = true (always for Vis Nav)
      renderer.rotation = persistentMapDisplayOrientation
    """
    from PIL import Image

    # Y-flip first (raw bitmaps use Y=0 at bottom-left; PIL uses top-left)
    img = img.transpose(Image.Transpose.FLIP_TOP_BOTTOM)
    if rotation_deg:
        # PIL.rotate is counter-clockwise; negate to match app convention
        img = img.rotate(-rotation_deg, expand=True, resample=Image.Resampling.NEAREST)
    return img


def _palette_from_floor_plan(presentation_png: bytes):
    """Open the floor-plan PNG and recolour for clear display.

    Source palette:
      Black  (zone interior)
      White  (zone boundary / walls)
      Gray   (outside any zone)
    Re-paint to a soft, high-contrast palette so it reads clearly under the
    transparent dust overlay.
    """
    from PIL import Image

    img = Image.open(io.BytesIO(presentation_png)).convert("RGBA")
    pixels = img.load()
    w, h = img.size
    # Repaint: zones → light cream, boundaries → dark blue-grey, outside → near-white
    for y in range(h):
        for x in range(w):
            rgba_pixel: tuple[int, int, int, int] = pixels[x, y]  # type: ignore[assignment]
            r, g, b, _ = rgba_pixel
            if r > 200 and g > 200 and b > 200:
                # white = boundary
                pixels[x, y] = (60, 60, 90, 255)
            elif r < 50 and g < 50 and b < 50:
                # black = zone interior
                pixels[x, y] = (250, 248, 240, 255)
            else:
                # gray = outside
                pixels[x, y] = (235, 235, 235, 255)
    return img


def _render_dust_map_png(
    dust_map: dict,
    cleaned_footprint_png: bytes | None,
    presentation_png: bytes | None,
    rotation_deg: int = 0,
    map_offset_mm: tuple[float, float] | None = None,
    clean_position_mm: tuple[float, float] | None = None,
    map_resolution_mm_per_px: int = 20,
) -> bytes | None:
    """Render the dust map as a PNG, correctly positioned over the floor plan.

    Critical alignment math (ported from matterbridge-dyson-robot map.ts):
        dust_origin_in_pres_pixels = (cleanMapPosition - mapOffset) / mmPerPixel

    The dust map is a CROP of the world in clean-coordinates, smaller than the
    presentation map. To overlay it correctly we paste it onto a copy of the
    presentation map at the computed pixel offset. Just resizing the dust map
    to the presentation map's dimensions (the old approach) stretched it and
    made every pixel land in the wrong room.
    """
    try:
        from PIL import Image
    except ImportError:
        _LOGGER.warning("Pillow not available — cannot render dust map PNG")
        return None

    try:
        width = int(dust_map["width"])
        height = int(dust_map["height"])
        dust_data = dust_map["dustData"][0]
        scale = max(1, int(dust_data.get("scaleFactor") or 255))
        raw = zlib.decompress(base64.b64decode(dust_data["data"]))
    except (KeyError, ValueError, TypeError, zlib.error, IndexError) as err:
        _LOGGER.warning("Malformed dust map data: %s", err)
        return None

    if len(raw) < width * height:
        _LOGGER.warning(
            "Dust map size mismatch: declared %dx%d but only %d bytes",
            width,
            height,
            len(raw),
        )
        return None

    # Build the dust heatmap as RGBA in its native (clean-coordinate) space.
    rgba = bytearray(width * height * 4)
    n_levels = len(_DUST_GRADIENT_RGB)
    for i in range(width * height):
        level = raw[i]
        if level == 0:
            rgba[i * 4 + 3] = 0
            continue
        normalized = min(1.0, level / scale)
        idx = min(n_levels - 1, int(normalized * n_levels))
        r, g, b = _DUST_GRADIENT_RGB[idx]
        rgba[i * 4] = r
        rgba[i * 4 + 1] = g
        rgba[i * 4 + 2] = b
        rgba[i * 4 + 3] = 220
    dust_img = Image.frombytes("RGBA", (width, height), bytes(rgba))

    # No floor plan → render dust map alone with orientation
    if not presentation_png:
        composite = _apply_orientation(dust_img, rotation_deg)
    else:
        try:
            bg = _palette_from_floor_plan(presentation_png)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Floor plan palette failed: %s", err)
            composite = _apply_orientation(dust_img, rotation_deg)
        else:
            # Composite the dust map onto the presentation canvas at the right offset.
            # Apply orientation AFTER compositing so both layers transform together.
            if map_offset_mm is not None and clean_position_mm is not None:
                ox = int(
                    round(
                        (clean_position_mm[0] - map_offset_mm[0])
                        / map_resolution_mm_per_px
                    )
                )
                oy = int(
                    round(
                        (clean_position_mm[1] - map_offset_mm[1])
                        / map_resolution_mm_per_px
                    )
                )
            else:
                # Fallback: centre the dust map on the presentation map
                ox = max(0, (bg.size[0] - width) // 2)
                oy = max(0, (bg.size[1] - height) // 2)

            canvas = bg.copy()
            # Pillow alpha_composite needs a transparent backing the same size.
            # We use paste with the dust map's own alpha as mask for in-place
            # compositing at an offset (alpha_composite has no offset variant).
            canvas.paste(dust_img, (ox, oy), dust_img)
            composite = _apply_orientation(canvas, rotation_deg)

    # Scale up for legibility
    max_dim = max(composite.size)
    if max_dim < 800:
        factor = max(1, 800 // max_dim)
        composite = composite.resize(
            (composite.size[0] * factor, composite.size[1] * factor),
            resample=Image.Resampling.NEAREST,
        )

    buf = io.BytesIO()
    composite.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def _render_v2_map_png(data: dict, rotation_deg: int = 0) -> bytes | None:
    """Render a v2 clean-maps-data response as a PNG image.

    The JSON returned by ``GET /v2/{serial}/clean-maps-data/{cleanId}``
    (reverse-engineered from the MyDyson APK ``sp0.CleanMapResponse``) has
    the following fields used here:

    .. code-block:: json

        {
          "dimensions": {"width": int, "height": int,
                         "resolution": float,     /* metres per cell */
                         "offsetX": float,         /* world-space origin X */
                         "offsetY": float},        /* world-space origin Y */
          "dustMap":   [{"type": "total", "data": [int, …]}],
          "cleanPath": [{"x": float, "y": float, "update": int}],
          "dockLocation": {"x": float, "y": float, "angle": float},
          "orientation": int
        }

    The ``dustMap.data`` list is a flat grid of *width × height* integer dust
    values (index ``i = row * width + col``, row 0 = world bottom row).
    The ``cleanPath`` and ``dockLocation`` coordinates are in the same world
    units as ``dimensions.resolution`` / ``offsetX`` / ``offsetY``.

    Renders the dust heatmap in the purple→white gradient, overlays the robot
    path as a blue line and the dock as a green circle, then applies the
    ``orientation`` rotation.
    """
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        _LOGGER.warning("Pillow not available — cannot render v2 map PNG")
        return None

    try:
        dims = data.get("dimensions") or {}
        width = int(dims.get("width") or 0)
        height = int(dims.get("height") or 0)
        resolution = float(dims.get("resolution") or 0.0)
        offset_x = float(dims.get("offsetX") or 0.0)
        offset_y = float(dims.get("offsetY") or 0.0)

        if width <= 0 or height <= 0:
            _LOGGER.debug("v2 map: invalid dimensions %dx%d", width, height)
            return None

        # --- Find the dust data layer (prefer "total", fall back to first) ---
        dust_data: list[int] = []
        for dm in data.get("dustMap") or []:
            if not isinstance(dm, dict):
                continue
            dm_type = (dm.get("type") or "").lower()
            dm_vals = dm.get("data") or []
            if dm_type in ("total", "areavisited") and dm_vals:
                dust_data = [int(v) for v in dm_vals]
                break
        if not dust_data:
            for dm in data.get("dustMap") or []:
                if isinstance(dm, dict) and dm.get("data"):
                    dust_data = [int(v) for v in dm["data"]]
                    break

        if not dust_data:
            _LOGGER.debug(
                "v2 map: dustMap present but no renderable data layer — types: %s",
                [dm.get("type") for dm in (data.get("dustMap") or [])],
            )
            return None

        # --- Render the dust heatmap onto a width×height RGBA canvas ---
        max_val = max(dust_data) or 1
        n_levels = len(_DUST_GRADIENT_RGB)
        rgba = bytearray(width * height * 4)
        for i in range(min(len(dust_data), width * height)):
            level = dust_data[i]
            if level == 0:
                continue
            normalized = min(1.0, level / max_val)
            idx = min(n_levels - 1, int(normalized * n_levels))
            r, g, b = _DUST_GRADIENT_RGB[idx]
            rgba[i * 4] = r
            rgba[i * 4 + 1] = g
            rgba[i * 4 + 2] = b
            rgba[i * 4 + 3] = 220

        img = Image.frombytes("RGBA", (width, height), bytes(rgba))
        draw = ImageDraw.Draw(img)

        def _world_to_px(wx: float, wy: float) -> tuple[int, int]:
            """Map world coordinates to dust-grid pixel coordinates."""
            if resolution <= 0:
                return (width // 2, height // 2)
            px = int((wx - offset_x) / resolution)
            py = int((wy - offset_y) / resolution)
            return (max(0, min(width - 1, px)), max(0, min(height - 1, py)))

        # --- Robot path (blue semi-transparent line) ---
        clean_path = data.get("cleanPath") or []
        if clean_path and resolution > 0:
            pts = [
                _world_to_px(float(p.get("x") or 0), float(p.get("y") or 0))
                for p in clean_path
                if isinstance(p, dict)
            ]
            if len(pts) > 1:
                draw.line(pts, fill=(30, 144, 255, 200), width=2)

        # --- Dock location (solid green circle) ---
        dock = data.get("dockLocation")
        if isinstance(dock, dict) and dock.get("x") is not None:
            dx, dy = _world_to_px(float(dock.get("x") or 0), float(dock.get("y") or 0))
            draw.ellipse([dx - 4, dy - 4, dx + 4, dy + 4], fill=(0, 200, 80, 255))

    except Exception as err:  # noqa: BLE001
        _LOGGER.debug("v2 map rendering failed: %s", err)
        return None

    # Apply orientation (Y-flip always, then rotation)
    img = _apply_orientation(img, rotation_deg)

    # Scale up for legibility
    max_dim = max(img.size)
    if max_dim < 800:
        factor = max(1, 800 // max_dim)
        img = img.resize(
            (img.size[0] * factor, img.size[1] * factor),
            resample=Image.Resampling.NEAREST,
        )

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def _render_v2_floor_plan_png(
    data: dict,
    rotation_deg: int = 0,
    robot_position: tuple[float, float, float | None] | None = None,
) -> bytes | None:
    """Render zone boundary lines from a v2 clean-maps-data response as a floor plan PNG.

    Uses the same response as ``_render_v2_map_png`` (``GET /v2/{serial}/clean-maps-data/
    {cleanId}``) but draws only the zone boundary line segments and dock location on a
    white canvas — no dust heatmap.  This gives a usable floor plan for v2 devices
    (e.g. RB05 Spot+Scrub) where ``GET /v2/app/{serial}/persistent-maps/{id}``
    returns structured JSON instead of a pre-rendered PNG.

    JSON fields used:

    .. code-block:: json

        {
          "dimensions": {"width": int, "height": int,
                         "resolution": float,     /* metres per cell */
                         "offsetX": float,
                         "offsetY": float},
          "zones": [
            {"presentation": [{"start": {"x": float, "y": float},
                               "end":   {"x": float, "y": float},
                               "type":   int}]}
          ],
          "dockLocation": {"x": float, "y": float, "angle": float},
          "orientation": int
        }

    ``robot_position``, when given, is ``(x, y, angle)`` in the same world
    metres as ``dockLocation`` — the robot's most recent ``globalPosition``
    pose from a live ``CURRENT-STATE`` MQTT message (see
    ``DysonDevice.robot_global_position``/``robot_global_angle``). Drawn as a
    blue dot with a heading tick so the floor plan shows where the robot
    currently is during an active clean, not just the static zone layout.
    """
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        _LOGGER.warning("Pillow not available — cannot render v2 floor plan PNG")
        return None

    try:
        dims = data.get("dimensions") or {}
        width = int(dims.get("width") or 0)
        height = int(dims.get("height") or 0)
        resolution = float(dims.get("resolution") or 0.0)
        offset_x = float(dims.get("offsetX") or 0.0)
        offset_y = float(dims.get("offsetY") or 0.0)

        if width <= 0 or height <= 0:
            _LOGGER.debug("v2 floor plan: invalid dimensions %dx%d", width, height)
            return None

        # White background
        img = Image.new("RGBA", (width, height), (255, 255, 255, 255))
        draw = ImageDraw.Draw(img)

        def _world_to_px(wx: float, wy: float) -> tuple[int, int]:
            if resolution <= 0:
                return (width // 2, height // 2)
            px = int((wx - offset_x) / resolution)
            py = int((wy - offset_y) / resolution)
            return (max(0, min(width - 1, px)), max(0, min(height - 1, py)))

        # Draw zone boundary lines
        # Line.type: 0 = outer wall (thick dark), other = room separator (thin gray)
        zones = data.get("zones") or []
        has_lines = False
        for zone in zones:
            if not isinstance(zone, dict):
                continue
            for seg in zone.get("presentation") or []:
                if not isinstance(seg, dict):
                    continue
                start = seg.get("start") or {}
                end = seg.get("end") or {}
                sx, sy = _world_to_px(
                    float(start.get("x") or 0), float(start.get("y") or 0)
                )
                ex, ey = _world_to_px(
                    float(end.get("x") or 0), float(end.get("y") or 0)
                )
                line_type = seg.get("type", 0)
                # Type 0 → outer wall (thicker, darker); others → room separator
                if line_type == 0:
                    draw.line([sx, sy, ex, ey], fill=(40, 40, 40, 255), width=2)
                else:
                    draw.line([sx, sy, ex, ey], fill=(130, 130, 130, 255), width=1)
                has_lines = True

        # Dock location — green filled circle
        dock = data.get("dockLocation")
        has_dock = isinstance(dock, dict) and dock.get("x") is not None
        if has_dock:
            dx, dy = _world_to_px(float(dock.get("x") or 0), float(dock.get("y") or 0))
            draw.ellipse(
                [dx - 5, dy - 5, dx + 5, dy + 5],
                fill=(0, 200, 80, 255),
                outline=(0, 120, 40, 255),
            )

        # Robot's current position — blue dot with a heading tick, drawn last
        # so it sits on top of the zone lines and dock marker.
        has_robot = robot_position is not None
        if robot_position is not None:
            rx, ry, angle = robot_position
            px, py = _world_to_px(rx, ry)
            draw.ellipse(
                [px - 6, py - 6, px + 6, py + 6],
                fill=(30, 100, 240, 255),
                outline=(10, 50, 150, 255),
            )
            if angle is not None:
                # Heading tick: short line from centre in the facing direction.
                # World angle is radians, image Y grows downward — negate for
                # the on-screen rotation to match world convention.
                import math

                tick_len = 10
                tx = px + tick_len * math.cos(angle)
                ty = py - tick_len * math.sin(angle)
                draw.line([px, py, tx, ty], fill=(10, 50, 150, 255), width=2)

        if not has_lines and not has_dock and not has_robot:
            _LOGGER.debug(
                "v2 floor plan: no zones, dock location, or robot position in response"
            )
            return None

    except Exception as err:  # noqa: BLE001
        _LOGGER.debug("v2 floor plan rendering failed: %s", err)
        return None

    # Apply orientation (Y-flip always, then rotation)
    img = _apply_orientation(img, rotation_deg)

    # Scale up for legibility
    max_dim = max(img.size)
    if max_dim < 800:
        factor = max(1, 800 // max_dim)
        img = img.resize(
            (img.size[0] * factor, img.size[1] * factor),
            resample=Image.Resampling.NEAREST,
        )

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


# Per-zone fill colour keyed by the live ``cleanStatus`` value from
# GET /v1/app/{serial}/live-maps/cleaning. Confirmed values (3 sep 2026
# live probe, see dyson/notes/06-...md): CLEAN_NOT_REQUESTED (not
# selected this run), CLEAN_PENDING (selected, not started yet),
# CLEAN_IN_PROGRESS (robot currently in this zone), CLEAN_COMPLETE
# (done), CANT_CLEAN (robot couldn't reach it — e.g. a physical
# obstruction on the path). Unknown/future values fall back to
# _ZONE_STATUS_FALLBACK_RGBA rather than being skipped, so a new status
# Dyson might introduce still renders visibly instead of vanishing.
_ZONE_STATUS_FILL_RGBA: dict[str, tuple[int, int, int, int]] = {
    "CLEAN_NOT_REQUESTED": (235, 235, 235, 255),  # light grey — not part of this run
    "CLEAN_PENDING": (255, 244, 200, 255),  # pale amber — queued
    "CLEAN_IN_PROGRESS": (190, 225, 255, 255),  # light blue — robot is here now
    "CLEAN_COMPLETE": (200, 240, 205, 255),  # light green — done
    "CANT_CLEAN": (255, 205, 205, 255),  # light red — unreachable
}
_ZONE_STATUS_FALLBACK_RGBA: tuple[int, int, int, int] = (235, 235, 235, 255)


def _render_live_map_png(
    data: dict,
    rotation_deg: int = 0,
) -> bytes | None:
    """Render a live-map PNG from ``GET /v1/app/{serial}/live-maps/cleaning``.

    Unlike ``_render_v2_floor_plan_png`` (static zone outlines from the
    last *completed* clean, with no per-zone status), this draws the
    zones filled by their live ``cleanStatus`` colour (see
    ``_ZONE_STATUS_FILL_RGBA``), plus furniture silhouettes, no-go
    restriction zones, the dock, and the robot's current position —
    everything the live-maps/cleaning response provides.

    Unlike the v2 clean-maps-data response, this endpoint has no
    ``dimensions`` object (no width/height/resolution/offset in pixels) —
    only raw world-metre coordinates. The pixel canvas and world→pixel
    mapping are derived here from the bounding box of every coordinate in
    the response (zone outlines, furniture, dock, robot), with a fixed
    resolution and a small margin, rather than trusting a server-provided
    canvas size that doesn't exist for this endpoint.

    JSON fields used: ``zones`` (list of ``{id, name, cleanStatus,
    presentation: [{start, end, type}]}``), ``furniture`` (list of
    ``{type, points: [{x, y}, ...]}`` polygons), ``restrictions`` (list of
    ``{points, behavior}`` polygons), ``dockLocation`` (``{x, y, angle}``),
    ``robotLocation`` (``{x, y, angle}``), ``orientation`` (int).
    """
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        _LOGGER.warning("Pillow not available — cannot render live map PNG")
        return None

    try:
        zones = data.get("zones") or []
        furniture = data.get("furniture") or []
        restrictions = data.get("restrictions") or []
        dock = data.get("dockLocation")
        robot = data.get("robotLocation")

        # Collect every world-coordinate point to derive the bounding box —
        # this endpoint gives no canvas dimensions, unlike v2 clean-maps-data.
        points: list[tuple[float, float]] = []
        for zone in zones:
            if not isinstance(zone, dict):
                continue
            for seg in zone.get("presentation") or []:
                if not isinstance(seg, dict):
                    continue
                for key in ("start", "end"):
                    pt = seg.get(key) or {}
                    if pt.get("x") is not None and pt.get("y") is not None:
                        points.append((float(pt["x"]), float(pt["y"])))
        for item in furniture:
            if not isinstance(item, dict):
                continue
            for pt in item.get("points") or []:
                if isinstance(pt, dict) and pt.get("x") is not None:
                    points.append((float(pt["x"]), float(pt["y"])))
        for item in restrictions:
            if not isinstance(item, dict):
                continue
            for pt in item.get("points") or []:
                if isinstance(pt, dict) and pt.get("x") is not None:
                    points.append((float(pt["x"]), float(pt["y"])))
        if isinstance(dock, dict) and dock.get("x") is not None:
            points.append((float(dock["x"]), float(dock["y"])))
        if isinstance(robot, dict) and robot.get("x") is not None:
            points.append((float(robot["x"]), float(robot["y"])))

        if not points:
            _LOGGER.debug("Live map: no coordinates found in response")
            return None

        min_x = min(p[0] for p in points)
        max_x = max(p[0] for p in points)
        min_y = min(p[1] for p in points)
        max_y = max(p[1] for p in points)

        margin_m = 0.3
        resolution = 0.02  # metres per pixel — matches the ~2 cm grid used elsewhere
        offset_x = min_x - margin_m
        offset_y = min_y - margin_m
        width = max(1, int((max_x - min_x + 2 * margin_m) / resolution))
        height = max(1, int((max_y - min_y + 2 * margin_m) / resolution))

        img = Image.new("RGBA", (width, height), (255, 255, 255, 255))
        draw = ImageDraw.Draw(img, "RGBA")

        def _world_to_px(wx: float, wy: float) -> tuple[int, int]:
            px = int((wx - offset_x) / resolution)
            py = int((wy - offset_y) / resolution)
            return (max(0, min(width - 1, px)), max(0, min(height - 1, py)))

        # Zone fills first (so outlines/furniture/robot draw on top).
        for zone in zones:
            if not isinstance(zone, dict):
                continue
            segs = zone.get("presentation") or []
            poly = [
                _world_to_px(float(seg["start"]["x"]), float(seg["start"]["y"]))
                for seg in segs
                if isinstance(seg, dict) and isinstance(seg.get("start"), dict)
            ]
            if len(poly) >= 3:
                status = zone.get("cleanStatus")
                fill = _ZONE_STATUS_FILL_RGBA.get(status, _ZONE_STATUS_FALLBACK_RGBA)
                draw.polygon(poly, fill=fill)

        # Zone outlines (same wall/room-separator distinction as the v2 renderer).
        for zone in zones:
            if not isinstance(zone, dict):
                continue
            for seg in zone.get("presentation") or []:
                if not isinstance(seg, dict):
                    continue
                start = seg.get("start") or {}
                end = seg.get("end") or {}
                sx, sy = _world_to_px(
                    float(start.get("x") or 0), float(start.get("y") or 0)
                )
                ex, ey = _world_to_px(
                    float(end.get("x") or 0), float(end.get("y") or 0)
                )
                line_type = seg.get("type", 0)
                if line_type == 0:
                    draw.line([sx, sy, ex, ey], fill=(40, 40, 40, 255), width=2)
                else:
                    draw.line([sx, sy, ex, ey], fill=(130, 130, 130, 255), width=1)

        # Furniture silhouettes — light brown fill, no per-type styling (the
        # ``type`` field, e.g. "tvStand"/"doubleBed", is cosmetic only here).
        for item in furniture:
            if not isinstance(item, dict):
                continue
            poly = [
                _world_to_px(float(pt["x"]), float(pt["y"]))
                for pt in item.get("points") or []
                if isinstance(pt, dict) and pt.get("x") is not None
            ]
            if len(poly) >= 3:
                draw.polygon(
                    poly, fill=(210, 190, 165, 200), outline=(160, 140, 115, 255)
                )

        # No-go restriction zones — hatched-looking red outline (solid fill
        # would obscure the zone-status colour underneath, which matters more).
        for item in restrictions:
            if not isinstance(item, dict):
                continue
            poly = [
                _world_to_px(float(pt["x"]), float(pt["y"]))
                for pt in item.get("points") or []
                if isinstance(pt, dict) and pt.get("x") is not None
            ]
            if len(poly) >= 3:
                draw.polygon(poly, outline=(200, 40, 40, 255))

        # Dock — green filled circle.
        if isinstance(dock, dict) and dock.get("x") is not None:
            dx, dy = _world_to_px(float(dock["x"]), float(dock["y"]))
            draw.ellipse(
                [dx - 6, dy - 6, dx + 6, dy + 6],
                fill=(0, 200, 80, 255),
                outline=(0, 120, 40, 255),
            )

        # Robot — blue dot with heading tick, drawn last so it's always visible.
        if isinstance(robot, dict) and robot.get("x") is not None:
            rx, ry = _world_to_px(float(robot["x"]), float(robot["y"]))
            draw.ellipse(
                [rx - 7, ry - 7, rx + 7, ry + 7],
                fill=(30, 100, 240, 255),
                outline=(10, 50, 150, 255),
            )
            angle = robot.get("angle")
            if angle is not None:
                import math

                tick_len = 12
                tx = rx + tick_len * math.cos(float(angle))
                ty = ry - tick_len * math.sin(float(angle))
                draw.line([rx, ry, tx, ty], fill=(10, 50, 150, 255), width=2)

    except Exception as err:  # noqa: BLE001
        _LOGGER.debug("Live map rendering failed: %s", err)
        return None

    img = _apply_orientation(img, rotation_deg)

    max_dim = max(img.size)
    if max_dim < 800:
        factor = max(1, 800 // max_dim)
        img = img.resize(
            (img.size[0] * factor, img.size[1] * factor),
            resample=Image.Resampling.NEAREST,
        )

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def _render_presentation_png(
    presentation_png: bytes, rotation_deg: int = 0
) -> bytes | None:
    """Render the floor-plan PNG with the same orientation as the dust map."""
    try:
        from PIL import Image
    except ImportError:
        return None
    try:
        img = _palette_from_floor_plan(presentation_png)
        img = _apply_orientation(img, rotation_deg)
    except Exception as err:  # noqa: BLE001
        _LOGGER.debug("Failed to render presentation PNG: %s", err)
        return None
    max_dim = max(img.size)
    if max_dim < 600:
        factor = max(1, 600 // max_dim)
        img = img.resize(
            (img.size[0] * factor, img.size[1] * factor),
            resample=Image.Resampling.NEAREST,
        )
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


# ----------------------------------------------------------------------------
# Entities
# ----------------------------------------------------------------------------


class DysonDustMapImage(DysonEntity, ImageEntity):
    """Dust-density heatmap of the most recent clean, rendered as PNG."""

    coordinator: DysonDataUpdateCoordinator
    _attr_content_type = "image/png"

    def __init__(
        self, hass: HomeAssistant, coordinator: DysonDataUpdateCoordinator
    ) -> None:
        ImageEntity.__init__(self, hass)
        DysonEntity.__init__(self, coordinator)
        self._attr_unique_id = f"{coordinator.serial_number}_dust_map"
        self._attr_translation_key = "dust_map"
        self._attr_icon = "mdi:map-search"
        self._render_cache_key: tuple | None = None
        self._cached_png: bytes | None = None

    @property
    def should_poll(self) -> bool:
        # _attr_should_poll is inert on CoordinatorEntity subclasses (#408).
        return True

    async def _build(self) -> bytes | None:
        cleans = await fetch_clean_maps(self.coordinator)
        if not cleans:
            return None
        latest = cleans[0]
        clean_id = latest.clean_id

        dust_map_model = getattr(latest, "dust_map", None)
        download_url = getattr(latest, "download_url", None)

        _LOGGER.debug(
            "Dust map build for %s: clean_id=%s, has_dust_map=%s, has_download_url=%s",
            self.coordinator.serial_number,
            clean_id,
            dust_map_model is not None,
            download_url is not None,
        )

        # -------------------------------------------------------------------
        # v2 path: no embedded dust map blob — use the cloud APIs to get or
        # render a map image for the clean session.
        # Triggered for any record that has a clean_id but no embedded dust
        # map, regardless of whether a download_url is present.
        # -------------------------------------------------------------------
        if not dust_map_model and clean_id:
            if self._render_cache_key == ("v2", clean_id) and self._cached_png:
                return self._cached_png
            # Strategy 1: Map Visualizer API (works for Vis Nav, 404 for RB05).
            png = await _fetch_map_image(self.coordinator, clean_id)
            # Strategy 2: v2 clean-maps-data endpoint (logs response for diagnostics).
            if png is None:
                png = await _fetch_clean_map_data_image(self.coordinator, clean_id)
            if png is None:
                _LOGGER.debug(
                    "Dust map for %s: no image available for clean_id=%s"
                    " (map visualizer and clean-maps-data both returned nothing);"
                    " check DEBUG logs for clean_map_data response structure",
                    self.coordinator.serial_number,
                    clean_id,
                )
                return None
            self._render_cache_key = ("v2", clean_id)
            self._cached_png = png
            self._attr_image_last_updated = datetime.now(timezone.utc)
            return png

        # -------------------------------------------------------------------
        # v1 path: dust map blob embedded in the CleanRecord.
        # -------------------------------------------------------------------
        if not dust_map_model:
            return None

        # Fingerprint the render inputs before deciding whether the cached
        # PNG is still valid. Dyson updates the clean record in place during
        # a clean and re-versions the persistent map afterwards, so the
        # cleanId alone cannot tell a mid-clean snapshot from the final map.
        try:
            dust_blob = dust_map_model.dust_data[0].get("data") or ""
        except (AttributeError, IndexError, TypeError):
            dust_blob = ""
        dust_fp = hashlib.sha1(dust_blob.encode()).hexdigest() if dust_blob else None

        pmap = None
        pmap_id = latest.persistent_map_id
        if pmap_id:
            pmap = await _fetch_persist_map(self.coordinator, pmap_id)

        render_key = ("v1", clean_id, dust_fp, pmap_id, _pmap_fingerprint(pmap))
        if render_key == self._render_cache_key and self._cached_png:
            return self._cached_png

        # Convert DustMapData to the dict shape expected by _render_dust_map_png.
        dust_map_dict = {
            "width": dust_map_model.width,
            "height": dust_map_model.height,
            "resolution": dust_map_model.resolution,
            "dustData": dust_map_model.dust_data,
        }

        # Coordinate info for correctly positioning the dust crop on the
        # presentation map. Both maps quote positions in world mm; resolution
        # is mm/pixel (typically 20 for Vis Nav).
        resolution_mm_per_px = dust_map_model.resolution
        clean_position_mm: tuple[float, float] | None = None
        map_offset_mm: tuple[float, float] | None = None
        if latest.clean_map_position:
            pos = latest.clean_map_position
            clean_position_mm = (pos.x, pos.y)

        presentation_png: bytes | None = None
        rotation_deg = 0
        if pmap:
            if pmap.presentation_map_data:
                try:
                    presentation_png = base64.b64decode(pmap.presentation_map_data)
                except (ValueError, TypeError):
                    presentation_png = None
            rotation_deg = pmap.display_orientation
            if pmap.offset_x is not None and pmap.offset_y is not None:
                map_offset_mm = (pmap.offset_x, pmap.offset_y)

        cleaned_fp_png: bytes | None = None
        if latest.cleaned_footprint and latest.cleaned_footprint.data:
            try:
                cleaned_fp_png = base64.b64decode(latest.cleaned_footprint.data)
            except (ValueError, TypeError):
                cleaned_fp_png = None

        # The renderer is pure-Python pixel loops over the full bitmap —
        # run it in the executor so the event loop is not blocked.
        png = await self.hass.async_add_executor_job(
            partial(
                _render_dust_map_png,
                dust_map_dict,
                cleaned_fp_png,
                presentation_png,
                rotation_deg,
                map_offset_mm=map_offset_mm,
                clean_position_mm=clean_position_mm,
                map_resolution_mm_per_px=resolution_mm_per_px,
            )
        )
        if png:
            self._render_cache_key = render_key
            self._cached_png = png
            self._attr_image_last_updated = datetime.now(timezone.utc)
        return png

    async def async_image(self) -> bytes | None:
        return await self._build()

    async def async_update(self) -> None:
        # Trigger a refresh on HA's polling cycle so image_last_updated is fresh.
        await self._build()


class DysonFloorPlanImage(DysonEntity, ImageEntity):
    """Floor plan image entity — rendered from the persistent map or v2 zone boundaries.

    While actively cleaning: prefers ``GET /v1/app/{serial}/live-maps/cleaning``
    (``_render_live_map_png``) — per-zone ``cleanStatus``, furniture and
    restriction-zone geometry, none of which the sources below provide.
    Falls through to the below when idle or when that call fails/404s:

    For v1 devices (Vis Nav): uses the pre-rendered presentation PNG embedded in
    ``GET /v2/app/{serial}/persistent-maps/{id}``.
    For v2 devices (e.g. RB05 Spot+Scrub): renders zone boundary lines from
    ``GET /v2/{serial}/clean-maps-data/{cleanId}`` via ``_render_v2_floor_plan_png``.
    """

    coordinator: DysonDataUpdateCoordinator
    _attr_content_type = "image/png"

    def __init__(
        self, hass: HomeAssistant, coordinator: DysonDataUpdateCoordinator
    ) -> None:
        ImageEntity.__init__(self, hass)
        DysonEntity.__init__(self, coordinator)
        self._attr_unique_id = f"{coordinator.serial_number}_floor_plan"
        self._attr_translation_key = "floor_plan"
        self._attr_icon = "mdi:floor-plan"
        self._render_cache_key: tuple | None = None
        self._cached_png: bytes | None = None

    @property
    def should_poll(self) -> bool:
        # _attr_should_poll is inert on CoordinatorEntity subclasses (#408).
        return True

    async def _build(self) -> bytes | None:
        # While a clean is actively running, prefer the live-maps/cleaning
        # endpoint: it carries per-zone cleanStatus, furniture and
        # restriction-zone geometry that the completed-clean sources below
        # don't have at all. Only attempted while cleaning — this endpoint
        # 404s once the robot is docked/idle (confirmed via probe, see
        # dyson/notes/06-...md), so there's no point calling it otherwise.
        # Never cached (see _fetch_live_map_cleaning) and always retried on
        # failure — a transient miss here should fall through to the
        # completed-clean renderer below, not surface as "no floor plan".
        device = self.coordinator.device
        if device is not None:
            ha_activity = ROBOT_STATE_TO_HA_STATE.get(device.robot_state)
            if ha_activity == VacuumActivity.CLEANING:
                live_data = await _fetch_live_map_cleaning(self.coordinator)
                if live_data:
                    rotation = int(live_data.get("orientation") or 0)
                    png = _render_live_map_png(live_data, rotation)
                    if png is not None:
                        self._attr_image_last_updated = datetime.now(timezone.utc)
                        return png

        cleans = await fetch_clean_maps(self.coordinator)
        if not cleans:
            return None
        pmap_id = cleans[0].persistent_map_id
        if not pmap_id:
            _LOGGER.warning(
                "Floor plan for %s: most recent clean record has no"
                " persistent_map_id — cannot render floor plan",
                self.coordinator.serial_number,
            )
            return None
        pmap = await _fetch_persist_map(self.coordinator, pmap_id)
        if not pmap:
            _LOGGER.warning(
                "Floor plan for %s: persistent map %s could not be fetched"
                " (API error or unsupported endpoint for this device model)",
                self.coordinator.serial_number,
                pmap_id,
            )
            return None
        if not pmap.presentation_map_data:
            # For some device models (e.g. RB05/Spot+Scrub) the v1
            # persistent-maps endpoint returns a stub with no embedded image.
            # Fall back to the Map Visualizer API which renders the floor plan
            # server-side.
            _LOGGER.debug(
                "Floor plan for %s: persistent map %s has no presentation_map_data;"
                " trying Map Visualizer API",
                self.coordinator.serial_number,
                pmap_id,
            )
            # Robot's live position, if it's actively cleaning right now —
            # see DysonDevice.robot_global_position for the message format.
            # Never part of the cache key/short-circuit below: a moving robot
            # must re-render on every poll, not freeze at its first position.
            # Uses the same ROBOT_STATE_TO_HA_STATE mapping as vacuum.activity
            # rather than the raw cleaningState field, which only distinguishes
            # NOT_CLEANING/REMOVING_DIRT and isn't a reliable "is moving" signal.
            robot_pos = None
            device = self.coordinator.device
            if device is not None:
                robot_state = device.robot_state
                ha_activity = ROBOT_STATE_TO_HA_STATE.get(robot_state)
                if ha_activity == VacuumActivity.CLEANING:
                    pos = device.robot_global_position
                    if pos is not None:
                        robot_pos = (pos[0], pos[1], device.robot_global_angle)

            render_key = ("v2fp", pmap_id, cleans[0].clean_id)
            if (
                robot_pos is None
                and render_key == self._render_cache_key
                and self._cached_png
            ):
                return self._cached_png
            # Map Visualizer PNGs are server-rendered bitmaps — there's no
            # way to overlay a robot marker on them client-side, so the live
            # position only ever appears via the v2 zone-boundary renderer
            # below. Not a gap in practice: this API 404s for every v2
            # device (RB05 included), so v2 devices always fall through.
            png = await _fetch_map_image(self.coordinator, pmap_id)
            if png is None:
                _LOGGER.debug(
                    "Floor plan for %s: map visualizer returned no image for"
                    " persistent_map_id=%s — trying v2 zone-boundary renderer",
                    self.coordinator.serial_number,
                    pmap_id,
                )
                # v2 devices (e.g. RB05): no pre-rendered floor plan bitmap
                # exists anywhere.  Render the zone boundary lines (plus the
                # robot's live position, if cleaning) from the most recent
                # clean-maps-data response instead.
                clean_id = cleans[0].clean_id
                png = None
                if clean_id:
                    fp_data = await _fetch_v2_floor_plan_data(
                        self.coordinator, clean_id
                    )
                    if fp_data:
                        rotation = int(fp_data.get("orientation") or 0)
                        png = _render_v2_floor_plan_png(
                            fp_data, rotation, robot_position=robot_pos
                        )
                if png is None:
                    _LOGGER.debug(
                        "Floor plan for %s: no floor plan image available for"
                        " persistent_map_id=%s (v1 Map Visualizer 404, v2"
                        " zone-boundary render returned nothing)",
                        self.coordinator.serial_number,
                        pmap_id,
                    )
                    return None
            # Don't persist a robot-position render into the entity cache —
            # otherwise the next poll's cache-key match (pmap_id/clean_id
            # unchanged) would return this frame forever once the robot
            # stops cleaning and robot_pos goes back to None.
            if robot_pos is None:
                self._render_cache_key = render_key
                self._cached_png = png
            self._attr_image_last_updated = datetime.now(timezone.utc)
            return png

        # The map UUID never changes across map versions — fingerprint the
        # map's content so post-clean re-versions replace the cached render.
        render_key = ("v1fp", pmap_id, _pmap_fingerprint(pmap))
        if render_key == self._render_cache_key and self._cached_png:
            return self._cached_png
        try:
            png_in = base64.b64decode(pmap.presentation_map_data)
        except (ValueError, TypeError) as err:
            _LOGGER.warning(
                "Floor plan for %s: could not base64-decode presentation_map_data: %s",
                self.coordinator.serial_number,
                err,
            )
            return None

        png = await self.hass.async_add_executor_job(
            _render_presentation_png, png_in, pmap.display_orientation
        )
        if png:
            self._render_cache_key = render_key
            self._cached_png = png
            self._attr_image_last_updated = datetime.now(timezone.utc)
        return png

    async def async_image(self) -> bytes | None:
        return await self._build()

    async def async_update(self) -> None:
        await self._build()
