import { AppState, ViewModule } from '../main';
import { ZolaAPI } from '../api';

let animFrameId: number | null = null;
let containerElement: HTMLElement | null = null;
let visibilityHandler: (() => void) | null = null;

// Oscilloscope state variables
let phase = 0;

function drawOscilloscope(ctx: CanvasRenderingContext2D, width: number, height: number, appState: AppState) {
  // ── PHOSPHOR DECAY ─────────────────────────────────────────────────────────
  // Do NOT use clearRect. Instead paint a semi-transparent black layer over the
  // previous frame. The residual brightness of the old trace creates authentic
  // phosphor persistence / ghosting without needing any extra render targets.
  // Opacity 0.12 gives ~3-4 frames of decay at 60 fps — enough glow without lag.
  ctx.globalCompositeOperation = 'source-over';
  ctx.fillStyle = 'rgba(0, 0, 0, 0.12)';
  ctx.fillRect(0, 0, width, height);

  // ── CRT GRID (redrawn every frame at very low opacity to stay subtle) ──────
  ctx.strokeStyle = 'rgba(51, 255, 102, 0.04)';
  ctx.lineWidth = 1;
  ctx.shadowBlur = 0;
  for (let gridX = 40; gridX < width; gridX += 40) {
    ctx.beginPath();
    ctx.moveTo(gridX, 0);
    ctx.lineTo(gridX, height);
    ctx.stroke();
  }
  for (let gridY = 20; gridY < height; gridY += 20) {
    ctx.beginPath();
    ctx.moveTo(0, gridY);
    ctx.lineTo(width, gridY);
    ctx.stroke();
  }

  // ── TRACE LINE ─────────────────────────────────────────────────────────────
  let lineColor = '#33ff66';
  let amplitude = 4;
  const frequency = 0.05;

  if (appState.connected) {
    if (appState.isRecording) {
      lineColor = '#ffb000'; // recording: amber
      amplitude = 35;
    }
    // else: idle green stays
  } else {
    lineColor = '#ff3333'; // offline: warning red
    amplitude = 1;
  }

  ctx.strokeStyle = lineColor;
  ctx.lineWidth = 2;
  ctx.shadowColor = lineColor;
  ctx.shadowBlur = 10; // slightly stronger glow to complement the decay trail
  ctx.beginPath();

  const centerY = height / 2;
  ctx.moveTo(0, centerY);

  for (let x = 0; x < width; x++) {
    let y = centerY;

    if (!appState.connected) {
      // Offline: flat line with tiny fuzz
      y += (Math.random() - 0.5) * 2;
    } else if (appState.isRecording) {
      // Recording: highly erratic, jagged peaks
      const noise = (Math.random() - 0.5) * amplitude;
      const envelope = Math.sin((x / width) * Math.PI);
      y += noise * envelope;
    } else {
      // Idle: smooth composite sine drift
      const wave1 = Math.sin(x * frequency + phase) * amplitude;
      const wave2 = Math.cos(x * 0.02 - phase * 0.5) * (amplitude * 0.5);
      const envelope = Math.sin((x / width) * Math.PI);
      y += (wave1 + wave2) * envelope;
    }

    ctx.lineTo(x, y);
  }

  ctx.stroke();
  ctx.shadowBlur = 0; // reset shadow for grid lines on next frame

  // Advance phase
  phase += 0.15;
}

/** Draw a clean black frame to blank the canvas before pausing */
function blankCanvas(ctx: CanvasRenderingContext2D, width: number, height: number) {
  ctx.globalCompositeOperation = 'source-over';
  ctx.fillStyle = 'rgba(0, 0, 0, 1)';
  ctx.fillRect(0, 0, width, height);
}

function startAnimation(canvas: HTMLCanvasElement, appState: AppState) {
  const ctx = canvas.getContext('2d') as CanvasRenderingContext2D;
  if (!ctx) return;

  function loop() {
    // Guard: bail immediately if hidden, canvas removed, or destroy() was called
    if (document.hidden || !canvas || !canvas.parentElement) return;

    // Resize canvas if needed — note: resizing resets the canvas bitmap,
    // which is fine since it also clears any phosphor trail during layout changes.
    const rect = canvas.parentElement.getBoundingClientRect();
    if (canvas.width !== Math.round(rect.width) || canvas.height !== Math.round(rect.height)) {
      canvas.width = Math.round(rect.width);
      canvas.height = Math.round(rect.height);
    }

    drawOscilloscope(ctx, canvas.width, canvas.height, appState);
    animFrameId = requestAnimationFrame(loop);
  }

  // Page Visibility API — pause when window is hidden, resume on restore
  visibilityHandler = () => {
    if (document.hidden) {
      // Blank canvas before pausing to prevent frozen-frame artifact on restore
      if (canvas.width > 0 && canvas.height > 0) {
        blankCanvas(ctx, canvas.width, canvas.height);
      }
      if (animFrameId !== null) {
        cancelAnimationFrame(animFrameId);
        animFrameId = null;
      }
    } else {
      // Resume animation only if canvas is still in the DOM
      if (animFrameId === null && canvas.parentElement) {
        loop();
      }
    }
  };

  document.addEventListener('visibilitychange', visibilityHandler);

  loop();
}

function formatUptime(seconds: number): string {
  if (isNaN(seconds) || seconds <= 0) return '00:00:00';
  const hrs = Math.floor(seconds / 3600).toString().padStart(2, '0');
  const mins = Math.floor((seconds % 3600) / 60).toString().padStart(2, '0');
  const secs = Math.floor(seconds % 60).toString().padStart(2, '0');
  return `${hrs}:${mins}:${secs}`;
}

const view: ViewModule = {
  render(container: HTMLElement, state: AppState, _api: ZolaAPI) {
    containerElement = container;

    container.innerHTML = `
      <div class="pane">
        <div class="pane-title">SYS_MONITOR // OSCILLOSCOPE <span style="font-size:10px; color: var(--forest-green); margin-left: 8px;">[ PHOSPHOR DECAY ACTIVE ]</span></div>
        <div class="oscilloscope-container">
          <canvas id="oscilloscope"></canvas>
        </div>
      </div>

      <div class="pane">
        <div class="pane-title">DATA_STREAM // LIVE_FEED</div>
        <div class="live-feed-container" id="live-feed-box">
          <span class="live-feed-prompt">&gt; </span><span id="live-text"></span><span class="cursor-block">█</span>
        </div>
      </div>

      <div class="telemetry-grid">
        <div class="telemetry-cell">
          <div class="telemetry-label">DAEMON_UPTIME</div>
          <div class="telemetry-value" id="uptime-val">00:00:00</div>
        </div>
        <div class="telemetry-cell">
          <div class="telemetry-label">CHARS_INJECTED</div>
          <div class="telemetry-value" id="chars-val">0</div>
        </div>
        <div class="telemetry-cell">
          <div class="telemetry-label">VAD_STATE</div>
          <div class="telemetry-value standby" id="vad-val">STANDBY</div>
        </div>
        <div class="telemetry-cell">
          <div class="telemetry-label">ENGINE_RT</div>
          <div class="telemetry-value" id="engine-rt-val">${state.whisperRealtime}</div>
        </div>
        <div class="telemetry-cell">
          <div class="telemetry-label">ENGINE_BATCH</div>
          <div class="telemetry-value" id="engine-batch-val">${state.whisperBatch}</div>
        </div>
        <div class="telemetry-cell">
          <div class="telemetry-label">LLM_CORE</div>
          <div class="telemetry-value" id="llm-val">${state.ollamaModel.split('/').pop() || state.ollamaModel}</div>
        </div>
      </div>
    `;

    const canvas = container.querySelector('#oscilloscope') as HTMLCanvasElement;
    if (canvas) {
      startAnimation(canvas, state);
    }

    this.updateState?.(state);
  },

  updateState(state: AppState) {
    if (!containerElement) return;

    const liveText = containerElement.querySelector('#live-text');
    if (liveText) {
      liveText.textContent = state.latestTranscript || 'AWAITING_INPUT';
    }

    const uptimeVal = containerElement.querySelector('#uptime-val');
    if (uptimeVal) {
      uptimeVal.textContent = formatUptime(state.uptimeS);
    }

    const charsVal = containerElement.querySelector('#chars-val');
    if (charsVal) {
      charsVal.textContent = state.charsInjected.toLocaleString();
    }

    const vadVal = containerElement.querySelector('#vad-val');
    if (vadVal) {
      if (state.connected && state.isRecording) {
        vadVal.textContent = 'ACTIVE';
        vadVal.className = 'telemetry-value active';
      } else {
        vadVal.textContent = 'STANDBY';
        vadVal.className = 'telemetry-value standby';
      }
    }

    const engineRt = containerElement.querySelector('#engine-rt-val');
    if (engineRt) engineRt.textContent = state.whisperRealtime.toUpperCase();

    const engineBatch = containerElement.querySelector('#engine-batch-val');
    if (engineBatch) engineBatch.textContent = state.whisperBatch.toUpperCase();

    const llmCore = containerElement.querySelector('#llm-val');
    if (llmCore) {
      const displayModel = state.ollamaModel.split('/').pop() || state.ollamaModel;
      llmCore.textContent = displayModel.toUpperCase();
    }
  },

  destroy() {
    if (animFrameId) {
      cancelAnimationFrame(animFrameId);
      animFrameId = null;
    }
    // Remove the visibility listener to prevent orphaned handlers
    if (visibilityHandler) {
      document.removeEventListener('visibilitychange', visibilityHandler);
      visibilityHandler = null;
    }
    containerElement = null;
  }
};

export default view;
