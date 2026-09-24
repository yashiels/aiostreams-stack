# aiostreams-stack

A reusable Docker Compose deployment for AIOStreams in native Jellyfin API mode. The default
Compose project is `media-gateway`; its persistent SQLite data lives in `data/aiostreams`.

## Prerequisites

- A TorBox account and API key
- A free TMDB API key
- A Cloudflare-managed domain and remotely managed tunnel
- A Debian or Ubuntu host with at least 10 GiB free
- Docker Engine with Compose 2.24 or newer, restic, rsync, Python 3, and SSH

Run `ops/provision.sh` on the host as its non-root deploy user to install the host packages.

## Quickstart

1. Copy `.env.example` to `.env`, replace every placeholder, and leave
   `AIOSTREAMS_UUID` and `AIOSTREAMS_PASSWORD` empty. Copy
   `config/household.example.json` to `config/household.json` and edit the household.
2. Run `make sync HOST=user@host`, then `make env HOST=user@host`. Both destinations default to
   `~/apps/media-gateway`; override `REMOTE` when needed.
3. Configure the remote-managed tunnel hostname, such as `aio.example.com`, to route to
   `http://aiostreams:3000`, with a catch-all route returning HTTP 404. Do not publish a host port.
4. Run `make deploy HOST=user@host`, then `make bootstrap`. Store the one-time
   `AIOSTREAMS_UUID` and `AIOSTREAMS_PASSWORD` output in `.env` and run `make env` again.
5. Run `python3 ops/bootstrap.py --print-urls` when you need the server and picker URLs, then run
   `make verify HOST=user@host`. Treat the picker URL as a secret because it contains a reusable
   encrypted credential.

`make deploy EXPECTED_COMPOSE_SHA256=<hash>` refuses all deployment changes when the rendered
Compose configuration differs from the expected hash. Obtain the read-only hash on a host with
`bash ops/deploy.sh --print-config-sha` or run `ops/compose-config-sha.sh <stack-dir>`.

## Traffic model

The host handles AIOStreams, metadata, search, configuration, and Jellyfin-compatible API calls.
Video bytes are returned as direct debrid URLs and do not normally pass through this server. The
verification tool checks this by comparing the AIOStreams container transmit counter around a
64 MiB ranged download.

The tunnel is remotely managed. Export its complete configuration before changing ingress or DNS,
keep account and tunnel identifiers outside this repository, and allow only the chosen public
hostname plus the catch-all 404 route.

## Household bootstrap

`ops/bootstrap.py` creates a primary configuration from `config/aiostreams.template.json` when the
UUID is empty. It injects the TorBox key, creates the configured primary user and personas, and
prints the new UUID and plaintext password once. Existing configurations only converge native
Jellyfin settings and household personas by default, preserving unmanaged persona fields such as
PINs. Add `--apply-template` to converge the tuned presets, filters, language preferences, sorting,
and service settings while preserving every service credential.

Use `--dry-run` to inspect an existing configuration without writing. URLs are printed only with
`--print-urls`.

## Smoke test

`make smoke-up` creates an isolated `.smoke` environment, exposes only
`127.0.0.1:38080`, starts no tunnel container, asserts the container and port layout, waits for
health, and creates a fresh configuration. It reads only the TorBox and TMDB keys from the local
`.env`. Run `make smoke-down` to remove its containers, volume, and scratch directory.

On a host without `make`, call the script directly: `./ops/smoke.sh up .smoke 38080` and
`./ops/smoke.sh down .smoke 38080`. A crash-looping container fails the run immediately and
prints its last log lines.

## Known limits

- Browser players generally cannot play MKV containers or HEVC and DTS media directly.
- The disabled `tmdb-addon` profile requires MongoDB with its pinned release. It ships disabled;
  AIOStreams uses its public metadata fallback.
- Some scraper providers block datacenter IP ranges, so results vary by hosting network.

## Backups

The user-level `media-gateway-backup.timer` runs nightly at 02:30. It stops AIOStreams, snapshots
`data/aiostreams` to the configured restic repository, restarts the service, and retains 7 daily,
4 weekly, and 6 monthly snapshots. Deployments also keep the latest five pre-upgrade snapshots.

List snapshots with `make restore-check`. Restore on the host with
`./backup/restore.sh <snapshot-id>`. The displaced data remains under `restore-rollback/` until you
remove it after verification. See [RUNBOOK.md](RUNBOOK.md) for routine operations and recovery.

## Configuration

`HOST`, `REMOTE`, and `PROJECT` are overridable Make variables. The production-compatible defaults
for the remote directory and project remain `~/apps/media-gateway` and `media-gateway`. `.env`,
`config/household.json`, application data, lock files, and restore workspaces are ignored by Git.
