# Client setup

This guide connects player apps to a running stack. It assumes that `make verify` passes. The
examples use `aio.example.com` as the public hostname.

## Get the URLs

Run this command in the stack directory. The `--dry-run` option makes sure that the command
changes nothing.

```sh
python3 ops/bootstrap.py --dry-run --print-urls
```

The command prints three URLs:

- `server_url` is the server address for Jellyfin apps. It contains no secret.
- `picker_url` signs a Jellyfin app in without a password.
- `stremio_manifest_url` installs the addon in Stremio and Nuvio.

The picker URL and the manifest URL give access to the full configuration. The picker URL
opens every profile that has no PIN. Both URLs stream through your TorBox account. Give them
only to people in your household, in a private message.

## Jellyfin apps

Use this method for Odin, Infuse, Swiftfin, Findroid, Streamyfin, and Jellyfin for Android TV.

1. Add a Jellyfin server in the app.
2. Enter the picker URL as the server address. The app shows the household profiles.
3. Select a profile.

If the app does not accept the picker URL, use the manual sign-in. The values come from `.env`.

1. Enter `server_url` as the server address, for example `https://aio.example.com/jellyfin`.
2. Enter `<AIOSTREAMS_UUID>/<profile>` as the username, for example `<AIOSTREAMS_UUID>/Sam`.
3. Enter `AIOSTREAMS_PASSWORD` as the password. If the profile has a PIN, enter
   `<AIOSTREAMS_PASSWORD>/<pin>`.

Each profile has its own watch history. All profiles use the settings of the one configuration.

## Stremio and Nuvio

These apps use the Stremio addon protocol. They do not use the profiles.

1. Open the addon settings in the app.
2. Paste `stremio_manifest_url` into the addon URL field.
3. Install the addon.

## Player settings

- Set the preferred audio language in each app. The app selects the audio track, not the server.
- Remove other stream addons from Stremio and Nuvio. These addons duplicate the stream list.
- Use a native app. Web browsers cannot play many MKV and HEVC files.

## Revoke access

A password change stops both URLs for every person. Do these steps in this order:

1. Change the password with the **Change Password** option on the AIOStreams configure page.
2. Put the new password in `AIOSTREAMS_PASSWORD` in `.env`. Then run `make env HOST=user@host`.
3. Run `python3 ops/bootstrap.py --dry-run --print-urls`.
4. Give the new URLs to each person who still needs access.
