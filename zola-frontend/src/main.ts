import { ZolaAPI, StatusData } from './api';

// Global state interface
export interface AppState {
  isRecording: boolean;
  activeMode: string | null;
  latestTranscript: string;
  uptimeS: number;
  charsInjected: number;
  connected: boolean;
  micLevel: number; // peak 0..1 of the latest mic block, while recording
  // Configuration snapshots
  ollamaModel: string;
  whisperRealtime: string;
  whisperBatch: string;
}

// Global state instance
export const state: AppState = {
  isRecording: false,
  activeMode: null,
  latestTranscript: 'AWAITING_INPUT',
  uptimeS: 0,
  charsInjected: 0,
  connected: false,
  micLevel: 0,
  ollamaModel: '',
  whisperRealtime: 'base',
  whisperBatch: 'small'
};

// API Instance
export const api = new ZolaAPI();

// View interfaces
export interface ViewModule {
  render: (container: HTMLElement, state: AppState, api: ZolaAPI) => void;
  updateState?: (state: AppState) => void;
  destroy?: () => void;
}

// Keep track of the active view
let currentView: ViewModule | null = null;
const viewport = document.getElementById('viewport');

// Hash Router
const routes: Record<string, () => Promise<ViewModule>> = {
  '/': () => import('./views/dashboard').then(m => m.default),
  '/recent': () => import('./views/recent').then(m => m.default),
  '/history': () => import('./views/history').then(m => m.default),
  '/settings': () => import('./views/settings').then(m => m.default)
};

async function navigate() {
  // Call destroy on the previous view if it exists
  if (currentView && typeof currentView.destroy === 'function') {
    currentView.destroy();
  }
  currentView = null;

  if (!viewport) return;
  viewport.innerHTML = '<div style="color: var(--forest-green); padding: 20px;">LOADING_DATABANK...</div>';

  let hash = window.location.hash.slice(1) || '/';
  if (!hash.startsWith('/')) {
    hash = '/' + hash;
  }

  // Find matching route
  const loadRoute = routes[hash] || routes['/'];
  try {
    const view = await loadRoute();
    currentView = view;
    viewport.innerHTML = '';
    view.render(viewport, state, api);
    updateNavLinks(hash);
  } catch (err) {
    console.error('Failed to load route', err);
    viewport.innerHTML = `<div style="color: var(--warning-red); padding: 20px;">CRITICAL_ROUTING_ERROR: ${err}</div>`;
  }
}

function updateNavLinks(activeRoute: string) {
  const links = document.querySelectorAll('.nav-link');
  links.forEach(link => {
    const routeAttr = link.getAttribute('data-route');
    if (routeAttr === activeRoute || (activeRoute === '/' && routeAttr === '/')) {
      link.classList.add('active');
    } else {
      link.classList.remove('active');
    }
  });
}

// Update the Connection Indicator on Left Nav
const connectionIndicator = document.getElementById('connection-status');
const daemonStatusText = document.getElementById('daemon-status');

function updateConnectionUI(status: 'ONLINE' | 'OFFLINE' | 'RECONNECTING') {
  if (!connectionIndicator || !daemonStatusText) return;

  connectionIndicator.className = 'status-indicator'; // clear
  state.connected = status === 'ONLINE';

  if (status === 'ONLINE') {
    connectionIndicator.classList.add('online');
    if (state.isRecording) {
      connectionIndicator.classList.add('recording');
      daemonStatusText.textContent = `RECORDING // ${state.activeMode?.toUpperCase()}`;
    } else {
      daemonStatusText.textContent = 'STANDBY';
    }
  } else if (status === 'OFFLINE') {
    connectionIndicator.classList.add('offline');
    daemonStatusText.textContent = 'OFFLINE';
  } else {
    connectionIndicator.classList.add('offline'); // Pulsate red during reconnect
    daemonStatusText.textContent = 'RECONNECTING';
  }
}

// Handle Incoming SSE Events
function handleSSEEvent(event: any) {
  if (event.type === 'state_change') {
    const statusData: StatusData = event.data;
    state.isRecording = statusData.is_recording;
    state.activeMode = statusData.active_mode;
    state.uptimeS = statusData.uptime_s;
    state.ollamaModel = statusData.ollama_model;
    state.whisperRealtime = statusData.whisper_realtime;
    state.whisperBatch = statusData.whisper_batch;
    if (!state.isRecording) state.micLevel = 0;

    updateConnectionUI(state.connected ? 'ONLINE' : 'OFFLINE');

    // Notify active view of state changes
    if (currentView && typeof currentView.updateState === 'function') {
      currentView.updateState(state);
    }
  } else if (event.type === 'model_download') {
    // The settings view shows download progress
    window.dispatchEvent(new CustomEvent('zola-model-download', { detail: event.data }));
  } else if (event.type === 'level') {
    // Read by the dashboard animation; no view update needed
    state.micLevel = event.data.level;
  } else if (event.type === 'new_transcript') {
    const transcriptData = event.data;
    if (transcriptData.session_transcript) {
      state.latestTranscript = transcriptData.session_transcript;
    } else if (transcriptData.transcript) {
      state.latestTranscript = transcriptData.transcript;
    }

    // Accumulate total character injected count. A realtime session ends with a
    // summary event (duration_s > 0) repeating text already counted per utterance.
    const isRealtimeSummary =
      String(transcriptData.mode).startsWith('realtime') && transcriptData.duration_s > 0;
    if (transcriptData.transcript && !isRealtimeSummary) {
      state.charsInjected += transcriptData.transcript.length;
    }

    // Notify active view of state changes (which includes new transcripts)
    if (currentView && typeof currentView.updateState === 'function') {
      currentView.updateState(state);
    }
  }
}

// Initial bootstrap of the frontend
async function bootstrap() {
  // Listen to hash routes
  window.addEventListener('hashchange', navigate);

  // Read initial history once to calculate charsInjected
  try {
    const history = await api.fetchHistory(100);
    state.charsInjected = history.reduce((sum, item) => sum + (item.transcript?.length || 0), 0);
  } catch (e) {
    console.warn('Failed to load initial history for characters count', e);
  }

  // Attempt to contact daemon for initial state
  try {
    const initStatus = await api.getStatus();
    state.isRecording = initStatus.is_recording;
    state.activeMode = initStatus.active_mode;
    state.uptimeS = initStatus.uptime_s;
    state.ollamaModel = initStatus.ollama_model;
    state.whisperRealtime = initStatus.whisper_realtime;
    state.whisperBatch = initStatus.whisper_batch;
    updateConnectionUI('ONLINE');
  } catch (err) {
    updateConnectionUI('OFFLINE');
  }

  // Check if first-run setup is needed
  try {
    const config = await api.fetchSettings();
    if (config.first_run_completed === false) {
      const modal = document.getElementById('onboarding-modal');
      const form = document.getElementById('onboarding-form') as HTMLFormElement;
      
      if (modal && form) {
        modal.style.display = 'flex';
        
        form.addEventListener('submit', async (e) => {
          e.preventDefault();
          const hw = (document.getElementById('onboarding-hw') as HTMLSelectElement).value;
          const lang = (document.getElementById('onboarding-lang') as HTMLSelectElement).value;
          
          let w_real = 'base', w_batch = 'small';
          if (hw === 'light') { w_real = 'tiny'; w_batch = 'base'; }
          else if (hw === 'accurate') { w_real = 'small'; w_batch = 'large-v3-turbo'; }

          // Benchmarked on a 2-core i3 (STT_EVALUATION.md): English-only models are
          // faster and more accurate for English. Standard Whisper sizes cannot
          // transcribe Hindi well, so Hindi/Hinglish get a downloaded model:
          // Hinglish Swift (Roman script) or Nemotron 3.5 (Devanagari).
          let downloadModel: string | null = null;
          if (lang === 'en') {
            w_real = 'base.en';
            w_batch = hw === 'light' ? 'base.en' : 'small.en';
          } else {
            const preferred = lang === 'hi' ? 'nemotron-3.5' : 'hinglish-swift';
            const installed = await api.fetchWhisperModels().catch(() => ({ models: [] as string[] }));
            if (installed.models.includes(preferred)) {
              w_real = preferred;
              w_batch = preferred;
            } else {
              downloadModel = preferred;
            }
          }

          const submitBtn = form.querySelector('button');
          if (submitBtn) submitBtn.textContent = 'SAVING...';

          try {
            await api.saveSettings({
              whisper_realtime: w_real,
              whisper_batch: w_batch,
              spoken_language: lang,
              first_run_completed: true
            });
            if (downloadModel) {
              // Switch to the model once its download finishes (progress: Settings → MODELS).
              // shortcut: if the app is closed mid-download the switch is skipped; pick it in Settings.
              const target = downloadModel;
              const onDownload = (ev: Event) => {
                const detail = (ev as CustomEvent).detail;
                if (detail?.name !== target || detail.status === 'downloading') return;
                window.removeEventListener('zola-model-download', onDownload);
                if (detail.status === 'done') {
                  api.saveSettings({ whisper_realtime: target, whisper_batch: target })
                    .catch(err => console.error('Failed to switch to downloaded model', err));
                }
              };
              window.addEventListener('zola-model-download', onDownload);
              await api.manageModel('download', target).catch(err => {
                window.removeEventListener('zola-model-download', onDownload);
                console.error('Failed to start model download', err);
              });
            }
            modal.style.display = 'none';
          } catch (err) {
            console.error('Failed to save first-run settings', err);
            if (submitBtn) submitBtn.textContent = 'ERROR_RETRY';
          }
        });
      }
    }
  } catch (err) {
    console.error('Failed to fetch config for onboarding check', err);
  }

  // The daemon only reports uptime on state changes; tick locally in between
  window.setInterval(() => {
    if (state.connected) state.uptimeS += 1;
  }, 1000);

  // Start listening to the SSE stream
  api.listenToEvents(
    (event) => handleSSEEvent(event),
    (status) => updateConnectionUI(status)
  );

  // Trigger initial navigation
  navigate();
}

// Run bootstrap
document.addEventListener('DOMContentLoaded', bootstrap);
