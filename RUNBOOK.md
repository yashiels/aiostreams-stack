# aiostreams-stack runbook

The remote examples start in `~/apps/media-gateway`. Replace `user@host` with the SSH
destination.

## Service checks

After each deployment, run `make probe HOST=user@host`. Before the tunnel ingress is active, run
`make deploy-prep HOST=user@host`. That target starts no tunnel connector. It checks only the
internal endpoints.

To examine the project directly, run these commands:

```sh
docker compose -p media-gateway --env-file .env --env-file versions.env -f compose.yml ps
docker compose -p media-gateway --env-file .env --env-file versions.env -f compose.yml logs --since=30m aiostreams cloudflared
```

If a client cannot connect, examine these items in this order:

1. The internal Jellyfin API endpoint
2. The public endpoint
3. The tunnel logs

The production Compose file publishes no host port.

## Configuration and profiles

Before you change household entries, run `make bootstrap-dry`. To apply the primary user and the
profiles, run `make bootstrap`. The script keeps profile fields that it does not manage.

If you change the configuration password, the picker URL and the manifest URL stop working for
every person. To change the password and get new URLs, follow "Revoke access" in
[docs/SETUP.md](docs/SETUP.md).

## Change home rows

1. Edit `config/aiostreams.template.json` and `config/expected-catalogs.json`. Run
   `make sync HOST=user@host`. Sync keeps the host household file.
2. On the host, save the current configuration:
   `ops/config-snapshot.py save --config main`.
   Record the printed snapshot path.
3. On the host, run `make rows-dry`. Read the preset order, changed paths, and catalog entries.
   Copy the printed `expect_sha256` value.
4. On the host, run `ops/bootstrap.py --apply-rows --expect-sha256 <hash>`. Use the hash from
   step 3. The script stops if the current rows changed. It saves an emergency snapshot and checks the
   full configuration after the update.
5. From the local tree, run `make check-catalogs HOST=user@host`. It checks all manifest catalogs
   and the exact Jellyfin view order. Before you restore a failed update, confirm the state
   with this check. To restore, run
   `ops/config-snapshot.py restore --config main <snapshot-path>` on the host.
   Use the saved file from step 2. The script checks the full restored configuration.

The AIOStreams sign-in limit is 10 sign-ins per IP address in 5 minutes. Each script run uses
one AIOStreams sign-in. `check-catalogs` also uses one Jellyfin sign-in. Do not run the steps in
a fast loop. If a step reports `HTTP 429`, wait 5 minutes. Then run the same step again.

AIOStreams fetches the manifest of every addon when it saves a configuration. If a save fails,
the script reads the configuration to find if the save took effect. If it finds an unexpected
state, it tries to restore the pre-write configuration and checks the result. If rollback fails,
use the printed emergency snapshot path and restore command. Do not continue to the next
configuration until you confirm the state.

The scripts read the host `.env`. Keep snapshots outside the synced stack tree. Each new
snapshot directory has mode 0700. Each snapshot file has mode 0600. Do not share these files.

## Search results

1. Look in the AIOStreams logs for provider errors.
2. Make sure that each configured addon answers.

The local `tmdb-addon` profile is off. Its pinned version needs MongoDB analytics when it
starts. To use a compatible image, add `COMPOSE_PROFILES=tmdb` and the matching addon URL at the
same time. To stop using it, remove both at the same time.

## Backup and restore

```sh
systemctl --user status media-gateway-backup.timer
restic snapshots --tag media-gateway
./backup/backup.sh
./backup/restore.sh <snapshot-id>
```

The restore script refuses a snapshot without `data/aiostreams`. It moves the old data to
`restore-rollback/<timestamp>/`. If the deployment fails, the script puts the old data back.

Remove the rollback copy only after these two checks pass:

1. `./ops/probe.sh --verbose` succeeds.
2. A test video plays.

## Safe deployment

Before you replace a stack tree, get the hash of the rendered configuration:

```sh
ops/compose-config-sha.sh ~/apps/media-gateway
```

Give that value as `EXPECTED_COMPOSE_SHA256` on the next deployment. If the hash is different,
the deployment stops before it pulls images, makes a backup, installs the timer, makes
directories, or changes containers. `deploy.sh --print-config-sha` gives the same hash and
changes nothing.

## Rollback

Before a structural upgrade, make two copies:

1. A tar archive of the stack tree, without `data/`
2. A copy of `.env` with mode 0600

To roll back, restore the files and `.env`. Then run `bash ops/deploy.sh`. The application data
stays in place. Use restic only when the data itself needs recovery.

Do not run two connectors for the same remotely managed tunnel during a rollback.

### Roll back an AIOStreams update

An AIOStreams update can apply database migrations when it starts. An older image does not start
on a migrated database. The log shows `Database is at migration N but this build only knows up
to M`. Thus, `make rollback` alone is not sufficient after such an update.

Each deployment makes a pre-upgrade snapshot. To roll back an update:

1. Find the pre-upgrade snapshot: `restic snapshots --tag media-gateway-pre-upgrade`.
2. Put the old `versions.env` back: `git checkout <git-ref> -- versions.env`. Then run
   `make sync HOST=user@host`.
3. Run `./backup/restore.sh <snapshot-id>` on the host. The script deploys with the old image.
4. Run `make probe HOST=user@host`.

Changes that people made after the update, such as watch state, are lost.

## Disk space

```sh
df -h /
du -sh data/aiostreams /var/lib/docker 2>/dev/null
docker system df
```

The deployment does not start when the free space is less than 10 GiB. The probe fails when the
free space is less than 6 GiB. Find what uses the space before you remove Docker data or
rollback copies.

## Sync all configurations

Run the sync commands on the host in `~/apps/media-gateway`. Keep snapshots outside this tree.
The default snapshot directory is `~/media-gateway-restore`. To change it, set `SNAPSHOT_DIR`.
An existing directory must belong to the current user, have no group or world permission bits,
and not be a symbolic link. New directories have mode 0700. Snapshot files have mode 0600.

1. Get the UUID and password of each extra configuration from your password manager. Use a
   command that prints them to standard output in this format, without writing a local file:
   `{"configs":[{"name":"person_one","uuid":"replace-uuid","password":"replace-password"}]}`.
   Do not include the main configuration (the one in `.env`). `load_configs` rejects its UUID
   and the reserved name `main`.

   Pipe that command straight into this SSH command. The host writes the file atomically under
   `umask 077`. Replace `print-extra-configs` with your command and `user@host` with the host.
   The credentials go through standard input only.

   ```sh
   print-extra-configs | ssh -o StrictHostKeyChecking=yes user@host '
     set -eu
     umask 077
     cd ~/apps/media-gateway
     temporary=$(mktemp config/.configs.XXXXXXXX)
     trap '\''rm -f "$temporary"'\'' EXIT
     cat > "$temporary"
     python3 -m json.tool "$temporary" >/dev/null
     mv "$temporary" config/configs.json
   '
   ```

   Keep credentials out of command arguments, local files, and terminal output. To check the
   host file and examine all diffs, run `bash ops/each-config.sh ops/bootstrap.py --dry-run --sync-template --diff` on the host (`make sync-dry-all` where `make` exists).
2. Run `bash ops/each-config.sh ops/config-snapshot.py save` on the host (`make snapshot-all` where `make` exists). Record the printed paths. The scripts process `main`
   first, then the order in `configs.json`. They wait 40 seconds between configurations.
   Set `EACH_CONFIG_PAUSE` to an integer from 30 to 600 to change this wait. The batch stops
   when a command fails and prints that configuration's name.
3. For each configuration, run these commands on the host. Replace `NAME` with `main` or a
   standalone name. Examine the changed keys, preset paths, and catalog tuples. Use the hash
   from that configuration's dry run for its write. If any preset is removed, stop and examine
   that removal before you add `--allow-preset-removal`.

   ```sh
   python3 ops/bootstrap.py --config NAME --dry-run --sync-template --diff
   python3 ops/bootstrap.py --config NAME --sync-template --expect-sha256 <hash>
   ```

   The hash includes only live template keys. It excludes only service and preset credentials.
   A write saves an emergency snapshot before the PUT. The check compares the full configuration,
   except server fields `statistics`, `trusted`, `ip`, `activeVariants`, `autoVariants`,
   `healthResults`, and `variantSelectorLocation`. It also checks existing preset credentials
   and standalone Jellyfin settings. If the check fails, the script tries to roll back and
   reports the result. If it reports "no change was made", stop and examine the failed request.
4. Run `bash ops/each-config.sh ops/check-catalogs.py` on the host (`make check-catalogs-all` where `make` exists). It checks the manifest catalog set and the ordered
   Jellyfin views for each configuration. If a batch fails, confirm that configuration's state
   before you continue.
5. To restore one configuration, use its saved path and the same name:

   ```sh
   python3 ops/config-snapshot.py restore --config NAME <snapshot-path>
   ```

   Restore requires both the saved name and UUID to match. It rejects old snapshots without a
   target identity. It checks the full restored configuration with the same server field list.

Each script run uses one AIOStreams admin sign-in. The sign-in limit is 10 per IP address in
5 minutes. Other clients can use the same limit. A request can still receive HTTP 429. The HTTP
client retries 429 at most three times. It uses `Retry-After`, limits the wait to 1 through 330
seconds, and adds up to 5 seconds. If the header is absent, invalid, or a date in the past, the
wait is 60 seconds before the extra delay. Reconciliation GET requests use three bounded retries
at 5, 15, and 30 seconds for transport failures, HTTP 5xx, or HTTP 429.

## MDBList genre rows

Put `MDBLIST_API_KEY` in the host `.env`. Use mode 0600 for that file. Set `MDBLIST_ROWS=on`.
The loader requires the key when a managed preset is enabled. It stops before HTTP calls if
`MDBLIST_ROWS` is not `on` or `off`. An unset or empty value means `on`. Do not print the key.
Use `--sync-template` for each configuration to add rows, rotate the key, or change a list.
Examine each diff and use its current hash as described in "Sync all configurations".

If the MDBList addon is unavailable, use this recovery procedure:

1. Set `MDBLIST_ROWS=off` in the host `.env`.
2. For each configuration, run `ops/bootstrap.py --config NAME --dry-run --sync-template --diff`.
   Confirm that only the managed genre presets are disabled. Get the current hash.
3. Run `ops/bootstrap.py --config NAME --sync-template --expect-sha256 <hash>`.
   Disabled presets do not require addon validation on save.
4. Run `ops/check-catalogs.py --config NAME`. It reports the seven genre catalogs and views
   missing and exits non-zero. This is expected while the rows are off. Confirm that the other
   catalogs and views are present and in order.
5. When the addon answers again, set `MDBLIST_ROWS=on`. Repeat the dry run and sync for each
   configuration. Run `check-catalogs` again. All 26 views must be present.

Snapshots use `{MDBLIST_API_KEY}` in managed manifest URLs. Save works without the current key.
Restore requires the key from `.env`, even when the saved rows are disabled. Each save keeps the
newest 10 snapshots for that target. Automatic rollback uses the original configuration in
memory and does not read a snapshot file.

Before rollout, measure the quota on the isolated smoke stack. Read the remaining quota from
the `X-RateLimit-Remaining` response header. This command returns the account information:

```sh
curl -s "https://api.mdblist.com/user?apikey=$MDBLIST_API_KEY"
```

Add `-D - -o /dev/null` to read only the response headers. Load the key from `.env` first.
Keep shell tracing off. Do not print the expanded command or the key.

Measure the remaining quota before and after these checks:

1. Request the first page of all 26 rows from three configurations.
2. Request three pages of each genre row.
3. Repeat step 1. This repeat must use no extra MDBList calls.
4. Restart the container and repeat step 1.

Proceed only if the complete validation run uses at most 150 calls and at least 600 requests
remain after validation. Otherwise, stop rollout and revise the cache or row design. Measure
again before and after live rollout, and 24 hours later.

The operational estimate is the number of distinct catalog URLs requested, multiplied by
one plus the number of restarts, plus manifest fetches. It is not a worst-case bound. Each page
and genre filter can add a cache entry. The catalog TTL is 86400 seconds for the MDBList host.
The default manifest cache can fetch seven manifests per 12 hours, with more fetches on a
restart or configuration save.

The enabled-only addon validation, URL log masking, shared URL cache keys, and TTL parsing
assumptions apply to the pinned `2026.10.12.0225-nightly` AIOStreams image in `versions.env`.
Before any image bump, repeat the smoke checks: block the MDBList host and confirm that enabled
rows fail to save, disabled rows save, and enabled rows return after access is restored; measure
the quota; check logs and API responses for key leaks; and confirm the exact cache TTL and that
a repeated first-page request uses no extra calls.

## Locked staged promotion

Use this entrypoint for a reviewed upgrade. Do not run `make deploy` or `make sync` during
this procedure. Use absolute paths without symbolic links. Keep the reviewed non-secret
stage, private render tree, and transactions outside production and outside each other.
Create each private parent with mode 0700. Transaction and private render directories must
not exist before the helper creates them.

### Bootstrap the guards

Review the three guard files and their stage identity separately before bootstrap.
Establish a cooperative maintenance exclusion window for every deploy, sync, backup,
restore, and configuration writer, including scheduled automation. `--maintenance-window`
asserts that this exclusion already exists; the helper does not stop automation or services.
Keep the exclusion in place across interruption until bootstrap or recovery is verified.
The old deploy script cannot enforce the new admission rule before it has been installed.

Run the reviewed helper from outside production. This bootstrap changes only `with-lock.sh`,
`staged-promotion.py`, and `deploy.sh`. It acquires the real production `.stack.lock`, verifies
the old tree and reviewed stage identities, and retains the exact old guard presence, bytes,
and permissions outside production. It preserves private files, data, and the lock inode.
It invokes no service commands.

```sh
REVIEWED=/srv/reviewed-stage
PRODUCTION=/srv/stack
TRANSACTIONS=/srv/stack-transactions
mkdir -m 700 "$TRANSACTIONS"
BOOTSTRAP_STAGE_SHA=$(python3 "$REVIEWED/ops/staged-promotion.py" identity --root "$REVIEWED")
BOOTSTRAP_OLD_TREE_SHA=$(python3 "$REVIEWED/ops/staged-promotion.py" identity --root "$PRODUCTION")
```

Review these identities and the guard diff before running the next command.

```sh
python3 "$REVIEWED/ops/staged-promotion.py" bootstrap \
  --production "$PRODUCTION" --stage "$REVIEWED" \
  --transaction "$TRANSACTIONS/bootstrap" \
  --stage-sha "$BOOTSTRAP_STAGE_SHA" --baseline-sha "$BOOTSTRAP_OLD_TREE_SHA" \
  --maintenance-window
for guard in with-lock.sh staged-promotion.py deploy.sh; do
  cmp "$REVIEWED/ops/$guard" "$PRODUCTION/ops/$guard" || exit 1
done
test ! -e "$PRODUCTION/.promotion-state"
```

Each file is published with an atomic rename; the deployment admission guard is last.
The set is protected by durable transaction state, not one atomic operation across three
independent files. Release the maintenance exclusion only after all verification succeeds.
A SIGKILL or power loss leaves the pending marker and transaction available for recovery.
Do not continue bootstrap or deploy automatically. While maintaining the exclusion, run:

```sh
python3 "$REVIEWED/ops/staged-promotion.py" recover \
  --production "$PRODUCTION" --transaction "$TRANSACTIONS/bootstrap"
```

Recovery takes the same lock and restores old guards, removing any that were originally
absent. It can be retried after interruption. It refuses changed snapshots, unrelated later
guard edits, and changes to other managed files. Verify recovery before releasing the
exclusion; another bootstrap needs a fresh separately reviewed baseline and transaction.

### Prepare the candidate hash and promote

The non-secret stage must not contain `.env`, household or configuration credentials,
data, restoration directories, Git metadata, or lock files. The helper rejects symbolic
links, hard links, and special files in managed paths. Tree identities include file bytes,
names, and permissions. The stage must retain the same three installed guards; promotion
refuses changes to them and does not rewrite unchanged managed files.

Capture the old tree and rendered hash after bootstrap. Freeze and review the candidate
stage and record its identity. The `render` command verifies both tree identities under
the production lock, then creates a separate owner-only PRIVATE tree containing the stage
and a mode-0600 copy of production `.env`. It performs metadata rendering only, without
creating data, promoting files, or changing services. It prints only the candidate hash.
Never promote or share this private tree; the promotion entrypoint rejects its `.env`.

```sh
OLD_TREE_SHA=$(python3 "$REVIEWED/ops/staged-promotion.py" identity --root "$PRODUCTION")
OLD_COMPOSE_SHA=$(bash "$PRODUCTION/ops/compose-config-sha.sh" "$PRODUCTION")
STAGE_SHA=$(python3 "$REVIEWED/ops/staged-promotion.py" identity --root "$REVIEWED")
mkdir -m 700 /srv/private-renders
PRIVATE_RENDER=/srv/private-renders/upgrade
CANDIDATE_COMPOSE_SHA=$(python3 "$REVIEWED/ops/staged-promotion.py" render \
  --production "$PRODUCTION" --stage "$REVIEWED" --private-render "$PRIVATE_RENDER" \
  --stage-sha "$STAGE_SHA" --baseline-sha "$OLD_TREE_SHA")
```

The private render uses the same project, profiles, environment, and Compose files as
production. It passes `--project-directory "$PRODUCTION"` to Compose and absolute paths
for the private input files. Thus relative bind mounts use production paths. It does not
replace path strings in rendered secrets. Keep environment values out of stdout, arguments,
and evidence. Treat the private render tree as secret material and retain it owner-only.

After review, promote only the NONSECRET stage. The helper verifies that the resulting
production render equals `CANDIDATE_COMPOSE_SHA` before deployment. The final command below
checks the same equivalence independently after promotion.

```sh
python3 "$REVIEWED/ops/staged-promotion.py" promote \
  --production "$PRODUCTION" --stage "$REVIEWED" \
  --transaction "$TRANSACTIONS/upgrade" \
  --stage-sha "$STAGE_SHA" --baseline-sha "$OLD_TREE_SHA" \
  --baseline-compose-sha "$OLD_COMPOSE_SHA" \
  --candidate-compose-sha "$CANDIDATE_COMPOSE_SHA"
test "$CANDIDATE_COMPOSE_SHA" = "$(bash "$PRODUCTION/ops/compose-config-sha.sh" "$PRODUCTION")"
```

Add `--no-tunnel` to select the existing internal deployment and probe behavior.
The process acquires the production `.stack.lock` with the operating system flock API.
It ignores inherited lock-held markers and rejects a foreign lockfile override. The lock
covers baseline checks, copying, final rendering, deployment, stopped-writer backup,
startup, and probes. Nested deploy and backup commands inherit this owned lock.

The transaction retains verified copies of old and candidate non-secret files. Promotion
preserves private files, writer data, restoration directories, and the lock inode. A
persistent `.promotion-state` blocks ordinary deployment while the transaction is pending.
A failed or interrupted transaction never resumes deployment automatically.

### Reconcile completed transactions

A crash can occur after the durable phase becomes `complete` and before marker cleanup
finishes. Use the reviewed helper and the exact transaction directory:

```sh
python3 "$REVIEWED/ops/staged-promotion.py" reconcile \
  --production "$PRODUCTION" --transaction "$TRANSACTIONS/upgrade"
```

Use `$TRANSACTIONS/bootstrap` for a completed bootstrap. Reconciliation acquires the actual
project lock. It verifies the transaction schema, root, lock identity, snapshots, journal,
and expected completed tree. For a completed promotion it also checks the rendered hash.
It clears only the matching marker and never restores files or repeats deployment, backup,
startup, probes, or migrations. It is safe to retry if marker cleanup itself was interrupted,
including after unlink but before directory fsync. A changed completed tree refuses without
mutation. Do not remove the marker manually or run file rollback against a completed state.

### Recover interrupted file transitions

Each live path mutation has a durable intent before individually atomic publication,
deletion, directory creation, or mode change. The journal records the exact checkpoint and
at most one pending transition. Before any restoration, recovery verifies both snapshots
and the journal, then requires the live tree to equal that checkpoint or the single pending
transition's result. An unrelated edit, addition, deletion, or mode change refuses before
restoration. Resolve such drift through separately reviewed recovery; do not overwrite the
journal with the current tree. Unsupported schemas and damaged evidence also refuse.

If deployment has not started, use the installed reviewed helper to restore the exact old
managed file set. This removes candidate additions and restores deleted files. Recovery
acquires the same production lock and can be retried if interrupted. It starts no services.
A durable `recovered` transaction can be retried with `recover` or finalized with `reconcile`;
both verify the old tree and finish marker cleanup without replaying file restoration.

```sh
python3 /path/to/reviewed/ops/staged-promotion.py recover \
  --production /srv/stack --transaction /srv/stack-transactions/upgrade
```

If the durable phase is `deploying`, deployment may have started or applied migrations.
The helper refuses file-only recovery and keeps the deployment gate closed. Use the
separately rehearsed stopped-writer recovery: stop the candidate writer under the project
lock, retain candidate data, restore independent old writer data and old files, then start
only the retained old image with project-scoped `up -d --no-deps --pull never aiostreams`.
Run probes separately. Clear the pending marker only after that manual recovery has been
verified. Do not use `deploy.sh` or `restore.sh` for this offline start. M0 does not qualify
migration compatibility, restore old writer data, or implement the M1 rollback rehearsal.

Retain transaction evidence and old files through acceptance and the required rollback
window. Do not place transactions inside any tree managed by sync.
