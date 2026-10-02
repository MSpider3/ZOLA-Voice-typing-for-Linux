import asyncio
import sys
import os
import time

async def eval_clipboard():
    print("\n--- Clipboard Injection Round-Trip Test ---")
    sys.path.append(os.path.abspath("zola-backend"))
    from injector import KeystrokeInjector
    injector = KeystrokeInjector()
    
    # Payload of 900 chars
    text = "A" * 900
    
    # Warmup
    await injector.inject_text("Warmup", 0.0)
    
    t0 = time.time()
    await injector.inject_text(text, 0.0)
    total_time_ms = (time.time() - t0) * 1000
    print(f"Total round-trip time for 900 chars: {total_time_ms:.2f} ms")

if __name__ == "__main__":
    asyncio.run(eval_clipboard())
