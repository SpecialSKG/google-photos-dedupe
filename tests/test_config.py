"""Tests para la carga y validación de configuración."""

import pytest
from argparse import Namespace

from photos_dedupe.config import Config


class FakeArgs:
    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)


def test_defaults():
    c = Config()
    assert c.inputs == []
    assert c.out_dir == "output_consolidado"
    assert c.mode == "exact"
    assert c.action == "dry-run"
    assert c.phash_threshold == 6
    assert c.workers == 4
    assert c.keep_structure is False
    assert c.group_by_year is False
    assert c.reports_csv and c.reports_json and c.reports_xlsx


def test_load_from_dict():
    c = Config()
    c.load_from_dict({
        "inputs": ["a", "b"],
        "out_dir": "out",
        "mode": "perceptual",
        "action": "copy",
        "workers": 8,
        "phash_threshold": 3,
        "keep_structure": True,
        "ignore_json": False,
        "group_by_year": True,
        "unknown_year_dir": "SIN_AÑO",
        "date_source_priority": ["exif", "mtime"],
        "timezone_mode": "utc",
        "reports": {"csv": False, "json": True, "xlsx": False},
    })
    assert c.inputs == ["a", "b"]
    assert c.out_dir == "out"
    assert c.mode == "perceptual"
    assert c.action == "copy"
    assert c.workers == 8
    assert c.phash_threshold == 3
    assert c.keep_structure is True
    assert c.ignore_json is False
    assert c.group_by_year is True
    assert c.unknown_year_dir == "SIN_AÑO"
    assert c.date_source_priority == ["exif_datetime_original", "mtime"]
    assert c.timezone_mode == "utc"
    assert c.reports_csv is False
    assert c.reports_json is True
    assert c.reports_xlsx is False


def test_date_source_migration_takeout_json():
    c = Config()
    c.load_from_dict({"date_source_priority": ["takeout_json", "mtime"]})
    assert c.date_source_priority == ["takeout_photo_taken_time", "mtime"]


def test_merge_args_cli_wins():
    c = Config()
    c.load_from_dict({"inputs": ["file"], "out_dir": "a", "mode": "exact", "workers": 2})
    args = FakeArgs(
        inputs=None,
        out_dir="cli_dir",
        mode="perceptual",
        action="move",
        phash_threshold=9,
        workers=None,
        keep_structure=None,
    )
    c.merge_args(args)
    assert c.out_dir == "cli_dir"
    assert c.mode == "perceptual"
    assert c.action == "move"
    assert c.phash_threshold == 9
    assert c.workers == 2
    assert c.keep_structure is False


def test_merge_args_ignores_none():
    c = Config()
    c.load_from_dict({"mode": "exact"})
    args = FakeArgs(inputs=None, out_dir=None, mode=None, action=None,
                    phash_threshold=None, workers=None, keep_structure=None)
    c.merge_args(args)
    assert c.mode == "exact"


def test_validate_requires_inputs(tmp_path):
    c = Config()
    with pytest.raises(ValueError, match="At least one input"):
        c.validate()


def test_validate_invalid_mode(tmp_path):
    d = tmp_path / "in"
    d.mkdir()
    c = Config()
    c.inputs = [str(d)]
    c.mode = "bogus"
    with pytest.raises(ValueError, match="Invalid mode"):
        c.validate()


def test_validate_invalid_action(tmp_path):
    d = tmp_path / "in"
    d.mkdir()
    c = Config()
    c.inputs = [str(d)]
    c.action = "delete"
    with pytest.raises(ValueError, match="Invalid action"):
        c.validate()


def test_validate_negative_threshold(tmp_path):
    d = tmp_path / "in"
    d.mkdir()
    c = Config()
    c.inputs = [str(d)]
    c.phash_threshold = -1
    with pytest.raises(ValueError, match="phash_threshold"):
        c.validate()


def test_validate_workers_at_least_one(tmp_path):
    d = tmp_path / "in"
    d.mkdir()
    c = Config()
    c.inputs = [str(d)]
    c.workers = 0
    with pytest.raises(ValueError, match="workers"):
        c.validate()


def test_validate_input_must_exist(tmp_path):
    c = Config()
    c.inputs = [str(tmp_path / "no_existe")]
    with pytest.raises(FileNotFoundError):
        c.validate()


def test_validate_bad_date_source(tmp_path):
    d = tmp_path / "in"
    d.mkdir()
    c = Config()
    c.inputs = [str(d)]
    c.date_source_priority = ["nasa"]
    with pytest.raises(ValueError, match="Invalid date source"):
        c.validate()


def test_load_from_file(tmp_path):
    cfg = tmp_path / "c.yaml"
    cfg.write_text("inputs: [x]\nout_dir: yy\nmode: copy\n", encoding="utf-8")
    c = Config()
    c.load_from_file(str(cfg))
    assert c.inputs == ["x"]
    assert c.out_dir == "yy"


def test_load_from_file_missing():
    c = Config()
    with pytest.raises(FileNotFoundError):
        c.load_from_file("no_such_file.yaml")


def test_load_from_dict_none():
    c = Config()
    c.load_from_dict(None)
    assert c.inputs == []