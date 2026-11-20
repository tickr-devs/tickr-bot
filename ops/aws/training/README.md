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

`autorestart.tf` adds one Auto Scaling group per job in `var.jobs`: `tickr-download` (8 vCPU
types, 150 GB disk, runs `scripts/tickboot.sh`) and `tickr-train` (64 vCPU types, 500 GB disk,
runs `scripts/l1boot.sh`). Each keeps at most one spot machine, picks among several instance
types in every zone (`capacity-optimized`, the least interrupted pools) and replaces a machine
AWS takes back within minutes. A group is created with desired capacity 0, and an apply never
changes it afterwards; nothing runs until someone sets it to 1.

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

Always create the scheduled action before setting 1; never start a group without it.

Check once after the first apply, as the research user: a scheduled action that tries to lift the
size must be refused (`put-scheduled-update-group-action ... --max-size 2` returns AccessDenied),
and a test start with a deadline ten minutes ahead must end with the group back at 0.

Stop it early: `set-desired-capacity ... 0` (the machine gets SIGTERM and saves), or write a past
time into `until`.

The boot wrapper (`user-data.sh`) runs the job script and:
- watches the spot interruption notice every 5 s; on it, creates `/work/STOP` and sends SIGTERM
  so the job saves within the 2-minute warning;
- checks `jobs/<job>/until` every minute and stops the job and the group when it has passed;
- on exit 0 writes `jobs/<job>/done` and sets the group to 0, so a finished job never restarts;
- on SIGTERM gives the job 90 s to save, then SIGKILL;
- on any other exit counts it in `jobs/<job>/failures`, powers off, and the group starts a fresh
  machine; after `job_max_failures` (3) failed runs it sets the group to 0;
- if the machine setup itself fails (packages, AWS CLI), it tries to set the group to 0 and powers
  off at once instead of idling 24 hours. The scheduled "desired 0" covers every case where the
  machine cannot stop the group itself.

The 24-hour power-off stays: the group simply replaces that machine and the job resumes.

The deadline is a working rule, not a security barrier: the research user and the job machine
may write the bucket, so they could move it. The budget mail is the hard backstop.

The spot quota is shared: download (8) and train (64) together need 72 vCPU, more than the 64
approved, so they run one after the other unless the spot quota is raised.

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
