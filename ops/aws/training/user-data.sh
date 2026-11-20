#!/bin/bash
# First boot of a training machine. Hard stop: the machine powers off (and so terminates,
# launch template) ${max_runtime_minutes} minutes after boot, whatever is running. A machine of
# an auto-restart group is then replaced and its job resumes from S3.
set -eu
shutdown -h +${max_runtime_minutes}
%{ if job != "" ~}
# Any setup error: try to stop the group (once the CLI exists) and power off now, not in 24 h.
fail_boot() {
  aws autoscaling set-desired-capacity --region ${region} --auto-scaling-group-name ${group} --desired-capacity 0 || true
  shutdown -h now
}
trap fail_boot ERR
%{ endif ~}
apt-get update -q
apt-get install -y -q python3-venv git unzip curl
curl -sSfL https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip -o /tmp/awscli.zip
unzip -q /tmp/awscli.zip -d /tmp
/tmp/aws/install
curl -LsSf https://astral.sh/uv/install.sh | UV_INSTALL_DIR=/usr/local/bin sh
install -d -o ubuntu -g ubuntu /work
echo "bucket=${bucket}" > /work/README
%{ if job != "" ~}

# --- Auto-restart job "${job}" (group ${group}) -----------------------------------------------
# Contract for the job script (s3://${bucket}/${job_script}, owned by research):
# - resume from what it already saved in S3; save progress to S3 as often as it can;
# - on SIGTERM, or when /work/STOP appears, save and exit within 90 seconds;
# - exit 0 only when the whole job is finished.
# Env: BUCKET, JOB, STOP_FILE, AWS_DEFAULT_REGION. jobs/${job}/failures counts failed runs.
# The wrapper runs it only while s3://${bucket}/jobs/${job}/until holds a future Unix time
# (written after the owner's yes on a named sum) and no jobs/${job}/done exists.
cat > /usr/local/bin/tickr-job <<'WRAP'
#!/bin/bash
set -u
export AWS_DEFAULT_REGION=${region} BUCKET=${bucket} JOB=${job} STOP_FILE=/work/STOP
M=s3://${bucket}/jobs/${job}
stop_group() {
  aws autoscaling set-desired-capacity --auto-scaling-group-name ${group} --desired-capacity 0 || true
  shutdown -h now
}
# Reads retry a few times, so one S3 hiccup does not end the job. A done marker that cannot be
# checked counts as unknown (keep going); a deadline that cannot be read counts as none (stop).
s3_read() {
  for _ in 1 2 3 4 5; do aws s3 cp "$1" - 2>/dev/null && return 0; sleep 3; done
  return 1
}
is_done() {
  for _ in 1 2 3; do
    out=$(aws s3api head-object --bucket ${bucket} --key "jobs/${job}/done" 2>&1) && return 0
    case "$out" in *"Not Found"*|*404*) return 1 ;; esac
    sleep 3
  done
  return 1
}
allowed() {
  u=$(s3_read "$M/until" | tr -dc 0-9)
  [ -n "$u" ] && [ "$(date +%s)" -lt "$u" ] && ! is_done
}
# SIGTERM to the whole job, 90 s to save (the contract), then SIGKILL.
stop_job() {
  touch /work/STOP
  kill -TERM -- "-$pid" 2>/dev/null
  for _ in $(seq 18); do kill -0 "$pid" 2>/dev/null || return 0; sleep 5; done
  kill -KILL -- "-$pid" 2>/dev/null
}
spot_notice() {
  t=$(curl -s -X PUT http://169.254.169.254/latest/api/token -H 'X-aws-ec2-metadata-token-ttl-seconds: 60')
  curl -sf -o /dev/null -H "X-aws-ec2-metadata-token: $t" http://169.254.169.254/latest/meta-data/spot/instance-action
}
allowed || { echo "not allowed: no future deadline or job done"; stop_group; exit 0; }
aws s3 cp "s3://${bucket}/${job_script}" /work/job.sh || { sleep 60; shutdown -h now; exit 1; }
cd /work
rm -f /work/STOP
setsid bash /work/job.sh &
pid=$!
n=0
while kill -0 "$pid" 2>/dev/null; do
  sleep 5
  n=$((n + 1))
  if spot_notice; then
    echo "spot interruption notice: stopping the job to save"
    stop_job; exit 0
  fi
  if [ $((n % 12)) -eq 0 ] && ! allowed; then
    echo "deadline passed or done marker set: stopping"
    stop_job; stop_group; exit 0
  fi
done
wait "$pid"; rc=$?
if [ "$rc" -eq 0 ]; then
  date -u +%s | aws s3 cp - "$M/done"
  echo "job finished"; stop_group
elif [ -e /work/STOP ]; then
  echo "job stopped for a spot notice"
else
  # Crash loop guard: after max_failures failed runs the group stops until research looks.
  f=$(( $(s3_read "$M/failures" | tr -dc 0-9 || true) + 1 ))
  echo "$f" | aws s3 cp - "$M/failures" || true
  echo "job exited $rc (failure $f of ${max_failures})"
  if [ "$f" -ge ${max_failures} ]; then stop_group; fi
  sleep 60; shutdown -h now
fi
WRAP
chmod 755 /usr/local/bin/tickr-job
nohup /usr/local/bin/tickr-job >> /var/log/tickr-job.log 2>&1 &
%{ endif ~}
