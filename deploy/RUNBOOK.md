# Deployment runbook: one EC2 server

This puts the public job-search demo online at `https://<name>.duckdns.org` on one EC2
instance (Amazon Linux 2023, t3.micro, 20 GiB disk, region us-east-2):

- **Docker Compose** runs three containers: PostgreSQL (`db`), the website (`web`,
  gunicorn), and Caddy (`caddy`, HTTPS with a Let's Encrypt certificate).
- **Only ports 80 and 443 are open to the public.** PostgreSQL and the website are
  reachable only from the other containers. Administration uses AWS Systems Manager
  Session Manager, a shell in the browser. Your existing SSH access stays open during setup
  as a fallback, and is closed (step F8) only after Session Manager has worked, including
  after a reboot.
- **S3** keeps raw responses and backups (encrypted, deleted after 30 days by lifecycle
  rules). The server reaches S3 through an IAM role, so no AWS keys are stored anywhere.
- **Nothing runs on a schedule.** Collection, extraction, analytics builds, and backups
  run when you start them (section G). The website always shows when data was last
  collected.

Every step says **where** to run it, **what** it does, the **values** you supply, and how
to **check** it worked. Do the sections in order. Placeholders look like `YOUR-BUCKET-NAME`;
replace them, including the capital letters.

Never paste a password, key, or the contents of `.env` into a chat, an issue, or a commit.

---

## A. Before you start

**A1. Collect what you need.**
- An AWS console login for the account that owns the instance.
- The instance ID (EC2 > Instances, looks like `i-0abc...`) and its region, us-east-2.
- A DuckDNS account (free; sign in at https://www.duckdns.org with GitHub or Google).
- On your Mac: this repository with the latest `main`, and PostgreSQL 17 at
  `/Library/PostgreSQL/17/bin` (only to copy your existing data, step F3).

**A2. Decide the names.** Write them down; later steps use them.
- Bucket name, globally unique, lowercase, for example `jobmarket-yourname-data`.
- DuckDNS name, for example `jobsdemo` (the site becomes `jobsdemo.duckdns.org`).

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
- What: keeps the same IPv4 address across stops and starts, so DuckDNS stays correct.
- Cost: AWS charges for every public IPv4 address (about $0.005 per hour, about $3.60 a
  month), Elastic or not; your credits cover it. A stopped instance with an Elastic IP is
  still charged for the address.
- Check: the instance's "Public IPv4 address" equals the Elastic IP. Write it down.

**B8. Optional: a budget alert.** Billing > Budgets > Create budget > a monthly cost budget
with an email alert, so you hear about charges early.

---

## C. DuckDNS (in your browser)

**C1. Point your name at the server.**
- Where: https://www.duckdns.org after signing in.
- Values: your name from A2 > add domain; then in its "current ip" field type the Elastic
  IP from B7 > update ip.
- Check, on your Mac (Terminal): `dig +short YOURNAME.duckdns.org` prints the Elastic IP.
  It can take a few minutes.

Keep the DuckDNS token private; this setup never needs it on the server because the
Elastic IP does not change.

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
- Check: `whoami` prints `ec2-user`. Start every later session with this command.

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
for i in 1 2 3 4; do openssl rand -hex 24; done
nano .env
```

- Values: in `nano`, paste one of the four printed values into each of
  `POSTGRES_PASSWORD`, `DB_PASSWORD`, `ANALYTICS_DB_PASSWORD`, `PRIVATE_DB_PASSWORD`
  (a different one each). Set `SITE_DOMAIN` to `YOURNAME.duckdns.org` and
  `JOBMARKET_S3_BUCKET` to your bucket name. Save with Ctrl+O, Enter; exit with Ctrl+X.
  These are new passwords for the server: do not reuse the ones from your Mac.
- Check: `grep -c CHANGE_ME .env` prints `0`, and `docker compose config --quiet` prints
  nothing (no errors). Keep a copy of the four passwords in your password manager.

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
docker compose run --rm jobs jobmarket analytics build
```

   Check: `check-access` shows the analytics login cannot use the private schema and the
   private login cannot read or write job data; `analytics build` ends with `ERROR=0`.
   Your owner account came with the backup: sign in later with your usual password.

*Option 2: start empty.* Run `migrate`, `sources sync`, `analytics grant`, `private grant`,
`private check-access`, then `owner create --username YOURNAME` (it asks for a new
password at a hidden prompt; type it yourself), all as
`docker compose run --rm jobs jobmarket ...`. The site will have no jobs until a collection
runs (section G), then `analytics build`.

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
- Browser: `https://YOURNAME.duckdns.org` shows Job search with a padlock and the "Last
  complete collection" time; `/reports` shows the reports with their build time.
- Browser, signed out: `https://YOURNAME.duckdns.org/profile` sends you to Sign in. Sign in
  with your owner account; Profile, Saved jobs, and Applications work; Sign out.
- Mac Terminal:
  - `curl -sI http://YOURNAME.duckdns.org | head -1` shows `308` (HTTP goes to HTTPS).
  - `nc -vz -w 5 YOURNAME.duckdns.org 5432` fails or times out (the database is closed).

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
- Check: from your Mac, `nc -vz -w 5 YOURNAME.duckdns.org 22` fails or times out; then open
  one more Session Manager session to confirm you can still get in.

---

## G. Running jobs (by hand)

Run these from `~/Job-Market-Intelligence/deploy`. **One at a time:** never start a job
while another is running (memory, see SIZING.md). Nothing here is scheduled; choosing a
schedule is a separate decision.

| Task | Command (each starts with `docker compose run --rm jobs`) |
|---|---|
| Collect jobs from all boards | `jobmarket collect` |
| Run AI extraction (needs `GEMINI_API_KEY` and the `JOBMARKET_AI_*` limits in `.env`) | `jobmarket extract run` |
| Refresh the reports and serving views | `jobmarket analytics build` |
| Copy raw responses to S3, then remove local ones older than 4 days | `jobmarket sync-raw`, then `jobmarket cleanup-raw` |
| Back up the database to S3 | `jobmarket backup create --upload` |
| Owner account status, password reset, sign out everywhere | `jobmarket owner status` / `owner reset-password` / `owner sign-out-all` |

A typical refresh: `collect`, then `extract run` (if configured), then `analytics build`,
then `sync-raw` and `cleanup-raw`, then `backup create --upload`.

Local backups in the `appdata` volume are not deleted automatically. List them with
`docker compose run --rm jobs ls data/backups`.

---

## H. Updating to a new version

1. Back up first: `docker compose run --rm jobs jobmarket backup create --upload`.
   Write down the STAMP.
2. Note the current version: `grep APP_VERSION .env` (needed for a rollback).
3. Get the code and build:

```bash
git pull
sed -i "s/^APP_VERSION=.*/APP_VERSION=$(git rev-parse --short HEAD)/" .env
docker compose build web
```

4. Apply new migrations and rebuild analytics:
   `docker compose run --rm jobs jobmarket migrate`, then
   `docker compose run --rm jobs jobmarket analytics build`.
5. Restart the website on the new image: `docker compose up -d web caddy`.
6. Check: `docker compose ps` shows `web` healthy; repeat the F5 browser checks.

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
`analytics grant`, `private grant`, `private check-access`, and `analytics build` as in F3,
and `docker compose up -d web caddy`. Changes made after the backup are not in that
database; the newer `Job_Market` database is kept until you decide to remove it.

---

## J. Rebuilding after losing the server

On a new Amazon Linux 2023 instance: B5 to B7 (role, security group, Elastic IP; update
DuckDNS if the IP changed), D, E, F1, F2, then F3 Option 1 steps 3 to 5 with the newest
STAMP from `backup list`, then F4 to F8. Give the new instance an SSH rule and keep it
until Session Manager works there too (F8). Raw responses and backups older than 30 days
are gone by design.

---

## K. Troubleshooting

| Symptom | Check |
|---|---|
| No certificate, browser warns | `dig +short YOURNAME.duckdns.org` equals the Elastic IP; B6 allows TCP 80 and 443; `docker compose logs caddy` |
| Site shows "Database not reachable" | `docker compose ps` (is `db` healthy?); `docker compose logs db` |
| Site shows "Search data is being rebuilt" or reports "not built yet" | `docker compose run --rm jobs jobmarket analytics build` |
| `web` unhealthy | `docker compose logs web` |
| Slow, or a job stopped by itself | `free -h`, `docker stats --no-stream`, `sudo dmesg \| grep -i oom`; run one job at a time |
| Disk full | `df -h /`, `docker system df`; `docker image prune` removes unused images (keep the current and previous tag) |
| `backup list` says AccessDenied | B4 inline policy (bucket name), B5 role attached |
| Session Manager will not connect | B4 `AmazonSSMManagedInstanceCore`, B5, reboot, wait 10 minutes. Meanwhile use SSH; it stays open until F8. If SSH was already closed, add the TCP 22 rule back (source "My IP") |

---

## Remaining manual work checklist

Done in the code (this repository): Docker image, Compose stack, Caddy HTTPS, database
first-start script, backups and S3 commands, IAM and bucket policy examples, this runbook.
Not done yet, and only you can do it in AWS:

- [ ] B1 S3 bucket (block public access, SSE-S3)
- [ ] B2 bucket policy (HTTPS only)
- [ ] B3 lifecycle rules, 30 days for `raw/` and `backups/`
- [ ] B4 IAM role `jobmarket-ec2` with Session Manager and the `jobmarket-s3` inline policy
- [ ] B5 role attached to the instance
- [ ] B6 security group: 80 and 443 added, existing SSH rule kept, nothing for 5432
- [ ] B7 Elastic IP associated
- [ ] B8 budget alert (optional)
- [ ] C1 DuckDNS name pointing to the Elastic IP
- [ ] E1 to E5 server preparation (swap, Docker, plugins, code, `.env`)
- [ ] F1 to F7 first start, checks, restart test, first S3 backup
- [ ] F8 SSH rule removed, only after Session Manager worked in D1 and after the F6 reboot
- [ ] Record real memory use in `deploy/SIZING.md` after a few days
- [ ] Decide schedules for collection, extraction, analytics builds, and backups (none are
      set up)
