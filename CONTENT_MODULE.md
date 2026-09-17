# "Content" module — mods, packs and modpacks (fork feature)

Adds in-panel content management to Crafty: identify installed mods by hash on
**Modrinth** (sha1) and **CurseForge** (murmur2 fingerprint), find and apply
updates for the server's MC version + loader, search and install new mods /
resource packs / shaders / datapacks, and — since the modpack browser —
**install whole modpacks** into an existing server or **create a new server
from a modpack**. Source order: Modrinth first, CurseForge as fallback (its
text search is restricted for standard API keys).

## Files

| File | Role |
|---|---|
| `app/classes/minecraft/content_manager.py` | Engine (stdlib only): `scan / plan / apply / search / install / versions / modpack_search / cf_slug_to_id`, MC + loader autodetect |
| `app/classes/minecraft/modpack_installer.py` | `ModpackInstaller`: parse `.mrpack` / CurseForge `manifest.json` zips (`resolve*`) and install them (`install`) |
| `app/classes/web/routes/api/servers/server/content.py` | `POST /api/v2/servers/<id>/content` — per-server actions (needs the server **Files** permission) |
| `app/classes/web/routes/api/crafty/modpack.py` | `POST /api/v2/crafty/modpack` — modpack browsing for the creation wizard (needs **Server Creation**) |
| `app/classes/shared/main_controller.py` | `create_type=modpack` server creation (`_prepare_modpack_create`, `_t_create_modpack_server`) + loader build installers |
| `app/frontend/templates/panel/server_content.html` | "Content" tab: *Installed* pane + *Add content* pane |
| `app/frontend/templates/server/wizard.html` | "Create from a modpack" card |
| `app/frontend/static/assets/js/shared/mccm-common.js`, `mccm-modpack.js`, `css/mccm.css` | shared UI code |
| `app/config/content.json.example` | template for the CurseForge API key |

## CurseForge API key

Only needed for CurseForge-exclusive content and CurseForge modpacks. Modrinth
works without a key.

```bash
export CURSEFORGE_API_KEY=your_key          # environment variable wins
# or
cp app/config/content.json.example app/config/content.json   # and fill it in
```

CurseForge projects with *"allow mod distribution"* disabled cannot be
downloaded through the API; they are listed as **blocked** with a link to the
project page so you can add them by hand. The install still completes.

## Per-server API — `POST /api/v2/servers/<server_id>/content`

Body is JSON with an `action`. Optional `mc` / `loader` override the detected
server context (detected from `libraries/` first, then from the execution
command Crafty stores for the server).

| `action` | body | effect |
|---|---|---|
| `context` | — | `{mc, loader, mc_detected, loader_detected, cf_enabled}` |
| `scan` / `cached` / `scan_one` | — / — / `filename` | unified inventory (source, side, update, channel…) |
| `plan` | `channel` (`release`/`beta`/`alpha`) | list of available updates, nothing written |
| `apply` | `channel`, `confirm:true` | download → verify hash → backup to `mods/_backup/<ts>/` → replace |
| `search` | `query`, `type` (`mod`/`resourcepack`/`shader`/`datapack`), `limit` | search Modrinth |
| `install` / `install_version` | `id`/`slug` (+ `version_id`) | install a project (required deps for mods too) |
| `detail` / `versions` | `source`, `id` | project card / version list |
| `toggle` / `remove` / `update_one` | `filename` | per-file actions |
| `modpack_search` | `query`, `limit`, `mc`, `loader`, `source` | modpacks from Modrinth (+ CurseForge with a key); `cf_enabled` says whether CF was queried |
| `modpack_versions` | `source`, `id` | versions with `game_versions`, `loaders`, `filename` |
| `modpack_resolve` | `source`, `id`, `version_id` | downloads + parses the pack; returns `{name, version, mc, loader, loader_build, file_count, total_bytes, blocked, skipped, mismatch}` |
| `modpack_install` | `source`, `id`, `version_id`, `mode` (`merge`/`replace`), `confirm:true` | starts the install in a background thread (409 `JOB_RUNNING` if one is active) |
| `modpack_status` | — | `.content_cache/modpack_install.json` + `running` |

`mode=merge` adds the pack on top of what is there (files with the same hash
are skipped); `mode=replace` first moves the current `mods/` to
`mods/_backup/<timestamp>/`. Restart the server afterwards.

Example:

```bash
curl -X POST https://YOUR_CRAFTY/api/v2/servers/<id>/content \
  -H "Authorization: Bearer <token>" -H "Content-Type: application/json" \
  -d '{"action":"modpack_install","source":"modrinth","id":"AuI3VLGI","version_id":"VjrstANB","mode":"merge","confirm":true}'
```

## Creating a server from a modpack

Wizard: **Create New Server → Create from a modpack** — search, paste a
`modrinth.com/modpack/…` or `curseforge.com/minecraft/modpacks/…` link, or
upload a `.mrpack` / CurseForge `.zip`. Crafty reads the pack's Minecraft
version + loader, creates the server, installs that exact loader build
(NeoForge / Forge / Fabric; Quilt is not supported by the loader installer)
and downloads all server-side files, showing the usual "Importing…" state.

API: `POST /api/v2/servers` with

```json
"minecraft_java_create_data": {
  "create_type": "modpack",
  "modpack_create_data": {
    "source": "modrinth",            // modrinth | curseforge | upload
    "project_id": "AuI3VLGI",        // or "archive_name" for source=upload
    "version_id": "VjrstANB",
    "pack_token": "<from /api/v2/crafty/modpack modpack_resolve, optional>",
    "loader_build": "",              // optional override
    "mem_min": 2, "mem_max": 4, "server_properties_port": 25565,
    "agree_to_eula": true
  }
}
```

`POST /api/v2/crafty/modpack` supports `modpack_search`, `modpack_versions`,
`modpack_resolve` (returns a `pack_token` so the downloaded archive is reused
by the create call) and `cf_slug` (CurseForge slug → id).

## Sharing a pack: server pack export

**Content → Installed → Export server pack** zips the server-side mods
(client-only ones removed by Modrinth hash lookup), `config/`, `kubejs/`,
`defaultconfigs/`, … plus the loader installer / launcher, `run.sh`/`run.bat`,
a `README-SERVER.txt` and a `server-pack.json` manifest — **no world, no
player data, no server.properties**. The zip lands in
`.content_cache/exports/` and downloads through the files API
(`action: modpack_export` on the content endpoint).

The recipient can either run it as a plain server (unzip → run the installer
→ `eula=true` → `run.sh`) or upload it in this fork's wizard, which recognises
`server-pack.json` and builds the server from it.

## Supported archive formats (wizard upload / `import/upload`)

| Format | Detected by | Files |
|---|---|---|
| Modrinth pack | `modrinth.index.json` | downloaded (hash verified) + overrides |
| CurseForge pack | `manifest.json` | downloaded via CF API (key required) + overrides |
| Prism / MultiMC instance export | `mmc-pack.json` | all local: game dir copied minus client clutter; singleplayer saves offered as importable world |
| Crafty server pack | `server-pack.json` | all local |

In every case jars are hash-checked against Modrinth and client-only projects
are dropped. Large archives can be copied straight into `import/upload/`
(`docker/import/upload/` with the compose file) and picked from the wizard's
"Upload file" tab instead of going through the browser.

## Safety

- Every path from a pack index or archive is validated (no `..`, no absolute
  paths, must resolve inside the server directory); symlink entries are dropped.
- Downloads are https-only from Modrinth's allowed hosts
  (`cdn.modrinth.com`, `github.com`, `raw.githubusercontent.com`, `gitlab.com`)
  and the CurseForge CDN; sha1 is verified when the index provides it.
- Archive size / entry count are capped; the CurseForge key never reaches the browser.
- No downgrades in `plan`; beta/alpha channels are opt-in; every replacement is backed up.

## Tests

`python -m pytest tests/classes/minecraft tests/classes/web/api/test_modpack_create_schema.py -q`
