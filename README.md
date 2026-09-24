# aiostreams-stack

This repository deploys AIOStreams with Docker Compose. AIOStreams runs in native Jellyfin API
mode, so Jellyfin apps and Stremio apps can both connect to it. The default Compose project is
`media-gateway`. The stack keeps its SQLite data in `data/aiostreams`.

To connect player apps after deployment, read [docs/SETUP.md](docs/SETUP.md).

## Prerequisites

- A TorBox account and API key
- A free TMDB API key
- A domain on Cloudflare and a remotely managed Cloudflare tunnel
- A Debian or Ubuntu host with at least 10 GiB of free disk space
- Docker Engine with Compose 2.24 or newer, restic, rsync, Python 3, and SSH

To install the host packages, run `ops/provision.sh` on the host as its non-root deploy user.

## Quickstart

1. Copy `.env.example` to `.env`. Replace every placeholder. Leave `AIOSTREAMS_UUID` and
   `AIOSTREAMS_PASSWORD` empty.
2. Copy `config/household.example.json` to `config/household.json`. Add one entry for each person.
3. Run `make sync HOST=user@host`. Then run `make env HOST=user@host`. The remote directory is
   `~/apps/media-gateway`. To use a different directory, set `REMOTE`.
4. In the tunnel, route the public hostname (for example `aio.example.com`) to
   `http://aiostreams:3000`. Add a catch-all route that returns HTTP 404. Do not publish a host
   port.
5. Run `make deploy HOST=user@host`. Then run `make bootstrap`.
6. The bootstrap prints `AIOSTREAMS_UUID` and `AIOSTREAMS_PASSWORD` one time only. Put both
   values in `.env`. Then run `make env HOST=user@host` again.
7. Run `make verify HOST=user@host`.

To stop a deployment that changes the rendered Compose configuration, run
`make deploy EXPECTED_COMPOSE_SHA256=<hash>`. To get the hash, run
`bash ops/deploy.sh --print-config-sha` or `ops/compose-config-sha.sh <stack-dir>` on the host.
These commands change nothing.

## Traffic

The host answers API calls for metadata, search, configuration, and the Jellyfin API. For video,
AIOStreams gives the player a direct debrid URL. Thus, video data does not usually go through the
host. `make verify` checks this. It measures the network traffic of the AIOStreams container
during a 64 MiB download.

The tunnel is remotely managed. Obey these rules:

- Before you change ingress or DNS, export the full tunnel configuration.
- Keep account and tunnel identifiers out of this repository.
- Allow only the public hostname and the catch-all 404 route.

## Household bootstrap

`ops/bootstrap.py` makes the primary configuration when `AIOSTREAMS_UUID` is empty. It uses
`config/aiostreams.template.json` and adds the TorBox key. It also makes one profile for each
person in `config/household.json`.

When a configuration exists, the script changes only the native Jellyfin settings and the
profiles. It keeps profile fields that it does not manage, such as PINs. To also apply the
template presets, filters, languages, sorting, and service settings, add `--apply-template`.
This option keeps all service credentials.

- `--dry-run` shows the changes and writes nothing.
- `--print-urls` prints the client URLs. Without `--dry-run`, the script also applies its changes
  first. See [docs/SETUP.md](docs/SETUP.md).

## Smoke test

`make smoke-up` starts a separate test stack in `.smoke`. The test stack has no tunnel. It
listens only on `127.0.0.1:38080`. The test makes sure that only the expected container and port
exist. Then it waits for health and makes a new configuration. It reads only the TorBox key and
the TMDB key from the local `.env`.

To remove the test stack, run `make smoke-down`. On a host without `make`, run
`./ops/smoke.sh up .smoke 38080` and `./ops/smoke.sh down .smoke 38080`. If the container
restarts in a loop, the test stops and prints the last log lines.

## Known limits

- Web browsers cannot play many MKV, HEVC, and DTS files. Use a native app.
- The `tmdb-addon` profile is off. Its pinned release needs MongoDB. AIOStreams uses its public
  metadata source.
- Some scraper providers block data center IP ranges. Results can change with the host network.
- Streams from the public Comet and StremThru instances go through those services to the debrid
  CDN. A new stream can start only when those services are available. A self-hosted StremThru
  removes this dependency.
- The player selects the audio track. Set the preferred audio language in each app.
- AIOStreams rate limits are on. The server allows 10 sign-in attempts per IP address in 5
  minutes. The server reads the client IP address from the forwarding headers only when the
  request comes from a private network, such as the tunnel container.

## Backups

The user-level `media-gateway-backup.timer` starts each night at 02:30. It stops AIOStreams,
saves `data/aiostreams` to the restic repository, and starts AIOStreams again. It keeps 7 daily,
4 weekly, and 6 monthly snapshots. When a SQLite database exists, each deployment also makes a
pre-upgrade snapshot. The stack keeps the last five of these snapshots.

Before each backup and restore, the scripts remove stale restic locks. Thus, an interrupted run
does not stop the next backup.

To list the snapshots, run `make restore-check`. To restore, run
`./backup/restore.sh <snapshot-id>` on the host. The restore moves the old data to
`restore-rollback/`. Remove that copy after you check the restore. For routine operations and
recovery, read [RUNBOOK.md](RUNBOOK.md).

## Configuration

`HOST`, `REMOTE`, and `PROJECT` are Make variables. Their defaults are compatible with
production: `~/apps/media-gateway` for `REMOTE` and `media-gateway` for `PROJECT`. Git ignores
`.env`, `config/household.json`, application data, lock files, and restore directories.
