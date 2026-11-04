# Deployment runbook: one EC2 server

This puts the public job-search demo online at `https://YOURDOMAIN.dev` on one EC2
instance (Amazon Linux 2023, 20 GiB disk, region us-east-2):

- **Docker Compose** runs PostgreSQL (`db`), the website (`web`, gunicorn), Caddy
  (`caddy`, HTTPS with a Let's Encrypt certificate), and Airflow (`airflow-scheduler`,
  `airflow-dag-processor`, `airflow-api`), which runs the pipeline on its schedule.
- **Only ports 80 and 443 are open to the public.** PostgreSQL and the website are
  reachable only from the other containers. Administration uses AWS Systems Manager
  Session Manager, a shell in the browser. Your existing SSH access stays open during setup
  as a fallback, and is closed (step F8) only after Session Manager has worked, including
  after a reboot.
- **S3** keeps raw responses and backups (encrypted, deleted after 30 days by lifecycle
  rules). The server reaches S3 through an IAM role, so no AWS keys are stored anywhere.
- **Scheduling needs a bigger instance.** Airflow and the rest of the stack do not fit in
  a t3.micro's 1 GiB (see `SIZING.md`); a 2 GiB t3.small is the candidate, and changing
  size is your budget decision. Sections A to F work on the current t3.micro with jobs run
  by hand; section G (Airflow) waits for that decision.

Every step says **where** to run it, **what** it does, the **values** you supply, and how
to **check** it worked. Do the sections in order. Placeholders look like `YOUR-BUCKET-NAME`;
replace them, including the capital letters.

Never paste a password, key, or the contents of `.env` into a chat, an issue, or a commit.

---

## A. Before you start

**A1. Collect what you need.**
- An AWS console login for the account that owns the instance.
- The instance ID (EC2 > Instances, looks like `i-0abc...`) and its region, us-east-2.
- A domain registered at Cloudflare (step C1), for example `yourproduct.dev`.
- On your Mac: this repository with the latest `main`, and PostgreSQL 17 at
  `/Library/PostgreSQL/17/bin` (only to copy your existing data, step F3).

**A2. Decide the names.** Write them down; later steps use them.
- Bucket name, globally unique, lowercase, for example `jobmarket-yourname-data`.
- The domain, for example `yourproduct.dev`. The site lives at the domain itself.

---

## B. AWS console (in your browser)

**B1. Create the S3 bucket.**
- Where: AWS console > S3 > Create bucket. Region: US East (Ohio) us-east-2.
- What: a private bucket for raw responses and backups.
- Values: Bucket name from A2. Keep **Block all public access** turned on. Keep
  **Default encryption** at "Server-side encryption with Amazon S3 managed keys (SSE-S3)".
  Leave everything else as it is.
- Check: the bucket appears in the list, and its Permissions tab says "Block all public
  access: On".

**B2. Refuse unencrypted connections to the bucket.**
- Where: S3 > your bucket > Permissions > Bucket policy > Edit.
- What: denies any request that does not use HTTPS.
- Values: paste `deploy/aws/bucket-policy.json` and replace both `YOUR-BUCKET-NAME`.
- Check: "Save changes" succeeds and the policy is shown.

**B3. Delete old objects automatically (30 days).**
- Where: S3 > your bucket > Management > Create lifecycle rule. Do this twice.
- What: removes raw responses and backups 30 days after upload (your retention decision).
- Values:
  - Rule 1: name `expire-raw-responses-after-30-days`; scope "Limit the scope using
    filters"; prefix `raw/`; action "Expire current versions of objects"; 30 days.
  - Rule 2: name `expire-backups-after-30-days`; prefix `backups/`; same action; 30 days.
- Check: Management shows two enabled rules. (`deploy/aws/lifecycle.json` is the same
  configuration as JSON.)

**B4. Create the role the server uses.**
- Where: IAM > Roles > Create role.
- What: lets the instance use Session Manager and read and write only its own S3 prefixes.
- Values:
  1. Trusted entity: AWS service, use case **EC2**. (This is `deploy/aws/trust-policy-ec2.json`.)
  2. Permissions: tick **AmazonSSMManagedInstanceCore**. Next.
  3. Role name: `jobmarket-ec2`. Create role.
  4. Open the role > Add permissions > Create inline policy > JSON. Paste
     `deploy/aws/instance-role-s3-policy.json`, replace the four `YOUR-BUCKET-NAME`, and
     name it `jobmarket-s3`.
- Check: the role's Permissions tab lists `AmazonSSMManagedInstanceCore` and `jobmarket-s3`.
  The role has no `s3:DeleteObject`: the server cannot delete backups.

**B5. Attach the role to the instance.**
- Where: EC2 > Instances > select yours > Actions > Security > Modify IAM role.
- Values: `jobmarket-ec2`. Update IAM role.
- Check: the instance's Details tab shows IAM Role `jobmarket-ec2`.

**B6. Open ports 80 and 443; keep SSH for now.**
- Where: EC2 > Instances > yours > Security tab > click the security group > Edit inbound
  rules.
- What: lets browsers reach Caddy, and keeps your current way into the server until
  Session Manager is proven.
- Values:
  - Add HTTP, TCP 80, source `0.0.0.0/0`; and again with source `::/0`.
  - Add HTTPS, TCP 443, source `0.0.0.0/0`; and again with source `::/0`.
  - Optional for HTTP/3: Custom UDP 443, sources `0.0.0.0/0` and `::/0`.
  - **Keep the existing SSH rule (TCP 22). Do not delete it yet**; step F8 removes it after
    Session Manager has worked. If its source is "Anywhere" (`0.0.0.0/0`), you may narrow
    it to "My IP"; if your home address changes later, edit the rule to your new IP.
  - Delete any other rule, such as one for 5432.
- Check: rules for 80 and 443, your SSH rule, and nothing for 5432. Session Manager does
  not need any inbound rule.

**B7. Give the instance a fixed public address.**
- Where: EC2 > Elastic IPs > Allocate Elastic IP address > Allocate; then select it >
  Actions > Associate Elastic IP address > your instance.
- What: keeps the same IPv4 address across stops and starts, so the domain's DNS record
  stays correct.
- Cost: AWS charges for every public IPv4 address (about $0.005 per hour, about $3.60 a
  month), Elastic or not; your credits cover it. A stopped instance with an Elastic IP is
  still charged for the address.
- Check: the instance's "Public IPv4 address" equals the Elastic IP. Write it down.

**B8. Optional: a budget alert.** Billing > Budgets > Create budget > a monthly cost budget
with an email alert, so you hear about charges early.

---

## C. Domain and DNS at Cloudflare (in your browser)

**C1. Register the domain.**
- Where: https://dash.cloudflare.com > Domain Registration > Register Domains.
- What: buys the domain; Cloudflare then also answers its DNS (the lookups that turn the
  name into the server's address).
- Values: the domain from A2. Turn on auto-renew so it does not lapse.
- Check: the domain appears under Websites with the status "Active".

**C2. Point the domain at the server.**
- Where: Cloudflare > your domain > DNS > Records > Add record.
- Values: Type `A`, Name `@` (the domain itself), IPv4 address = the Elastic IP from B7,
  Proxy status **DNS only** (grey cloud, not orange). Save.
- Why DNS only: Caddy on the server gets the HTTPS certificate from Let's Encrypt itself,
  which needs visitors and Let's Encrypt to reach the server directly on ports 80 and 443.
  With the orange cloud, Cloudflare would sit in between and its default "Flexible" mode
  talks plain HTTP to the server, which loops with Caddy's redirect to HTTPS.
- Do not add a `www` record: the certificate covers the domain only, so `www.` would show
  a certificate warning.
- Check, on your Mac (Terminal): `dig +short YOURDOMAIN.dev` prints the Elastic IP. It can
  take a few minutes.

`.dev` domains only open over HTTPS in browsers (the whole `.dev` ending is on the browsers'
HTTPS-only list). Caddy provides HTTPS in F4, so the site shows nothing in a browser until
then; that is expected.

---

## D. Open a shell on the server

**D1. Start a Session Manager session.**
- Where: EC2 > Instances > yours > Connect > Session Manager tab > Connect.
- What: a shell in your browser, without SSH.
- Check: a terminal opens with a prompt like `sh-5.2$`. If the Connect button is greyed
  out, wait 10 minutes after B5, or reboot the instance (Instance state > Reboot), then try
  again.
- If Session Manager still does not work, continue with SSH as you do today (you log in as
  `ec2-user` directly, so skip D2) and see Troubleshooting. Do not do F8 until D1 works.

**D2. Work as `ec2-user`.**
- Where: the Session Manager shell. Run: `sudo su - ec2-user`
- Check: `whoami` prints `ec2-user`. Start every later session with this command, then
  `cd ~/Job-Market-Intelligence/deploy` (after E4).
- Paste `sudo su - ec2-user` on its own and press Enter before the next command: a line
  pasted together with it is lost while the user switches.
- The browser shell can leave `nano` as a blank screen. If that happens, press Ctrl+X (then
  N if asked to save) and make the change with the commands given in that step instead.

---

## E. Prepare the server (Session Manager or SSH shell, as ec2-user)

**E1. Add 2 GiB of swap.**
- What: disk space the kernel can use when memory runs out, so a short spike slows the
  server down instead of stopping a process (see `deploy/SIZING.md`).

```bash
sudo dd if=/dev/zero of=/swapfile bs=1M count=2048
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
echo '/swapfile swap swap defaults 0 0' | sudo tee -a /etc/fstab
```

- Check: `free -h` shows `Swap: 2.0Gi`. Run `grep swapfile /etc/fstab` once; it must
  print exactly one line (so it comes back after a reboot).

**E2. Install updates, Docker, and Git; start Docker at boot.**

```bash
sudo dnf -y upgrade
sudo dnf -y install docker git
sudo systemctl enable --now docker
sudo usermod -aG docker ec2-user
exit
```

- Then run `sudo su - ec2-user` again (the group change needs a new login).
- Check: `docker info --format '{{.ServerVersion}}'` prints a version without `sudo`, and
  `systemctl is-enabled docker` prints `enabled`.

**E3. Install the Docker Compose and Buildx plugins.**
- What: Amazon Linux does not package them. These are the official binaries, checked
  against the checksums published with each release.

```bash
cd /tmp
curl -fsSLO https://github.com/docker/compose/releases/download/v5.6.0/docker-compose-linux-x86_64
curl -fsSLO https://github.com/docker/compose/releases/download/v5.6.0/docker-compose-linux-x86_64.sha256
sha256sum -c docker-compose-linux-x86_64.sha256
curl -fsSLO https://github.com/docker/buildx/releases/download/v0.38.0/buildx-v0.38.0.linux-amd64
curl -fsSL https://github.com/docker/buildx/releases/download/v0.38.0/checksums.txt | grep 'linux-amd64$' | sha256sum -c
sudo mkdir -p /usr/local/lib/docker/cli-plugins
sudo install -m 755 docker-compose-linux-x86_64 /usr/local/lib/docker/cli-plugins/docker-compose
sudo install -m 755 buildx-v0.38.0.linux-amd64 /usr/local/lib/docker/cli-plugins/docker-buildx
cd ~
```

- Check: both `sha256sum` lines print `OK`. `docker compose version` prints v5.6.0 and
  `docker buildx version` prints v0.38.0. If a checksum is not OK, stop and download again.

**E4. Get the code.**

```bash
git clone https://github.com/Niyog59/Job-Market-Intelligence.git ~/Job-Market-Intelligence
cd ~/Job-Market-Intelligence/deploy
```

- Check: `git log --oneline -1` shows the latest commit on `main`.

**E5. Create the settings file with new passwords.**
- What: `deploy/.env` holds this server's passwords and names. It is ignored by Git.

```bash
cp env.example .env
chmod 600 .env
for i in 1 2 3 4 5 6; do openssl rand -hex 24; done
python3 -c "import base64,os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())"
nano .env
```

- Values: in `nano`:
  - Paste one of the six printed hex values into each of `POSTGRES_PASSWORD`,
    `DB_PASSWORD`, `ANALYTICS_DB_PASSWORD`, `PRIVATE_DB_PASSWORD`, `AIRFLOW_DB_PASSWORD`,
    and `AIRFLOW_JWT_SECRET` (a different one each).
  - Paste the last line (ends with `=`) into `AIRFLOW_FERNET_KEY`.
  - Set `SITE_DOMAIN` to `YOURDOMAIN.dev` and `JOBMARKET_S3_BUCKET` to your bucket.
  - Email lines: fill them in step E6, or leave them for now.
  - Save with Ctrl+O, Enter; exit with Ctrl+X. These are new secrets for the server: do
    not reuse the ones from your Mac.
- Without `nano`: `sed -i 's/^SITE_DOMAIN=.*/SITE_DOMAIN=YOURDOMAIN.dev/' .env` sets a
  value, and the same pattern works for every line here.
- Check: `grep '^SITE_DOMAIN' .env` shows your domain (Caddy requests the certificate for
  exactly this name in F4), `grep -c CHANGE_ME .env` prints `0` (after E6), and
  `docker compose config --quiet` prints nothing. Keep a copy of the secrets in your
  password manager.

**E6. Let the server send email (Gmail app password).**
- Where: your browser, signed in to the Gmail account that will send the messages.
- What: an app password is a separate 16-character password that only lets an app send
  mail as you; it can be revoked any time without changing your Google password. EC2
  allows outbound port 587, which Gmail uses.
- Values:
  1. Google Account > Security > 2-Step Verification: turn it on if it is off.
  2. Google Account > Security > App passwords: create one named `Job Pipeline server`,
     and copy it (shown once).
  3. In `deploy/.env` on the server (`nano .env`): `JOBMARKET_SMTP_USER` and
     `JOBMARKET_ALERT_FROM` = that Gmail address; `JOBMARKET_SMTP_PASSWORD` = the app
     password without spaces; `JOBMARKET_ALERT_TO` = `upretyyniyog@gmail.com`.
- Check (after F2): `docker compose run --rm jobs jobmarket notifications --send-test`
  prints `Sent 1`, and the "[Job Pipeline] Test email" arrives. If it says failed,
  `jobmarket notifications --full` shows the error.

---

## F. First start

All commands in `~/Job-Market-Intelligence/deploy`.

**F1. Build the application image.**
- What: builds the image for the commit you are on and tags it with that commit, so a
  later rollback can return to it. Takes several minutes on a t3.micro.

```bash
sed -i "s/^APP_VERSION=.*/APP_VERSION=$(git rev-parse --short HEAD)/" .env
docker compose build web
```

- Check: `docker images jobmarket-app` lists a tag equal to `git rev-parse --short HEAD`.

**F2. Start the database.**
- What: creates the database volume and, on this first start only, the three logins and
  the empty `Job_Market` database (`postgres-init/10-jobmarket.sh`).

```bash
docker compose up -d db
docker compose ps
```

- Check: after about 30 seconds `docker compose ps` shows `db` as `healthy`.
  `docker compose logs db | grep -i error` prints nothing important.

**F3. Load your data.** Choose one of the two.

*Option 1 (recommended): copy your existing data from your Mac.*

1. **Mac, Terminal**, in your project folder:
   `JOBMARKET_PG_BIN=/Library/PostgreSQL/17/bin .venv/bin/jobmarket backup create`.
   It prints the backup folder, for example `data/backups/20261102T150000Z`.
   Check: it lists `ingestion.dump` and `private.dump` with their sizes.
2. **Browser, S3 console**, your bucket: create the folders `backups/ingestion/STAMP/` and
   `backups/private/STAMP/` (STAMP is the folder name from step 1, like
   `20261102T150000Z`). Upload `ingestion.dump` and `manifest.json` into the first, and
   `private.dump` and `manifest.json` into the second.
   The private file holds your account's password hash and your profile: it goes only
   into this private bucket.
3. **Server**: `docker compose run --rm jobs jobmarket backup list`.
   Check: it lists your STAMP with `ingestion, private`. This also proves the IAM role
   works. If it says "AccessDenied", recheck B4 and B5.
4. **Server**: download, verify, and restore into the empty database:

```bash
docker compose run --rm jobs jobmarket backup fetch STAMP
docker compose run --rm jobs jobmarket backup restore data/backups/STAMP --database Job_Market
```

   Check: the restore prints the number of postings and `1 owner account(s)`, matching
   your Mac. Restore refuses a database that already has data, so it cannot overwrite
   anything.
5. **Server**: bring the schema up to date and set the logins' access:

```bash
docker compose run --rm jobs jobmarket migrate
docker compose run --rm jobs jobmarket analytics grant
docker compose run --rm jobs jobmarket private grant
docker compose run --rm jobs jobmarket private check-access
docker compose run --rm jobs jobmarket analytics publish
```

   Check: `check-access` shows the analytics login cannot use the private schema and the
   private login cannot read or write job data; `analytics publish` prints "Published
   reports built ...".
   Your owner account came with the backup: sign in later with your usual password.

*Option 2: start empty.* Run `migrate`, `sources sync`, `analytics grant`, `private grant`,
`private check-access`, then `owner create --username YOURNAME` (it asks for a new
password at a hidden prompt; type it yourself), all as
`docker compose run --rm jobs jobmarket ...`. The site will have no jobs until a collection
runs (section G), then `analytics publish`.

**F4. Start the website and HTTPS.**

```bash
docker compose up -d web caddy
docker compose ps
docker compose logs caddy | grep -i "certificate obtained"
```

- What: starts gunicorn and Caddy. Caddy asks Let's Encrypt for a certificate, which
  checks over port 80 that the name points to this server.
- Check: `web` is `healthy`; the caddy log shows "certificate obtained successfully". If
  not, see Troubleshooting.

**F5. Check the site from your Mac.**
- Browser: `https://YOURDOMAIN.dev` shows Job search with a padlock and the "Last
  complete collection" time; `/reports` shows the reports with their build time.
- Browser, signed out: `https://YOURDOMAIN.dev/profile` sends you to Sign in. Sign in
  with your owner account; Profile, Saved jobs, and Applications work; Sign out.
- Mac Terminal:
  - `curl -sI http://YOURDOMAIN.dev | head -1` shows `308` (HTTP goes to HTTPS).
  - `nc -vz -w 5 YOURDOMAIN.dev 5432` fails or times out (the database is closed).

**F6. Check that everything comes back after a restart.**
- Where: EC2 > Instances > Instance state > Reboot. Wait 2 minutes, reconnect (D1, D2),
  then `cd ~/Job-Market-Intelligence/deploy && docker compose ps`.
- Check: `db`, `web`, and `caddy` are running again (Docker starts at boot and the
  containers have `restart: unless-stopped`), and the site loads.

**F7. Take the first server backup.**

```bash
docker compose run --rm jobs jobmarket backup create --upload
```

- Check: it prints "Uploaded 4 files to S3". `backup list` shows the new STAMP.

**F8. Close SSH, only after Session Manager is proven.**
- Do this only when Session Manager has worked twice: in D1, and again after the F6 reboot
  (open a new session, run `sudo su - ec2-user`, then `docker compose ps` in
  `~/Job-Market-Intelligence/deploy`). Until then, leave SSH open.
- Where: EC2 > Instances > yours > Security tab > the security group > Edit inbound rules.
- What: delete the SSH rule (TCP 22). Keep the 80 and 443 rules. Keep your key pair file:
  if SSH is ever needed again, adding the rule back restores access.
- Check: from your Mac, `nc -vz -w 5 YOURDOMAIN.dev 22` fails or times out; then open
  one more Session Manager session to confirm you can still get in.

---

## G. Scheduled pipeline (Airflow)

**What runs when** (America/Chicago time, daylight saving included):

| When | What |
|---|---|
| 12:00 and 21:00 | Collect from every enabled board, copy raw files to S3 and remove local ones S3 confirmed, then publish reports |
| 21:00 run only, right after collection | Back up both schemas to S3 (confirmed), keep the newest 3 verified local backups, send the daily summary email, clean Airflow history older than 30 days |
| Every 15 minutes | Retry the boards whose retry is due (up to 3 per failed board, about 1, 2, and 3 hours after the failure), then send any alert |
| Every hour at :30 | One AI batch of at most 20 listings (free tier; pauses itself on a quota error) |

Only one task runs at a time. If the server was off at a scheduled time, Airflow makes one
catch-up run for the latest missed time when it starts, not one per missed time.
Backups: S3 keeps them 30 days (B3); restoring is F3 option 1 steps 3 to 5, or section I.

**G0. Before Airflow: the instance size (your decision).** Airflow plus the rest needs more
than 1 GiB (`SIZING.md`). Do not start Airflow on the t3.micro; keep using G5. When you
decide to change size (a 2 GiB t3.small is the candidate; about $20.58 a month before
credits):
- Where: EC2 > Instances > yours > Instance state > Stop; then Actions > Instance settings
  > Change instance type > `t3.small` > Apply; then Instance state > Start.
- What: the disk and the Elastic IP stay; Docker starts the containers again at boot.
- Check: after 2 minutes, reconnect (D1, D2), `free -h` shows about 1.9Gi total, and
  `docker compose ps` shows `db`, `web`, and `caddy` running.

**G1. Start Airflow.**

```bash
docker compose run --rm airflow-init
docker compose up -d airflow-api airflow-dag-processor airflow-scheduler
docker compose ps
```

- What: creates Airflow's tables in its own `airflow` database, then starts it. The three
  schedules are active at once: Airflow makes one catch-up collection for the most recent
  12:00 or 21:00 slot, one retry check, and one AI batch at the next :30.
- Check: `airflow-api` shows `healthy` within 2 minutes, and
  `curl -s http://127.0.0.1:8080/api/v2/monitor/health` (on the server) shows the
  scheduler as healthy. If your database volume was created before this version (no
  `airflow` database yet), create it first:
  `docker compose exec db psql -U postgres -c "CREATE ROLE airflow LOGIN PASSWORD '<AIRFLOW_DB_PASSWORD from .env>'"`
  and `docker compose exec db psql -U postgres -c 'CREATE DATABASE airflow OWNER airflow'`.

**G2. Open the Airflow UI privately.** It listens only on the server's own address, so you
reach it through a tunnel from your Mac:
- While SSH is still open (before F8), with the key pair file from the instance's creation
  (once: `chmod 400` it, since SSH refuses a key other users can read):
  `ssh -i ~/.ssh/YOUR-KEY.pem -o ServerAliveInterval=30 -L 8080:127.0.0.1:8080 ec2-user@ELASTIC_IP`,
  then open http://localhost:8080 on your Mac. The tunnel lasts only while that command
  runs; after the Mac sleeps, run it again.
- With Session Manager (needs the AWS CLI and its Session Manager plugin on your Mac):
  `aws ssm start-session --target i-YOURINSTANCEID --region us-east-2 --document-name AWS-StartPortForwardingSession --parameters '{"portNumber":["8080"],"localPortNumber":["8080"]}'`,
  then open http://localhost:8080.
- Sign in as `admin` with the generated password, printed on its own by
  `docker compose exec airflow-api python -c "import json; print(json.load(open('/app/airflow/simple_auth_manager_passwords.json.generated'))['admin'])"`.
- Check: three DAGs (`jobmarket_collection`, `jobmarket_retries`, `jobmarket_extraction`),
  no import errors, and the catch-up runs green or skipped (skipped is normal: a noon run
  skips the evening-only steps, and AI skips until Gemini is configured).

**G3. Check the first evening run (after 21:00 Chicago time).**
- The summary email "[Job Pipeline] Daily summary ..." arrives (E6 must be done).
- `docker compose run --rm jobs jobmarket backup list` shows today's backup with
  `ingestion, private`.
- Airflow UI: the 21:00 run's tasks are green; `daily_summary` and `backup` ran.
- Memory: `free -h` and `docker stats --no-stream`; write the numbers into `SIZING.md`.

**G4. Pause, resume, or run now.**

```bash
docker compose exec airflow-scheduler airflow dags pause jobmarket_collection
docker compose exec airflow-scheduler airflow dags unpause jobmarket_collection
docker compose exec airflow-scheduler airflow dags trigger jobmarket_collection
```

A run triggered after 17:00 Chicago time counts as an evening run (backup and summary).
Gemini extraction runs only when `GEMINI_API_KEY` and the `JOBMARKET_AI_*` limits are set
in `.env`; until then that DAG skips. To set them, replace RPM, TPM, and DAILY below with
your account's limits (the values in your Mac's `.env`; README "Requirement extraction"),
and read the key at a hidden prompt, so it never appears on screen or in the shell history:

```bash
sed -i -e 's/^# *JOBMARKET_AI_RPM=.*/JOBMARKET_AI_RPM=RPM/' -e 's/^# *JOBMARKET_AI_TPM=.*/JOBMARKET_AI_TPM=TPM/' -e 's/^# *JOBMARKET_AI_DAILY_LIMIT=.*/JOBMARKET_AI_DAILY_LIMIT=DAILY/' .env
read -rs -p "Gemini key: " K; echo; sed -i "s|^# *GEMINI_API_KEY=.*|GEMINI_API_KEY=$K|" .env; unset K
docker compose up -d airflow-scheduler
docker compose exec airflow-scheduler jobmarket extract status
```

- Check: `extract status` shows the active configuration and no "Paused until" line; the
  next :30 run of `jobmarket_extraction` is green. Use one machine for extraction: the Mac
  and the server share the same key's free daily limit.

**G5. Running jobs by hand.** Works with or without Airflow, from
`~/Job-Market-Intelligence/deploy`. If Airflow is running, a job it is running at the same
moment makes yours stop with "Not started: ... already running" (and the reverse), so
nothing runs twice. Without Airflow, start one job at a time (memory).

| Task | Command (each starts with `docker compose run --rm jobs`) |
|---|---|
| Collect jobs from all boards | `jobmarket collect` |
| Retry boards whose retry is due | `jobmarket retry-due` |
| Run one AI batch | `jobmarket extract run --limit 20` |
| Rebuild and publish the reports | `jobmarket analytics publish` |
| Copy raw responses to S3, then remove local ones S3 confirmed | `jobmarket sync-raw`, then `jobmarket cleanup-raw --require-s3` |
| Back up to S3, then keep the newest 3 local copies | `jobmarket backup create --upload`, then `jobmarket backup prune --keep 3` |
| Daily summary (once per day) and pending email | `jobmarket summary send` |
| Test email | `jobmarket notifications --send-test` |
| Owner account status, password reset, sign out everywhere | `jobmarket owner status` / `owner reset-password` / `owner sign-out-all` |

**G6. Outside monitoring (optional, free; set up when you want it).**
- Heartbeat, so you hear when the evening run stops happening: create a free check at
  healthchecks.io with period 1 day and a grace time of your choice (the summary normally
  finishes a few minutes after 21:00). Put its ping URL into `.env` as
  `JOBMARKET_HEARTBEAT_URL` (it identifies your check: keep it private), then
  `docker compose up -d airflow-scheduler`. The evening summary step requests it.
- Uptime, so you hear when the site is down: a free UptimeRobot HTTP(s) monitor for
  `https://YOURDOMAIN.dev/healthz`, expecting the word `ok`.
- Check: both services show the first successful check.

---

## T. Dashboard data Sheet (Google, for Tableau Public)

The pipeline copies the published dashboard tables to a Google Sheet after every report
publication; Tableau Public re-reads that Sheet about once a day. These steps create the
Sheet and a "service account" (a robot Google account the server uses) that may edit only
that Sheet. All free; no billing account is needed.

**T1. Create a Google Cloud project and turn on the Sheets API.**
- Where: https://console.cloud.google.com in your browser, signed in to your Google account.
- Values: project name `job-market-dashboards`. Then APIs & Services > Library > "Google
  Sheets API" > Enable.
- Check: APIs & Services > Enabled APIs lists Google Sheets API.

**T2. Create the service account and its key.**
- Where: IAM & Admin > Service Accounts > Create service account.
- Values: name `jobmarket-sheets`; skip the optional role and user steps (it needs no project
  role). Open it > Keys > Add key > Create new key > JSON. A `.json` file downloads.
- Check: the account's email ends in `iam.gserviceaccount.com`. The JSON file is a secret:
  never commit, email, or paste it anywhere public.

**T3. Create the Sheet and share it with the service account only.**
- Where: https://sheets.google.com > Blank spreadsheet, named
  `Job Market Intelligence - dashboard data`.
- Values: Share > add the service account's email as **Editor** (untick "Notify"). Copy the
  spreadsheet id: the part of the address between `/d/` and `/edit`.
- Check: Share lists you (owner) and the service account (editor), nobody else.

**T4. Put the key on the server and point the pipeline at the Sheet.**
- Where: the server, in `~/Job-Market-Intelligence/deploy`.
- What: `nano secrets/google-sheets.json`, paste the whole JSON file's text, save. Then make it
  readable only by the containers' user (10001):
  `sudo chown 10001:10001 secrets/google-sheets.json && sudo chmod 400 secrets/google-sheets.json`.
- Values: in `.env`, set `JOBMARKET_SHEETS_ID=` the id from T3 and
  `JOBMARKET_SHEETS_KEY_FILE=/app/secrets/google-sheets.json`; then
  `docker compose up -d airflow-scheduler` (reloads the settings).
- Check: `docker compose run --rm jobs jobmarket tableau sync --dry-run` lists the tabs and
  their sizes; `docker compose run --rm jobs jobmarket tableau sync` prints "Updated the
  dashboard Sheet", and the Sheet now has tabs `about`, `jobs`, `job_skills`, and more.

From then on every publication updates the Sheet (Airflow task `sheets_sync`), and the daily
summary says when it last did. If an update fails, the Sheet keeps the previous complete
data. To stop sharing data with Google, delete the Sheet or remove the service account's
access, and remove `JOBMARKET_SHEETS_ID` from `.env`.

**T5. Show the published dashboards on the site.**
- Where: Tableau Public, after building and publishing the workbook from the Sheet
  (`docs/tableau/BUILD_GUIDE.md`); then the server, in `~/Job-Market-Intelligence/deploy`.
- Values: open the main dashboard on Tableau Public; its address is
  `https://public.tableau.com/views/NAME/WhatEmployersAskFor`. In `.env`, set
  `JOBMARKET_TABLEAU_WORKBOOK=NAME` and, optionally, `JOBMARKET_TABLEAU_PROFILE=` your
  profile address. Then `docker compose up -d web`.
- Check: `https://YOURDOMAIN.dev/insights` shows the main dashboard, each card under
  "See more dashboards" opens its own dashboard, and the job-search page links to it.

**Before the server exists:** the same sync works from your Mac (in the project folder, with
the key file somewhere outside the project):
`JOBMARKET_SHEETS_ID=... JOBMARKET_SHEETS_KEY_FILE=/path/to/key.json .venv/bin/jobmarket tableau sync`,
after `jobmarket migrate` and `jobmarket analytics publish`.

---

## H. Updating to a new version

1. Back up first: `docker compose run --rm jobs jobmarket backup create --upload`.
   Write down the STAMP.
2. Note the current version: `grep APP_VERSION .env` (needed for a rollback).
3. If Airflow runs, stop it so no task starts during the update:
   `docker compose stop airflow-scheduler`.
4. Get the code and build:

```bash
git pull
sed -i "s/^APP_VERSION=.*/APP_VERSION=$(git rev-parse --short HEAD)/" .env
docker compose build web
```

5. Apply new migrations and publish analytics:
   `docker compose run --rm jobs jobmarket migrate`, then
   `docker compose run --rm jobs jobmarket analytics publish`.
6. Restart on the new image: `docker compose up -d web caddy`, and if Airflow runs,
   `docker compose run --rm airflow-init` then
   `docker compose up -d airflow-api airflow-dag-processor airflow-scheduler`.
7. Check: `docker compose ps` shows `web` healthy; repeat the F5 browser checks.

The previous image stays on the server under its old tag.

---

## I. Rolling back

**If the update ran no new migration** (step H4 said the schema was up to date):

```bash
git checkout PREVIOUS_COMMIT          # the APP_VERSION you noted in H2
sed -i "s/^APP_VERSION=.*/APP_VERSION=PREVIOUS_COMMIT/" .env
docker compose up -d web caddy
```

Check: `docker compose ps` shows `web` healthy on `jobmarket-app:PREVIOUS_COMMIT`. Return to
`main` later with `git checkout main`.

**If the update applied a migration:** migrations only move forward, so the old code may
not work with the new schema. Restore the backup from H1 into a new database, and switch to
it:

```bash
docker compose exec db psql -U postgres -c 'CREATE DATABASE "Job_Market_before" OWNER jobmarket'
docker compose run --rm jobs jobmarket backup restore data/backups/STAMP --database Job_Market_before
```

Then set `DB_NAME=Job_Market_before` in `.env`, roll the code back as above, run
`analytics grant`, `private grant`, `private check-access`, and `analytics publish` as in F3,
and `docker compose up -d web caddy`. Changes made after the backup are not in that
database; the newer `Job_Market` database is kept until you decide to remove it.

---

## J. Rebuilding after losing the server

On a new Amazon Linux 2023 instance: B5 to B7 (role, security group, Elastic IP; update
the Cloudflare A record if the IP changed), D, E, F1, F2, then F3 Option 1 steps 3 to 5 with the newest
STAMP from `backup list`, then F4 to F8, then G1 if the instance has 2 GiB or more. Give the new instance an SSH rule and keep it
until Session Manager works there too (F8). Raw responses and backups older than 30 days
are gone by design.

---

## K. Troubleshooting

| Symptom | Check |
|---|---|
| No certificate, browser warns | `dig +short YOURDOMAIN.dev` equals the Elastic IP; B6 allows TCP 80 and 443; `docker compose logs caddy` |
| Caddy log: `rejectedIdentifier` for a name that is not yours (such as `change_me...`) | `SITE_DOMAIN` in `.env` still holds the placeholder. Set it (E5), then `docker compose up -d caddy`; `restart` keeps the old setting |
| `ssh`: `Permission denied (publickey)` | Pass the key pair file with `-i`; `chmod 400` it first (G2) |
| Airflow sign-in says invalid credentials | Copy only the password, not the JSON around it (G2 prints it on its own) |
| Airflow task pink (skipped) | Normal for evening-only steps in a noon run, for `sheets_sync` until the Sheet exists, and for `extract` until Gemini is configured; otherwise its log says why (another job held the lock, or a quota pause) |
| Site shows "Database not reachable" | `docker compose ps` (is `db` healthy?); `docker compose logs db` |
| Site shows "Search data is being rebuilt", or Reports says "Reports not ready yet" | `docker compose run --rm jobs jobmarket analytics publish` |
| Reports page says the latest rebuild did not pass its checks | The alert email (or `jobmarket notifications --full`) has the error; the site keeps the last good reports. Fix, then `analytics publish` |
| No email arrives | E6; `jobmarket notifications --full` shows each message's last error; `notifications --send-test` |
| Airflow UI does not open | G2 tunnel running? `docker compose ps` (is `airflow-api` healthy?); `docker compose logs airflow-api` |
| A DAG is missing or shows an import error | `docker compose exec airflow-scheduler airflow dags list-import-errors` |
| A task keeps failing | Airflow UI > the task > Logs; the same command can be run by hand (G5) to see its output |
| `web` unhealthy | `docker compose logs web` |
| Slow, or a job stopped by itself | `free -h`, `docker stats --no-stream`, `sudo dmesg \| grep -i oom`; run one job at a time |
| Disk full | `df -h /`, `docker system df`; `docker image prune` removes unused images (keep the current and previous tag) |
| `backup list` says AccessDenied | B4 inline policy (bucket name), B5 role attached |
| Session Manager will not connect | B4 `AmazonSSMManagedInstanceCore`, B5, reboot, wait 10 minutes. Meanwhile use SSH; it stays open until F8. If SSH was already closed, add the TCP 22 rule back (source "My IP") |

---

## Remaining manual work checklist

Done in the code (this repository): Docker image, Compose stack, Caddy HTTPS, database
first-start script, backups and S3 commands, IAM and bucket policy examples, the Airflow
schedules, email and summary commands, this runbook.
Not done yet, and only you can do it in AWS:

- [ ] B1 S3 bucket (block public access, SSE-S3)
- [ ] B2 bucket policy (HTTPS only)
- [ ] B3 lifecycle rules, 30 days for `raw/` and `backups/`
- [ ] B4 IAM role `jobmarket-ec2` with Session Manager and the `jobmarket-s3` inline policy
- [ ] B5 role attached to the instance
- [ ] B6 security group: 80 and 443 added, existing SSH rule kept, nothing for 5432
- [ ] B7 Elastic IP associated
- [ ] B8 budget alert (optional)
- [ ] C1 domain registered at Cloudflare
- [ ] C2 A record (DNS only) pointing to the Elastic IP
- [ ] E1 to E5 server preparation (swap, Docker, plugins, code, `.env`)
- [ ] F1 to F7 first start, checks, restart test, first S3 backup
- [ ] F8 SSH rule removed, only after Session Manager worked in D1 and after the F6 reboot
- [ ] E6 Gmail app password; `notifications --send-test` arrives
- [ ] G0 instance size decision (budget); resize only after approving it
- [ ] G1 to G3 Airflow started, UI reached privately, first evening run and summary email
- [ ] G6 heartbeat and uptime monitors (optional)
- [ ] T1 to T4 dashboard Sheet: Google project, service account, Sheet shared, key on the server
- [ ] T5 workbook published on Tableau Public and its name set for the Market insights page
- [ ] Record real memory use in `deploy/SIZING.md` after a few days of schedules
