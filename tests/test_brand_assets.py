"""The committed brand files (app icons, iOS launch screen) are exactly what the script writes."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "apps" / "scripts" / "make_brand_assets.py"


def load():
    spec = importlib.util.spec_from_file_location("make_brand_assets", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_committed_files_match_the_script():
    stale = [str(path.relative_to(ROOT)) for path, content in load().outputs().items()
             if not path.exists() or path.read_text(encoding="utf-8") != content]
    assert not stale, f"run apps/scripts/make_brand_assets.py and commit: {stale}"


def test_icon_has_no_files_the_script_does_not_write():
    module = load()
    written = set(module.outputs())
    for icon in module.ICON_DIRS:
        extra = [p for p in icon.rglob("*") if p.is_file() and p not in written]
        assert not extra


def test_icon_layers_exist_and_dark_appearance_is_set():
    module = load()
    for icon in module.ICON_DIRS:
        spec = json.loads((icon / "icon.json").read_text())
        for group in spec["groups"]:
            for layer in group["layers"]:
                assert (icon / "Assets" / layer["image-name"]).is_file()
        assert [s["appearance"] for s in spec["fill-specializations"]] == ["dark"]


def test_launch_background_is_the_window_background():
    module = load()
    light, dark = module.palette()["background"]
    path = module.IOS_ASSETS / "LaunchBackground.colorset" / "Contents.json"
    colorset = json.loads(path.read_text())
    reds = [c["color"]["components"]["red"] for c in colorset["colors"]]
    assert reds == [f"0x{light[:2]}", f"0x{dark[:2]}"]


def test_opening_animation_starts_from_the_launch_mark_size():
    """LaunchHandoff sizes the spark and the flight by Tokens.Size.launchMark: the image size."""
    module = load()
    tokens = (ROOT / "apps/shared/Sources/TickrDesign/Tokens.swift").read_text()
    assert f"public static let launchMark: CGFloat = {module.LAUNCH_MARK}\n" in tokens
