"""GitHub job names say what they check (owner rule 2026-11-16).

A job's id is the name of its check on a pull request and in main's required checks, where it
stands alone, without its workflow's name. So every job id in .github/workflows names its
platform or subject and what it does (`python-tests`, `ios-build-and-test`, `swift-lint`), never
a bare `test`, `build`, `check` or `lint`, and no two workflows share one: a required check named
`build` would be satisfied by any job called `build`.
"""

import re
from pathlib import Path

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"
# A job id: a key directly under the top-level `jobs:` (the workflows are indented by two spaces).
JOB = re.compile(r"^  ([A-Za-z0-9_-]+):\s*$")
NAME = re.compile(r"^    name:")
SEPARATOR = "-"


def jobs_of(text: str) -> tuple[list[str], list[str]]:
    """The job ids of a workflow file, and the jobs that set a display name of their own."""
    ids: list[str] = []
    renamed: list[str] = []
    inside = False
    for line in text.splitlines():
        if line and not line[0].isspace() and not line.startswith("#"):
            inside = line.rstrip() == "jobs:"
        elif inside and (found := JOB.match(line)):
            ids.append(found.group(1))
        elif inside and ids and NAME.match(line):
            renamed.append(ids[-1])
    return ids, renamed


def test_every_job_id_says_what_it_checks_and_is_used_once():
    problems: list[str] = []
    seen: dict[str, str] = {}
    files = sorted(WORKFLOWS.glob("*.yml"))
    assert files
    for path in files:
        ids, renamed = jobs_of(path.read_text("utf-8"))
        assert ids, f"{path.name}: no jobs found"
        for job in ids:
            if job in seen:
                problems.append(f"{path.name}: job `{job}` is also in {seen[job]}")
            seen[job] = path.name
            if SEPARATOR not in job:
                problems.append(
                    f"{path.name}: job `{job}` does not say what it checks (name the platform "
                    "or subject and the action, like `ios-build-and-test`)")
        # The check is shown under `name:` when a job has one, so the id would no longer be it.
        problems += [f"{path.name}: job `{job}` sets `name:`; the id is its name"
                     for job in renamed]
    assert not problems, "\n".join(problems)


def test_the_job_reader_finds_ids_and_display_names():
    text = ("on:\n  push:\njobs:\n  a-b:\n    runs-on: x\n"
            "  c:\n    name: Other\n    steps:\n      - name: s\n")
    assert jobs_of(text) == (["a-b", "c"], ["c"])
