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

    tableBody.innerHTML = sortedHistory.map((entry: HistoryEntry, index: number) => {
      const modeClass = entry.mode.startsWith('realtime') ? 'mode-rt' : 'mode-batch';
      const displayMode = entry.mode.toUpperCase().replace('_', '-');
      const durationText = entry.duration_s ? `${entry.duration_s.toFixed(2)}s` : 'N/A';

      return `
        <tr>
          <td style="white-space: nowrap; font-size: 12px; color: var(--forest-green);">${formatTimestamp(entry.timestamp)}</td>
          <td style="white-space: nowrap; font-weight: 700;" class="${modeClass}">${displayMode}</td>
          <td style="word-break: break-word;">${escapeHtml(entry.transcript)}</td>
          <td style="white-space: nowrap; text-align: right; color: var(--forest-green);">${durationText}
            <button class="copy-row-btn commit-button" data-index="${index}" style="padding: 2px 8px; margin-left: 8px; font-size: 11px;">COPY</button>
          </td>
        </tr>
      `;
    }).join('');

    tableBody.querySelectorAll('.copy-row-btn').forEach(btn => {
      btn.addEventListener('click', async () => {
        const entry = sortedHistory[Number((btn as HTMLElement).dataset.index)];
        try {
          await navigator.clipboard.writeText(entry.transcript);
          btn.textContent = 'COPIED';
        } catch {
          btn.textContent = 'FAILED';
        }
        setTimeout(() => { btn.textContent = 'COPY'; }, 1200);
      });
    });

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
        <div class="pane-title" style="margin: 20px 20px 0 20px; display: flex; justify-content: space-between; align-items: center;">
          <span>SYSTEM_LOG // TRANSCRIPTION_LEDGER</span>
          <button id="clear-history-btn" class="commit-button" style="padding: 4px 12px; font-size: 11px; border-color: var(--warning-red); color: var(--warning-red);">CLEAR HISTORY</button>
        </div>
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

    container.querySelector('#clear-history-btn')?.addEventListener('click', async () => {
      if (!confirm('Delete all transcription history? This cannot be undone.')) return;
      try {
        await api.clearHistory();
      } catch (err) {
        console.error('Failed to clear history', err);
      }
      loadAndRenderHistory();
    });

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
