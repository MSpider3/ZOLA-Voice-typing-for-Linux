import sys
import os
import numpy as np
from faster_whisper.vad import get_vad_model, get_speech_timestamps

def test_vad():
    print("\n--- VAD Parameter Comparison (Synthetic Audio) ---")
    
    sr = 16000
    # Create 3 seconds of audio: 1s silence, 1s tone (speech), 1s silence
    silence = np.zeros(sr, dtype=np.float32)
    t = np.linspace(0, 1.0, sr, endpoint=False)
    speech = (0.5 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
    audio = np.concatenate([silence, speech, silence])
    
    vad_model = get_vad_model()
    
    # Old params
    old_params = {"min_silence_duration_ms": 500, "speech_pad_ms": 100, "threshold": 0.5, "min_speech_duration_ms": 200}
    old_segments = get_speech_timestamps(audio, vad_model, sampling_rate=sr, **old_params)
    print(f"Old VAD (thresh=0.5, min_silence=500ms): {len(old_segments)} segments")
    for s in old_segments:
        print(f"  {s['start']/sr:.2f}s -> {s['end']/sr:.2f}s")
        
    # New params
    new_params = {"min_silence_duration_ms": 300, "speech_pad_ms": 100, "threshold": 0.4, "min_speech_duration_ms": 200}
    new_segments = get_speech_timestamps(audio, vad_model, sampling_rate=sr, **new_params)
    print(f"\nNew VAD (thresh=0.4, min_silence=300ms): {len(new_segments)} segments")
    for s in new_segments:
        print(f"  {s['start']/sr:.2f}s -> {s['end']/sr:.2f}s")

if __name__ == "__main__":
    test_vad()
