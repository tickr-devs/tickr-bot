"""server-image builds a new release image whenever anything the image holds changes on main.

2026-11-27: #891 changed only ops/systemd/tickr-api.service.template, which the Dockerfile copies
into the image (deploy-server.sh renders the units inside it), but the workflow's path filter left
ops/systemd out, so no image was built for that commit and the fix could not be deployed.
"""

import re
from fnmatch import fnmatch
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _copy_sources() -> list[str]:
    """Every build-context path a Dockerfile COPY reads (not COPY --from another stage)."""
    sources: list[str] = []
    for line in (ROOT / "Dockerfile").read_text("utf-8").splitlines():
        words = line.split()
        if not words or words[0] != "COPY" or any(w.startswith("--from") for w in words):
            continue
        args = [w for w in words[1:] if not w.startswith("--")]
        sources.extend(args[:-1])
    return sources


def _push_paths() -> list[str]:
    text = (ROOT / ".github" / "workflows" / "server-image.yml").read_text("utf-8")
    block = re.search(r"(?ms)^  push:\n.*?^    paths:\n(.*?)^  \S", text)
    assert block is not None
    return re.findall(r"(?m)^      - (\S+)\s*$", block.group(1))


def test_every_path_the_image_copies_triggers_a_build():
    paths = _push_paths()
    sources = _copy_sources()
    assert {"src", "config", "ops/systemd", "ops/docker/compose.yaml"} <= set(sources)
    for source in sources:
        # A file matches itself; a folder matches through any file inside it.
        assert any(fnmatch(source, p) or fnmatch(f"{source}/x", p) for p in paths), source
