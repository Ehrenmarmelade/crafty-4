/* ===== Content manager - lógica compartida (Content + Add content) ===== */
const MCCM_SERVER_ID = window.MCCM_SERVER_ID;

function mccmCookie(name) {
    const v = document.cookie.match('(^|;)\\s*' + name + '\\s*=\\s*([^;]+)');
    return v ? v.pop() : '';
}
function mccmApi(payload) {
    return fetch(`/api/v2/servers/${MCCM_SERVER_ID}/content`, {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-XSRFToken": mccmCookie("_xsrf") },
        body: JSON.stringify(payload),
    }).then(r => r.json());
}
function esc(s) {
    return (s || '').toString().replace(/&/g, '&amp;').replace(/</g, '&lt;')
        .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}
function badge(src) {
    const c = src === 'modrinth' ? 'success' : src === 'curseforge' ? 'warning' : 'secondary';
    return `<span class="badge badge-${c}">${src}</span>`;
}
function mccmStatus(msg, spin) {
    const el = document.getElementById("mccm-status");
    if (el) el.innerHTML = (spin ? '<i class="ph ph-spinner"></i> ' : '') + (msg || '');
}
function mccmVerType(t) {
    const c = t === 'release' ? 'success' : t === 'beta' ? 'warning' : 'secondary';
    return `<span class="badge badge-${c}">${t || '?'}</span>`;
}

// modal inyectado una vez (sirve para ambas pestañas)
(function ensureModal() {
    if (document.getElementById('mccm-modal')) return;
    const d = document.createElement('div');
    d.id = 'mccm-modal';
    d.innerHTML = '<div class="box"><button class="close" type="button">&times;</button><div id="mccm-modal-body"></div></div>';
    document.body.appendChild(d);
    d.querySelector('.close').onclick = () => d.style.display = 'none';
    d.onclick = (ev) => { if (ev.target === d) d.style.display = 'none'; };
})();

function mccmGameCell(gvs) {
    gvs = gvs || [];
    const mc = window.MCCM_MC;
    if (mc && gvs.indexOf(mc) !== -1) {
        const others = gvs.length - 1;
        return `<span class="badge badge-info">${esc(mc)}</span>` + (others > 0 ? ` <small class="text-muted">+${others}</small>` : '');
    }
    if (!gvs.length) return '<span class="text-muted">?</span>';
    return `<span class="text-muted">${gvs.slice(0, 2).map(esc).join(', ')}${gvs.length > 2 ? ' +' + (gvs.length - 2) : ''}</span>`;
}
async function mccmOpenDetail(source, ident, currentFile, ptype) {
    ptype = ptype || 'mod';
    if (!ident) { mccmStatus('Este contenido no está identificado, no hay ficha.'); return; }
    const modal = document.getElementById('mccm-modal');
    const body = document.getElementById('mccm-modal-body');
    modal.style.display = 'flex';
    body.innerHTML = '<div class="text-muted">Cargando ficha...</div>';
    const [dRes, vRes] = await Promise.all([
        mccmApi({ action: 'detail', source: source, id: ident }),
        mccmApi({ action: 'versions', source: source, id: ident, type: ptype })
    ]);
    mccmRenderDetail(body, dRes.data || {}, vRes.data || [], source, ident, currentFile || '', false, ptype);
}
function mccmRenderDetail(body, d, vs, src, ident, file, allChecked, ptype) {
    ptype = ptype || 'mod';
    const verRows = vs.map(v =>
        `<tr><td>${mccmVerType(v.type)}</td><td>${esc(v.name)}</td>` +
        `<td>${mccmGameCell(v.game_versions)}</td>` +
        `<td class="text-muted">${(v.date || '').slice(0, 10)}</td>` +
        `<td><button class="btn btn-sm btn-outline-primary mccm-instver" data-src="${src}" data-id="${esc(String(ident))}" data-ver="${esc(String(v.id))}" data-file="${esc(file)}" data-type="${esc(ptype)}">Instalar</button></td></tr>`
    ).join('');
    const cats = (d.categories || []).join(', ');
    const dl = (d.downloads || 0).toLocaleString();
    body.innerHTML =
        `<div style="display:flex;gap:14px;align-items:center">` +
        (d.icon ? `<img src="${esc(d.icon)}" width="56" height="56" style="border-radius:8px">` : '') +
        `<div><h4 style="margin:0">${esc(d.title || file || 'Mod')}</h4>` +
        `<div class="text-muted">${d.author ? 'por ' + esc(d.author) + ' · ' : ''}${dl} descargas · ${badge(src)}</div></div></div>` +
        `<p class="mt-2">${esc(d.summary || '')}</p>` +
        `<div class="text-muted small">${cats ? 'Categorías: ' + esc(cats) + ' · ' : ''}${d.url ? `<a href="${esc(d.url)}" target="_blank" rel="noopener">Ver ficha completa ↗</a>` : ''}</div>` +
        `<hr><div class="d-flex justify-content-between align-items-center"><h6 class="mb-0">Versiones (${vs.length})</h6>` +
        `<label class="small text-muted mb-0" style="cursor:pointer"><input type="checkbox" id="mccm-allver" ${allChecked ? 'checked' : ''}> mostrar todas (incl. incompatibles)</label></div>` +
        `<div style="max-height:320px;overflow:auto" class="mt-2"><table class="table table-sm"><thead><tr><th>Tipo</th><th>Versión</th><th>Juego</th><th>Fecha</th><th></th></tr></thead>` +
        `<tbody>${verRows || '<tr><td colspan="5" class="text-muted">Sin versiones para este filtro.</td></tr>'}</tbody></table></div>`;
    document.querySelectorAll('.mccm-instver').forEach(b => b.onclick = mccmInstallVersion);
    const allChk = document.getElementById('mccm-allver');
    if (allChk) allChk.onchange = async () => {
        const vr = await mccmApi({ action: 'versions', source: src, id: ident, all: allChk.checked, type: ptype });
        mccmRenderDetail(body, d, vr.data || [], src, ident, file, allChk.checked, ptype);
    };
}
async function mccmInstallVersion(e) {
    const b = e.currentTarget;
    b.disabled = true; b.textContent = 'Instalando...';
    const r = await mccmApi({ action: 'install_version', source: b.dataset.src, id: b.dataset.id, version_id: b.dataset.ver, current_filename: b.dataset.file || null, type: b.dataset.type || 'mod' });
    if (r.status === 'ok' && r.data && r.data.ok) {
        mccmStatus('Instalado: ' + (r.data.new || '') + '.');
        document.getElementById('mccm-modal').style.display = 'none';
        if (window.mccmAfterInstall) window.mccmAfterInstall({
            filename: r.data.new, type: b.dataset.type || 'mod', old: b.dataset.file || null
        });
    } else {
        b.disabled = false; b.textContent = 'Instalar';
        mccmStatus('No se pudo instalar: ' + ((r.data && r.data.reason) || r.error || 'error'));
    }
}
