"""Boot script of the training machines (ops/aws/training/user-data.sh).

The job wrapper is cut out of the template, its Terraform values filled in, and run with `aws`,
`shutdown`, `sleep` and `curl` replaced by stubs that record what they were asked, so the crash
loop guard is checked without AWS. The installer lines are checked as text.
"""

import os
import re
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
USER_DATA = ROOT / "ops/aws/training/user-data.sh"
MAX_FAILURES = 3  # the value the test fills into the template's max_failures

AWS_STUB = """#!/bin/bash
S={state}
echo "$*" >> $S/calls
case "$*" in
  "s3 cp s3://b/jobs/j/until -") echo {until} ;;
  "s3api head-object"*) echo "Not Found"; exit 254 ;;
  "s3 cp s3://b/scripts/job.sh "*) [ -e $S/job.sh ] && cp $S/job.sh "$4" || exit 1 ;;
  "s3 cp s3://b/jobs/j/failures -") [ -e $S/failures ] && cat $S/failures || exit 1 ;;
  "s3 cp - s3://b/jobs/j/failures") cat > $S/failures ;;
  "s3 cp - s3://b/jobs/j/done") cat > $S/done ;;
  "autoscaling set-desired-capacity"*) echo stop >> $S/stopped ;;
  *) exit 1 ;;
esac
"""


def _wrapper(work):
    text = USER_DATA.read_text()
    body = text.split("<<'WRAP'\n", 1)[1].split("\nWRAP\n", 1)[0]
    values = {
        "region": "us-east-1",
        "bucket": "b",
        "job": "j",
        "group": "tickr-j",
        "job_script": "scripts/job.sh",
        "max_failures": str(MAX_FAILURES),
    }
    body = re.sub(r"\$\{(\w+)\}", lambda m: values[m.group(1)], body)
    return body.replace("/work", str(work))


def _run(tmp_path, failures=None, job=None):
    state, work, bin_ = tmp_path / "state", tmp_path / "work", tmp_path / "bin"
    for d in (state, work, bin_):
        d.mkdir()
    if failures is not None:
        (state / "failures").write_text(f"{failures}\n")
    if job is not None:
        (state / "job.sh").write_text(job)
    stubs = {
        "aws": AWS_STUB.format(state=state, until=int(time.time()) + 3600),
        "shutdown": f'#!/bin/sh\necho "$*" >> {state}/shutdown\n',
        "sleep": "#!/bin/sh\n",
        "curl": "#!/bin/sh\nexit 1\n",
    }
    for name, code in stubs.items():
        (bin_ / name).write_text(code)
        (bin_ / name).chmod(0o755)
    script = tmp_path / "tickr-job"
    script.write_text(_wrapper(work))
    env = {"PATH": f"{bin_}:{os.environ['PATH']}"}  # named variables only (rule 10)
    r = subprocess.run(["bash", str(script)], env=env, capture_output=True, text=True, timeout=30)

    def read(name):
        f = state / name
        return f.read_text().strip() if f.exists() else None

    return r, read


def test_job_script_that_cannot_be_fetched_counts_as_a_failure(tmp_path):
    r, read = _run(tmp_path)
    assert r.returncode == 1
    assert read("failures") == "1"
    assert read("stopped") is None  # below the limit: the group may try a fresh machine
    assert read("shutdown") == "-h now"
    assert "job script could not be fetched (failure 1 of 3)" in r.stdout


def test_fetch_failures_reach_the_limit_and_stop_the_group(tmp_path):
    r, read = _run(tmp_path, failures=MAX_FAILURES - 1)
    assert read("failures") == str(MAX_FAILURES)
    assert read("stopped") == "stop"
    assert "failure 3 of 3" in r.stdout


def test_job_that_exits_non_zero_still_counts(tmp_path):
    r, read = _run(tmp_path, failures=1, job="exit 7\n")
    assert read("failures") == "2"
    assert read("stopped") is None
    assert "job exited 7 (failure 2 of 3)" in r.stdout


def test_job_that_finishes_is_not_counted(tmp_path):
    r, read = _run(tmp_path, job="exit 0\n")
    assert read("failures") is None
    assert read("done") is not None and read("stopped") == "stop"


def test_installers_are_pinned_and_checked_before_they_run():
    text = USER_DATA.read_text().split("<<'WRAP'")[0]
    lines = [x.strip() for x in text.split("\n") if x.strip() and not x.strip().startswith("#")]
    # no installer is piped into a shell, and nothing is fetched from an unpinned URL
    assert not any(re.search(r"\|\s*(ba)?sh\b", x) for x in lines)
    fetches = [i for i, x in enumerate(lines) if x.startswith("curl ") and " -o /tmp/" in x]
    assert len(fetches) == 2
    for i in fetches:
        target = lines[i].split(" -o ")[1].split()[0]
        assert "${" in lines[i]  # the version or URL comes from the installers variable
        check = lines[i + 1]
        assert check.endswith("| sha256sum -c -") and target in check and "_sha256}" in check


def test_every_boot_script_render_gets_the_pinned_installers():
    variables = (ROOT / "ops/aws/training/variables.tf").read_text()
    block = variables.split('variable "installers"', 1)[1].split("\n}\n", 1)[0]
    default = block.split("default = {", 1)[1].split("\n  }", 1)[0]
    keys = re.findall(r"^\s{4}(\w+)\s+=", default, re.M)
    assert keys == ["aws_cli_version", "aws_cli_sha256", "uv_version", "uv_wheel_url", "uv_sha256"]
    for sha in re.findall(r'_sha256\s+=\s+"([^"]*)"', block):
        assert re.fullmatch(r"[0-9a-f]{64}", sha)
    used = set(re.findall(r"\$\{(\w+)\}", USER_DATA.read_text()))
    assert set(keys) <= used
    renders = 0
    for tf in ("main.tf", "autorestart.tf"):
        for call in (ROOT / "ops/aws/training" / tf).read_text().split("templatefile(")[1:]:
            renders += 1
            args = call.split("}))", 1)[0]
            for k in keys:
                assert f"{k} = var.installers.{k}" in re.sub(r"\s+", " ", args)
    assert renders == 2
