import copy
import json
import re
from urllib.parse import quote, urlsplit

TOKEN = "{MDBLIST_API_KEY}"
HOST = "1fe84bc728af-stremio-mdblist.baby-beamup.club"
ROWS = {
    "gac": ("3106", "movie", "action-movie", "Action Movies"),
    "gco": ("3107", "movie", "comedy-movie", "Comedy Movies"),
    "gcs": ("3122", "series", "comedy-shows-series", "Comedy Shows"),
    "gcr": ("3108", "movie", "crime-movie", "Crime Movies"),
    "gks": ("3126", "series", "crime-shows-series", "Crime Shows"),
    "gdr": ("3105", "movie", "drama-movie", "Drama Movies"),
    "gfa": (
        "2415",
        "movie",
        "latest-hd-family-movies-top-rated-from-1980-to-today-movie",
        "Family Movies",
    ),
}


def string_values(value, path=()):
    if isinstance(value, str):
        yield path, value
    elif isinstance(value, dict):
        for name, item in value.items():
            yield from string_values(item, (*path, name))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from string_values(item, (*path, index))


def contains_host_url(value):
    return HOST in value.lower()


def managed_segment(preset):
    host_paths = [
        path for path, value in string_values(preset) if contains_host_url(value)
    ]
    identifier = preset.get("instanceId")
    if identifier not in ROWS and not host_paths:
        if TOKEN in json.dumps(preset):
            raise SystemExit("MDBList token is outside a managed preset")
        return None
    if host_paths != [("options", "manifestUrl")]:
        raise SystemExit("MDBList URL must occur only in the managed manifestUrl")
    url = (preset.get("options") or {}).get("manifestUrl", "")
    try:
        parsed = urlsplit(url)
    except ValueError:
        raise SystemExit("invalid MDBList manifest URL") from None
    if (
        identifier not in ROWS
        or preset.get("type") != "custom"
        or parsed.scheme != "https"
        or parsed.netloc != HOST
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or "?" in url
        or "#" in url
    ):
        raise SystemExit("invalid managed MDBList manifest URL")
    match = re.fullmatch(r"/([0-9]+)/([^/]+)/manifest\.json", parsed.path)
    if match is None or match[1] != ROWS[identifier][0]:
        raise SystemExit("invalid MDBList list binding")
    return match[2]


def replace_strings(value, key):
    if isinstance(value, str):
        return value.replace(TOKEN, quote(key, safe=""))
    if isinstance(value, list):
        return [replace_strings(item, key) for item in value]
    if isinstance(value, dict):
        return {name: replace_strings(item, key) for name, item in value.items()}
    return value


def configure_rows(config, key, state="on"):
    if state not in ("", "on", "off"):
        raise SystemExit("MDBLIST_ROWS must be on or off")
    desired = copy.deepcopy(config)
    seen = set()
    for preset in desired.get("presets") or []:
        segment = managed_segment(preset)
        if segment is None:
            continue
        identifier = preset["instanceId"]
        if identifier in seen:
            raise SystemExit("duplicate managed MDBList preset")
        seen.add(identifier)
        if segment != TOKEN or preset["options"]["manifestUrl"].count(TOKEN) != 1:
            raise SystemExit("managed MDBList URL requires exactly one token")
        _, media_type, catalog_id, name = ROWS[identifier]
        bound_id = identifier + "e3b0." + catalog_id
        matches = [
            row
            for row in desired.get("catalogModifications") or []
            if row.get("id", "").startswith(identifier + "e3b0.")
        ]
        if (
            len(matches) != 1
            or matches[0].get("id") != bound_id
            or matches[0].get("type") != media_type
            or matches[0].get("name") != name
        ):
            raise SystemExit("invalid MDBList catalog binding")
        if state == "off":
            preset["enabled"] = False
        if preset.get("enabled") and not key:
            raise SystemExit("MDBLIST_API_KEY is required by the template")
    desired["presets"] = (
        replace_strings(desired.get("presets") or [], key)
        if key
        else desired.get("presets") or []
    )
    if key and TOKEN in json.dumps(desired.get("presets")):
        raise SystemExit("MDBList token substitution failed")
    return desired


def tokenized_config(config, key=""):
    desired = copy.deepcopy(config)
    for preset in desired.get("presets") or []:
        segment = managed_segment(preset)
        if segment is not None:
            identifier = preset["instanceId"]
            preset["options"]["manifestUrl"] = (
                f"https://{HOST}/{ROWS[identifier][0]}/{TOKEN}/manifest.json"
            )
    for preset in desired.get("presets") or []:
        segment = managed_segment(preset)
        if segment is not None and segment != TOKEN:
            raise SystemExit("snapshot contains an MDBList credential")
    serialized = json.dumps(desired)
    if key and (key in serialized or quote(key, safe="") in serialized):
        raise SystemExit("snapshot contains an MDBList credential")
    return desired


def restored_config(config, key):
    desired = tokenized_config(config)
    managed = any(
        managed_segment(preset) is not None for preset in desired.get("presets") or []
    )
    if managed and not key:
        raise SystemExit("MDBLIST_API_KEY is required to restore the snapshot")
    if "presets" in desired:
        desired["presets"] = replace_strings(desired["presets"], key)
    return desired
