# aiostreams-stack runbook

Remote examples assume `cd ~/apps/media-gateway`. Replace `user@host` with the SSH destination.

## Service checks

Run `make probe HOST=user@host` after a deployment. For a deployment before tunnel ingress is
active, use `make deploy-prep HOST=user@host`; it starts no tunnel connector and probes only the
internal endpoints.

Inspect the default project directly:

```sh
docker compose -p media-gateway --env-file .env --env-file versions.env -f compose.yml ps
docker compose -p media-gateway --env-file .env --env-file versions.env -f compose.yml logs --since=30m aiostreams cloudflared
```

When a client cannot connect, check the internal Jellyfin-compatible endpoint, the public endpoint,
and the tunnel logs in that order. The production Compose file publishes no host port.

## Configuration and personas

Run `make bootstrap-dry` before changing household entries. Run `make bootstrap` to converge the
primary user and personas. Existing unmanaged persona fields are preserved. A configuration
password change invalidates the credential-bearing picker URL; regenerate it with
`python3 ops/bootstrap.py --print-urls`.

## Search results

Check the AIOStreams logs for provider failures and confirm configured addons answer. The local
`tmdb-addon` profile is disabled because its pinned version requires MongoDB analytics during
startup. If a compatible image is later enabled, add `COMPOSE_PROFILES=tmdb` and the matching addon
URL together; remove both together when disabling it.

## Backup and restore

```sh
systemctl --user status media-gateway-backup.timer
restic snapshots --tag media-gateway
./backup/backup.sh
./backup/restore.sh <snapshot-id>
```

Restore refuses snapshots without `data/aiostreams`, moves the previous data to
`restore-rollback/<timestamp>/`, and automatically restores the previous tree if deployment fails.
Delete the rollback copy only after `./ops/probe.sh --verbose` succeeds and playback is confirmed.

## Safe deployment

Capture the rendered configuration hash before replacing a stack tree:

```sh
ops/compose-config-sha.sh ~/apps/media-gateway
```

Pass that value as `EXPECTED_COMPOSE_SHA256` on the next deploy. A mismatch exits before pull,
backup, timer installation, directory creation, or container changes. `deploy.sh
--print-config-sha` performs the same read-only calculation and exits.

## Rollback

Keep a tar archive of the stack tree and a separate mode-0600 copy of `.env` before a structural
upgrade. Exclude `data/` from the tree archive. To roll back, restore the files and `.env`, then run
`bash ops/deploy.sh`. Application data stays in place; use restic only when the data itself needs
recovery.

Never run two connectors for the same remotely managed tunnel during rollback.

## Disk pressure

```sh
df -h /
du -sh data/aiostreams /var/lib/docker 2>/dev/null
docker system df
```

Deploy refuses to start below 10 GiB free, and the probe fails below 6 GiB. Identify the consumer
before pruning Docker data or rollback copies.
