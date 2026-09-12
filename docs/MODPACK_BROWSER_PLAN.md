# Plan: Modpack + Mod download UI for Crafty 4 (luishidalgoa fork)

> **Status (2026-09-12): implemented** on `feature/modpack-browser` — see `CONTENT_MODULE.md` for the resulting API/UI. Deviations from this plan: path validation lives in `modpack_installer.safe_join` (stdlib, same semantics as `Helpers.validate_traversal`) to keep the engine self-contained; the wizard uses one `POST /api/v2/crafty/modpack` action endpoint instead of three routes; `create_type=modpack` is rewritten into a `download_jar` creation inside `create_api_server` so the existing command/registration code is reused.

Branch: `feature/modpack-browser` · Fork: `Ehrenmarmelade/crafty-4` · Upstream: `luishidalgoa/crafty-4`

## 0. Current state (verified 2026-09-12)

What the fork already has (commit `ab7adc8b` + `7442392b`):

| Layer | File | What it does |
|---|---|---|
| Engine | `app/classes/minecraft/content_manager.py` | `ContentManager(server_path, mc, loader, cf_key)` — stdlib only. `scan/plan/apply/search/install/detail/versions/install_version/toggle/remove/update_one`. Modrinth first, CurseForge (murmur2 fingerprint) fallback. Autodetects MC + loader from `libraries/net/{neoforged,minecraftforge,fabricmc}`. |
| API | `app/classes/web/routes/api/servers/server/content.py` | `POST /api/v2/servers/<id>/content` — single handler, `body.action` dispatch, needs server **FILES** permission, runs blocking work in `loop.run_in_executor`. |
| Routing | `app/classes/web/routes/api/api_handlers.py:403` | route registered |
| Page | `app/frontend/templates/panel/server_content.html` | "Content" tab: *Installed* pane (scan/plan/apply/toggle/remove) + *Add content* pane (`#mccm-type` = mod/resourcepack/shader/datapack → search cards → Install / detail modal with version picker). Resolved via `panel_handler.py:930` `f"panel/server_{subpage}.html"`. |
| JS/CSS | `app/frontend/static/assets/js/shared/mccm-common.js`, `app/frontend/static/assets/css/mccm.css` | `mccmApi()` fetch wrapper, install-version button handling |
| Nav | `app/frontend/templates/panel/parts/server_controls_list.html:40-45` | desktop tab (gated on Files perm) |
| Perms | `app/classes/web/panel_handler.py:38` | `SUBPAGE_PERMS["content"] = FILES` |
| Loader mgr | `app/classes/shared/main_controller.py:1258-1530` | `get_loader_builds(loader, mc)`, `_install_modded_build`, `_install_fabric_build`, `_t_create_loader_build` (NeoForge/Forge/Fabric) — used by wizard + Update Center |

### Gaps / bugs found

1. **No modpack support anywhere** — `search()` only accepts `mod|resourcepack|shader|datapack`; no `.mrpack` / CurseForge-zip parsing; no "create server from modpack".
2. **Content tab missing from mobile nav** — `app/frontend/templates/panel/parts/m_server_controls_list.html` was never updated, so on narrow screens the tab is invisible ("no UI").
3. `CONTENT_MODULE.md` still says *Frontend: pendiente* and references `app/config/content.json.example`, which does not exist in the repo.
4. UI strings are hardcoded (mixed EN/ES), not using `translate('page','key',lang)` like the rest of the panel.
5. `mc`/`loader` autodetect falls back to `1.21.1`/`neoforge` silently when nothing is found (vanilla/paper servers get modded search facets).

## 1. Target UX

**A. Content tab → "Add content" → type `modpack`** (existing server)
- Search Modrinth modpacks (CurseForge if key configured). Cards show icon, name, downloads, MC versions, loaders.
- Click → version picker (filtered to the server's MC + loader by default, toggle "show all").
- "Install into this server" with mode **merge** (add on top) or **replace** (backup `mods/` → `mods/_backup/<ts>/`, then clear). If pack MC/loader ≠ server's autodetected → red warning, requires explicit confirm.
- Progress panel (n/total, current file, blocked list for CF `allowModDistribution=false`), polled every 2 s; on finish → reload Installed pane.

**B. Server creation wizard → Minecraft Java → new option "From modpack"** (new server)
- Three inputs: (1) Modrinth search box + cards, (2) paste a Modrinth/CurseForge modpack URL, (3) upload `.mrpack` / CurseForge `.zip`.
- After selection show resolved: pack name, version, **MC version**, **loader + build**, file count, server-side file count. Optional loader-build override (reuse the fork's build selector).
- Name / port / memory / EULA as in the normal flow → **Create**. Server appears with the normal "Importing…" state until loader + files are installed.

**C. Fix mobile nav** so the Content tab shows everywhere.

## 2. Backend work

### 2.1 `content_manager.py` — modpack search/metadata
- `search(query, project_type, limit)`: accept `"modpack"`. For modpacks do **not** hard-filter on `versions:<mc>` unless caller passes `mc` explicitly (a pack defines its own MC version); loader facet optional.
- `versions(source, ident, all_versions, project_type)`: make sure it returns `game_versions`, `loaders`, `version_type`, `date_published`, primary file `{url, filename, hashes, size}` for modpacks.
- Add `cf_search_modpacks(query, limit)` → `GET /v1/mods/search?gameId=432&classId=4471&searchFilter=…` (key-gated; if 403/empty → return `[]`, do not raise).
- Fix gap 5: if autodetect finds nothing, set `self.loader = None` and don't add loader facets; expose in `context` action.

### 2.2 New `app/classes/minecraft/modpack_installer.py` (stdlib only, like content_manager)
```
class ModpackInstaller:
    def __init__(self, server_path, cf_key="", progress_cb=None)
    resolve(source, ident, version_id) -> PackInfo      # download index only, no files
    resolve_archive(path) -> PackInfo                   # local .mrpack / .zip
    install(pack: PackInfo, mode="merge") -> Result      # download files + overrides
PackInfo: name, version, mc, loader ("fabric"|"forge"|"neoforge"|"quilt"), loader_build,
          files[{path, url(s), sha1, sha512, size, server_side: bool}],
          overrides_dirs[], blocked[{name, project_id, file_id, website_url}]
```
- **mrpack**: `modrinth.index.json` → `dependencies.minecraft`, `dependencies.{fabric-loader|forge|neoforge|quilt}`; `files[]` with `path`, `hashes`, `downloads[]`, `env.server` (skip `unsupported`); apply `overrides/` then `server-overrides/`.
- **CurseForge zip**: `manifest.json` → `minecraft.version`, `minecraft.modLoaders[].id` (`forge-47.3.0`, `neoforge-21.1.65`, `fabric-0.16.9`), `files[{projectID,fileID,required}]`, `overrides`. Resolve URLs in batches via `POST /v1/mods/files {"fileIds":[…]}`; `downloadUrl == null` → append to `blocked[]` (never fail the whole install). Fallback URL pattern `https://edge.forgecdn.net/files/{id//1000}/{id%1000}/{fileName}` only if the API returns the fileName.
- Security: every `path` must pass `Helpers.validate_traversal(server_path, resolved)`; download hosts restricted to `cdn.modrinth.com`, `edge.forgecdn.net`, `mediafilez.forgecdn.net`, `github.com`/`raw.githubusercontent.com` (Modrinth's allowed list); https only; sha1/sha512 verified when present; cap archive size (e.g. 512 MiB) and entry count; reject symlinks in overrides.
- Progress: `progress_cb(dict)` called per file; installer also writes `<server>/.content_cache/modpack_install.json` `{status: queued|running|done|error, total, done, current, blocked, errors, started, finished}` so the UI can poll after page reloads.
- Reuse `_download()` / `_http_json()` from `content_manager.py` (move both into a small `app/classes/minecraft/content_http.py` if that avoids a circular import).

### 2.3 `content.py` — new actions (same FILES permission)
| action | body | returns |
|---|---|---|
| `modpack_search` | `query, source?, limit?` | hits (normalised: `{source,id,slug,title,icon,downloads,mc_versions,loaders,url}`) |
| `modpack_versions` | `source, id, all?` | versions list (see 2.1) |
| `modpack_resolve` | `source, id, version_id` | `PackInfo` summary + `mismatch: {mc, loader}` vs server context |
| `modpack_install` | `source, id, version_id, mode, confirm:true` | `{job: "<id>"}` — starts a `threading.Thread`, returns immediately |
| `modpack_status` | — | contents of `modpack_install.json` |
Only one modpack job per server at a time (409 if running). On completion broadcast `send_start_reload` via `WebSocketManager().broadcast_to_server_users(server_id, …)` like `_t_create_loader_build`.

### 2.4 Create server from modpack
- **Schema** `app/classes/web/routes/api/servers/index.py` (`minecraft_java_create_data`): add `"modpack"` to the `create_type` enum (line ~202) and a `modpack_create_data` object: `source (modrinth|curseforge|upload)`, `project_id?`, `version_id?`, `archive_name?` (file already in `import/upload`, same as `import_server`), `loader_build?`, `mem_min`, `mem_max`, `server_properties_port`, `agree_to_eula`. Extend the `allOf` if/then block (lines ~365-390).
- **`main_controller.create_api_server`**: new branch `root_create_data["create_type"] == "modpack"`:
  1. `ModpackInstaller.resolve(...)` **before** creating the DB record (needs `mc` + `loader` to build `server_file` / execution command). Map loader → `type`: `fabric→"fabric"`, `forge→"forge-installer"`, `neoforge→"neoforge-installer"`; `quilt` → return 400 `UNSUPPORTED_LOADER` (existing loader installer doesn't do Quilt).
  2. Create record exactly like `download_jar` with that type/version, EULA, server.properties.
  3. Start `_t_create_modpack_server(server_id, path, pack, loader_build)` thread: `ServersController.set_import` → `_install_modded_build` / `_install_fabric_build` with pack's build (fallback: latest from `get_loader_builds`) → `ModpackInstaller.install(pack, mode="merge")` → `finish_import` → broadcast reload. Wrap in try/except + `logger.exception` as the existing thread does.
- **Upload**: reuse the existing zip upload endpoint used by `import_server` (`import/upload`); accept `.mrpack` in the allowed extensions.
- New `GET /api/v2/crafty/modpack/search` + `/api/v2/crafty/modpack/<source>/<id>/versions` + `POST /api/v2/crafty/modpack/resolve` for the wizard (needs `SERVER_CREATION` crafty permission, no server id yet). Put in `app/classes/web/routes/api/crafty/modpack.py`, register in `api_handlers.py` next to `loader_builds`.

## 3. Frontend work

- `server_content.html` + `mccm-common.js`: add `<option value="modpack">` to `#mccm-type`; when modpack: card button = "Install into server…" → modal (version select, mode merge/replace, mismatch warning, confirm) → `modpack_install` → progress panel polling `modpack_status` → on `done` re-run `scan`. Use existing card markup/`mccm.css`, Phosphor icons (`ph ph-package`), Bootstrap 4.
- `server/wizard.html`: add radio/tab "From modpack" under Minecraft Java (next to Download jar / Import). Three inputs (search / URL / upload). Parse URLs: `modrinth.com/modpack/<slug>`, `curseforge.com/minecraft/modpacks/<slug>` (CF slug → id via `/v1/mods/search?slug=`). Show resolved summary; reuse loader-build `<select>` from `7442392b`. Submit `create_type: "modpack"`.
- `m_server_controls_list.html`: add the Content dropdown-item (copy of the desktop one, class `dropdown-item`).
- Translations: add `serverContent` and `serverWizard.modpack*` keys to `app/translations/en_EN.json`; use `translate(...)` for all **new** strings (fallback to en_EN is automatic, `translation.py:24`). Optionally migrate the existing hardcoded Content-tab strings.
- Never expose the CF key to the browser; `modpack_search` returns `cf_enabled: bool` so the UI can show "CurseForge: add API key" hint.

## 4. Docs / housekeeping
- Add `app/config/content.json.example` (`{"curseforge_api_key": ""}`) and make sure `app/config/content.json` is in `.gitignore`.
- Rewrite `CONTENT_MODULE.md` (English), drop the stale "pendiente" section, document modpack actions + wizard flow + env var.
- `CHANGELOG.md`: entry under a new unreleased section.

## 5. Tests & quality gates
- `tests/classes/minecraft/test_modpack_installer.py`: fixtures for a minimal `modrinth.index.json` and CF `manifest.json`; monkeypatch `_http_json`/`_download`; cover: loader parsing, `env.server=unsupported` skip, traversal rejection (`../x.jar`), blocked CF files, merge vs replace backup, sha mismatch.
- `tests/classes/web/...`: schema accepts `modpack` create_type, rejects quilt.
- `black --check app`, `pylint app` (config in repo, see `.gitlab-ci.yml`), `python -m pytest tests -q` must be green before each commit.
- Manual: run `python main.py`, create a Fabric server from a small Modrinth pack, then install a second pack in **merge** mode on the Content tab; verify progress + blocked list; test on a 400 px wide viewport that the Content tab appears.

## 6. Commit sequence (one PR, small commits)
1. `fix(content): add Content tab to mobile nav, add content.json.example, refresh docs`
2. `feat(content): modpack search + versions in ContentManager`
3. `feat(content): ModpackInstaller (mrpack + CurseForge manifest) with tests`
4. `feat(api): modpack_* actions on /servers/<id>/content`
5. `feat(ui): modpack type in Content tab with install modal + progress`
6. `feat(server-create): create_type=modpack schema + controller + installer thread`
7. `feat(ui): "From modpack" option in server wizard`
8. `docs: CONTENT_MODULE.md + CHANGELOG`
