# Deploy the form service to AWS

One EC2 instance (`t4g.nano` + 1 GB swap, Amazon Linux 2023 arm64) with Docker, the service and Caddy (automatic
HTTPS via Let's Encrypt) on its auto-assigned public IP (no Elastic IP; the IP changes on stop/start). Hostname
`<ip-with-dashes>.sslip.io`, derived on the instance at every start, no DNS change needed. Own name: set
`FORM_HOSTNAME` (SSM parameter `/soar-form/FORM_HOSTNAME`) and an A record to the instance IP.
Organisations that require tags on creation (SCP): `EXTRA_TAGS="k=v,k=v"`. Security group opens 80/443 only; no SSH, admin access via SSM Session Manager.
Secrets go from the macOS keychain to SSM Parameter Store (SecureString) and are read on the instance at start;
user data contains only code. Rough cost: ~8 USD/month (instance, 8 GB gp3, public IPv4).

```bash
aws sso login --profile <profile>
SOAR_URL=https://soar.example.com AWS_PROFILE=<profile> deploy/aws/deploy.sh --dry-run     # read-only check + plan
SOAR_URL=https://soar.example.com AWS_PROFILE=<profile> deploy/aws/deploy.sh               # create (idempotent), waits for /healthz
AWS_PROFILE=<profile> deploy/aws/deploy.sh --uninstall   # remove everything tagged Name=soar-form
```

Keychain items (service `soar-form`): `soar-token` (SOAR automation token, required), `form-api-key`
(created by the script if missing; the same value goes into the SOAR asset `api_key`).

New code: `--uninstall` and deploy again (state in the `soar-form-state` volume is lost; open links become invalid).
Logs: `aws ssm start-session --target <instance-id>`, then `sudo docker logs soar-form` / `sudo docker logs caddy`.
