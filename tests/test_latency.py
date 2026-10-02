import os
import sys
import time
import numpy as np
from faster_whisper import WhisperModel

def test_perceived_latency():
    print("\n--- Perceived Latency Test (Synthetic Audio) ---")
    
    # Generate 1.5 seconds of a 440Hz sine wave to simulate speech
    sr = 16000
    t = np.linspace(0, 1.5, int(sr * 1.5), endpoint=False)
    chunk = (0.5 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
    
    for model_name in ["tiny", "base", "small"]:
        print(f"\nLoading {model_name}...")
        threads = max(1, (os.cpu_count() or 2) // 2)
        model = WhisperModel(model_name, device="cpu", compute_type="int8", cpu_threads=threads)
        
        # Warmup
        model.transcribe(chunk, beam_size=1)
        
        t0 = time.time()
        model.transcribe(chunk, beam_size=1, vad_filter=True)
        t_infer = time.time() - t0
        
        perceived = 0.7 + t_infer
        print(f"[{model_name}] Inference: {t_infer:.2f}s | Silence Wait: 0.7s | Total Perceived Latency: {perceived:.2f}s")
        del model

if __name__ == "__main__":
    test_perceived_latency()
