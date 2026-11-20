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

## Limits IAM cannot hold

`allowed_instance_types` also holds c7a.2xlarge (8 vCPU, 16 GB) for downloads and small runs
that fit the 8 vCPU spot quota of a new account.

The research user may start only `allowed_instance_types` from this template, but IAM cannot
limit how many machines, their user data (so the 24-hour power-off can be dropped) or spot versus
on-demand. Those rest on:
- the account's EC2 vCPU quotas, set to what one machine needs: in Service Quotas, "All Standard
  (A, C, D, H, I, M, R, T, Z) Spot Instance Requests" = 64 and the on-demand standard quota no
  higher than the bot needs. A new account may start lower than 64: request the spot increase
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
