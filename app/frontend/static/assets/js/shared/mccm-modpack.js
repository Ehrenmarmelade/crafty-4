/* ===== Modpack browser: search -> versions -> resolve -> install/create =====
 * Shared by the server Content tab (install into an existing server) and the
 * server wizard (create a new server from a pack). Needs mccm-common.js
 * (mccmApi-style helpers: esc, badge, mccmStatus, mccmVerType, #mccm-modal).
 *
 * MccmModpack.init({
 *   api: (payload) => Promise<json>,   // POST {action: "modpack_*", ...}
 *   mode: "server" | "wizard",
 *   onSelect: (summary) => {},          // wizard: pack chosen
 *   progressEl: HTMLElement,            // server: where to render progress
 *   onDone: () => {},                   // server: install finished
 * });
 */
window.MccmModpack = (function () {
    const t = mccmT;
    let cfg = { api: null, mode: 'server', onSelect: null, progressEl: null, onDone: null };
    let pollTimer = null;

    function init(options) { cfg = Object.assign(cfg, options || {}); }

    function modalBody(html) {
        const modal = document.getElementById('mccm-modal');
        const body = document.getElementById('mccm-modal-body');
        if (!modal || !body) return null;
        modal.style.display = 'flex';
        body.innerHTML = html;
        return body;
    }
    function closeModal() {
        const modal = document.getElementById('mccm-modal');
        if (modal) modal.style.display = 'none';
    }
    function fmtBytes(n) {
        if (!n) return '';
        const u = ['B', 'KB', 'MB', 'GB'];
        let i = 0;
        while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
        return n.toFixed(i ? 1 : 0) + ' ' + u[i];
    }

    // ---------------------------------------------------------------- search
    function card(h) {
        const icon = h.icon
            ? `<img class="thumb" src="${esc(h.icon)}" onerror="this.style.visibility='hidden'">`
            : `<span class="thumb"></span>`;
        const loaders = (h.loaders || []).map(l => `<span class="badge badge-info">${esc(l)}</span>`).join(' ');
        const mcs = (h.mc_versions || []);
        const mcTxt = mcs.length > 3 ? mcs.slice(-3).join(', ') + ' +' + (mcs.length - 3) : mcs.join(', ');
        const dl = (h.downloads || 0).toLocaleString();
        return `<div class="mccm-card">
            ${icon}
            <div class="grow">
                <a href="#" class="mccm-mp-open" data-idx="${h._idx}"><b>${esc(h.title)}</b></a>
                <span class="text-muted"> ${t('by')} ${esc(h.author || '?')}</span> ${badge(h.source)}
                <div class="desc">${esc(h.summary || '')}</div>
                <div class="cats">${loaders}</div>
                <div class="meta">
                    <i class="ph ph-download-simple"></i> ${dl} ${t('downloads')} &nbsp;·&nbsp;
                    <i class="ph ph-cube"></i> ${esc(mcTxt || '?')}
                    ${h.url ? ` &nbsp;·&nbsp; <a href="${esc(h.url)}" target="_blank" rel="noopener">${esc(h.source)} ↗</a>` : ''}
                </div>
            </div>
            <div class="text-nowrap">
                <button class="btn btn-sm btn-outline-primary mccm-mp-open" data-idx="${h._idx}">
                    <i class="ph ph-list-bullets"></i> ${t('versions')}
                </button>
            </div>
        </div>`;
    }
    let lastHits = [];
    async function search(query, container, filters) {
        container.innerHTML = `<div class="text-muted"><i class="ph ph-spinner"></i> ${t('searching')}</div>`;
        const r = await cfg.api(Object.assign({ action: 'modpack_search', query: query || '', limit: 20 }, filters || {}));
        if (r.status !== 'ok') {
            container.innerHTML = `<div class="text-danger">${esc(r.error || 'error')}</div>`;
            return;
        }
        lastHits = (r.data.hits || []).map((h, i) => Object.assign(h, { _idx: i }));
        let html = lastHits.map(card).join('') || `<div class="text-muted">${t('noResults')}</div>`;
        if (!r.data.cf_enabled) html += `<div class="text-muted small mt-2"><i class="ph ph-info"></i> ${t('cfHint')}</div>`;
        container.innerHTML = html;
        container.querySelectorAll('.mccm-mp-open').forEach(el => el.onclick = function (ev) {
            ev.preventDefault();
            openVersions(lastHits[parseInt(this.dataset.idx, 10)]);
        });
    }

    // -------------------------------------------------------------- versions
    async function openVersions(hit) {
        const body = modalBody(`<div class="text-muted"><i class="ph ph-spinner"></i> ${t('loading')}</div>`);
        if (!body) return;
        const r = await cfg.api({ action: 'modpack_versions', source: hit.source, id: hit.id, all: true });
        const vs = (r.status === 'ok' && Array.isArray(r.data)) ? r.data : [];
        renderVersions(body, hit, vs);
    }
    function renderVersions(body, hit, vs) {
        const rows = vs.map((v, i) =>
            `<tr><td>${mccmVerType(v.type)}</td><td>${esc(v.name)}</td>` +
            `<td>${(v.game_versions || []).slice(0, 3).map(esc).join(', ')}</td>` +
            `<td>${(v.loaders || []).map(esc).join(', ')}</td>` +
            `<td class="text-muted">${(v.date || '').slice(0, 10)}</td>` +
            `<td><button class="btn btn-sm btn-outline-primary mccm-mp-pick" data-idx="${i}">${t('select')}</button></td></tr>`
        ).join('');
        const dl = (hit.downloads || 0).toLocaleString();
        body.innerHTML =
            `<div style="display:flex;gap:14px;align-items:center">` +
            (hit.icon ? `<img src="${esc(hit.icon)}" width="56" height="56" style="border-radius:8px">` : '') +
            `<div><h4 style="margin:0">${esc(hit.title)}</h4>` +
            `<div class="text-muted">${hit.author ? t('by') + ' ' + esc(hit.author) + ' · ' : ''}${dl} ${t('downloads')} · ${badge(hit.source)}` +
            `${hit.url ? ` · <a href="${esc(hit.url)}" target="_blank" rel="noopener">↗</a>` : ''}</div></div></div>` +
            `<p class="mt-2">${esc(hit.summary || '')}</p><hr>` +
            `<h6 class="mb-0">${t('versions')} (${vs.length})</h6>` +
            `<div style="max-height:320px;overflow:auto" class="mt-2"><table class="table table-sm"><thead><tr>` +
            `<th>${t('typeCol')}</th><th>${t('versionCol')}</th><th>${t('mcCol')}</th><th>${t('loaderCol')}</th><th>${t('dateCol')}</th><th></th></tr></thead>` +
            `<tbody>${rows || `<tr><td colspan="6" class="text-muted">${t('noResults')}</td></tr>`}</tbody></table></div>`;
        body.querySelectorAll('.mccm-mp-pick').forEach(b => b.onclick = () => resolvePack(hit, vs[parseInt(b.dataset.idx, 10)]));
    }

    // --------------------------------------------------------------- resolve
    async function resolvePack(hit, v) {
        const body = modalBody(`<div class="text-muted"><i class="ph ph-spinner"></i> ${t('resolving')}</div>`);
        if (!body) return;
        const r = await cfg.api({ action: 'modpack_resolve', source: hit.source, id: hit.id, version_id: v.id });
        if (r.status !== 'ok') {
            body.innerHTML = `<div class="text-danger">${esc(t('err_' + r.error) !== 'err_' + r.error ? t('err_' + r.error) : (r.error || 'error'))} ${esc(r.detail || '')}</div>`;
            return;
        }
        renderSummary(body, hit, v, r.data);
    }
    function summaryTable(s) {
        const row = (label, value) => `<tr><td class="text-muted" style="width:40%;white-space:nowrap">${label}</td><td>${value}</td></tr>`;
        return `<table class="table table-sm mb-2"><tbody>` +
            row(t('pack'), `${esc(s.name)} <span class="text-muted">${esc(s.version)}</span>`) +
            row(t('format'), t('fmt_' + (s.format || 'mrpack'))) +
            row(t('mcCol'), esc(s.mc)) +
            row(t('loaderCol'), esc(s.loader) + (s.loader_build ? ' <span class="text-muted">' + esc(s.loader_build) + '</span>' : '')) +
            row(t('files'), `${s.file_count} <span class="text-muted">${fmtBytes(s.total_bytes)}</span>` +
                ((s.skipped || []).length ? ` · <span class="text-muted">${t('skipped', { n: s.skipped.length })}</span>` : '')) +
            `</tbody></table>` + blockedList(s.blocked);
    }
    function blockedList(blocked) {
        if (!blocked || !blocked.length) return '';
        return `<div class="alert alert-warning py-2 mb-2"><b>${t('blocked')}</b><ul class="mb-0 pl-3">` +
            blocked.map(b => `<li>${b.url ? `<a href="${esc(b.url)}" target="_blank" rel="noopener">${esc(b.name)}</a>` : esc(b.name)}</li>`).join('') +
            `</ul></div>`;
    }
    function renderSummary(body, hit, v, s) {
        const mm = s.mismatch || {};
        let warn = '';
        if (mm.mc || mm.loader) {
            warn = `<div class="alert alert-danger py-2 mb-2"><i class="ph ph-warning"></i> ${t('mismatchWarn', {
                mc: s.mc, loader: s.loader, smc: mm.server_mc || '?', sloader: mm.server_loader || '?'
            })}</div>`;
        }
        let action;
        if (cfg.mode === 'wizard') {
            action = `<button id="mccm-mp-go" class="btn btn-primary"><i class="ph ph-check"></i> ${t('usePack')}</button>`;
        } else {
            action =
                `<div class="mb-2">
                    <label class="mr-3"><input type="radio" name="mccm-mp-mode" value="merge" checked> ${t('modeMerge')}</label>
                    <label><input type="radio" name="mccm-mp-mode" value="replace"> ${t('modeReplace')}</label>
                </div>
                <button id="mccm-mp-go" class="btn btn-primary"><i class="ph ph-download-simple"></i> ${t('installIntoServer')}</button>`;
        }
        body.innerHTML =
            `<h4 style="margin:0 0 .5rem">${esc(s.name || hit.title)} <small class="text-muted">${esc(v.name)}</small></h4>` +
            summaryTable(s) + warn + action;
        document.getElementById('mccm-mp-go').onclick = async function () {
            this.disabled = true;
            if (cfg.mode === 'wizard') {
                closeModal();
                if (cfg.onSelect) cfg.onSelect(s, hit, v);
                return;
            }
            const mode = (body.querySelector('input[name="mccm-mp-mode"]:checked') || {}).value || 'merge';
            const r = await cfg.api({ action: 'modpack_install', source: hit.source, id: hit.id, version_id: v.id, mode, confirm: true });
            if (r.status !== 'ok') {
                this.disabled = false;
                mccmStatus((r.error === 'JOB_RUNNING' ? t('jobRunning') : (r.error || 'error')));
                return;
            }
            closeModal();
            startPolling();
        };
    }

    // -------------------------------------------------------------- progress
    function renderProgress(st) {
        const el = cfg.progressEl;
        if (!el) return;
        if (!st || st.status === 'idle') { el.innerHTML = ''; el.style.display = 'none'; return; }
        el.style.display = '';
        const total = st.total || 0, done = st.done || 0;
        const pct = total ? Math.round(done * 100 / total) : (st.status === 'done' ? 100 : 0);
        let cls = 'info', head;
        if (st.status === 'done') { cls = 'success'; head = t('done'); }
        else if (st.status === 'error') { cls = 'danger'; head = `${t('failed')}: ${esc(st.error || '')}`; }
        else head = t('progress', { name: st.name || '', done, total }) + (st.current ? ` <span class="text-muted">${esc(st.current)}</span>` : '');
        const errs = (st.errors || []).length
            ? `<details class="mt-1"><summary>${t('errors')} (${st.errors.length})</summary><ul class="mb-0 pl-3">` +
              st.errors.map(e => `<li>${esc(e.name)}: <span class="text-muted">${esc(e.reason)}</span></li>`).join('') + `</ul></details>` : '';
        const backup = st.backup ? `<div class="small text-muted">${t('backup')} <code>${esc(st.backup)}</code></div>` : '';
        const removed = (st.removed || []).length
            ? `<details class="mt-1 small"><summary>${t('removed', { n: st.removed.length })}</summary><ul class="mb-0 pl-3">` +
              st.removed.map(r => `<li>${esc(r)}</li>`).join('') + `</ul></details>` : '';
        el.innerHTML = `<div class="alert alert-${cls} py-2 mb-3">
            <div class="d-flex justify-content-between"><div><i class="ph ph-package"></i> ${head}</div>
            ${st.status === 'done' || st.status === 'error' ? `<a href="#" id="mccm-mp-dismiss" class="text-muted">&times;</a>` : ''}</div>
            ${st.status === 'running' || st.status === 'queued' || st.status === 'resolving'
                ? `<div class="progress mt-2" style="height:6px"><div class="progress-bar" style="width:${pct}%"></div></div>` : ''}
            ${backup}${removed}${blockedList(st.blocked)}${errs}</div>`;
        const d = document.getElementById('mccm-mp-dismiss');
        if (d) d.onclick = (ev) => { ev.preventDefault(); el.innerHTML = ''; el.style.display = 'none'; };
    }
    async function poll() {
        const r = await cfg.api({ action: 'modpack_status' });
        const st = (r.status === 'ok') ? r.data : { status: 'error', error: r.error };
        renderProgress(st);
        if (!st.running && (st.status === 'done' || st.status === 'error' || st.status === 'idle')) {
            stopPolling();
            if (st.status === 'done' && cfg.onDone) cfg.onDone(st);
        }
    }
    function startPolling() {
        stopPolling();
        poll();
        pollTimer = setInterval(poll, 2000);
    }
    function stopPolling() { if (pollTimer) { clearInterval(pollTimer); pollTimer = null; } }
    async function resumeIfRunning() {
        if (!cfg.progressEl) return;
        const r = await cfg.api({ action: 'modpack_status' });
        if (r.status === 'ok' && r.data && r.data.status !== 'idle') {
            renderProgress(r.data);
            if (r.data.running) startPolling();
        }
    }

    return { init, search, openVersions, resolvePack, startPolling, resumeIfRunning, t, blockedList, summaryTable };
})();
