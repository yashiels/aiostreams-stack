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

## Disk space

```sh
df -h /
du -sh data/aiostreams /var/lib/docker 2>/dev/null
docker system df
```

The deployment does not start when the free space is less than 10 GiB. The probe fails when the
free space is less than 6 GiB. Find what uses the space before you remove Docker data or
rollback copies.
