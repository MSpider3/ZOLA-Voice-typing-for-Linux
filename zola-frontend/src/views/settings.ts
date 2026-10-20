import { AppState, ViewModule } from '../main';
import { CatalogModel, ZolaAPI } from '../api';

let containerElement: HTMLElement | null = null;
let apiInstance: ZolaAPI | null = null;
let downloadListener: ((e: Event) => void) | null = null;
let lastCatalog: CatalogModel[] = [];
let loadFailed = false; // retried when the daemon comes back (see updateState)

function escapeHtml(str: string): string {
  return str.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

// replacements setting <-> textarea lines of the form "wrong => right"
function replacementsToText(map: Record<string, string> | undefined): string {
  return Object.entries(map || {}).map(([k, v]) => `${k} => ${v}`).join('\n');
}

function textToReplacements(text: string): Record<string, string> {
  const map: Record<string, string> = {};
  for (const line of text.split('\n')) {
    const [wrong, right] = line.split('=>').map(part => part?.trim());
    if (wrong && right !== undefined) map[wrong] = right;
  }
  return map;
}

// ── Keybinding capture helper ─────────────────────────────────────────────────
// Turns a raw KeyboardEvent into a canonical "ctrl+shift+r" style string.
function keyEventToString(e: KeyboardEvent): string {
  const parts: string[] = [];
  if (e.ctrlKey)  parts.push('ctrl');
  if (e.altKey)   parts.push('alt');
  if (e.shiftKey) parts.push('shift');
  if (e.metaKey)  parts.push('super');

  const key = e.key.toLowerCase();
  // Skip bare modifier-only presses
  if (!['control', 'alt', 'shift', 'meta'].includes(key)) {
    parts.push(key);
  }
  return parts.join('+');
}

function attachKeybindingCapture(input: HTMLInputElement) {
  input.addEventListener('keydown', (e: KeyboardEvent) => {
    e.preventDefault();
    const combo = keyEventToString(e);
    if (combo && combo !== '') {
      input.value = combo;
    }
  });
  input.addEventListener('focus', () => {
    input.placeholder = '[ PRESS KEY COMBO ]';
    input.style.borderColor = 'var(--phosphor-green)';
    input.style.boxShadow = '0 0 8px rgba(51, 255, 102, 0.4)';
  });
  input.addEventListener('blur', () => {
    input.placeholder = '';
    input.style.borderColor = '';
    input.style.boxShadow = '';
  });
}

// ── Model manager (Settings → MODELS) ────────────────────────────────────────
function renderCatalog(models: CatalogModel[]) {
  lastCatalog = models;
  const box = containerElement?.querySelector('#model-catalog');
  if (!box) return;
  box.innerHTML = models.map(m => {
    const dl = m.download;
    const pct = dl && dl.total ? Math.floor((100 * dl.done) / dl.total) : 0;
    const action = dl
      ? `<span class="telemetry-value active" style="font-size: 13px;">DOWNLOADING ${pct}%</span>`
      : m.installed
        ? `<span class="telemetry-value" style="font-size: 13px;">INSTALLED</span>
           <button type="button" class="commit-button model-action" data-action="delete" data-name="${escapeHtml(m.name)}"
                   style="padding: 4px 10px; font-size: 11px; border-color: var(--warning-red); color: var(--warning-red);">DELETE</button>`
        : `<button type="button" class="commit-button model-action" data-action="download" data-name="${escapeHtml(m.name)}"
                   style="padding: 4px 10px; font-size: 11px;">DOWNLOAD (${m.size_mb} MB)</button>`;
    return `
      <div style="border: 1px solid var(--forest-green); padding: 12px; margin-bottom: 10px; background: rgba(0,0,0,0.3);">
        <div style="display: flex; justify-content: space-between; align-items: center; gap: 12px;">
          <strong>${escapeHtml(m.label)}</strong>
          <span style="display: flex; gap: 8px; align-items: center; white-space: nowrap;">${action}</span>
        </div>
        <div style="font-size: 12px; color: var(--subtle-green); margin-top: 6px;">${escapeHtml(m.description)}</div>
        ${dl ? `<div style="height: 4px; background: var(--forest-green); margin-top: 8px;"><div style="height: 4px; width: ${pct}%; background: var(--phosphor-green);"></div></div>` : ''}
        <div class="model-error" data-name="${escapeHtml(m.name)}" style="font-size: 12px; color: var(--warning-red); margin-top: 6px;"></div>
      </div>`;
  }).join('');

  box.querySelectorAll('.model-action').forEach(btn => {
    btn.addEventListener('click', async () => {
      const { action, name } = (btn as HTMLElement).dataset as { action: 'download' | 'delete'; name: string };
      if (action === 'delete' && !confirm(`Delete the ${name} model files?`)) return;
      try {
        await apiInstance?.manageModel(action, name);
      } catch (err) {
        const errEl = box.querySelector(`.model-error[data-name="${name}"]`);
        if (errEl) errEl.textContent = err instanceof Error ? err.message : String(err);
      }
      refreshModels();
    });
  });
}

/** Reload the catalog and the model pickers, keeping the current selections */
async function refreshModels() {
  if (!containerElement || !apiInstance) return;
  const [catalog, whisperInfo] = await Promise.all([
    apiInstance.fetchModelCatalog().catch(() => [] as CatalogModel[]),
    apiInstance.fetchWhisperModels().catch(() => null),
  ]);
  renderCatalog(catalog);
  if (!whisperInfo) return;
  for (const id of ['whisper_realtime', 'whisper_batch']) {
    const select = containerElement.querySelector(`#${id}`) as HTMLSelectElement | null;
    if (!select) continue;
    const active = select.value;
    select.innerHTML = whisperInfo.models.map(m =>
      `<option value="${escapeHtml(m)}" ${active === m ? 'selected' : ''}>${escapeHtml(whisperInfo.labels[m] || m.toUpperCase())}</option>`
    ).join('');
  }
}

async function loadSettingsAndModels() {
  if (!containerElement || !apiInstance) return;

  const form = containerElement.querySelector('#settings-form-el') as HTMLFormElement;
  const statusMsg = containerElement.querySelector('#settings-status');

  if (!form || !statusMsg) return;

  try {
    statusMsg.className = 'commit-status';
    statusMsg.textContent = 'QUERYING_CONFIG_REGISTRIES...';

    const [currentConfig, ollamaModels, whisperInfo, devices] = await Promise.all([
      apiInstance.fetchSettings(),
      apiInstance.fetchOllamaModels().catch(() => []),
      apiInstance.fetchWhisperModels().catch(() => ({
        models: ['tiny', 'base', 'small', 'medium', 'large-v2', 'large-v3'], labels: {} as Record<string, string>,
      })),
      apiInstance.fetchDevices().catch(() => [] as string[]),
    ]);

    // Whisper model selects: installed model folders show their label
    const modelOptions = (active: string) => whisperInfo.models.map(m =>
      `<option value="${escapeHtml(m)}" ${active === m ? 'selected' : ''}>${escapeHtml(whisperInfo.labels[m] || m.toUpperCase())}</option>`
    ).join('');
    const rtSelect = form.querySelector('#whisper_realtime') as HTMLSelectElement;
    if (rtSelect) rtSelect.innerHTML = modelOptions(currentConfig.whisper_realtime || 'base');
    const batchSelect = form.querySelector('#whisper_batch') as HTMLSelectElement;
    if (batchSelect) batchSelect.innerHTML = modelOptions(currentConfig.whisper_batch || 'small');

    // Microphone select ('' = system default)
    const deviceSelect = form.querySelector('#input_device') as HTMLSelectElement;
    if (deviceSelect) {
      const active = currentConfig.input_device || '';
      const names = active && !devices.includes(active) ? [active, ...devices] : devices;
      deviceSelect.innerHTML = `<option value="">SYSTEM DEFAULT</option>` + names.map(d =>
        `<option value="${escapeHtml(d)}" ${active === d ? 'selected' : ''}>${escapeHtml(d)}</option>`
      ).join('');
    }

    // Ollama Model
    const ollamaSelect = form.querySelector('#ollama_model') as HTMLSelectElement;
    if (ollamaSelect) {
      const activeModel = currentConfig.ollama_model || 'llama3.2';
      if (ollamaModels.length === 0) {
        ollamaSelect.innerHTML = `<option value="${activeModel}">${activeModel}</option>
                                  <option value="" disabled>&lt; NO OTHER MODELS FOUND &gt;</option>`;
      } else {
        ollamaSelect.innerHTML = ollamaModels.map(m =>
          `<option value="${m}" ${activeModel === m ? 'selected' : ''}>${m}</option>`
        ).join('');
      }
    }

    const setVal = (id: string, val: any, fallback: any = '') => {
      const el = form.querySelector(`#${id}`) as HTMLInputElement | HTMLSelectElement | null;
      if (el) el.value = (val !== undefined && val !== null ? val : fallback).toString();
    };

    setVal('ollama_url',                   currentConfig.ollama_url,                   'http://127.0.0.1:11434');
    setVal('compute_type',                 currentConfig.compute_type,                 'int8');
    setVal('cpu_threads',                  currentConfig.cpu_threads,                  4);
    setVal('spoken_language',              currentConfig.spoken_language ?? 'auto',    'auto');
    setVal('output_language',              currentConfig.output_language ?? 'preserve','preserve');
    setVal('sample_rate',                  currentConfig.sample_rate,                  16000);
    setVal('typing_delay_ms',              currentConfig.typing_delay_ms,              12);
    setVal('realtime_utterance_silence_s', currentConfig.realtime_utterance_silence_s, 0.5);
    setVal('realtime_force_commit_s',      currentConfig.realtime_force_commit_s,      2.5);
    setVal('realtime_chunk_s',             currentConfig.realtime_chunk_s,             1.0);
    setVal('realtime_silence_timeout_s',   currentConfig.realtime_silence_timeout_s,   3.0);
    setVal('batch_silence_timeout_s',      currentConfig.batch_silence_timeout_s,      3.0);
    setVal('batch_min_record_s',           currentConfig.batch_min_record_s,           0.5);
    setVal('history_max',                  currentConfig.history_max,                  50);
    setVal('llm_api',                      currentConfig.llm_api,                      'ollama');
    setVal('paste_method',                 currentConfig.paste_method,                 'ctrl_v');
    setVal('restore_clipboard',            String(!!currentConfig.restore_clipboard));
    setVal('audio_feedback',               String(currentConfig.audio_feedback !== false));
    setVal('save_history',                 String(currentConfig.save_history !== false));
    setVal('initial_prompt',               currentConfig.initial_prompt,               '');
    setVal('replacements',                 replacementsToText(currentConfig.replacements));

    // Keybinding & Active Mode
    setVal('active_mode',                  currentConfig.active_mode,                  'batch');
    setVal('keybinding',                   currentConfig.keybinding,                   'ctrl+shift+v');

    renderCatalog(await apiInstance.fetchModelCatalog().catch(() => [] as CatalogModel[]));

    statusMsg.className = 'commit-status success';
    statusMsg.textContent = 'REGISTRIES_LOADED_OK';
    loadFailed = false;

  } catch (err) {
    loadFailed = true;
    console.error('Failed to load settings', err);
    statusMsg.className = 'commit-status error';
    statusMsg.textContent = `CONFIG_LOAD_FAILURE: ${err instanceof Error ? err.message : String(err)}`;
  }
}

async function handleFormSubmit(e: SubmitEvent) {
  e.preventDefault();

  if (!containerElement || !apiInstance) return;

  const form = e.target as HTMLFormElement;
  const statusMsg = containerElement.querySelector('#settings-status');
  const section = containerElement.querySelector('.settings-section');

  if (!statusMsg || !form || !section) return;

  statusMsg.className = 'commit-status';
  statusMsg.textContent = 'COMMITTING_CHANGES_TO_DAEMON...';
  section.classList.remove('flash-success', 'flash-error');

  const getStr  = (id: string) => (form.querySelector(`#${id}`) as HTMLInputElement | HTMLSelectElement).value;
  const getInt  = (id: string) => parseInt(getStr(id), 10);
  const getFlt  = (id: string) => parseFloat(getStr(id));

  const rawSpokenLang = getStr('spoken_language').trim();
  const rawOutputLang = getStr('output_language').trim();

  const payload = {
    whisper_realtime:              getStr('whisper_realtime'),
    whisper_batch:                 getStr('whisper_batch'),
    ollama_model:                  getStr('ollama_model'),
    ollama_url:                    getStr('ollama_url').trim(),
    compute_type:                  getStr('compute_type'),
    cpu_threads:                   getInt('cpu_threads'),
    spoken_language:               rawSpokenLang === '' ? 'auto' : rawSpokenLang,
    output_language:               rawOutputLang === '' ? 'preserve' : rawOutputLang,
    sample_rate:                   getInt('sample_rate'),
    typing_delay_ms:               getInt('typing_delay_ms'),
    realtime_utterance_silence_s:  getFlt('realtime_utterance_silence_s'),
    realtime_force_commit_s:       getFlt('realtime_force_commit_s'),
    realtime_chunk_s:              getFlt('realtime_chunk_s'),
    realtime_silence_timeout_s:    getFlt('realtime_silence_timeout_s'),
    batch_silence_timeout_s:       getFlt('batch_silence_timeout_s'),
    batch_min_record_s:            getFlt('batch_min_record_s'),
    history_max:                   getInt('history_max'),
    llm_api:                       getStr('llm_api'),
    input_device:                  getStr('input_device') || null,
    audio_feedback:                getStr('audio_feedback') === 'true',
    paste_method:                  getStr('paste_method'),
    restore_clipboard:             getStr('restore_clipboard') === 'true',
    save_history:                  getStr('save_history') === 'true',
    initial_prompt:                getStr('initial_prompt').trim() || null,
    replacements:                  textToReplacements(getStr('replacements')),
    active_mode:                   getStr('active_mode'),
    keybinding:                    getStr('keybinding'),
  };

  try {
    const result = await apiInstance.saveSettings(payload);
    statusMsg.className = 'commit-status success';
    statusMsg.textContent = 'COMMIT_OK // CONFIG_APPLIED';
    section.classList.add('flash-success');
    console.info('Settings successfully saved', result);
  } catch (err) {
    console.error('Failed to save settings', err);
    statusMsg.className = 'commit-status error';
    statusMsg.textContent = `COMMIT_FAILURE: ${err instanceof Error ? err.message : String(err)}`;
    section.classList.add('flash-error');
  }
}

function applyDefaults(container: HTMLElement) {
  const form = container.querySelector('#settings-form-el') as HTMLFormElement;
  if (!form) return;

  const setVal = (id: string, val: string) => {
    const el = form.querySelector(`#${id}`) as HTMLInputElement | HTMLSelectElement | null;
    if (el) el.value = val;
  };

  setVal('whisper_realtime',           'base');
  setVal('whisper_batch',              'small');
  setVal('compute_type',               'int8');
  setVal('cpu_threads',                '2');
  setVal('spoken_language',            'auto');
  setVal('output_language',            'preserve');
  setVal('sample_rate',                '16000');
  setVal('typing_delay_ms',            '12');
  setVal('realtime_utterance_silence_s','0.7');
  setVal('realtime_force_commit_s',    '10.0');
  setVal('realtime_chunk_s',           '1.0');
  setVal('realtime_silence_timeout_s', '3.0');
  setVal('batch_silence_timeout_s',    '3.0');
  setVal('batch_min_record_s',         '0.5');
  setVal('history_max',                '50');
  setVal('llm_api',                    'ollama');
  setVal('input_device',               '');
  setVal('audio_feedback',             'true');
  setVal('paste_method',               'ctrl_v');
  setVal('restore_clipboard',          'false');
  setVal('save_history',               'true');
  setVal('initial_prompt',             '');
  setVal('replacements',               '');
  setVal('ollama_url',                 'http://127.0.0.1:11434');
  setVal('active_mode',                'batch');
  setVal('keybinding',                 'ctrl+shift+v');

  const ollamaSelect = form.querySelector('#ollama_model') as HTMLSelectElement;
  if (ollamaSelect && ollamaSelect.options.length > 0) {
    ollamaSelect.selectedIndex = 0;
  }

  const statusMsg = container.querySelector('#settings-status');
  if (statusMsg) {
    statusMsg.className = 'commit-status success';
    statusMsg.textContent = 'DEFAULTS_RESTORED_LOCAL';
  }
}

const view: ViewModule = {
  render(container: HTMLElement, _state: AppState, api: ZolaAPI) {
    containerElement = container;
    apiInstance = api;

    container.innerHTML = `
      <div class="settings-form">
        <form id="settings-form-el">
          <div class="settings-section">

            <!-- ─────────────────── GLOBAL TRIGGER CONFIG ─────────────────── -->
            <div class="settings-section-title">SYSTEM_REGISTRY // GLOBAL_TRIGGER_CONFIGURATION</div>
            <p style="font-size:11px; color: var(--subtle-green); margin: 0 0 16px; opacity: 0.8;">
              Configure your single global trigger mode and key combination. Bind your hotkey to invoke the endpoint <code>/trigger</code>.
            </p>
            <div class="matrix-grid" style="margin-bottom: 28px;">
              <div class="select-container" style="border: 1px solid var(--forest-green); padding: 14px; background: rgba(0,0,0,0.3);">
                <label class="select-label" for="active_mode">ACTIVE_VOICE_MODE (active_mode)</label>
                <div class="crt-select-wrap">
                  <select id="active_mode" class="crt-select">
                    <option value="batch">BATCH (One-shot recording, full sentence)</option>
                    <option value="batch-llm">BATCH + LLM (One-shot, LLM-polished)</option>
                    <option value="batch-translate">BATCH TRANSLATE (Experimental, Whisper → English)</option>
                    <option value="realtime">REALTIME (Continuous streaming, direct injection)</option>
                    <option value="realtime-llm">REALTIME + LLM (Continuous streaming, final LLM polish)</option>
                  </select>
                </div>
              </div>
              <div class="select-container" style="border: 1px solid var(--forest-green); padding: 14px; background: rgba(0,0,0,0.3);">
                <label class="select-label" for="keybinding">GLOBAL_SHORTCUT (click to capture)</label>
                <input type="text"
                       id="keybinding"
                       class="crt-select keybinding-input"
                       style="cursor: text; font-family: var(--font-mono); letter-spacing: 2px;" />
              </div>
            </div>

            <!-- ─────────────────── OLLAMA LLM ────────────────────────── -->
            <div class="settings-section-title">SYSTEM_REGISTRY // OLLAMA_LLM</div>
            <div class="matrix-grid" style="margin-bottom: 25px;">
              <div class="select-container">
                <label class="select-label" for="ollama_model">LLM_REFINEMENT_CORE (ollama_model)</label>
                <div class="crt-select-wrap">
                  <select id="ollama_model" class="crt-select">
                    <option>LOADING...</option>
                  </select>
                </div>
              </div>
              <div class="select-container">
                <label class="select-label" for="ollama_url">LLM_SERVER_URL (ollama_url, local only)</label>
                <input type="text" id="ollama_url" class="crt-select" style="cursor: text;" />
              </div>
              <div class="select-container">
                <label class="select-label" for="llm_api">LLM_SERVER_API (llm_api)</label>
                <div class="crt-select-wrap">
                  <select id="llm_api" class="crt-select">
                    <option value="ollama">OLLAMA</option>
                    <option value="openai">OPENAI-COMPATIBLE (llama.cpp / LM Studio)</option>
                  </select>
                </div>
              </div>
            </div>

            <!-- ─────────────────── MODELS ────────────────────────────── -->
            <div class="settings-section-title">SYSTEM_REGISTRY // MODELS</div>
            <p style="font-size:11px; color: var(--subtle-green); margin: 0 0 12px; opacity: 0.8;">
              Extra speech models, downloaded once and used offline. Standard Whisper sizes download automatically when selected below.
            </p>
            <div id="model-catalog" style="margin-bottom: 25px;">LOADING...</div>

            <!-- ─────────────────── WHISPER STT ───────────────────────── -->
            <div class="settings-section-title">SYSTEM_REGISTRY // WHISPER_STT</div>
            <div class="matrix-grid" style="margin-bottom: 25px;">
              <div class="select-container">
                <label class="select-label" for="whisper_realtime">WHISPER_REALTIME_SIZE (whisper_realtime)</label>
                <div class="crt-select-wrap">
                  <select id="whisper_realtime" class="crt-select">
                    <option>LOADING...</option>
                  </select>
                </div>
              </div>
              <div class="select-container">
                <label class="select-label" for="whisper_batch">WHISPER_BATCH_SIZE (whisper_batch)</label>
                <div class="crt-select-wrap">
                  <select id="whisper_batch" class="crt-select">
                    <option>LOADING...</option>
                  </select>
                </div>
              </div>
              <div class="select-container">
                <label class="select-label" for="compute_type">QUANTIZATION_COMPUTE_TYPE (compute_type)</label>
                <div class="crt-select-wrap">
                  <select id="compute_type" class="crt-select">
                    <option value="int8">INT8 (OPTIMIZED CPU)</option>
                    <option value="int8_float32">INT8_FLOAT32</option>
                    <option value="int16">INT16</option>
                    <option value="float32">FLOAT32</option>
                  </select>
                </div>
              </div>
              <div class="select-container">
                <label class="select-label" for="cpu_threads">CPU_THREADS (cpu_threads)</label>
                <input type="number" id="cpu_threads" class="crt-select" min="1" max="64" step="1" style="cursor: text;" />
              </div>
              <div class="select-container">
                <label class="select-label" for="spoken_language">SPOKEN_LANGUAGE (spoken_language)</label>
                <div class="crt-select-wrap">
                  <select id="spoken_language" class="crt-select" style="width: 100%;">
                    <option value="auto">AUTO-DETECT (Hinglish Supported)</option>
                    <option value="en">ENGLISH (en)</option>
                    <option value="hi">HINDI (hi)</option>
                    <option value="mixed">MIXED (Hinglish)</option>
                  </select>
                </div>
              </div>
              <div class="select-container">
                <label class="select-label" for="output_language">OUTPUT_LANGUAGE (output_language)</label>
                <div class="crt-select-wrap">
                  <select id="output_language" class="crt-select" style="width: 100%;">
                    <option value="preserve">PRESERVE SOURCE (Translate off)</option>
                    <option value="english">TRANSLATE TO ENGLISH</option>
                  </select>
                </div>
              </div>
            </div>

            <!-- ─────────────────── AUDIO CAPTURE ─────────────────────── -->
            <div class="settings-section-title">SYSTEM_REGISTRY // AUDIO_CAPTURE</div>
            <div class="matrix-grid" style="margin-bottom: 25px;">
              <div class="select-container">
                <label class="select-label" for="sample_rate">AUDIO_SAMPLE_RATE (sample_rate)</label>
                <div class="crt-select-wrap">
                  <select id="sample_rate" class="crt-select">
                    <option value="16000">16000 HZ (WHISPER STANDARD)</option>
                  </select>
                </div>
              </div>
              <div class="select-container">
                <label class="select-label" for="input_device">MICROPHONE (input_device)</label>
                <div class="crt-select-wrap">
                  <select id="input_device" class="crt-select">
                    <option value="">SYSTEM DEFAULT</option>
                  </select>
                </div>
              </div>
              <div class="select-container">
                <label class="select-label" for="audio_feedback">START/STOP_SOUND (audio_feedback)</label>
                <div class="crt-select-wrap">
                  <select id="audio_feedback" class="crt-select">
                    <option value="true">ON</option>
                    <option value="false">OFF</option>
                  </select>
                </div>
              </div>
            </div>

            <!-- ─────────────────── REALTIME ──────────────────────────── -->
            <div class="settings-section-title">SYSTEM_REGISTRY // REALTIME_TRANSCRIPTION</div>
            <div class="matrix-grid" style="margin-bottom: 25px;">
              <div class="select-container">
                <label class="select-label" for="realtime_utterance_silence_s">UTTERANCE_SILENCE_COMMIT (realtime_utterance_silence_s)</label>
                <input type="number" id="realtime_utterance_silence_s" class="crt-select" min="0.0" max="5.0" step="0.1" style="cursor: text;" />
              </div>
              <div class="select-container">
                <label class="select-label" for="realtime_force_commit_s">FORCE_COMMIT_TIMEOUT (realtime_force_commit_s)</label>
                <input type="number" id="realtime_force_commit_s" class="crt-select" min="1.0" max="60.0" step="0.5" style="cursor: text;" />
              </div>
              <div class="select-container">
                <label class="select-label" for="realtime_chunk_s">FIXED_INTERVAL_CHUNK (realtime_chunk_s)</label>
                <input type="number" id="realtime_chunk_s" class="crt-select" min="0.1" max="10.0" step="0.1" style="cursor: text;" />
              </div>
              <div class="select-container">
                <label class="select-label" for="realtime_silence_timeout_s">AUTO_STOP_SILENCE (realtime_silence_timeout_s)</label>
                <input type="number" id="realtime_silence_timeout_s" class="crt-select" min="0.0" max="60.0" step="0.5" style="cursor: text;" />
              </div>
            </div>

            <!-- ─────────────────── BATCH ─────────────────────────────── -->
            <div class="settings-section-title">SYSTEM_REGISTRY // BATCH_TRANSCRIPTION</div>
            <div class="matrix-grid" style="margin-bottom: 25px;">
              <div class="select-container">
                <label class="select-label" for="batch_silence_timeout_s">AUTO_STOP_SILENCE (batch_silence_timeout_s)</label>
                <input type="number" id="batch_silence_timeout_s" class="crt-select" min="0.0" max="60.0" step="0.5" style="cursor: text;" />
              </div>
              <div class="select-container">
                <label class="select-label" for="batch_min_record_s">MINIMUM_RECORD_DURATION (batch_min_record_s)</label>
                <input type="number" id="batch_min_record_s" class="crt-select" min="0.0" max="10.0" step="0.1" style="cursor: text;" />
              </div>
            </div>

            <!-- ─────────────────── KEYSTROKE ─────────────────────────── -->
            <div class="settings-section-title">SYSTEM_REGISTRY // KEYSTROKE_INJECTION</div>
            <div class="matrix-grid" style="margin-bottom: 25px;">
              <div class="select-container">
                <label class="select-label" for="typing_delay_ms">INTER_KEY_TYPING_DELAY (typing_delay_ms)</label>
                <input type="number" id="typing_delay_ms" class="crt-select" min="1" max="200" step="1" style="cursor: text;" />
              </div>
              <div class="select-container">
                <label class="select-label" for="paste_method">PASTE_KEYS (paste_method)</label>
                <div class="crt-select-wrap">
                  <select id="paste_method" class="crt-select">
                    <option value="ctrl_v">CTRL+V (MOST APPS)</option>
                    <option value="ctrl_shift_v">CTRL+SHIFT+V (TERMINALS)</option>
                    <option value="shift_insert">SHIFT+INSERT (APPS AND TERMINALS)</option>
                  </select>
                </div>
              </div>
              <div class="select-container">
                <label class="select-label" for="restore_clipboard">RESTORE_CLIPBOARD_AFTER_PASTE (restore_clipboard, text only)</label>
                <div class="crt-select-wrap">
                  <select id="restore_clipboard" class="crt-select">
                    <option value="true">ON</option>
                    <option value="false">OFF</option>
                  </select>
                </div>
              </div>
            </div>

            <!-- ─────────────────── CORRECTIONS ───────────────────────── -->
            <div class="settings-section-title">SYSTEM_REGISTRY // VOCABULARY_AND_CORRECTIONS</div>
            <div class="matrix-grid" style="margin-bottom: 25px;">
              <div class="select-container">
                <label class="select-label" for="initial_prompt">VOCABULARY_HINT (initial_prompt) — names and terms you use</label>
                <input type="text" id="initial_prompt" class="crt-select" style="cursor: text;" placeholder="e.g. ZOLA, STT, Hinglish, Bengaluru" />
              </div>
              <div class="select-container">
                <label class="select-label" for="replacements">REPLACEMENTS (one per line: heard =&gt; wanted)</label>
                <textarea id="replacements" class="crt-select" rows="4" style="cursor: text; resize: vertical;" placeholder="STD => STT"></textarea>
              </div>
            </div>

            <!-- ─────────────────── DATA LEDGER ───────────────────────── -->
            <div class="settings-section-title">SYSTEM_REGISTRY // DATA_LEDGER</div>
            <div class="matrix-grid" style="margin-bottom: 30px;">
              <div class="select-container">
                <label class="select-label" for="history_max">MAX_HISTORY_ENTRIES (history_max)</label>
                <input type="number" id="history_max" class="crt-select" min="5" max="1000" step="1" style="cursor: text;" />
              </div>
              <div class="select-container">
                <label class="select-label" for="save_history">KEEP_HISTORY_AFTER_RESTART (save_history)</label>
                <div class="crt-select-wrap">
                  <select id="save_history" class="crt-select">
                    <option value="true">ON</option>
                    <option value="false">OFF</option>
                  </select>
                </div>
              </div>
            </div>

            <div class="button-container">
              <button type="submit" class="commit-button">COMMIT_CHANGES</button>
              <button type="button" id="restore-defaults-btn" class="commit-button" style="border-color: var(--forest-green); color: var(--subtle-green);">RESTORE_DEFAULTS</button>
              <div id="settings-status" class="commit-status">AWAITING_INPUT</div>
            </div>
          </div>
        </form>
      </div>
    `;

    // Attach form submit
    const formEl = container.querySelector('#settings-form-el');
    if (formEl) formEl.addEventListener('submit', handleFormSubmit as any);

    // Attach keybinding capture to the single keybinding input
    const keybindingInput = container.querySelector('#keybinding') as HTMLInputElement | null;
    if (keybindingInput) attachKeybindingCapture(keybindingInput);

    // Restore defaults button
    const restoreBtn = container.querySelector('#restore-defaults-btn');
    if (restoreBtn) {
      restoreBtn.addEventListener('click', () => applyDefaults(container));
    }

    // Download progress / completion from the daemon (see main.ts)
    downloadListener = (e: Event) => {
      const detail = (e as CustomEvent).detail;
      const status = detail?.status;
      if (status === 'downloading') {
        renderCatalog(lastCatalog.map(m => m.name === detail.name
          ? { ...m, download: { status, done: detail.done, total: detail.total } }
          : m));
      } else {
        refreshModels();
        if (status === 'error') {
          const statusMsg = containerElement?.querySelector('#settings-status');
          if (statusMsg) {
            statusMsg.className = 'commit-status error';
            statusMsg.textContent = `MODEL_DOWNLOAD_FAILED: ${detail?.error || ''}`;
          }
        }
      }
    };
    window.addEventListener('zola-model-download', downloadListener);

    loadSettingsAndModels();
  },

  // The page may open before the daemon answers (e.g. while it starts up)
  updateState(state: AppState) {
    if (loadFailed && state.connected) {
      loadFailed = false;
      loadSettingsAndModels();
    }
  },

  destroy() {
    if (downloadListener) {
      window.removeEventListener('zola-model-download', downloadListener);
      downloadListener = null;
    }
    containerElement = null;
    apiInstance = null;
  }
};

export default view;
