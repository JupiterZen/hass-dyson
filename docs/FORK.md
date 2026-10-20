# About this fork

This is [JupiterZen/hass-dyson](https://github.com/JupiterZen/hass-dyson), a fork of
[cmgrayb/hass-dyson](https://github.com/cmgrayb/hass-dyson). It follows upstream
(currently merged up to v0.38.0) and adds features, mostly for the **Dyson Spot+Scrub Ai
(RB05) robot vacuum**. Release versions look like `0.38.0-jz.1`: the upstream version it is
based on, plus the fork's own counter.

> **Status: experimental.** Developed and tested against one Spot+Scrub Ai (RB05) on an EU
> account, two air purifiers and one V16 Piston Animal. Other robots, regions and firmware
> may behave differently. Several features rely on Dyson's cloud and MQTT behaviour that was
> worked out by observing the MyDyson app, not from documentation, so Dyson can break them
> at any time. Use at your own risk.

## Install (HACS)

1. HACS → ⋮ → **Custom repositories** → add `https://github.com/JupiterZen/hass-dyson`,
   category **Integration**.
2. Install **Dyson (JupiterZen fork)** and restart Home Assistant.

The fork uses the same domain (`hass_dyson`) as upstream, so **you can only have one of them
installed**. Remove the upstream copy first. Existing config entries carry over; fork-only
entities appear next to the ones you already have. Going back to upstream later works the
same way; the fork-only entities then become unavailable.

## What the fork adds

Everything below is for the RB05 unless noted. Where upstream built the same thing (it added
its own RB05 support), the fork keeps its own version only where entity IDs or states differ;
see the merge commit message for the decisions.

- **Per-room cleaning settings**: cleaning type (vacuum, vacuum + mop, mop only, vacuum then
  mop), water level, strategy (auto/quick/quiet/boost), mop passes and UV scan, as `select`
  entities. These are written to the same place the MyDyson app uses.
- **Room selection**: a target switch per room and a *Start selected zones* button;
  `hass_dyson.start_zone_clean` and `hass_dyson.set_zone_behaviour` services.
- **Dock actions**: stop/delay, empty bin, wash and dry, dry the mop
  (`hass_dyson.start_dock_action` / `abort_dock_action`, plus buttons).
- **Robot settings**: child lock, alarm, do-not-disturb, voice notifications, voice
  language, volume, wash-mop-before-clean, hot-water mop, detergent, collect-dust,
  self-clean interval and air-dry settings.
- **Status and planning sensors**: day status per room, resume simulation, planned clean
  estimate and duration, room preferences, dirt/obstacle/spot-zone tallies, consumables,
  dock state, back-wash frequency, a human-readable robot message, active fault code.
- **Floor plan image** with the live position, the driven route coloured by cleaning type,
  and furniture drawn in the MyDyson app.
- **V16 Piston Animal (BLE)**: a switch to hand the Bluetooth link to the MyDyson app, and
  detection of attachments.

## Known limitations

- **No dashboard or automations yet.** The fork's author uses a Lovelace dashboard and
  automations built on these entities. They are not generic yet (they contain one person's
  serial number and room names) and will be added later as a generated dashboard and
  blueprints.
- **Flight recorder is always on.** For research the integration appends non-routine
  `jdm` messages of the robot (including its serial number and map data) to
  `hass_dyson_jdm.jsonl` in your Home Assistant config folder (rotating, about 4 MB at most,
  never sent anywhere). It should become opt-in; until then you can delete the file freely.
- Some entity names and a few messages are in Dutch.
- Not every upstream feature is covered by the fork's own tests; the test suite passes
  (3700+ tests) but tests mock the robot.

## Reporting problems

Issues with the fork's features: open an issue on
[JupiterZen/hass-dyson](https://github.com/JupiterZen/hass-dyson/issues), not upstream.
Please don't ask the upstream maintainer for support for fork-only features.

