# Running the board on AWS

One EC2 server (t4g.small, us-east-1) runs the appointments board and its loops in Docker, with
Caddy in front for HTTPS and Litestream streaming the SQLite store to S3. Claude runs on Bedrock
under the server's IAM role; settings live in Parameter Store. About $18 a month before Bedrock.

| File | What it is |
|---|---|
| `../Dockerfile` | The board's image: `facility-profiles serve`, the store in `/data` |
| `docker-compose.yml` | The server: `app` (the board), `caddy` (HTTPS), `litestream` (the store's S3 copy) |
| `serve.sh` | Turns the loop settings (SCAN_CUSTOMERS, MAIL_EVERY, ...) into `serve` options |
| `Caddyfile`, `litestream.yml` | HTTPS for the board's name; the store's continuous S3 copy |
| `install.sh` | First boot: settings, store, systemd service (run by the instance's user data) |
| `fetch-env.sh` | Parameter Store `/pickup-booking/*` to `/etc/pickup-booking/app.env`, at every start |
| `restore.sh`, `seed.sh` | The store from its S3 copy (or a seed) when the server has none; replace it with a seed |
| `update.sh` | Move the server to another commit and restart (rolls back too) |
| `pickup-booking.service` | systemd: `docker compose up` at boot |
| `aws/pickup-booking.yml` | CloudFormation: server, IAM role, firewall, fixed IP, DNS name, backup bucket, auto-recovery |
| `aws/settings.env.example`, `aws/put-settings.ps1` | The board's settings, and the script that loads them into Parameter Store |

Commands below are for PowerShell, from the `facility-profiles` folder, signed in with
`aws sso login --profile paybot-admin`.

## 1. Google sign-in

In the Google Cloud console, create an OAuth client of type *Web application* (or reuse the
board's) and add the authorized redirect URI `https://pickups.circle-analytics.com/auth/callback`.
Keep its client ID and secret for step 2.

## 2. The settings

Copy `deploy/aws/settings.env.example` to `deploy/aws/settings.env` (git-ignored) and fill it in:
the Transport Pro login, the Google client, `FP_BOARD_SESSION_SECRET` (any long random string).
Write-back stays `false` for the first days. Then load it:

```bash
powershell -File deploy/aws/put-settings.ps1
```

The secrets are stored encrypted. To change a setting later, edit the file, run the script again
and restart the board (step 6).

## 3. The server

Find the default VPC and its public subnet in us-east-1a (an AZ that offers t4g):

```bash
aws ec2 describe-subnets --profile paybot-admin --region us-east-1 --filters Name=default-for-az,Values=true Name=availability-zone,Values=us-east-1a --query "Subnets[0].[VpcId,SubnetId]" --output text
```

Then create the stack with those two IDs:

```bash
aws cloudformation deploy --profile paybot-admin --region us-east-1 --stack-name pickup-booking --template-file deploy/aws/pickup-booking.yml --capabilities CAPABILITY_NAMED_IAM --parameter-overrides VpcId=<vpc id> SubnetId=<subnet id>
```

It creates the server, its IAM role, the firewall (443 and 80 only, no SSH), a fixed IP, the DNS
name `pickups.circle-analytics.com` and the backup bucket `circle-pickup-booking-<account>`. On
first boot the server installs Docker, clones the repository at `main`, reads the settings and
builds the board (about five minutes). Caddy gets the HTTPS certificate once the DNS name points
at the server.

```bash
aws cloudformation describe-stacks --profile paybot-admin --region us-east-1 --stack-name pickup-booking --query "Stacks[0].Outputs" --output table
```

## 4. Lidl's data (optional, once)

The board starts empty and its Transport Pro check fills it with the coming pickups. To carry the
laptop board's history over instead, upload its store and seed the server with it:

```bash
aws s3 cp <path to the laptop store>.db s3://<BackupBucket>/seed/board.db --profile paybot-admin
```

```bash
aws ssm send-command --profile paybot-admin --region us-east-1 --instance-ids <ServerId> --document-name AWS-RunShellScript --parameters "commands=sudo /opt/pickup-booking/src/deploy/seed.sh --yes"
```

`seed.sh` deletes the server's store and its S3 copy before taking the seed.

## 5. Check it

Open `https://pickups.circle-analytics.com` and sign in. The board's logs:

```bash
aws ssm send-command --profile paybot-admin --region us-east-1 --instance-ids <ServerId> --document-name AWS-RunShellScript --parameters "commands=sudo journalctl -t pickup-booking-app -n 50 --no-pager"
```

```bash
aws ssm get-command-invocation --profile paybot-admin --region us-east-1 --instance-id <ServerId> --command-id <CommandId from the last call> --query StandardOutputContent --output text
```

## 6. Day to day

| To | Run on the server (send-command as above) |
|---|---|
| Move to the latest `main` | `sudo /opt/pickup-booking/src/deploy/update.sh` |
| Go back to an earlier version | `sudo /opt/pickup-booking/src/deploy/update.sh <commit>` |
| Apply changed settings | `sudo systemctl restart pickup-booking` |
| Turn on Transport Pro write-back | set `FP_BOOKING_TPRO_WRITEBACK=true` (step 2), then restart |

A shell on the server, when needed: `aws ssm start-session --target <ServerId>` (needs the
Session Manager plugin for the AWS CLI).

## What happens when something fails

- **The board stops:** Docker restarts it; the `/health` check marks it unhealthy meanwhile.
- **The server stops answering:** a CloudWatch alarm reboots it; a failed host is replaced by
  AWS (same disk, same IP).
- **The server is lost:** run the `deploy` command again after terminating it, or update the stack;
  the new server restores the store from its S3 copy (seconds of work lost at most).
- **The AWS role cannot read the mail or reach Bedrock:** the board shows it in red on Today.

## Removing it

```bash
aws cloudformation delete-stack --profile paybot-admin --region us-east-1 --stack-name pickup-booking
```

The backup bucket is kept (it holds the store); delete it by hand when it is no longer needed.
