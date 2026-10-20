const API_BASE = 'http://127.0.0.1:5001';

export interface StatusData {
  is_recording: boolean;
  active_mode: string | null;
  current_audio_path: string;
  uptime_s: number;
  ollama_model: string;
  whisper_realtime: string;
  whisper_batch: string;
  models_ready: boolean; // false while the daemon loads/downloads its models
}

export interface HistoryEntry {
  timestamp: string;
  mode: string;
  transcript: string;
  duration_s: number;
}

export interface CatalogModel {
  name: string;
  label: string;
  description: string;
  size_mb: number;
  installed: boolean;
  download: { status: string; done: number; total: number } | null;
}

export interface SSEEvent {
  type: 'state_change' | 'new_transcript' | 'level' | 'model_download';
  data: any;
}

export type SSECallback = (event: SSEEvent) => void;
export type ConnectionCallback = (status: 'ONLINE' | 'OFFLINE' | 'RECONNECTING') => void;

export class ZolaAPI {
  private eventSource: EventSource | null = null;
  private sseCallback: SSECallback | null = null;
  private connectionCallback: ConnectionCallback | null = null;
  private reconnectTimeout: number | null = null;
  private reconnectAttempt = 0;
  private static readonly MAX_BACKOFF_MS = 15000;

  constructor() {}

  /** Helper to perform a fetch with a timeout */
  private async fetchWithTimeout(url: string, options: RequestInit = {}, timeoutMs = 5000): Promise<Response> {
    const controller = new AbortController();
    const id = setTimeout(() => controller.abort(), timeoutMs);
    try {
      const response = await fetch(url, {
        ...options,
        signal: controller.signal
      });
      clearTimeout(id);
      return response;
    } catch (err) {
      clearTimeout(id);
      throw err;
    }
  }

  /** GET /status */
  async getStatus(): Promise<StatusData> {
    const res = await this.fetchWithTimeout(`${API_BASE}/status`);
    if (!res.ok) {
      throw new Error(`HTTP error! status: ${res.status}`);
    }
    return await res.json();
  }

  /** POST /trigger or /trigger/{mode} */
  async triggerAction(mode?: string): Promise<any> {
    const url = mode ? `${API_BASE}/trigger/${mode}` : `${API_BASE}/trigger`;
    const res = await this.fetchWithTimeout(url, {
      method: 'POST'
    });
    if (!res.ok) {
      const detail = await res.json().catch(() => ({ detail: 'Unknown error' }));
      throw new Error(detail.detail || `Trigger failed with status: ${res.status}`);
    }
    return await res.json();
  }

  /** GET /history */
  async fetchHistory(limit = 50): Promise<HistoryEntry[]> {
    const res = await this.fetchWithTimeout(`${API_BASE}/history?limit=${limit}`);
    if (!res.ok) {
      throw new Error(`HTTP error! status: ${res.status}`);
    }
    const data = await res.json();
    return data.history || [];
  }

  /** GET /settings */
  async fetchSettings(): Promise<Record<string, any>> {
    const res = await this.fetchWithTimeout(`${API_BASE}/settings`);
    if (!res.ok) {
      throw new Error(`HTTP error! status: ${res.status}`);
    }
    return await res.json();
  }

  /** POST /settings */
  async saveSettings(settings: Record<string, any>): Promise<any> {
    // Long timeout: changing a Whisper model can trigger a multi-minute download
    const res = await this.fetchWithTimeout(`${API_BASE}/settings`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json'
      },
      body: JSON.stringify(settings)
    }, 600000);
    if (!res.ok) {
      const detail = await res.json().catch(() => ({ detail: 'Failed to save settings' }));
      throw new Error(detail.detail || `Save settings failed: ${res.status}`);
    }
    return await res.json();
  }

  /** GET /models/ollama */
  async fetchOllamaModels(): Promise<string[]> {
    const res = await this.fetchWithTimeout(`${API_BASE}/models/ollama`, {}, 10000); // Allow longer timeout for model check
    if (!res.ok) {
      throw new Error(`HTTP error! status: ${res.status}`);
    }
    const data = await res.json();
    return data.models || [];
  }

  /** GET /models/whisper — sizes plus installed model folders and their labels */
  async fetchWhisperModels(): Promise<{ models: string[]; labels: Record<string, string> }> {
    const res = await this.fetchWithTimeout(`${API_BASE}/models/whisper`);
    if (!res.ok) {
      throw new Error(`HTTP error! status: ${res.status}`);
    }
    const data = await res.json();
    return { models: data.models || [], labels: data.labels || {} };
  }

  /** GET /devices — microphone names */
  async fetchDevices(): Promise<string[]> {
    const res = await this.fetchWithTimeout(`${API_BASE}/devices`);
    if (!res.ok) {
      throw new Error(`HTTP error! status: ${res.status}`);
    }
    const data = await res.json();
    return data.devices || [];
  }

  /** GET /models/catalog — downloadable models and whether they are installed */
  async fetchModelCatalog(): Promise<CatalogModel[]> {
    const res = await this.fetchWithTimeout(`${API_BASE}/models/catalog`);
    if (!res.ok) {
      throw new Error(`HTTP error! status: ${res.status}`);
    }
    return (await res.json()).models || [];
  }

  /** POST /models/download or /models/delete; progress arrives as 'model_download' events */
  async manageModel(action: 'download' | 'delete', name: string): Promise<void> {
    const res = await this.fetchWithTimeout(`${API_BASE}/models/${action}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name }),
    });
    if (!res.ok) {
      const detail = await res.json().catch(() => ({ detail: `Model ${action} failed` }));
      throw new Error(detail.detail || `Model ${action} failed: ${res.status}`);
    }
  }

  /** POST /history/clear */
  async clearHistory(): Promise<void> {
    const res = await this.fetchWithTimeout(`${API_BASE}/history/clear`, { method: 'POST' });
    if (!res.ok) {
      throw new Error(`HTTP error! status: ${res.status}`);
    }
  }

  /** POST /cancel — stop recording without typing anything */
  async cancelRecording(): Promise<void> {
    await this.fetchWithTimeout(`${API_BASE}/cancel`, { method: 'POST' });
  }

  /** Starts the SSE listener connection */
  listenToEvents(onEvent: SSECallback, onConnectionChange: ConnectionCallback): void {
    this.sseCallback = onEvent;
    this.connectionCallback = onConnectionChange;
    this.connectSSE();
  }

  /** Internal SSE connection logic with exponential backoff + jitter reconnection */
  private connectSSE(): void {
    if (this.eventSource) {
      this.eventSource.close();
    }

    if (this.reconnectTimeout) {
      clearTimeout(this.reconnectTimeout);
      this.reconnectTimeout = null;
    }

    this.eventSource = new EventSource(`${API_BASE}/events`);

    this.eventSource.onopen = () => {
      this.reconnectAttempt = 0; // Reset counter on successful connection
      if (this.connectionCallback) {
        this.connectionCallback('ONLINE');
      }
    };

    this.eventSource.addEventListener('state_change', (e: MessageEvent) => {
      if (this.sseCallback) {
        try {
          const parsedData = JSON.parse(e.data);
          this.sseCallback({ type: 'state_change', data: parsedData });
        } catch (err) {
          console.error('[Zola SSE] Failed to parse state_change data', err);
        }
      }
    });

    this.eventSource.addEventListener('new_transcript', (e: MessageEvent) => {
      if (this.sseCallback) {
        try {
          const parsedData = JSON.parse(e.data);
          this.sseCallback({ type: 'new_transcript', data: parsedData });
        } catch (err) {
          console.error('[Zola SSE] Failed to parse new_transcript data', err);
        }
      }
    });

    this.eventSource.addEventListener('level', (e: MessageEvent) => {
      if (this.sseCallback) {
        try {
          this.sseCallback({ type: 'level', data: JSON.parse(e.data) });
        } catch (err) {
          console.error('[Zola SSE] Failed to parse level data', err);
        }
      }
    });

    this.eventSource.addEventListener('model_download', (e: MessageEvent) => {
      if (this.sseCallback) {
        try {
          this.sseCallback({ type: 'model_download', data: JSON.parse(e.data) });
        } catch (err) {
          console.error('[Zola SSE] Failed to parse model_download data', err);
        }
      }
    });

    this.eventSource.onerror = (_e) => {
      // Signal OFFLINE immediately so UI updates without delay
      if (this.connectionCallback) {
        this.connectionCallback('OFFLINE');
      }

      this.eventSource?.close();
      this.eventSource = null;

      // Exponential backoff with ±1s jitter: 1s → 2s → 4s → 8s → 15s cap
      this.reconnectAttempt++;
      const base = Math.min(1000 * Math.pow(2, this.reconnectAttempt - 1), ZolaAPI.MAX_BACKOFF_MS);
      const jitter = Math.random() * 1000;
      const delay = Math.round(base + jitter);

      console.warn(`[Zola SSE] Connection dropped. Reconnect attempt #${this.reconnectAttempt} in ${delay}ms`);

      this.reconnectTimeout = window.setTimeout(() => {
        if (this.connectionCallback) {
          this.connectionCallback('RECONNECTING');
        }
        this.connectSSE();
      }, delay);
    };
  }

  /** Stop SSE connection and clear all state */
  disconnect(): void {
    if (this.reconnectTimeout) {
      clearTimeout(this.reconnectTimeout);
      this.reconnectTimeout = null;
    }
    if (this.eventSource) {
      this.eventSource.close();
      this.eventSource = null;
    }
    this.reconnectAttempt = 0;
  }
}
