"""The server's tickr account and the release image's user share one uid and gid.

ops/aws/bot creates the account with `service_uid`; .github/workflows/server-image.yml builds
the image's user with TICKR_UID and TICKR_GID. The bind-mounted data folder and the env file are
owned by the account, so a mismatch leaves the container unable to read them.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_service_uid_matches_the_image_build():
    variables = (ROOT / "ops/aws/bot/variables.tf").read_text(encoding="utf-8")
    block = re.search(r'variable "service_uid" \{(.*?)\n\}', variables, re.S)
    assert block, "variable service_uid not found"
    default = re.search(r"default\s*=\s*(\d+)", block.group(1))
    assert default, "service_uid has no numeric default"
    workflow = (ROOT / ".github/workflows/server-image.yml").read_text(encoding="utf-8")
    uid = re.search(r'^\s*TICKR_UID:\s*"(\d+)"', workflow, re.M)
    gid = re.search(r'^\s*TICKR_GID:\s*"(\d+)"', workflow, re.M)
    assert uid and gid, "TICKR_UID or TICKR_GID not set in server-image.yml"
    assert uid.group(1) == default.group(1) == gid.group(1)
