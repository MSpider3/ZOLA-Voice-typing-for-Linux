import { ViewModule, AppState } from '../main';
import { ZolaAPI } from '../api';

// Maximum characters to display in the DOM at once (full text kept in memory for clipboard)
const DISPLAY_CHAR_CAP = 50000;

let activeContainer: HTMLElement | null = null;
let currentText = 'NO_TRANSCRIPTION_LOADED';
let currentMode = 'UNKNOWN';
let currentTimestamp = 'N/A';
let currentDuration = 0;
let isRefined = false;

// Track whether the DOM has been initially built to enable targeted updates
let domBuilt = false;
let lastIsLive = false; // Track live vs static mode to detect transitions requiring full rebuild

// Format duration helper
function formatDuration(s: number): string {
  const mins = Math.floor(s / 60);
  const secs = Math.round(s % 60);
  return `${mins.toString().padStart(2, '0')}:${secs.toString().padStart(2, '0')}`;
}

// Format timestamp helper
function formatTimestamp(isoString: string): string {
  try {
    const d = new Date(isoString);
    if (isNaN(d.getTime())) return isoString;
    const pad = (n: number) => n.toString().padStart(2, '0');

    const year = d.getFullYear();
    const month = pad(d.getMonth() + 1);
    const day = pad(d.getDate());
    const hours = pad(d.getHours());
    const minutes = pad(d.getMinutes());
    const seconds = pad(d.getSeconds());

    return `${year}-${month}-${day} ${hours}:${minutes}:${seconds}`;
  } catch (e) {
    return isoString;
  }
}

// Escape html helper
function escapeHTML(str: string): string {
  return str
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#039;');
}

/** Compute display-safe text: truncate to last DISPLAY_CHAR_CAP chars if over limit */
function displayText(text: string): string {
  if (text.length > DISPLAY_CHAR_CAP) {
    return '…' + text.slice(-DISPLAY_CHAR_CAP);
  }
  return text;
}

/** Full DOM build — only called on first render or when live↔static mode transitions */
function buildFullDOM(container: HTMLElement, isLive: boolean) {
  const wordCount = (currentText === 'NO_TRANSCRIPTION_LOADED' || currentText === 'EMPTY_TRANSCRIPT')
    ? 0
    : currentText.trim().split(/\s+/).filter(Boolean).length;

  const charCount = (currentText === 'NO_TRANSCRIPTION_LOADED' || currentText === 'EMPTY_TRANSCRIPT')
    ? 0
    : currentText.length;

  const refinedText = isRefined ? 'YES // OLLAMA' : 'NO';
  const durationText = formatDuration(currentDuration);

  container.innerHTML = `
    <div class="pane" style="display: flex; flex-direction: column; height: calc(100vh - 120px); margin-bottom: 0;">
      <div class="pane-title" style="display: flex; justify-content: space-between; align-items: center;">
        <span>DATA_VIEW // RECENT_TRANSCRIPTION</span>
        ${isLive ? '<span class="status-indicator recording" style="border:none; padding:0; background:none;"><span class="status-dot"></span> LIVE</span>' : ''}
      </div>

      <div style="flex-grow: 1; display: flex; flex-direction: column; gap: 15px; margin-bottom: 15px; overflow: hidden;">
        <div class="telemetry-grid" style="grid-template-columns: repeat(4, 1fr); gap: 10px;">
          <div class="telemetry-cell">
            <div class="telemetry-label">CAPTURE_MODE</div>
            <div class="telemetry-value ${isLive ? 'active' : ''}" id="recent-mode">${escapeHTML(currentMode.toUpperCase())}</div>
          </div>
          <div class="telemetry-cell">
            <div class="telemetry-label">TIMESTAMP</div>
            <div class="telemetry-value" id="recent-timestamp" style="font-size: 13px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;">
              ${escapeHTML(currentTimestamp)}
            </div>
          </div>
          <div class="telemetry-cell">
            <div class="telemetry-label">METRICS</div>
            <div class="telemetry-value" id="recent-metrics" style="font-size: 13px;">
              ${charCount} CHR // ${wordCount} WRD
            </div>
          </div>
          <div class="telemetry-cell">
            <div class="telemetry-label">DURATION / LLM</div>
            <div class="telemetry-value" id="recent-duration" style="font-size: 13px;">
              ${durationText} // ${refinedText}
            </div>
          </div>
        </div>

        <div style="flex-grow: 1; border: 1px solid var(--forest-green); background: rgba(0,0,0,0.5); padding: 20px; font-size: 16px; line-height: 1.6; overflow-y: auto; white-space: pre-wrap; font-family: var(--font-mono); position: relative;">
          <span class="live-feed-prompt">${isLive ? '&gt; SPEAK_NOW: ' : '&gt; '}</span><span id="recent-live-text">${escapeHTML(displayText(currentText))}</span><span class="cursor-block"></span>
        </div>
      </div>

      <div style="display: flex; gap: 15px;">
        <button id="copy-btn" class="commit-button" style="flex: 1;">COPY TO SYSTEM CLIPBOARD</button>
        <button id="clear-btn" class="commit-button" style="border-color: var(--forest-green); color: var(--subtle-green);">RESET VIEW</button>
      </div>
    </div>
  `;

  domBuilt = true;
  lastIsLive = isLive;

  // Bind event handlers
  const copyBtn = container.querySelector('#copy-btn');
  if (copyBtn) {
    copyBtn.addEventListener('click', async () => {
      try {
        await navigator.clipboard.writeText(currentText); // Always copy full text, not truncated display
        copyBtn.textContent = 'COPIED TO CLIPBOARD!';
        copyBtn.classList.add('flash-success');
        setTimeout(() => {
          copyBtn.textContent = 'COPY TO SYSTEM CLIPBOARD';
          copyBtn.classList.remove('flash-success');
        }, 1500);
      } catch (e) {
        copyBtn.textContent = 'COPY FAILED!';
        setTimeout(() => {
          copyBtn.textContent = 'COPY TO SYSTEM CLIPBOARD';
        }, 1500);
      }
    });
  }

  const clearBtn = container.querySelector('#clear-btn');
  if (clearBtn) {
    clearBtn.addEventListener('click', () => {
      currentText = 'NO_TRANSCRIPTION_LOADED';
      currentMode = 'UNKNOWN';
      currentTimestamp = 'N/A';
      currentDuration = 0;
      isRefined = false;
      domBuilt = false; // Force full rebuild on next updateState
      lastIsLive = false;
      state_ref && buildFullDOM(container, false);
    });
  }
}

// Hold a reference to the last state object for the clear button callback
let state_ref: AppState | null = null;

/** Targeted update — only mutates the specific text nodes, no layout recalculation */
function patchDOM(container: HTMLElement, _isLive: boolean) {
  const wordCount = (currentText === 'NO_TRANSCRIPTION_LOADED' || currentText === 'EMPTY_TRANSCRIPT')
    ? 0
    : currentText.trim().split(/\s+/).filter(Boolean).length;

  const charCount = (currentText === 'NO_TRANSCRIPTION_LOADED' || currentText === 'EMPTY_TRANSCRIPT')
    ? 0
    : currentText.length;

  const refinedText = isRefined ? 'YES // OLLAMA' : 'NO';
  const durationText = formatDuration(currentDuration);

  const liveTextEl = container.querySelector('#recent-live-text');
  if (liveTextEl) liveTextEl.textContent = displayText(currentText);

  const modeEl = container.querySelector('#recent-mode');
  if (modeEl) modeEl.textContent = currentMode.toUpperCase();

  const tsEl = container.querySelector('#recent-timestamp');
  if (tsEl) tsEl.textContent = currentTimestamp;

  const metricsEl = container.querySelector('#recent-metrics');
  if (metricsEl) metricsEl.textContent = `${charCount} CHR // ${wordCount} WRD`;

  const durEl = container.querySelector('#recent-duration');
  if (durEl) durEl.textContent = `${durationText} // ${refinedText}`;
}

const RecentView: ViewModule = {
  render: async (container: HTMLElement, state: AppState, api: ZolaAPI) => {
    activeContainer = container;
    state_ref = state;
    domBuilt = false;
    lastIsLive = false;

    // Default loading screen while fetching history
    container.innerHTML = `
      <div class="pane">
        <div class="pane-title">QUERYING_LATEST_LOG...</div>
        <div style="color: var(--subtle-green);">ACCESSING RECORD LEDGER...</div>
      </div>
    `;

    // Attempt to pull the most recent transcript from server history
    try {
      const history = await api.fetchHistory(1);
      if (history.length > 0) {
        // get_history returns self.history[-limit:], newest is the last element
        const latest = history[history.length - 1];
        currentText = latest.transcript || 'EMPTY_TRANSCRIPT';
        currentMode = latest.mode || 'UNKNOWN';
        currentTimestamp = latest.timestamp ? formatTimestamp(latest.timestamp) : 'N/A';
        currentDuration = latest.duration_s || 0;
        isRefined = latest.mode ? latest.mode.endsWith('-llm') : false;
      }
    } catch (e) {
      console.warn('[Zola] Failed to load latest history item', e);
    }

    // Overwrite with active state if currently recording or has live text
    if (state.isRecording && state.latestTranscript && state.latestTranscript !== 'AWAITING_INPUT') {
      currentText = state.latestTranscript;
      currentMode = state.activeMode || 'REALTIME';
      currentTimestamp = 'LIVE_STREAMING';
      currentDuration = state.uptimeS;
      isRefined = currentMode.endsWith('-llm');
    } else if (state.latestTranscript && state.latestTranscript !== 'AWAITING_INPUT' && state.latestTranscript !== 'NO_TRANSCRIPTION_LOADED') {
      currentText = state.latestTranscript;
      currentMode = state.activeMode || currentMode;
      if (currentTimestamp === 'N/A' || currentTimestamp === 'LIVE_STREAMING') {
        currentTimestamp = 'CURRENT_SESSION';
      }
    }

    const isLive = currentTimestamp === 'LIVE_STREAMING';
    buildFullDOM(container, isLive);
  },

  updateState: (state: AppState) => {
    if (!activeContainer) return;
    state_ref = state;

    const wasLive = lastIsLive;

    // Update data model
    if (state.isRecording && state.latestTranscript && state.latestTranscript !== 'AWAITING_INPUT') {
      currentText = state.latestTranscript;
      currentMode = state.activeMode || 'REALTIME';
      currentTimestamp = 'LIVE_STREAMING';
      currentDuration = state.uptimeS;
      isRefined = currentMode.endsWith('-llm');
    } else if (!state.isRecording && state.latestTranscript && state.latestTranscript !== 'AWAITING_INPUT' && state.latestTranscript !== 'NO_TRANSCRIPTION_LOADED') {
      currentText = state.latestTranscript;
    }

    const isLive = currentTimestamp === 'LIVE_STREAMING';

    if (!domBuilt || wasLive !== isLive) {
      // Mode transition (live→static or static→live) or first render: full rebuild
      buildFullDOM(activeContainer, isLive);
    } else {
      // Same mode: use targeted textContent updates — no layout recalculation
      patchDOM(activeContainer, isLive);
    }
  },

  destroy: () => {
    activeContainer = null;
    state_ref = null;
    domBuilt = false;
    lastIsLive = false;
  }
};

export default RecentView;
