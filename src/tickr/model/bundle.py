"""A versioned model bundle: open it, prove it is the bundle that was trained, score rows.

A bundle is one folder `<[run] data_dir>/<[model] bundles_dir>/<name>-<version>/` holding
`manifest.json` and one LightGBM model text file per head (as the trainer saves them with
`Booster.save_model`). The manifest, written by the training side when it hands a model over:

    {
      "schema": 1,
      "name": "l1", "version": "2026-12-01a",
      "lightgbm": "4.7.0",
      "feature_code": {"wm2_l1f.py": "<md5>", ...},
      "features": ["m", "sess", "c", ...],
      "feature_list_md5": "<md5 of the names joined by newlines, as l1train records it>",
      "heads": {"h30e": {"file": "h30e.txt", "sha256": "<hex>", "trees": 812}, ...},
      "training": {...}
    }

`training` is a free record (train cutoff, label version, data digests) kept for the journal and
never interpreted here. Everything else is checked before a single row is scored; each failure
is a ModelRefused with one cause:

- `manifest_*`: missing, not JSON, wrong schema, unknown or missing keys;
- `bundle_identity`: the folder is not `<name>-<version>`;
- `bundle_file_*`: a head file is missing, outside the folder, or its sha256 differs; a file the
  manifest does not name lies in the folder (`bundle_file_unlisted`);
- `lightgbm_version`: the installed LightGBM is not the one that trained the model;
- `feature_code_differs`: the bundle was built by other feature code than the pinned copy the bot
  runs (`tickr.model.features.PINS`), so its features would not mean the same thing live;
- `feature_list`: names twice, or a digest that does not match the list;
- `feature_not_live`: a feature the bot cannot compute live yet (only the groups of
  `tickr.model.features.LIVE_GROUPS` are served);
- `head_features` / `head_trees`: a model file disagrees with the manifest.

Scoring takes the feature rows as `tickr.model.features.minute_rows` returns them and builds the
matrix in the manifest's order (float32, as the trainer and scorer read the package); a
missing column refuses (`row_feature_missing`), never fills.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tickr.config import Config
from tickr.model import features
from tickr.model.errors import ModelRefused

MANIFEST = "manifest.json"
SCHEMA = 1
KEYS = frozenset({"schema", "name", "version", "lightgbm", "feature_code", "features",
                  "feature_list_md5", "heads", "training"})
HEAD_KEYS = frozenset({"file", "sha256", "trees"})
_NAME = re.compile(r"[a-z0-9][a-z0-9._]*")  # no "-": the folder is <name>-<version>
_VERSION = re.compile(r"[a-z0-9][a-z0-9._-]*")
_HEAD = re.compile(r"[a-z0-9_]+")
_CHUNK = 1 << 20


@dataclass(frozen=True)
class Head:
    name: str
    path: Path
    trees: int


@dataclass(frozen=True)
class Bundle:
    folder: Path
    name: str
    version: str
    lightgbm: str
    features: tuple[str, ...]
    heads: tuple[Head, ...]
    training: dict[str, Any]

    @property
    def identity(self) -> str:
        return f"{self.name}-{self.version}"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(_CHUNK):
            h.update(chunk)
    return h.hexdigest()


def feature_list_md5(names: list[str] | tuple[str, ...]) -> str:
    """The digest the trainer records for a feature list (l1train: md5 of the names joined by
    newlines)."""
    return hashlib.md5("\n".join(names).encode("utf-8")).hexdigest()


def _manifest(folder: Path) -> dict[str, Any]:
    path = folder / MANIFEST
    if not path.is_file():
        raise ModelRefused("manifest_missing", str(folder))
    try:
        data = json.loads(path.read_text("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ModelRefused("manifest_unreadable", type(exc).__name__) from exc
    if not isinstance(data, dict):
        raise ModelRefused("manifest_unreadable", "not an object")
    if data.get("schema") != SCHEMA:
        raise ModelRefused("manifest_schema", repr(data.get("schema")))
    if set(data) != KEYS:
        extra, missing = sorted(set(data) - KEYS), sorted(KEYS - set(data))
        raise ModelRefused("manifest_keys", f"unknown {extra}, missing {missing}")
    return data


def _text(data: dict[str, Any], key: str) -> str:
    value = data[key]
    if not isinstance(value, str) or not value:
        raise ModelRefused("manifest_keys", f"{key} is not a text")
    return value


def open_bundle(folder: Path | str) -> Bundle:
    """Verify every file and promise of the bundle in `folder`; nothing is loaded into LightGBM
    yet (`Scorer` does that)."""
    folder = Path(folder)
    data = _manifest(folder)
    name, version = _text(data, "name"), _text(data, "version")
    if (not (_NAME.fullmatch(name) and _VERSION.fullmatch(version))
            or folder.name != f"{name}-{version}"):
        raise ModelRefused("bundle_identity", f"folder {folder.name}, manifest {name}-{version}")
    if data["feature_code"] != features.PINS:
        differ = sorted(k for k in set(features.PINS) | set(data["feature_code"] or {})
                        if (data["feature_code"] or {}).get(k) != features.PINS.get(k))
        raise ModelRefused("feature_code_differs", ", ".join(differ))
    names = data["features"]
    if (not isinstance(names, list) or not names
            or not all(isinstance(n, str) and n for n in names)):
        raise ModelRefused("feature_list", "not a list of names")
    if len(set(names)) != len(names):
        raise ModelRefused("feature_list", "a name twice")
    if data["feature_list_md5"] != feature_list_md5(names):
        raise ModelRefused("feature_list", "digest differs from the list")
    live = set(features.live_features())
    not_live = [n for n in names if n not in live]
    if not_live:
        raise ModelRefused("feature_not_live", ", ".join(not_live))
    heads_raw = data["heads"]
    if not isinstance(heads_raw, dict) or not heads_raw:
        raise ModelRefused("manifest_keys", "heads is empty")
    heads = []
    for head, spec in sorted(heads_raw.items()):
        if (not _HEAD.fullmatch(head) or not isinstance(spec, dict) or set(spec) != HEAD_KEYS
                or not isinstance(spec["trees"], int) or spec["trees"] < 1
                or not isinstance(spec["file"], str) or not isinstance(spec["sha256"], str)):
            raise ModelRefused("manifest_keys", f"head {head!r}")
        path = folder / spec["file"]
        if path.parent.resolve() != folder.resolve() or path.name == MANIFEST:
            raise ModelRefused("bundle_file_outside", spec["file"])
        if not path.is_file():
            raise ModelRefused("bundle_file_missing", spec["file"])
        if _sha256(path) != spec["sha256"]:
            raise ModelRefused("bundle_file_sha256", spec["file"])
        heads.append(Head(head, path, spec["trees"]))
    listed = {MANIFEST} | {h.path.name for h in heads}
    unlisted = sorted(p.name for p in folder.iterdir() if p.name not in listed)
    if unlisted:
        raise ModelRefused("bundle_file_unlisted", ", ".join(unlisted))
    training = data["training"]
    if not isinstance(training, dict):
        raise ModelRefused("manifest_keys", "training is not an object")
    return Bundle(folder, name, version, _text(data, "lightgbm"), tuple(names), tuple(heads),
                  training)


class Scorer:
    """The bundle's heads loaded into LightGBM, after the version check."""

    def __init__(self, bundle: Bundle) -> None:
        try:
            import lightgbm
        except ImportError as exc:
            raise ModelRefused("model_libraries_missing", exc.name or "lightgbm") from exc
        if lightgbm.__version__ != bundle.lightgbm:
            raise ModelRefused("lightgbm_version",
                               f"installed {lightgbm.__version__}, trained {bundle.lightgbm}")
        self.bundle = bundle
        self._boosters = {}
        for head in bundle.heads:
            booster = lightgbm.Booster(model_file=str(head.path))
            if tuple(booster.feature_name()) != bundle.features:
                raise ModelRefused("head_features", head.name)
            if booster.num_trees() != head.trees:
                raise ModelRefused("head_trees", f"{head.name}: {booster.num_trees()}")
            self._boosters[head.name] = booster

    def matrix(self, rows: Any) -> Any:
        import numpy as np

        missing = [n for n in self.bundle.features if n not in rows.columns]
        if missing:
            raise ModelRefused("row_feature_missing", ", ".join(missing))
        return np.ascontiguousarray(rows[list(self.bundle.features)].to_numpy(np.float32))

    def score(self, rows: Any) -> dict[str, Any]:
        """Head name -> one prediction per row, in the rows' order."""
        x = self.matrix(rows)
        return {name: booster.predict(x) for name, booster in self._boosters.items()}


def bundle_folder(cfg: Config) -> Path:
    """The configured bundle's folder: `<data_dir>/<bundles_dir>/<bundle>`."""
    if not cfg.model.bundle:
        raise ModelRefused("bundle_not_configured", "[model] bundle is empty")
    for key, value in (("bundles_dir", cfg.model.bundles_dir), ("bundle", cfg.model.bundle)):
        path = Path(value)
        if path.is_absolute() or ".." in path.parts or (key == "bundle" and len(path.parts) != 1):
            raise ModelRefused("bundle_path_outside", f"[model] {key} must be relative")
    return cfg.root / cfg.run.data_dir / cfg.model.bundles_dir / cfg.model.bundle


def load_configured(cfg: Config) -> Scorer:
    """Open, verify and load the bundle `[model]` names."""
    return Scorer(open_bundle(bundle_folder(cfg)))
