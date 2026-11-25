"""The model bundle: verified before use, refused with one cause, scored in the manifest's order."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import shutil
from pathlib import Path

import lightgbm as lgb
import model_bars
import numpy as np
import pandas as pd
import pytest

from tickr import config
from tickr.model import bundle as B
from tickr.model import features
from tickr.model.errors import ModelRefused

ROOT = Path(__file__).resolve().parents[2]
NAME, VERSION = "l1", "2026-12-01a"
FEATURES = ["m", "sess", "c", "chg", "ret5", "rise30", "inl", "spy_chg", "r_rv", "vu"]


@pytest.fixture(scope="module")
def rows():
    days = model_bars.market(7)
    today, bars = days[-1]
    state = features.history(days[:-1], {}, today, None)
    minute = 400
    return features.minute_rows(model_bars.cut(bars, today, minute), today, minute, state, None)


def train(rows: pd.DataFrame, names: list[str], seed: int) -> lgb.Booster:
    rs = np.random.RandomState(seed)
    x = np.tile(rows[names].to_numpy(np.float32), (20, 1))
    y = rs.normal(size=len(x))
    params = {"objective": "huber", "num_leaves": 4, "min_data_in_leaf": 5, "verbose": -1,
              "seed": seed}
    return lgb.train(params, lgb.Dataset(x, y, feature_name=names), num_boost_round=6)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_bundle(base: Path, rows: pd.DataFrame, names=FEATURES, **changes) -> Path:
    folder = base / f"{NAME}-{VERSION}"
    folder.mkdir(parents=True)
    heads = {}
    for k, head in enumerate(("h30e", "t10e")):
        booster = train(rows, names, k)
        path = folder / f"{head}.txt"
        booster.save_model(str(path))
        heads[head] = {"file": path.name, "sha256": sha256(path), "trees": booster.num_trees()}
    manifest = {"schema": 1, "name": NAME, "version": VERSION, "lightgbm": lgb.__version__,
                "feature_code": dict(features.PINS), "features": list(names),
                "feature_list_md5": B.feature_list_md5(names), "heads": heads,
                "training": {"labels": "l1/v14", "train_cutoff": "2026-11-18"}}
    manifest.update(changes)
    (folder / B.MANIFEST).write_text(json.dumps(manifest))
    return folder


def rewrite(folder: Path, **changes) -> None:
    path = folder / B.MANIFEST
    data = json.loads(path.read_text())
    data.update(changes)
    path.write_text(json.dumps(data))


def refused(folder: Path) -> str:
    with pytest.raises(ModelRefused) as exc:
        B.Scorer(B.open_bundle(folder))
    return exc.value.cause


def test_a_good_bundle_scores_rows_as_its_boosters_do(tmp_path, rows):
    folder = write_bundle(tmp_path, rows)
    scorer = B.Scorer(B.open_bundle(folder))
    assert scorer.bundle.identity == f"{NAME}-{VERSION}"
    assert scorer.bundle.training["labels"] == "l1/v14"
    shuffled = rows[list(reversed(rows.columns))]  # the column order of the rows does not matter
    got = scorer.score(shuffled)
    x = rows[FEATURES].to_numpy(np.float32)
    for head in ("h30e", "t10e"):
        want = lgb.Booster(model_file=str(folder / f"{head}.txt")).predict(x)
        assert np.array_equal(got[head], want)
        assert len(got[head]) == len(rows)


def test_a_changed_model_file_is_refused(tmp_path, rows):
    folder = write_bundle(tmp_path, rows)
    with (folder / "h30e.txt").open("a") as f:
        f.write("\n")
    assert refused(folder) == "bundle_file_sha256"


def test_a_missing_or_stray_file_is_refused(tmp_path, rows):
    folder = write_bundle(tmp_path, rows)
    (folder / "notes.txt").write_text("x")
    assert refused(folder) == "bundle_file_unlisted"
    (folder / "notes.txt").unlink()
    (folder / "t10e.txt").unlink()
    assert refused(folder) == "bundle_file_missing"


def test_a_head_file_outside_the_folder_is_refused(tmp_path, rows):
    folder = write_bundle(tmp_path, rows)
    shutil.copy(folder / "h30e.txt", tmp_path / "h30e.txt")
    data = json.loads((folder / B.MANIFEST).read_text())
    data["heads"]["h30e"]["file"] = "../h30e.txt"
    rewrite(folder, heads=data["heads"])
    assert refused(folder) == "bundle_file_outside"


def test_other_feature_code_is_refused(tmp_path, rows):
    folder = write_bundle(tmp_path, rows)
    rewrite(folder, feature_code={**features.PINS, "wm.py": "0" * 32})
    assert refused(folder) == "feature_code_differs"


def test_a_feature_the_bot_cannot_compute_live_is_refused(tmp_path, rows):
    rows = rows.assign(k_fl5=0.0)  # a tick feature: no live tick feed yet
    folder = write_bundle(tmp_path, rows, names=[*FEATURES, "k_fl5"])
    assert refused(folder) == "feature_not_live"


def test_a_feature_list_digest_that_does_not_match_is_refused(tmp_path, rows):
    folder = write_bundle(tmp_path, rows)
    rewrite(folder, feature_list_md5="0" * 32)
    assert refused(folder) == "feature_list"


def test_a_manifest_list_that_differs_from_the_model_file_is_refused(tmp_path, rows):
    folder = write_bundle(tmp_path, rows)
    names = [*FEATURES[1:], FEATURES[0]]
    rewrite(folder, features=names, feature_list_md5=B.feature_list_md5(names))
    assert refused(folder) == "head_features"


def test_a_tree_count_that_differs_is_refused(tmp_path, rows):
    folder = write_bundle(tmp_path, rows)
    data = json.loads((folder / B.MANIFEST).read_text())
    data["heads"]["h30e"]["trees"] += 1
    rewrite(folder, heads=data["heads"])
    assert refused(folder) == "head_trees"


def test_another_lightgbm_version_is_refused(tmp_path, rows):
    folder = write_bundle(tmp_path, rows, lightgbm="4.6.0")
    assert refused(folder) == "lightgbm_version"


@pytest.mark.parametrize("change,cause", [
    ({"schema": 2}, "manifest_schema"),
    ({"extra": 1}, "manifest_keys"),
    ({"version": "2026-12-02"}, "bundle_identity"),
])
def test_manifest_refusals(tmp_path, rows, change, cause):
    folder = write_bundle(tmp_path, rows)
    rewrite(folder, **change)
    assert refused(folder) == cause


def test_no_manifest_is_refused(tmp_path):
    (tmp_path / f"{NAME}-{VERSION}").mkdir()
    assert refused(tmp_path / f"{NAME}-{VERSION}") == "manifest_missing"


def test_a_missing_row_feature_is_refused_not_filled(tmp_path, rows):
    scorer = B.Scorer(B.open_bundle(write_bundle(tmp_path, rows)))
    with pytest.raises(ModelRefused) as exc:
        scorer.score(rows.drop(columns=["vu"]))
    assert exc.value.cause == "row_feature_missing"


def test_the_shipped_config_keeps_the_model_off_and_names_no_bundle():
    cfg = config.load(ROOT / "config" / "bot.toml")
    assert cfg.model.enabled is False and cfg.model.bundle == ""
    with pytest.raises(ModelRefused) as exc:
        B.load_configured(cfg)
    assert exc.value.cause == "bundle_not_configured"


def test_the_configured_bundle_is_found_under_the_data_folder(tmp_path, rows):
    text = (ROOT / "config" / "bot.toml").read_text()
    text = text.replace('bundle = ""', f'bundle = "{NAME}-{VERSION}"')
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "bot.toml").write_text(text)
    cfg = config.load(tmp_path / "config" / "bot.toml", root=tmp_path)
    write_bundle(tmp_path / cfg.run.data_dir / cfg.model.bundles_dir, rows)
    assert B.load_configured(cfg).bundle.identity == f"{NAME}-{VERSION}"


@pytest.mark.parametrize("key,value", [
    ("bundles_dir", "/tmp/models"), ("bundles_dir", "../models"), ("bundle", "../l1-x"),
    ("bundle", "a/l1-x")])
def test_bundle_paths_stay_under_the_data_folder(key, value):
    cfg = config.load(ROOT / "config" / "bot.toml")
    model = dataclasses.replace(cfg.model, **{"bundle": "l1-x", key: value})
    with pytest.raises(ModelRefused) as exc:
        B.bundle_folder(dataclasses.replace(cfg, model=model))
    assert exc.value.cause == "bundle_path_outside"


def test_switching_the_model_on_is_refused_until_it_is_wired(tmp_path):
    text = (ROOT / "config" / "bot.toml").read_text().replace(
        "[model]\n", "[model]\n", 1)
    text = text.replace("enabled = false\n# Folder of model bundles",
                        "enabled = true\n# Folder of model bundles")
    path = tmp_path / "bot.toml"
    path.write_text(text)
    with pytest.raises(config.ConfigError, match=r"\[model\] enabled"):
        config.load(path, root=ROOT)
