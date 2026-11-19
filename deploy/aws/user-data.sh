#!/bin/bash
# Boot script for the EC2 instance (filled in by deploy.sh). Contains code, no secrets:
# secrets are read from SSM Parameter Store at service start.
set -euxo pipefail
NAME=@NAME@
export AWS_REGION=@REGION@

# t4g.nano has 0.5 GB RAM: dnf gets OOM-killed without swap
if [ ! -f /swapfile ]; then
  dd if=/dev/zero of=/swapfile bs=1M count=1024
  chmod 600 /swapfile
  mkswap /swapfile
  swapon /swapfile
  echo '/swapfile swap swap defaults 0 0' >>/etc/fstab
fi
dnf install -y docker xz
systemctl enable --now docker

mkdir -p /opt/$NAME/src /opt/$NAME/caddy
base64 -d >/opt/$NAME/src.tar.xz <<'BUNDLE'
@BUNDLE@
BUNDLE
tar -xJf /opt/$NAME/src.tar.xz -C /opt/$NAME/src
docker build -t $NAME /opt/$NAME/src

# start script: secrets from SSM -> root-only env file -> containers
cat >/opt/$NAME/start.sh <<EOF
#!/bin/bash
set -euo pipefail
umask 077
: >/opt/$NAME/env
param() { aws ssm get-parameter --region $AWS_REGION --with-decryption --name /$NAME/\$1 --query Parameter.Value --output text; }
for p in FORM_API_KEY SOAR_TOKEN SOAR_URL SOAR_VERIFY_TLS; do
  printf '%s=%s\n' "\$p" "\$(param \$p)" >>/opt/$NAME/env
done
# hostname: fixed name from the parameter, or <public-ip>.sslip.io (IP from IMDSv2 at every start)
HOST=\$(param FORM_HOSTNAME)
if [ "\$HOST" = "auto" ]; then
  TOKEN=\$(curl -sS -X PUT -H "X-aws-ec2-metadata-token-ttl-seconds: 60" http://169.254.169.254/latest/api/token)
  IP=\$(curl -sS -H "X-aws-ec2-metadata-token: \$TOKEN" http://169.254.169.254/latest/meta-data/public-ipv4)
  HOST=\$(echo "\$IP" | tr . -).sslip.io
fi
echo "FORM_BASE_URL=https://\$HOST" >>/opt/$NAME/env
printf '%s {\n    reverse_proxy $NAME:8080\n}\n' "\$HOST" >/opt/$NAME/caddy/Caddyfile
docker network inspect $NAME >/dev/null 2>&1 || docker network create $NAME
docker rm -f $NAME caddy >/dev/null 2>&1 || true
docker run -d --name $NAME --network $NAME --restart unless-stopped --env-file /opt/$NAME/env -v $NAME-state:/data $NAME
docker run -d --name caddy --network $NAME --restart unless-stopped -p 80:80 -p 443:443 \
  -v /opt/$NAME/caddy/Caddyfile:/etc/caddy/Caddyfile:ro -v caddy-data:/data caddy:2.8.4
EOF
chmod 700 /opt/$NAME/start.sh

cat >/etc/systemd/system/$NAME.service <<EOF
[Unit]
Description=soar-form service (load secrets, start containers)
After=docker.service network-online.target
Requires=docker.service
[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/opt/$NAME/start.sh
[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now $NAME.service
