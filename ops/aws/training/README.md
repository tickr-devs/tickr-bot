# Training stack (AWS)

One private bucket and one on-demand training machine for model research, in the owner's AWS
account, apart from the bot (backlog T15; training spec stage 4). The owner applies it from his
Mac; the research thread then uses only the narrow `tickr-research` user.

What it creates:
- `tickr-research-<account id>`: private, encrypted S3 bucket, TLS only. Data derived from the
  owner's personal Massive plan stays here; delete it within 30 days after that plan ends.
- Launch template `tickr-training`: Ubuntu 24.04, `instance_type` (default c7a.16xlarge,
  64 vCPU, 128 GB; m7a.16xlarge gives 256 GB), spot, 500 GB encrypted gp3 deleted with the
  machine, no inbound port, reached only through SSM. It powers itself off (and terminates)
  `max_runtime_hours` after boot.
- IAM user `tickr-research`: may start machines only from that template, stop or terminate
  only machines tagged `tickr:role=training`, run SSM commands and sessions only on them, and
  read and write only the bucket. No Secrets Manager, no bot server, no IAM, no billing.
- Secret `tickr-research/massive-flatfiles`, created empty: the owner's Massive flat-file key, so
  a machine can download market data straight from Massive. Only the machine role may read it
  (and no other secret or parameter). Whoever has a shell on the machine (the research user,
  through SSM) can therefore reach this one key; it is a market-data key, never a broker key.
  About 0.40 USD a month.
- Budget `tickr-account-monthly` (the only budget for the whole account; the bot's stack must not add another) (default 280 USD: the bot's ~80 plus 200 for training) with mail at
  50 % spent and at a forecast over 100 %.

## Apply (owner, on the Mac)

Needs `brew install terraform awscli` and an admin sign-in profile for the account.

```sh
cd ~/Development/Tickr/ops/aws/training
terraform init
terraform apply -var budget_email='you@example.com'
```

Then create the research user's key by hand, so it never sits in Terraform state:

```sh
aws iam create-access-key --user-name tickr-research
```

Put the two values into the project's cloud environment as the secrets
`AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY` (never into chat, a file in the repo or a log).
Delete the key with `aws iam delete-access-key` when the training round ends.

Fill the Massive secret from the env file without printing it (the values go through a pipe, not
the command line):

```sh
( set -a; . ~/.config/tickr/env; set +a
  printf '{"MASSIVE_S3_ACCESS_KEY_ID":"%s","MASSIVE_S3_SECRET_ACCESS_KEY":"%s"}' \
    "$MASSIVE_S3_ACCESS_KEY_ID" "$MASSIVE_S3_SECRET_ACCESS_KEY" ) |
  aws secretsmanager put-secret-value --secret-id tickr-research/massive-flatfiles \
    --secret-string file:///dev/stdin > /dev/null && echo stored
```

On the machine: `aws secretsmanager get-secret-value --secret-id tickr-research/massive-flatfiles`
inside the download script only; never echo or log it.

Every machine start still needs the owner's yes on a named sum.

## Auto-restart groups (no idle time on spot)

`autorestart.tf` adds one Auto Scaling group per job in `var.jobs`:
- `tickr-download`: up to 4 machines of 16 vCPU (.4xlarge), 300 GB gp3 disk at 500 MB/s, runs
  `scripts/tickboot.sh`; the job script shares the days between the machines.
- `tickr-train`: one machine of 64 vCPU and 256 GB (m-type .16xlarge), 500 GB gp3 disk at
  500 MB/s, runs `scripts/l1boot.sh` (layer 1 settings search: several fits side by side).
- `tickr-labels`: one memory-rich machine of 64 vCPU and 512 GB (r-type .16xlarge), 500 GB gp3
  disk at 500 MB/s, runs `scripts/labelboot.sh` (label tables and the tick content check: one
  process per core, about 6 GB each, so every core works). The job script and its runner are
  research files in the bucket (`scripts/l1/job/`), pinned by md5 inside the job script.

Each group keeps at most `machines` spot machines, picks among several instance types in every
zone (`capacity-optimized`, the least interrupted pools) and replaces a machine AWS takes back,
or one that is terminated by hand, within minutes, with no extra setting. A group is created with
desired capacity 0, and an apply never changes it afterwards; nothing runs until someone sets it.

Start a job (research user, only after the owner's yes on a named sum):

```sh
# deadline = how long the approved sum lasts
UNTIL=$(date -u -d '+30 hours' +%s)
echo "$UNTIL" | aws s3 cp - s3://tickr-research-<account>/jobs/download/until
aws s3 rm s3://tickr-research-<account>/jobs/download/done       # only when re-running
aws s3 rm s3://tickr-research-<account>/jobs/download/failures   # only when re-running
# backstop AWS enforces even if no machine boots: desired 0 at the deadline
aws autoscaling put-scheduled-update-group-action --auto-scaling-group-name tickr-download \
  --scheduled-action-name deadline --start-time "$(date -u -d @"$UNTIL" +%Y-%m-%dT%H:%M:%SZ)" \
  --desired-capacity 0
aws autoscaling set-desired-capacity --auto-scaling-group-name tickr-download --desired-capacity 1
```

`tickr-download` is always started with 1, never 4: the first machine compares one day with the
Mac's files and raises the group to 4 itself after a PASS (see "The download job").

Always create the scheduled action before setting 1; never start a group without it.

Check once after the first apply, as the research user: a scheduled action that tries to lift the
size must be refused (`put-scheduled-update-group-action ... --max-size 5` on `tickr-download` or
`--max-size 2` on `tickr-train` returns AccessDenied, and so does `--min-size 1` on either group),
and a test start with a deadline ten minutes ahead must end with the group back at 0.

Stop it early: `set-desired-capacity ... 0` (the machine gets SIGTERM and saves), or write a past
time into `until`.

The boot wrapper (`user-data.sh`) runs the job script and:
- watches the spot interruption notice every 5 s; on it, creates `/work/STOP` and sends SIGTERM
  so the job saves within the 2-minute warning;
- checks `jobs/<job>/until` every minute and stops the job and the group when it has passed;
- on exit 0 writes `jobs/<job>/done` and sets the group to 0, so a finished job never restarts
  (with several machines, exit 0 must mean the whole job is done, not only this machine's share:
  setting 0 stops every machine of the group);
- on SIGTERM gives the job 90 s to save, then SIGKILL;
- on any other exit, and when the job script cannot be fetched from the bucket, counts it in
  `jobs/<job>/failures`, powers off, and the group starts a fresh
  machine; after `job_max_failures` (3) failed runs it sets the group to 0 (the counter is shared
  by all machines of a group);
- installs the AWS CLI and uv only at the versions pinned in the `installers` variable, and
  only when the downloaded file has the pinned sha256;
- if the machine setup itself fails (packages, AWS CLI, a checksum), it tries to set the group to 0 and powers
  off at once instead of idling 24 hours. The scheduled "desired 0" covers every case where the
  machine cannot stop the group itself.

The 24-hour power-off stays: the group simply replaces that machine and the job resumes.

The deadline is a working rule, not a security barrier: the research user and the job machine
may write the bucket, so they could move it. The budget mail is the hard backstop.

The spot quota is shared: download (4 x 16 = 64), train (64) and labels (64) each need the whole
64 vCPU approved, so they run one after the other unless the spot quota is raised.

### The download job (`jobs/`)

`jobs/tickboot.sh` is the job script of `tickr-download` and `jobs/tickrun.py` the runner it
starts; the research thread copies both to the bucket (`scripts/tickboot.sh`,
`scripts/fleet/tickrun.py`, with `us-equity-closed-days.txt` beside it) and writes the md5 of the reviewed runner to
`jobs/download/runner.md5`, so a machine refuses any other file. How 4 machines share the days:

- A day is taken by creating `claims/ticks/<day>.json` with `If-None-Match: *`; S3 lets only
  one machine create it. The holder refreshes it; a claim not refreshed for 20 minutes is taken
  over with `If-Match` on the stale object, again by one machine only.
- Only the claim holder converts and stores a day. A machine that finds its claim taken kills
  that day's converter. Scratch is per machine and per day; stored objects are per day.
- A day is done when the frozen converter's manifest exists and the stored tables have the
  sizes it records (plus the raw-archive record when `jobs/download/raw` says `on`).
- Exit 0 only when every day is done, whoever did it: before it ends, the runner reads every
  day of the list again in S3. A machine whose share is finished waits for the others.
  SIGTERM (spot notice): claims released, exit 3 inside a minute.
- Start the group with desired 1, never 4. The first machine converts one day into
  `fleet-test/` and compares it with the Mac's files of that day; no machine starts the runner
  before `jobs/download/crosscheck` says `PASS`. PASS: that machine raises the group to
  `jobs/download/machines` (default 4). FAIL: `until` := 0 and the group := 0, with nothing
  written outside `fleet-test/`. The comparison has a 45-minute lease (a replacement machine
  takes it over) and waiting for it has a 90-minute limit.
- A day without source files is a holiday only when `jobs/us-equity-closed-days.txt` lists it
  as closed. A session with a file missing, or files on a listed day, is a failed day.
- `jobs/download/pass` is required and says which part of the history the group works on:
  `open` (2021-11-16 .. 2026-11-19, the bucket's root) or `older` (2019-11-01 .. 2021-11-15, the
  two closed years, everything under `older/`, claims `ticks-older`, raw files under `raw/` as in
  the open pass). The older pass filters by its own type list
  (`reference/types-2019-11-01_2021-11-15.csv`, made by `jobs/build_types.py` with the rule of
  `types-5y.csv` and the earlier first day, md5 pinned in the job script): with the 5-year list
  the older days would lack every stock that left the market before the open range. A missing
  or unknown value refuses. Between the passes the research thread removes `done` and `failures`
  (both are one object for the job, not per pass), checks that `until` and the scheduled stop
  still hold the owner's stop time, writes the pass and starts the group with desired 1 again.
  It stays at one machine until the first older day is checked (keys only under `older/`).
  No older day can be compared with a Mac file: the evidence is the same frozen converter and
  runner, and a type list from the rule that reproduces the 5-year list byte for byte.
  Change the pass only after the running pass has written `done` and the group is at 0, never
  while machines work: a machine reads the pass once at its start, so a replacement would work
  on the other pass, and the first exit 0 of either pass stops the whole group.
- One converter per machine unless `jobs/download/conv` says `2`. Then each converter gets half
  of the cores and a memory limit by the machine's size (20GB each from 56 GB of memory, 10GB
  each below). A machine reads the switch once, at its start; writing `1` puts later machines back.
  A machine under 56 GB first makes a 32 GB swap file on its work disk (two large days together
  can need more memory than it has); if that fails it works with one converter.
  Before any day is made that way, a machine converts two of the Mac's days at the same time with
  those settings into `fleet-test2/` and compares both with the Mac's files
  (`jobs/download/crosscheck2-<threads>-<mem>`, one result per setting: a machine with other
  settings compares for itself); a FAIL writes the switch back to `1`.
- The raw archive is off unless `jobs/download/raw` says `on`. The owner's recorded decision is
  not to keep raw files; the research thread writes `on` only after he chooses the archive
  option on the launch card.

`tests/ops/test_tickrun.py` runs these rules in the runner's local test mode.

## Limits IAM cannot hold

A second template, `tickr-training-ondemand`, is the same machine at the regular price (never
taken back mid-run, about 3-4 times the spot price); the research user may start only
`ondemand_instance_types` (default c7a.2xlarge) from it. On-demand needs the account quota
"Running On-Demand Standard (A, C, D, H, I, M, R, T, Z) instances" to cover 8 vCPU beside the bot.

`allowed_instance_types` also holds c7a.2xlarge (8 vCPU, 16 GB) for downloads and small runs
that fit the 8 vCPU spot quota of a new account.

The research user may start only `allowed_instance_types` (spot) and `ondemand_instance_types`
(on-demand) from these templates, but IAM cannot limit how many machines or their user data (so
the 24-hour power-off can be dropped). Those rest on:
- the account's EC2 vCPU quotas, set to what one machine needs: in Service Quotas, "All Standard
  (A, C, D, H, I, M, R, T, Z) Spot Instance Requests" = 64 and the on-demand standard quota no
  higher than the bot plus one small machine (8 vCPU) needs. A new account may start lower than 64: request the spot increase
  right after the account is created, it can take a day;
- deleting the research key when the training round ends;
- the budget mail.

The research user can read the output of any SSM command run in the account
(`ssm:GetCommandInvocation` cannot be limited by resource). While this user exists, no SSM command
on the bot server may print anything secret.

## Check after apply

As the research user, before the first real start:

```sh
aws ec2 run-instances --launch-template LaunchTemplateName=tickr-training --dry-run
```

`DryRunOperation` = allowed. `UnauthorizedOperation` = tell the cloud-move thread.
Commit `.terraform.lock.hcl` after the first `terraform init`.
