"""jdm flight recorder: noise filter, JSON lines, size cap, coordinator hook."""

import json
from unittest.mock import MagicMock

from custom_components.hass_dyson.coordinator import DysonDataUpdateCoordinator
from custom_components.hass_dyson.jdm_log import JdmFlightRecorder, is_noise


class TestNoiseFilter:
    def test_polls_are_noise(self):
        for method in (
            "prop.get",
            "service.get_preference",
            "service.get_order",
            "service.get_map_list",
        ):
            assert is_noise({"method": method})

    def test_repeating_broadcasts_are_noise(self):
        assert is_noise({"method": "prop.post", "params": {"quantity": 70}})
        assert is_noise(
            {"method": "prop.post", "params": {"work_time": {"total": 10800}}}
        )
        assert is_noise(
            {"method": "prop.post", "params": {"cur_path": [1, -0.03, 0.0, -1.7, 0, 1]}}
        )
        assert is_noise(
            {"method": "prop.post", "params": {"quantity": 85, "cleaning_area": 1504}}
        )

    def test_everything_else_is_kept(self):
        assert not is_noise({"method": "prop.set", "params": {"alarm": 1}})
        assert not is_noise({"method": "service.set_virtual_wall", "params": {}})
        assert not is_noise({"method": "prop.post", "params": {"fault": 2510}})
        assert not is_noise(
            {"method": "prop.post", "params": {"quantity": 70, "fault": 1}}
        )
        assert not is_noise({"method": "event.map_change.post", "params": {}})
        assert not is_noise({"status": "SUCCESS"})


class TestRecorder:
    def test_writes_one_json_line_per_message(self, tmp_path):
        path = str(tmp_path / "jdm.jsonl")
        recorder = JdmFlightRecorder(path)
        assert recorder.record(
            "SER", "command/jdm", {"method": "prop.set", "params": {"alarm": 1}}
        )
        assert not recorder.record("SER", "status/jdm", {"method": "prop.get"})
        lines = open(path, encoding="utf-8").read().splitlines()
        assert len(lines) == 1
        row = json.loads(lines[0])
        assert row["serial"] == "SER"
        assert row["topic"] == "command/jdm"
        assert row["data"]["params"] == {"alarm": 1}
        assert row["t"]

    def test_long_strings_and_lists_are_shortened(self, tmp_path):
        path = str(tmp_path / "jdm.jsonl")
        JdmFlightRecorder(path).record(
            "S", "status/jdm", {"method": "x", "s": "a" * 5000, "l": list(range(500))}
        )
        row = json.loads(open(path, encoding="utf-8").read())
        assert len(row["data"]["s"]) < 1600
        assert len(row["data"]["l"]) == 201

    def test_file_size_is_capped_by_rotation(self, tmp_path):
        path = str(tmp_path / "jdm.jsonl")
        recorder = JdmFlightRecorder(path, max_bytes=2000, backup_count=2)
        for i in range(200):
            recorder.record(
                "S", "command/jdm", {"method": "prop.set", "params": {"n": i}}
            )
        files = sorted(p.name for p in tmp_path.iterdir())
        assert files == ["jdm.jsonl", "jdm.jsonl.1", "jdm.jsonl.2"]
        assert sum(p.stat().st_size for p in tmp_path.iterdir()) < 3 * 2200


class TestCoordinatorHook:
    def _coordinator(self, tmp_path):
        coord = MagicMock(spec=DysonDataUpdateCoordinator)
        coord.serial_number = "7VS-EU-TST0001A"
        coord.hass = MagicMock()
        coord.hass.config.path = MagicMock(return_value=str(tmp_path / "x.jsonl"))
        coord._record_jdm = DysonDataUpdateCoordinator._record_jdm.__get__(coord)
        return coord

    def test_records_a_setting_write(self, tmp_path):
        coord = self._coordinator(tmp_path)
        coord._record_jdm(
            "RB05/7VS-EU-TST0001A/command/jdm",
            {"method": "prop.set", "params": {"child_lock": 1}},
        )
        row = json.loads((tmp_path / "x.jsonl").read_text())
        assert row["topic"] == "command/jdm"
        assert row["data"]["params"] == {"child_lock": 1}

    def test_failure_never_raises(self, tmp_path):
        coord = self._coordinator(tmp_path)
        coord.hass.config.path = MagicMock(side_effect=OSError("disk"))
        coord._record_jdm("RB05/S/command/jdm", {"method": "prop.set"})
