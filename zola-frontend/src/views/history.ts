import { AppState, ViewModule } from '../main';
import { ZolaAPI, HistoryEntry } from '../api';

let containerElement: HTMLElement | null = null;
let apiInstance: ZolaAPI | null = null;
let historyDebounceTimer: number | null = null;

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

function escapeHtml(str: string): string {
  if (!str) return '';
  return str
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#039;');
}

async function loadAndRenderHistory() {
  if (!containerElement || !apiInstance) return;

  const tableBody = containerElement.querySelector('#history-table-body');
  if (!tableBody) return;

  tableBody.innerHTML = `
    <tr>
      <td colspan="4" style="color: var(--forest-green); text-align: center; padding: 30px;">
        QUERYING_DATABANK...
      </td>
    </tr>
  `;

  try {
    const history = await apiInstance.fetchHistory(50);

    if (history.length === 0) {
      tableBody.innerHTML = `
        <tr>
          <td colspan="4" class="empty-ledger">
            &lt; NO TRANSCRIPTION LEDGER ENTRIES FOUND &gt;
          </td>
        </tr>
      `;
      return;
    }

    // Sort newest first, cap at 100 rows to prevent DOM bloat
    const sortedHistory = [...history].reverse().slice(0, 100);

    tableBody.innerHTML = sortedHistory.map((entry: HistoryEntry) => {
      const modeClass = entry.mode.startsWith('realtime') ? 'mode-rt' : 'mode-batch';
      const displayMode = entry.mode.toUpperCase().replace('_', '-');
      const durationText = entry.duration_s ? `${entry.duration_s.toFixed(2)}s` : 'N/A';

      return `
        <tr>
          <td style="white-space: nowrap; font-size: 12px; color: var(--forest-green);">${formatTimestamp(entry.timestamp)}</td>
          <td style="white-space: nowrap; font-weight: 700;" class="${modeClass}">${displayMode}</td>
          <td style="word-break: break-all;">${escapeHtml(entry.transcript)}</td>
          <td style="white-space: nowrap; text-align: right; color: var(--forest-green);">${durationText}</td>
        </tr>
      `;
    }).join('');

  } catch (err) {
    console.error('Failed to load history', err);
    tableBody.innerHTML = `
      <tr>
        <td colspan="4" style="color: var(--warning-red); text-align: center; padding: 30px;">
          CRITICAL_DATABANK_READ_FAILURE: ${err instanceof Error ? err.message : String(err)}
        </td>
      </tr>
    `;
  }
}

const view: ViewModule = {
  render(container: HTMLElement, _state: AppState, api: ZolaAPI) {
    containerElement = container;
    apiInstance = api;

    container.innerHTML = `
      <div class="ledger-container">
        <div class="pane-title" style="margin: 20px 20px 0 20px;">SYSTEM_LOG // TRANSCRIPTION_LEDGER</div>
        <div class="ledger-table-wrap scrollable-container">
          <table class="ledger-table">
            <thead>
              <tr>
                <th style="width: 170px;">TIMESTAMP</th>
                <th style="width: 140px;">RECORDING_MODE</th>
                <th>TRANSCRIPT_DATA</th>
                <th style="width: 100px; text-align: right;">AUDIO_DUR</th>
              </tr>
            </thead>
            <tbody id="history-table-body">
              <!-- Dynamically populated -->
            </tbody>
          </table>
        </div>
      </div>
    `;

    loadAndRenderHistory();
  },

  updateState(_state: AppState) {
    // Debounce: wait 500ms after the last state update before querying API
    // This prevents fetching on every character during realtime transcription
    if (historyDebounceTimer) clearTimeout(historyDebounceTimer);
    historyDebounceTimer = window.setTimeout(() => {
      loadAndRenderHistory();
      historyDebounceTimer = null;
    }, 500);
  },

  destroy() {
    if (historyDebounceTimer) {
      clearTimeout(historyDebounceTimer);
      historyDebounceTimer = null;
    }
    containerElement = null;
    apiInstance = null;
  }
};

export default view;
