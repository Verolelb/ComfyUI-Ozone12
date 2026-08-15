"""Measure every global preset on a reference signal and classify it.

For each preset we compute, on the processed vs dry signal:
- rms_delta_db  : overall loudness change (volume-type presets)
- bass/mid/high : band energy deltas (tonal-type presets)
- crest_delta   : peak-to-RMS change (dynamics/transient-type presets)

Classification (per preset, based on the measured deltas):
- 'loud'    : |rms| >= 1.5 dB, dominant volume change
- 'tonal'   : strong band tilt (max band delta >= 2 dB), small rms
- 'dynamic' : crest change >= 1 dB (transient shaping / compression)
- 'full'    : both loud and tonal (complete mastering)
- 'subtle'  : everything below thresholds

Output: JSON file with per-preset metrics + a summary by category.
"""
import json
import os
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ozone_engine as eng

SR = 44100
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "preset_measurements.json")

# Reference signal: full-range program with bass, mids, highs, transients.
rng = np.random.default_rng(3)
n = SR  # 1 s
t = np.linspace(0, 1, n, endpoint=False)
sig = (
    0.4 * np.sin(2 * np.pi * 100 * t)
    + 0.3 * np.sin(2 * np.pi * 800 * t)
    + 0.2 * np.sin(2 * np.pi * 5000 * t)
    + 0.35 * rng.standard_normal(n) * (0.6 + 0.4 * np.sin(2 * np.pi * 4 * t))
)
wav = np.stack([sig, sig]).astype(np.float32)


def rms_db(x):
    return 20 * np.log10(max(float(np.sqrt(np.mean(np.square(x)))), 1e-12))


def crest_db(x):
    return 20 * np.log10(max(float(np.max(np.abs(x))), 1e-12)) - rms_db(x)


def band_db(x, lo, hi):
    spec = np.fft.rfft(x)
    freqs = np.fft.rfftfreq(len(x), 1 / SR)
    mask = (freqs >= lo) & (freqs < hi)
    return 20 * np.log10(max(float(np.sqrt(np.mean(np.square(np.abs(spec[mask]))))), 1e-12))


def classify(m):
    rms = m["rms_delta_db"]
    tilt = max(m["bass_delta_db"], m["mid_delta_db"], m["high_delta_db"])
    crest = m["crest_delta_db"]
    loud = abs(rms) >= 1.5
    tonal = tilt >= 2.0
    dynamic = abs(crest) >= 1.0
    if loud and tonal:
        return "full"
    if loud:
        return "loud"
    if tonal:
        return "tonal"
    if dynamic:
        return "dynamic"
    return "subtle"


def main():
    presets = eng.discover_global_presets()
    results = []
    t0 = time.time()
    for i, p in enumerate(presets):
        try:
            y, info = eng._apply_global_preset(wav, SR, p["path"])
        except Exception as e:  # noqa: BLE001
            results.append({"preset": p["name"], "error": str(e)})
            continue
        y = np.asarray(y, dtype=np.float32)
        m = {
            "preset": p["name"],
            "rms_delta_db": round(rms_db(y[0]) - rms_db(wav[0]), 2),
            "bass_delta_db": round(band_db(y[0], 20, 150) - band_db(wav[0], 20, 150), 2),
            "mid_delta_db": round(band_db(y[0], 300, 2000) - band_db(wav[0], 300, 2000), 2),
            "high_delta_db": round(band_db(y[0], 4000, 12000) - band_db(wav[0], 4000, 12000), 2),
            "crest_delta_db": round(crest_db(y[0]) - crest_db(wav[0]), 2),
            "modules": [m2["section"] for m2 in info["modules"] if m2.get("enabled") and m2.get("params_applied")],
        }
        m["type"] = classify(m)
        results.append(m)
        if (i + 1) % 25 == 0:
            print(f"  {i+1}/{len(presets)} ({time.time()-t0:.0f}s)", flush=True)

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=1, ensure_ascii=False)

    # Summary by category
    counts = {}
    for m in results:
        if "error" in m:
            continue
        counts[m["type"]] = counts.get(m["type"], 0) + 1
    print("\n=== SUMMARY ===")
    for k in ("loud", "tonal", "dynamic", "full", "subtle"):
        print(f"  {k:8s}: {counts.get(k, 0)}")
    print("errors:", sum(1 for m in results if "error" in m))
    print(f"output: {OUT}")


if __name__ == "__main__":
    main()
