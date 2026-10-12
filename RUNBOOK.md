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
   `ops/config-snapshot.py save ~/media-gateway-restore/<timestamp>.json`.
   Replace `<timestamp>` with a new time stamp. The file must not exist.
3. On the host, run `make rows-dry`. Read the preset order, changed paths, and catalog entries.
   Copy the printed `expect_sha256` value.
4. On the host, run `ops/bootstrap.py --apply-rows --expect-sha256 <hash>`. Use the hash from
   step 3. The script stops if the current rows changed. It checks the rows after the update.
5. From the local tree, run `make check-catalogs HOST=user@host`. It checks all manifest catalogs
   and the exact Jellyfin view order. Before you restore a failed update, confirm the state
   with this check. To restore, run
   `ops/config-snapshot.py restore ~/media-gateway-restore/<timestamp>.json` on the host.
   Use the saved file from step 2. The script checks the full restored configuration.

The AIOStreams sign-in limit is 10 sign-ins per IP address in 5 minutes. Each script run uses
one AIOStreams sign-in. `check-catalogs` also uses one Jellyfin sign-in. Do not run the steps in
a fast loop. If a step reports `HTTP 429`, wait 5 minutes. Then run the same step again.

AIOStreams fetches the manifest of every addon when it saves a configuration. If an addon is
not available, `--apply-rows` fails. The live configuration does not change. Try again later.

If the post-write check fails after the "update AIOStreams home rows" line, the write was done.
Run `make check-catalogs HOST=user@host` to confirm the state before any other step.

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

1. Find the pre-upgrade snapshot: `restic snapshots --tag media-gateway`.
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
