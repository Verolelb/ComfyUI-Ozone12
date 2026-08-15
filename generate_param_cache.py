"""Regenerate param_cache.json (widget metadata for all Ozone modules).

Run once (or after reinstalling Ozone / moving the VST folder):

    D:/ComfyUI_windows_portable/python_embeded/python.exe generate_param_cache.py

Loading all modules takes a minute or two. The cache makes ComfyUI startup
instant afterwards; node schemas are built from the JSON, not from live
plugin loads.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

# The embedded Python (python_embeded) uses a ._pth file that isolates
# sys.path, so the script's own directory is not included automatically.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from ozone_engine import DEFAULT_VST_DIR, extract_parameter_meta, list_ozone_plugins  # noqa: E402


def main() -> None:
    plugins = [p for p in list_ozone_plugins() if p != "Ozone 12.vst3"]
    if not plugins:
        print(f"No .vst3 found in {DEFAULT_VST_DIR}")
        return

    out: dict = {}
    for i, name in enumerate(plugins, 1):
        t0 = time.time()
        try:
            metas = extract_parameter_meta(name)
            out[name] = metas
            n_widgets = len([m for m in metas if not m["locked_dup"]])
            print(f"[{i}/{len(plugins)}] {name}: {len(metas)} params "
                  f"({n_widgets} widget-able) in {time.time() - t0:.1f}s")
        except Exception as e:  # noqa: BLE001 - keep going on individual failures
            print(f"[{i}/{len(plugins)}] {name}: FAILED ({e})")

    dest = Path(__file__).resolve().parent / "param_cache.json"
    dest.write_text(json.dumps(out, indent=1), encoding="utf-8")
    print(f"\nWritten {dest} ({dest.stat().st_size / 1024:.0f} KB, {len(out)} modules)")


if __name__ == "__main__":
    main()
