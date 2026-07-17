import { AppState, ViewModule } from '../main';
import { ZolaAPI } from '../api';

let containerElement: HTMLElement | null = null;
let apiInstance: ZolaAPI | null = null;

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

async function loadSettingsAndModels() {
  if (!containerElement || !apiInstance) return;

  const form = containerElement.querySelector('#settings-form-el') as HTMLFormElement;
  const statusMsg = containerElement.querySelector('#settings-status');

  if (!form || !statusMsg) return;

  try {
    statusMsg.className = 'commit-status';
    statusMsg.textContent = 'QUERYING_CONFIG_REGISTRIES...';

    const [currentConfig, ollamaModels, whisperModels] = await Promise.all([
      apiInstance.fetchSettings(),
      apiInstance.fetchOllamaModels().catch(() => []),
      apiInstance.fetchWhisperModels().catch(() => ['tiny', 'base', 'small', 'medium', 'large-v2', 'large-v3'])
    ]);

    // Whisper Realtime
    const rtSelect = form.querySelector('#whisper_realtime') as HTMLSelectElement;
    if (rtSelect) {
      const activeRt = currentConfig.whisper_realtime || 'base';
      rtSelect.innerHTML = whisperModels.map(m =>
        `<option value="${m}" ${activeRt === m ? 'selected' : ''}>${m.toUpperCase()}</option>`
      ).join('');
    }

    // Whisper Batch
    const batchSelect = form.querySelector('#whisper_batch') as HTMLSelectElement;
    if (batchSelect) {
      const activeBatch = currentConfig.whisper_batch || 'small';
      batchSelect.innerHTML = whisperModels.map(m =>
        `<option value="${m}" ${activeBatch === m ? 'selected' : ''}>${m.toUpperCase()}</option>`
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
    setVal('language',                     currentConfig.language ?? '',               '');
    setVal('sample_rate',                  currentConfig.sample_rate,                  16000);
    setVal('typing_delay_ms',              currentConfig.typing_delay_ms,              12);
    setVal('realtime_utterance_silence_s', currentConfig.realtime_utterance_silence_s, 0.5);
    setVal('realtime_force_commit_s',      currentConfig.realtime_force_commit_s,      2.5);
    setVal('realtime_chunk_s',             currentConfig.realtime_chunk_s,             1.0);
    setVal('realtime_silence_timeout_s',   currentConfig.realtime_silence_timeout_s,   3.0);
    setVal('batch_silence_timeout_s',      currentConfig.batch_silence_timeout_s,      3.0);
    setVal('batch_min_record_s',           currentConfig.batch_min_record_s,           0.5);
    setVal('batch_chunk_s',               currentConfig.batch_chunk_s,               6.0);
    setVal('silence_rms_threshold',        currentConfig.silence_rms_threshold,        0.035);
    setVal('history_max',                  currentConfig.history_max,                  50);

    // Keybinding & Active Mode
    setVal('active_mode',                  currentConfig.active_mode,                  'batch');
    setVal('keybinding',                   currentConfig.keybinding,                   'ctrl+shift+v');

    statusMsg.className = 'commit-status success';
    statusMsg.textContent = 'REGISTRIES_LOADED_OK';

  } catch (err) {
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

  const rawLang = getStr('language').trim();

  const payload = {
    whisper_realtime:              getStr('whisper_realtime'),
    whisper_batch:                 getStr('whisper_batch'),
    ollama_model:                  getStr('ollama_model'),
    ollama_url:                    getStr('ollama_url').trim(),
    compute_type:                  getStr('compute_type'),
    cpu_threads:                   getInt('cpu_threads'),
    language:                      rawLang === '' ? null : rawLang,
    sample_rate:                   getInt('sample_rate'),
    typing_delay_ms:               getInt('typing_delay_ms'),
    realtime_utterance_silence_s:  getFlt('realtime_utterance_silence_s'),
    realtime_force_commit_s:       getFlt('realtime_force_commit_s'),
    realtime_chunk_s:              getFlt('realtime_chunk_s'),
    realtime_silence_timeout_s:    getFlt('realtime_silence_timeout_s'),
    batch_silence_timeout_s:       getFlt('batch_silence_timeout_s'),
    batch_min_record_s:            getFlt('batch_min_record_s'),
    batch_chunk_s:                 getFlt('batch_chunk_s'),
    silence_rms_threshold:         getFlt('silence_rms_threshold'),
    history_max:                   getInt('history_max'),
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
  setVal('cpu_threads',                '4');
  setVal('language',                   '');
  setVal('sample_rate',                '16000');
  setVal('typing_delay_ms',            '12');
  setVal('realtime_utterance_silence_s','0.5');
  setVal('realtime_force_commit_s',    '2.5');
  setVal('realtime_chunk_s',           '1.0');
  setVal('realtime_silence_timeout_s', '3.0');
  setVal('batch_silence_timeout_s',    '3.0');
  setVal('batch_min_record_s',         '0.5');
  setVal('batch_chunk_s',             '6.0');
  setVal('silence_rms_threshold',      '0.035');
  setVal('history_max',                '50');
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
                <label class="select-label" for="ollama_url">OLLAMA_SERVER_URL (ollama_url)</label>
                <input type="text" id="ollama_url" class="crt-select" style="cursor: text;" />
              </div>
            </div>

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
                    <option value="int8_float16">INT8_FLOAT16</option>
                    <option value="int16">INT16</option>
                    <option value="float16">FLOAT16 (OPTIMIZED GPU)</option>
                    <option value="float32">FLOAT32</option>
                  </select>
                </div>
              </div>
              <div class="select-container">
                <label class="select-label" for="cpu_threads">CPU_THREADS (cpu_threads)</label>
                <input type="number" id="cpu_threads" class="crt-select" min="1" max="64" step="1" style="cursor: text;" />
              </div>
              <div class="select-container">
                <label class="select-label" for="language">TRANSCRIPTION_LANGUAGE (language)</label>
                <input type="text" id="language" class="crt-select" placeholder="e.g. en, hi (leave empty for auto)" style="cursor: text;" />
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
                    <option value="8000">8000 HZ</option>
                    <option value="22050">22050 HZ</option>
                    <option value="44100">44100 HZ</option>
                    <option value="48000">48000 HZ</option>
                  </select>
                </div>
              </div>
              <div class="select-container">
                <label class="select-label" for="silence_rms_threshold">SILENCE_RMS_THRESHOLD (silence_rms_threshold)</label>
                <input type="number" id="silence_rms_threshold" class="crt-select" min="0.001" max="1.0" step="0.001" style="cursor: text;" />
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
              <div class="select-container">
                <label class="select-label" for="batch_chunk_s">PROCESSING_SEGMENT_SIZE (batch_chunk_s)</label>
                <input type="number" id="batch_chunk_s" class="crt-select" min="1.0" max="60.0" step="0.5" style="cursor: text;" />
              </div>
            </div>

            <!-- ─────────────────── KEYSTROKE ─────────────────────────── -->
            <div class="settings-section-title">SYSTEM_REGISTRY // KEYSTROKE_INJECTION</div>
            <div class="matrix-grid" style="margin-bottom: 25px;">
              <div class="select-container">
                <label class="select-label" for="typing_delay_ms">INTER_KEY_TYPING_DELAY (typing_delay_ms)</label>
                <input type="number" id="typing_delay_ms" class="crt-select" min="1" max="200" step="1" style="cursor: text;" />
              </div>
            </div>

            <!-- ─────────────────── DATA LEDGER ───────────────────────── -->
            <div class="settings-section-title">SYSTEM_REGISTRY // DATA_LEDGER</div>
            <div class="matrix-grid" style="margin-bottom: 30px;">
              <div class="select-container">
                <label class="select-label" for="history_max">MAX_HISTORY_ENTRIES (history_max)</label>
                <input type="number" id="history_max" class="crt-select" min="5" max="1000" step="1" style="cursor: text;" />
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

    loadSettingsAndModels();
  },

  updateState(_state: AppState) {},

  destroy() {
    containerElement = null;
    apiInstance = null;
  }
};

export default view;
