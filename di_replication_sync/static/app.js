// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (C) 2026 D-LAB-5
//
// app.js — the three stages: pick, compare, confirm.
//
// A "ref" is how the server is told which flow to use: either a file in its
// folder ({kind:"folder", name}) or something uploaded this session
// ({kind:"upload", handle}). The browser never parses a .tgz itself, so the
// comparison the page shows is the one replication.py computed.

'use strict';

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

const state = {
  source: null,      // {ref, info}
  target: null,
  compared: null,    // the last /api/diff response
  generated: null,   // the last /api/apply response
};

// ------------------------------------------------------------------ util ---

async function api(path, opts = {}) {
  const res = await fetch(path, opts);
  if (!res.ok) {
    let message = `${res.status} ${res.statusText}`;
    try {
      const body = await res.json();
      message = body.problems ? body.problems.join('; ')
                              : (body.description || body.message || message);
    } catch { /* not JSON — the status line is all we have */ }
    throw new Error(message);
  }
  return res.status === 204 ? null : res.json();
}

let toastTimer;
function toast(message, bad = false) {
  const el = $('#toast');
  el.textContent = message;
  el.classList.toggle('bad', bad);
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, bad ? 7000 : 3500);
}

// Children are written as `condition ? node : null`, so null and undefined have
// to be dropped rather than stringified — replaceChildren(null) would put the
// literal text "null" on the page.
const kept = (kids) => kids.flat(Infinity)
  .filter((kid) => kid !== null && kid !== undefined && kid !== false)
  .map((kid) => (kid instanceof Node ? kid : String(kid)));

function el(tag, attrs = {}, ...kids) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === 'class') node.className = v;
    else if (v !== null && v !== undefined) node.setAttribute(k, v);
  }
  node.append(...kept(kids));
  return node;
}

const fill = (node, ...kids) => node.replaceChildren(...kept(kids));

const dl = (pairs) => el('dl', {},
  pairs.flatMap(([term, value, cls]) =>
    value === null || value === undefined
      ? []
      : [el('dt', {}, term), el('dd', cls ? { class: cls } : {}, value)]));

const VIEWS = ['home', 'explore', 'file', 'wizard'];
const STAGES = ['pick', 'compare', 'confirm', 'result'];

function view(name) {
  for (const id of VIEWS) $(`#view-${id}`).hidden = id !== name;
  $$('.nav-btn').forEach((b) => b.classList.toggle('is-on', b.dataset.view === name));
  if (location.hash !== `#${name}`) history.replaceState(null, '', `#${name}`);
  window.scrollTo({ top: 0 });
  if (name === 'explore') renderExplore();
}

function stage(name) {
  view('wizard');
  for (const id of STAGES) $(`#stage-${id}`).hidden = id !== name;
  // The rail is the only thing telling someone how far through they are, and
  // where they can go back to.
  const at = STAGES.indexOf(name);
  $$('#wizard-steps li').forEach((li, i) => {
    li.classList.toggle('is-on', i === at);
    li.classList.toggle('is-done', i < at);
  });
  window.scrollTo({ top: 0 });
}

// ------------------------------------------------------------------ pick ---

async function loadFolder() {
  let listing;
  try {
    listing = await api('/api/flows');
  } catch (err) {
    toast(`cannot read the folder: ${err.message}`, true);
    return;
  }
  for (const side of ['source', 'target']) {
    const picker = $(`.picker[data-side="${side}"]`);
    const previous = picker.value;
    fill(picker, listing.flows.map((flow) => {
      const label = flow.error
        ? `${flow.file} — unreadable`
        : `${flow.file}  ·  ${flow.taskCount} table${flow.taskCount === 1 ? '' : 's'}`;
      return el('option', {
        value: flow.file,
        disabled: flow.error ? '' : null,
        title: flow.error || flow.flowName,
      }, label);
    }));
    if (previous) picker.value = previous;
  }
  if (!listing.flows.length) toast('no .tgz files in the folder — upload instead');
}

function show(side, info, ref) {
  state[side] = { ref, info };
  const card = $(`.card[data-side="${side}"]`);
  const src = info.sourceSpace || {};
  const tgt = info.targetSpace || {};
  fill(card,
    el('div', { class: 'flow' }, info.flowName || '(unnamed)'),
    dl([
      ['file', info.file],
      ['tables', String(info.taskCount)],
      ['from', `${src.connectionId || '?'} (${src.connectionType || '?'}) ${src.container || ''}`.trim()],
      ['to', `${tgt.connectionId || '?'} (${tgt.connectionType || '?'})`],
    ]),
  );
  card.hidden = false;
  $('#btn-compare').disabled = !(state.source && state.target);
}

async function pickFromFolder(side, name) {
  if (!name) return;
  const listing = await api('/api/flows');
  const info = listing.flows.find((f) => f.file === name);
  if (!info || info.error) { toast(info?.error || 'no such file', true); return; }
  show(side, info, { kind: 'folder', name });
}

async function pasteJson(side) {
  const box = $(`.paste-box[data-side="${side}"]`);
  const text = $(`textarea[data-side="${side}"]`).value;
  if (!text.trim()) { toast('nothing pasted', true); return; }
  try {
    const info = await api('/api/paste', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text }),
    });
    show(side, info, { kind: 'upload', handle: info.handle });
    $(`.picker[data-side="${side}"]`).value = '';
    box.hidden = true;
    toast(`${info.flowName} loaded from pasted JSON — ${info.taskCount} tables`);
  } catch (err) {
    toast(err.message, true);
  }
}

async function uploadFile(side, file) {
  if (!file) return;
  const form = new FormData();
  form.append('file', file);
  try {
    const info = await api('/api/upload', { method: 'POST', body: form });
    show(side, info, { kind: 'upload', handle: info.handle });
    $(`.picker[data-side="${side}"]`).value = '';
    toast(`${file.name} loaded — ${info.taskCount} tables`);
  } catch (err) {
    toast(`${file.name}: ${err.message}`, true);
  }
}

// --------------------------------------------------------------- compare ---

function renderCompare(data) {
  const d = data.diff;
  const plural = (n, word) => `${n} ${word}${n === 1 ? '' : 's'}`;

  // Scope and deployment state are answered separately. A flow can differ on
  // every task and still have no gap, which is exactly the case that reads as
  // alarming if the two are added together.
  const verdict = $('#verdict');
  verdict.className = d.hasGap ? 'verdict gap' : 'verdict';
  verdict.textContent = d.hasGap
    ? `Gap: the target is missing ${plural(d.added.length, 'table')}`
      + (d.removed.length ? ` and has ${plural(d.removed.length, 'it should not')}` : '')
    : (d.identical
      ? `No gap — ${plural(d.common.length, 'table')}, identical on both sides`
      : `No gap — the same ${plural(d.common.length, 'table')} on both sides`);

  fill($('#headline'),
    d.added.length ? el('span', { class: 'pill add' }, `${plural(d.added.length, 'table')} to add`) : null,
    d.removed.length ? el('span', { class: 'pill del' }, `${plural(d.removed.length, 'table')} to remove`) : null,
    d.changed.length ? el('span', { class: 'pill chg' }, `${plural(d.changed.length, 'attribute')} changed`) : null,
    el('span', { class: 'pill same' }, `${plural(d.common.length, 'table')} in common`),
  );

  // Deployment state, said plainly rather than counted as a difference.
  const stateNote = $('#state-note');
  if (d.state.length) {
    const fields = [...new Set(d.state.map((c) => c.field))].join(', ');
    fill(stateNote,
      el('b', {}, `${plural(d.state.length, 'task')} differ on ${fields}. `),
      'That is deployment state — DI writes it back on a deployed flow and it '
      + 'does not appear in the modeller — so it is not counted as a gap. '
      + 'A sync carries it across unless you turn that off at the next step.');
    stateNote.className = 'note';
    stateNote.hidden = false;
  } else {
    stateNote.hidden = true;
  }

  // A target whose space names carry another landscape's connection id.
  const issues = $('#issues-note');
  if (d.targetIssues.length) {
    fill(issues,
      el('b', {}, 'The target needs normalising. '),
      'Its space names do not match the connections they use, which is what a '
      + 'migration done by hand leaves behind:',
      el('ul', {}, d.targetIssues.map((line) => el('li', {}, line))),
      'A sync fixes this on the way through. Use the button below to fix it in '
      + 'place without changing the scope.');
    issues.hidden = false;
  } else {
    issues.hidden = true;
  }
  $('#btn-normalise').hidden = !(d.targetIssues.length && data.target.fromFolder);

  const changesByTable = new Map();
  for (const c of d.changed.concat(d.state)) {
    if (!changesByTable.has(c.table)) changesByTable.set(c.table, []);
    changesByTable.get(c.table).push(c);
  }
  const fmt = (v) => (v === null || v === undefined ? '—' : String(v));
  // Never assume a default here: the server reports how each table is actually
  // configured, because some carry truncate and most do not.
  const detail = (t) => d.detail[t] || {};
  const isState = (field) => d.stateFields.includes(field);

  const rows = [
    ...d.added.map((t) => el('tr', { class: 'add' },
      el('td', { class: 'mark' }, '+'), el('td', { class: 'tbl' }, t),
      el('td', {}, fmt(detail(t).loadType)), el('td', {}, fmt(detail(t).truncate)),
      el('td', {}, 'not in the target yet'))),
    ...d.removed.map((t) => el('tr', { class: 'del' },
      el('td', { class: 'mark' }, '−'), el('td', { class: 'tbl' }, t),
      el('td', {}, fmt(detail(t).loadType)), el('td', {}, fmt(detail(t).truncate)),
      el('td', {}, 'in the target, not in the source — the sync drops it'))),
    ...[...changesByTable].map(([t, cs]) => {
      const scoped = cs.some((c) => !isState(c.field));
      const truncate = cs.find((c) => c.field === 'truncate');
      return el('tr', { class: scoped ? 'chg' : '' },
        el('td', { class: 'mark' }, scoped ? '~' : '·'), el('td', { class: 'tbl' }, t),
        el('td', {}, fmt(cs.find((c) => c.field === 'loadType')?.source
                         ?? detail(t).loadType ?? 'REPLICATE')),
        el('td', {}, truncate ? `${fmt(truncate.target)} → ${fmt(truncate.source)}`
                              : fmt(detail(t).truncate)),
        el('td', {}, cs.map((c) => `${c.field}: ${fmt(c.target)} → ${fmt(c.source)}`
          + (isState(c.field) ? ' (state)' : '')).join('; ')));
    }),
  ];
  if (!rows.length) rows.push(el('tr', {}, el('td', { colspan: '5' }, 'nothing to change')));
  fill($('#table-diff tbody'), rows);

  // Connections, both sides. Green where they agree, red where they do not, and
  // amber for the space name, which is the one field a sync rewrites. Painting a
  // differing connection id red says "these are not the same" — it must not be
  // read as "the sync will change it", which is why every row spells out what
  // happens to it.
  const connRows = (data.connections || []).map((row) => {
    const cls = row.changes ? 'updated' : (row.match ? 'match' : 'delta');
    const after = row.changes
      ? row.afterSync
      : el('span', { class: 'note-kept' },
           row.match ? 'unchanged' : "kept — the target's own");
    return el('tr', { class: cls },
      el('td', { class: 'side' }, row.side),
      el('td', {}, row.field),
      el('td', { class: 'val' }, fmt(row.source)),
      el('td', { class: 'val b' }, fmt(row.target)),
      el('td', { class: 'after' }, after));
  });
  fill($('#table-conn tbody'), connRows.length ? connRows
    : [el('tr', {}, el('td', { colspan: '5' }, 'no connections to compare'))]);
}

async function compare() {
  try {
    const data = await api('/api/diff', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ source: state.source.ref, target: state.target.ref }),
    });
    state.compared = data;
    renderCompare(data);
    stage('compare');
  } catch (err) {
    toast(err.message, true);
  }
}

// --------------------------------------------------------------- confirm ---

function renderConfirm() {
  const { outcome, source, target, diff: d } = state.compared;
  fill($('#outcome'),
    el('p', {}, d.identical
      ? 'Nothing would change — the generated archive would match the target as it is.'
      : `The generated archive is the target flow with ${outcome.taskCount} `
        + `table${outcome.taskCount === 1 ? '' : 's'} from ${source.flowName}. `
        + 'Its name, description and both connections are the target\'s own.'),
    // Say the destructive part out loud: a sync is a replacement, not a merge.
    outcome.targetHasTasks
      ? el('p', { class: 'warn-line' },
          `The target's ${outcome.targetHasTasks} existing task`
          + `${outcome.targetHasTasks === 1 ? '' : 's'} will be discarded first — `
          + 'a sync always starts from an empty shell, so nothing from a previous '
          + 'scope can survive one.')
      : null,
    dl([
      ['flow name', outcome.flowName],
      ['file inside', outcome.member],
      ['tables', String(outcome.taskCount)],
      ['tasks read from', outcome.sourceSpace],
      ['tasks write to', outcome.targetSpace],
      ['target file', target.file],
    ]),
  );
  $('#out-name').value = outcome.suggestedFile;
  $('#wb-dir').textContent = document.body.dataset.flowsDir;
  const props = state.compared.diff.properties || [];
  $('#props-summary').textContent = props.length
    ? `${props.length} differ${props.length === 1 ? 's' : ''} between the two flows.`
    : 'Both flows already write the same way — this changes nothing either way.';
  $('input[name="properties"][value="source"]').checked = true;
  $('#state-fields').textContent = outcome.stateFields.join(', ');
  $('#state-source').textContent =
    `The source has it on ${outcome.sourceStateCount} of ${outcome.taskCount} tasks.`;
  // Show what each choice actually produces, so "flow-wide" is not a claim the
  // reader has to take on trust.
  $('#state-count-copy').textContent = `${outcome.sourceStateCount} of ${outcome.taskCount}`;
  $('#state-count-on').textContent = `${outcome.taskCount} of ${outcome.taskCount}`;
  $('#state-count-off').textContent = `0 of ${outcome.taskCount}`;
  $('#state-row').hidden = !outcome.taskCount;
  $('input[name="state"][value="copy"]').checked = true;
  $('#writeback-row').hidden = false;
  $('#chk-writeback').checked = target.fromFolder;
  $('#result').hidden = true;
  stage('confirm');
}

async function apply() {
  const button = $('#btn-apply');
  button.disabled = true;
  const result = $('#result');
  try {
    const data = await api('/api/apply', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        source: state.source.ref,
        target: state.target.ref,
        writeBack: $('#chk-writeback').checked,
        outName: $('#out-name').value.trim(),
        state: $('input[name="state"]:checked').value,
        properties: $('input[name="properties"]:checked').value,
      }),
    });
    state.generated = data;
    result.hidden = true;
    fill($('#result-banner'),
      el('b', {}, 'Verification clean.'),
      ` ${data.result.taskCount} tables, ${data.bytes} bytes.`,
      data.written ? el('div', {}, el('code', {}, data.written), ' written') : null,
      data.backup
        ? el('div', {}, 'the file it replaced was kept as ',
             el('code', {}, data.backup))
        : null,
      el('div', {}, `truncate is on ${data.result.stateCount} of `
        + `${data.result.taskCount} tasks.`),
      el('div', { class: 'actions' },
        el('a', { class: 'dl', href: `/api/download/${data.handle}`,
                  download: data.file }, `Download ${data.file}`)),
      el('p', {}, 'Upload this file to the target tenant by hand — this tool '
        + 'never talks to a DI system.'));
    stage('result');
    await loadFolder();
  } catch (err) {
    result.className = 'result bad';
    fill(result,
      el('b', {}, 'Nothing was written.'),
      el('ul', {}, err.message.split('; ').map((line) => el('li', {}, line))));
    result.hidden = false;
  } finally {
    button.disabled = false;
  }
}

function openGenerated() {
  const made = state.generated;
  if (!made) { toast('nothing generated yet', true); return; }
  // Prefer the file on disk when it was written there, so Delete and the
  // download link act on the same thing the folder shows.
  const ref = made.written ? { kind: 'folder', name: made.file }
                           : { kind: 'upload', handle: made.handle };
  const url = made.written ? `/api/file/${encodeURIComponent(made.file)}`
                           : `/api/download/${made.handle}`;
  openFlow(ref, url, 'wizard');
}

// --------------------------------------------------------------- explore ---

let listing = { flows: [] };

const KB = (n) => (n < 1024 ? `${n} B` : `${(n / 1024).toFixed(1)} kB`);

async function renderExplore() {
  try {
    listing = await api('/api/flows');
  } catch (err) {
    toast(`cannot read the folder: ${err.message}`, true);
    return;
  }
  const rows = listing.flows.map((flow) => {
    const backup = flow.isBackup;
    const template = !backup && flow.taskCount === 0 && !flow.error;
    const cls = flow.error ? 'is-broken' : (backup ? 'is-backup' : (template ? 'is-template' : ''));

    const meta = flow.error
      ? el('div', { class: 'meta' }, flow.error)
      : el('div', { class: 'meta' },
          `${flow.flowName} · ${flow.taskCount} table${flow.taskCount === 1 ? '' : 's'}`,
          ` · ${flow.sourceSpace.connectionId} → ${flow.targetSpace.connectionId}`,
          ` · ${KB(flow.bytes)}`,
          flow.issues && flow.issues.length
            ? el('span', { class: 'tag' }, 'needs normalising') : null);

    return el('div', { class: `row ${cls}` },
      el('div', {},
        el('div', { class: 'name' }, flow.file,
          backup ? el('span', { class: 'tag' }, 'backup') : null,
          template ? el('span', { class: 'tag' }, 'template') : null),
        meta),
      el('div', { class: 'row-actions' },
        flow.error ? null : el('button', { 'data-open': flow.file }, 'Open'),
        el('a', { class: 'dl', href: `/api/file/${encodeURIComponent(flow.file)}`,
                  download: flow.file }, 'Download'),
        el('button', { class: 'stop', 'data-delete': flow.file }, 'Delete')));
  });

  fill($('#explore-list'), rows.length ? rows
    : [el('div', { class: 'row' }, el('div', {},
        el('div', { class: 'name' }, 'nothing here yet'),
        el('div', { class: 'meta' },
           `put .tgz exports in ${listing.dir}, or upload one in the sync wizard`)))]);

  const real = listing.flows.filter((f) => !f.isBackup).length;
  $('#home-count').textContent =
    `${real} flow${real === 1 ? '' : 's'} in the folder`;
}

async function removeFile(name) {
  const backup = name.endsWith('.bk');
  const warning = backup
    ? `Delete ${name} permanently?\n\nA backup has nowhere further to go — this cannot be undone.`
    : `Delete ${name}?\n\nIt is kept as ${name}.bk, so this can be undone by renaming it back.`;
  if (!confirm(warning)) return;
  try {
    const res = await api('/api/delete', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name }),
    });
    toast(res.keptAs ? `${res.removed} removed — kept as ${res.keptAs}`
                     : `${res.removed} removed permanently`);
    await renderExplore();
    await loadFolder();
  } catch (err) {
    toast(err.message, true);
  }
}

// ------------------------------------------------------------------ file ---

let opened = null;    // {ref, info, text, downloadUrl, from}

async function openFlow(ref, downloadUrl, from) {
  try {
    const info = await api('/api/content', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ flow: ref }),
    });
    opened = { ref, info, text: info.text, downloadUrl, from };
    $('#file-title').textContent = info.file || info.flowName;
    fill($('#file-summary'), dl([
      ['flow name', info.flowName],
      ['file inside', info.member],
      ['tables', String(info.taskCount)],
      ['truncate on', `${info.stateCount} of ${info.taskCount} tasks`],
      ['reads from', `${info.sourceSpace.connectionId} (${info.sourceSpace.connectionType}) ${info.sourceSpace.container || ''}`.trim()],
      ['writes to', `${info.targetSpace.connectionId} (${info.targetSpace.connectionType})`],
      ...(info.issues && info.issues.length ? [['needs fixing', info.issues.join('; ')]] : []),
    ]));
    $('#btn-file-download').href = downloadUrl;
    $('#btn-file-download').setAttribute('download', info.file || `${info.flowName}.tgz`);
    // Deleting only means anything for a file the server can see.
    $('#btn-file-delete').hidden = ref.kind !== 'folder';
    $('#chk-pretty').checked = false;
    paintContent();
    view('file');
  } catch (err) {
    toast(err.message, true);
  }
}

function paintContent() {
  if (!opened) return;
  const pretty = $('#chk-pretty').checked;
  $('#file-content').value = pretty
    ? JSON.stringify(JSON.parse(opened.text), null, 2)
    : opened.text;
}

async function copyContent() {
  const text = $('#file-content').value;
  try {
    await navigator.clipboard.writeText(text);
    toast(`${text.length.toLocaleString()} characters copied`);
  } catch {
    // Clipboard access is refused over plain http on some setups; selecting the
    // text is then the only thing left, and it is one keystroke from done.
    const box = $('#file-content');
    box.focus();
    box.select();
    toast('selected — press Ctrl+C to copy');
  }
}

// ------------------------------------------------------------------ wire ---

function wire() {
  $$('.picker').forEach((picker) => {
    picker.addEventListener('change', () =>
      pickFromFolder(picker.dataset.side, picker.value).catch((e) => toast(e.message, true)));
  });

  $$('input[type="file"]').forEach((input) => {
    input.addEventListener('change', () => {
      uploadFile(input.dataset.side, input.files[0]);
      input.value = '';
    });
  });

  // A button that calls click() on the hidden input, rather than a <label>
  // wrapping it: the label worked, but read as a caption and went unnoticed.
  $$('.upload-btn').forEach((btn) => btn.addEventListener('click', () =>
    $(`input[type="file"][data-side="${btn.dataset.side}"]`).click()));

  $$('.paste-btn').forEach((btn) => btn.addEventListener('click', () => {
    const box = $(`.paste-box[data-side="${btn.dataset.side}"]`);
    box.hidden = !box.hidden;
    if (!box.hidden) $(`textarea[data-side="${btn.dataset.side}"]`).focus();
  }));
  $$('.paste-use').forEach((btn) =>
    btn.addEventListener('click', () => pasteJson(btn.dataset.side)));
  $$('.paste-cancel').forEach((btn) => btn.addEventListener('click', () => {
    $(`.paste-box[data-side="${btn.dataset.side}"]`).hidden = true;
  }));

  // Pasting straight into the panel, without opening the box first.
  $$('.drop').forEach((zone) => zone.addEventListener('paste', (ev) => {
    const text = ev.clipboardData?.getData('text') || '';
    if (!text.trim().startsWith('{')) return;      // not a flow; let it be
    ev.preventDefault();
    $(`textarea[data-side="${zone.dataset.side}"]`).value = text;
    $(`.paste-box[data-side="${zone.dataset.side}"]`).hidden = false;
    pasteJson(zone.dataset.side);
  }));

  $$('.drop').forEach((zone) => {
    for (const type of ['dragenter', 'dragover']) {
      zone.addEventListener(type, (ev) => {
        ev.preventDefault();
        zone.classList.add('over');
      });
    }
    for (const type of ['dragleave', 'drop']) {
      zone.addEventListener(type, () => zone.classList.remove('over'));
    }
    zone.addEventListener('drop', (ev) => {
      ev.preventDefault();
      uploadFile(zone.dataset.side, ev.dataTransfer.files[0]);
    });
  });

  $('#btn-compare').addEventListener('click', compare);

  // Both act on the target and then re-run the gap check, so the page always
  // shows the state of the folder rather than what it was before the click.
  const folderAction = async (path, confirmText, describeIt) => {
    const target = state.compared?.target;
    if (!target?.fromFolder) { toast('that only works on a file in the folder', true); return; }
    const outName = describeIt(target);
    if (!confirm(`${confirmText}\n\nwrites ${outName}`)) return;
    try {
      const res = await api(path, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ flow: state.target.ref, outName }),
      });
      toast(res.written
        ? `written: ${res.file}${res.backup ? ` (previous kept as ${res.backup})` : ''}`
        : res.message);
      await loadFolder();
      await compare();
    } catch (err) {
      toast(err.message, true);
    }
  };

  $('#btn-normalise').addEventListener('click', () => folderAction(
    '/api/normalise',
    'Rename the target\'s spaces to match the connections they use? The table '
    + 'scope and every task attribute stay as they are.',
    (target) => target.file));

  $('#btn-template').addEventListener('click', () => folderAction(
    '/api/template',
    'Write the target\'s empty shell? Connections and name are kept, every task '
    + 'is dropped. This is the file a sync should target.',
    (target) => `${target.flowName}.tgz.template`));
  $('#btn-back').addEventListener('click', () => stage('pick'));
  $('#btn-back2').addEventListener('click', () => stage('compare'));
  $('#btn-toconfirm').addEventListener('click', renderConfirm);
  $('#btn-apply').addEventListener('click', apply);
  // The connections table marks a write setting as rewritten only when the sync
  // is actually taking the source's, so the check has to follow the choice.
  $$('input[name="properties"]').forEach((radio) =>
    radio.addEventListener('change', () => { compare().then(renderConfirm); }));
  $('#btn-reload').addEventListener('click', () =>
    Promise.all([loadFolder(), renderExplore()]).then(() => toast('folder re-read')));

  // --- navigation ---
  $('#nav-home').addEventListener('click', () => view('home'));
  $$('.nav-btn, .card-big').forEach((el_) =>
    el_.addEventListener('click', () => {
      if (el_.dataset.view === 'wizard') stage('pick'); else view(el_.dataset.view);
    }));
  $('#btn-explore-sync').addEventListener('click', () => stage('pick'));
  $$('#wizard-steps li').forEach((li, i) => li.addEventListener('click', () => {
    // Only backwards: a later step has nothing to show until the earlier one ran.
    const at = STAGES.indexOf(STAGES.find((n) => !$(`#stage-${n}`).hidden));
    if (i < at) stage(STAGES[i]);
  }));

  // --- explore ---
  $('#explore-list').addEventListener('click', (ev) => {
    const open = ev.target.closest('[data-open]');
    if (open) {
      const name = open.dataset.open;
      openFlow({ kind: 'folder', name },
               `/api/file/${encodeURIComponent(name)}`, 'explore');
      return;
    }
    const del = ev.target.closest('[data-delete]');
    if (del) removeFile(del.dataset.delete);
  });

  // --- one flow ---
  $('#btn-copy').addEventListener('click', copyContent);
  $('#chk-pretty').addEventListener('change', paintContent);
  $('#btn-file-delete').addEventListener('click', async () => {
    if (opened?.ref.kind !== 'folder') return;
    await removeFile(opened.ref.name);
    view('explore');
  });
  $('#btn-file-back').addEventListener('click', () =>
    (opened?.from === 'wizard' ? stage('result') : view('explore')));

  // --- stage 4 ---
  $('#btn-result-open').addEventListener('click', openGenerated);
  $('#btn-result-explore').addEventListener('click', () => view('explore'));
  $('#btn-result-again').addEventListener('click', () => {
    state.compared = state.generated = null;
    stage('pick');
  });

  $('#btn-about').addEventListener('click', async () => {
    const info = await api('/api/about').catch(() => null);
    if (info) {
      $('#about-dlg .meta').textContent =
        `version ${info.version} · ${info.licence}\n${info.source}\n${info.dir}`;
      $('#about-dlg .disclaimer-line').textContent = info.disclaimer;
    }
    $('#about-dlg').showModal();
  });

  $('#btn-stop').addEventListener('click', async () => {
    if (!confirm('Shut the local server down?')) return;
    await fetch('/api/shutdown', { method: 'POST' }).catch(() => {});
    document.body.innerHTML =
      '<main><h2>Server stopped.</h2><p>Start it again with <code>./run.sh</code>.</p></main>';
  });

  // Theme is a per-viewer convenience, so localStorage is the right home for it
  // — and it may be unavailable, so never let a read or write break the page.
  const select = $('#theme-select');
  let saved = 'auto';
  try { saved = localStorage.getItem('di-sync-theme') || 'auto'; } catch { /* private mode */ }
  const paint = (value) => {
    if (value === 'auto') document.documentElement.removeAttribute('data-theme');
    else document.documentElement.setAttribute('data-theme', value);
  };
  select.value = saved;
  paint(saved);
  select.addEventListener('change', () => {
    paint(select.value);
    try { localStorage.setItem('di-sync-theme', select.value); } catch { /* ignore */ }
  });
}

wire();
// Both at boot: the folder feeds the wizard's pickers, and the listing feeds
// the explorer and the count on the home card, which is on screen immediately.
loadFolder();
renderExplore();
// Land where the URL says, so a reload keeps you where you were.
view(VIEWS.includes(location.hash.slice(1)) ? location.hash.slice(1) : 'home');
