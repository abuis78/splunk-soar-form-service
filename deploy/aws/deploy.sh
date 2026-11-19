#!/usr/bin/env bash
# Deploy the form service to AWS: one small EC2 instance (arm64, Amazon Linux 2023) with Docker, the service and
# Caddy (automatic Let's Encrypt) on its public IP. Hostname: <ip-with-dashes>.sslip.io unless FORM_HOSTNAME is set.
#
#   deploy/aws/deploy.sh --dry-run      # read-only: checks and plan, creates nothing
#   deploy/aws/deploy.sh                # create or update (idempotent, resources tagged Name=$NAME)
#   deploy/aws/deploy.sh --uninstall    # remove everything this script created
#
# Env: AWS_PROFILE, AWS_REGION (default eu-central-1), NAME (default soar-form), INSTANCE_TYPE (default t4g.nano),
#      SOAR_URL (required), SOAR_VERIFY_TLS (default 1), FORM_HOSTNAME (optional),
#      EXTRA_TAGS (optional, "k=v,k=v", e.g. tags required by an SCP).
# Secrets come from the macOS keychain and go to SSM Parameter Store (SecureString), never into user data:
#      soar-form/form-api-key  -> /$NAME/FORM_API_KEY      soar-form/soar-token -> /$NAME/SOAR_TOKEN
set -euo pipefail

MODE=${1:-apply}
case "$MODE" in apply | --dry-run | --uninstall) ;; *) echo "usage: $0 [--dry-run|--uninstall]" >&2; exit 2 ;; esac

export AWS_REGION=${AWS_REGION:-eu-central-1}
export AWS_PAGER=""
NAME=${NAME:-soar-form}
INSTANCE_TYPE=${INSTANCE_TYPE:-t4g.nano}
SOAR_URL=${SOAR_URL:-}
SOAR_VERIFY_TLS=${SOAR_VERIFY_TLS:-1}
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
# EXTRA_TAGS="k1=v1,k2=v2": tags some organisations require on creation (e.g. via SCP)
EXTRA=""
IFS=',' read -ra _pairs <<<"${EXTRA_TAGS:-}"
for kv in "${_pairs[@]}"; do [ -n "$kv" ] && EXTRA="$EXTRA,{Key=${kv%%=*},Value=${kv#*=}}"; done
TAGS="ResourceType=%s,Tags=[{Key=Name,Value=$NAME},{Key=app,Value=soar-form-service}$EXTRA]"

say() { printf '%s\n' "$*"; }
run() { if [ "$MODE" = "--dry-run" ]; then say "  [dry-run] $*"; else "$@"; fi; }
keychain() { security find-generic-password -s soar-form -a "$1" -w; }

instance_id() {
  aws ec2 describe-instances --filters "Name=tag:Name,Values=$NAME" \
    "Name=instance-state-name,Values=pending,running,stopping,stopped" \
    --query 'Reservations[].Instances[].InstanceId' --output text
}
eip_alloc() { aws ec2 describe-addresses --filters "Name=tag:Name,Values=$NAME" --query 'Addresses[0].AllocationId' --output text; }
eip_ip() { aws ec2 describe-addresses --filters "Name=tag:Name,Values=$NAME" --query 'Addresses[0].PublicIp' --output text; }
sg_id() {
  aws ec2 describe-security-groups --filters "Name=group-name,Values=$NAME" --query 'SecurityGroups[0].GroupId' --output text
}

say "Account: $(aws sts get-caller-identity --query Arn --output text)  Region: $AWS_REGION  Name: $NAME"

# ---------------------------------------------------------------- uninstall
if [ "$MODE" = "--uninstall" ]; then
  iid=$(instance_id)
  if [ -n "$iid" ]; then
    say "terminate instance $iid"
    aws ec2 terminate-instances --instance-ids $iid >/dev/null
    aws ec2 wait instance-terminated --instance-ids $iid
  fi
  alloc=$(eip_alloc)
  [ "$alloc" != "None" ] && say "release Elastic IP $alloc" && aws ec2 release-address --allocation-id "$alloc"
  sg=$(sg_id)
  [ "$sg" != "None" ] && say "delete security group $sg" && aws ec2 delete-security-group --group-id "$sg"
  if aws iam get-instance-profile --instance-profile-name "$NAME" >/dev/null 2>&1; then
    say "delete IAM instance profile and role $NAME"
    aws iam remove-role-from-instance-profile --instance-profile-name "$NAME" --role-name "$NAME" 2>/dev/null || true
    aws iam delete-instance-profile --instance-profile-name "$NAME"
  fi
  if aws iam get-role --role-name "$NAME" >/dev/null 2>&1; then
    aws iam detach-role-policy --role-name "$NAME" --policy-arn arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore
    aws iam delete-role-policy --role-name "$NAME" --policy-name read-params
    aws iam delete-role --role-name "$NAME"
  fi
  for p in FORM_API_KEY SOAR_TOKEN FORM_HOSTNAME FORM_BASE_URL SOAR_URL SOAR_VERIFY_TLS; do
    aws ssm delete-parameter --name "/$NAME/$p" 2>/dev/null && say "delete parameter /$NAME/$p" || true
  done
  say "Uninstall done."
  exit 0
fi
[ -n "$SOAR_URL" ] || { echo "set SOAR_URL to your SOAR base URL, e.g. https://soar.example.com" >&2; exit 2; }

# ---------------------------------------------------------------- checks (read-only)
if ! keychain form-api-key >/dev/null 2>&1; then
  say "create keychain item soar-form/form-api-key (random API key for the service)"
  [ "$MODE" = "--dry-run" ] || security add-generic-password -s soar-form -a form-api-key \
    -w "$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
fi
keychain soar-token >/dev/null || { say "missing keychain item soar-form/soar-token"; exit 1; }
VPC=$(aws ec2 describe-vpcs --filters Name=isDefault,Values=true --query 'Vpcs[0].VpcId' --output text)
[ "$VPC" = "None" ] && { say "no default VPC in $AWS_REGION"; exit 1; }
AMI=$(aws ssm get-parameter --name /aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-arm64 \
  --query Parameter.Value --output text)
say "Default VPC: $VPC  AMI: $AMI  Type: $INSTANCE_TYPE"

# ---------------------------------------------------------------- IAM role for SSM access + reading /$NAME/*
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
if ! aws iam get-role --role-name "$NAME" >/dev/null 2>&1; then
  say "create IAM role + instance profile $NAME"
  run aws iam create-role --role-name "$NAME" --assume-role-policy-document \
    '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"ec2.amazonaws.com"},"Action":"sts:AssumeRole"}]}' >/dev/null
  run aws iam attach-role-policy --role-name "$NAME" --policy-arn arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore
  run aws iam put-role-policy --role-name "$NAME" --policy-name read-params --policy-document \
    "{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Action\":\"ssm:GetParameter\",\"Resource\":\"arn:aws:ssm:$AWS_REGION:$ACCOUNT:parameter/$NAME/*\"}]}"
  run aws iam create-instance-profile --instance-profile-name "$NAME" >/dev/null
  run aws iam add-role-to-instance-profile --instance-profile-name "$NAME" --role-name "$NAME"
  [ "$MODE" = "--dry-run" ] || sleep 10 # IAM propagation before run-instances
else
  say "IAM role $NAME exists"
fi

# ---------------------------------------------------------------- security group: 80/443 only, no SSH
SG=$(sg_id)
if [ "$SG" = "None" ]; then
  say "create security group $NAME (80, 443 from anywhere; no SSH)"
  if [ "$MODE" != "--dry-run" ]; then
    SG=$(aws ec2 create-security-group --group-name "$NAME" --description "soar-form: HTTP/HTTPS only" \
      --vpc-id "$VPC" --tag-specifications "$(printf "$TAGS" security-group)" --query GroupId --output text)
    for port in 80 443; do
      aws ec2 authorize-security-group-ingress --group-id "$SG" --protocol tcp --port $port --cidr 0.0.0.0/0 >/dev/null
    done
  fi
else
  say "security group $SG exists"
fi

# No Elastic IP (account limit): the instance uses its auto-assigned public IP and derives the hostname itself
# at every start (start.sh via IMDS). After a stop/start the IP and URL change -> update the SOAR asset base_url.

# ---------------------------------------------------------------- parameters (secrets as SecureString)
put_param() { # name type value  (value via temp JSON file, never on the command line)
  if [ "$MODE" = "--dry-run" ]; then say "  [dry-run] put-parameter /$NAME/$1 ($2)"; return; fi
  local f; f=$(mktemp); chmod 600 "$f"
  python3 -c 'import json,sys; print(json.dumps({"Name":sys.argv[1],"Type":sys.argv[2],"Value":sys.stdin.read().rstrip("\n"),"Overwrite":True}))' \
    "/$NAME/$1" "$2" <<<"$3" >"$f"
  aws ssm put-parameter --cli-input-json "file://$f" >/dev/null
  rm -f "$f"
}
say "write SSM parameters /$NAME/*"
put_param FORM_API_KEY SecureString "$(keychain form-api-key)"
put_param SOAR_TOKEN SecureString "$(keychain soar-token)"
put_param FORM_HOSTNAME String "${FORM_HOSTNAME:-auto}"
put_param SOAR_URL String "$SOAR_URL"
put_param SOAR_VERIFY_TLS String "$SOAR_VERIFY_TLS"

# ---------------------------------------------------------------- user data (code + boot script, no secrets)
USERDATA=$(mktemp)
BUNDLE=$(COPYFILE_DISABLE=1 tar --exclude=__pycache__ -cJf - -C "$ROOT/service" soar_form forms Dockerfile | base64)
sed -e "s|@NAME@|$NAME|g" -e "s|@REGION@|$AWS_REGION|g" "$ROOT/deploy/aws/user-data.sh" \
  | awk -v bundle="$BUNDLE" '{ if ($0 == "@BUNDLE@") print bundle; else print }' | gzip -9 >"$USERDATA"
say "user data: $(wc -c <"$USERDATA" | tr -d ' ') bytes gzip (limit 16384)"
[ "$(wc -c <"$USERDATA")" -lt 16384 ] || { say "user data too large"; exit 1; }

# ---------------------------------------------------------------- instance
IID=$(instance_id)
if [ -n "$IID" ]; then
  say "instance $IID exists. To roll out new code: $0 --uninstall && $0  (or replace the instance)"
else
  say "launch $INSTANCE_TYPE"
  if [ "$MODE" != "--dry-run" ]; then
    IID=$(aws ec2 run-instances --image-id "$AMI" --instance-type "$INSTANCE_TYPE" --security-group-ids "$SG" \
      --iam-instance-profile Name="$NAME" --user-data "fileb://$USERDATA" \
      --metadata-options HttpTokens=required,HttpEndpoint=enabled \
      --block-device-mappings 'DeviceName=/dev/xvda,Ebs={VolumeSize=8,VolumeType=gp3,Encrypted=true}' \
      --tag-specifications "$(printf "$TAGS" instance)" "$(printf "$TAGS" volume)" \
      --query 'Instances[0].InstanceId' --output text)
    aws ec2 wait instance-running --instance-ids "$IID"
    say "instance $IID running"
  fi
fi
if [ "$MODE" != "--dry-run" ]; then
  IP=$(aws ec2 describe-instances --instance-ids "$IID" --query 'Reservations[0].Instances[0].PublicIpAddress' --output text)
  [ "$IP" = "None" ] && { say "instance has no public IP (subnet without auto-assign?)"; exit 1; }
  HOST=${FORM_HOSTNAME:-${IP//./-}.sslip.io}
fi
rm -f "$USERDATA"

[ "$MODE" = "--dry-run" ] && { say "Dry run done, nothing created."; exit 0; }
say "Waiting for https://$HOST/healthz (first boot + certificate take a few minutes) ..."
for _ in $(seq 1 60); do
  if curl -fsS --max-time 5 "https://$HOST/healthz" 2>/dev/null; then say ""; say "OK: https://$HOST"; exit 0; fi
  sleep 10
done
say "Not healthy after 10 minutes. Logs: aws ssm start-session --target $IID, then: sudo docker logs soar-form"
exit 1
