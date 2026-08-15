"""Core engine for hosting iZotope Ozone VST3 plugins inside ComfyUI.

Pure processing logic (no ComfyUI imports) so it can be tested standalone.

Requirements: pedalboard (already installed in ComfyUI's embedded Python).

Notes:
- Plugins are loaded lazily and cached per file path (loading Ozone takes a
  few seconds, so we keep the instance alive between executions).
- Each cached plugin is guarded by its own lock: ComfyUI can execute nodes
  from multiple threads, and pedalboard plugin instances are not
  thread-safe.
- The plugins must be authorized on this machine (iZotope Product Portal /
  iLok). If not, load_plugin() will raise.
"""

from __future__ import annotations

import math
import os
import re
import threading
import time
from pathlib import Path

import numpy as np

# Default folder: the standard Windows VST3 location where the iZotope
# plugins are installed on this machine.
DEFAULT_VST_DIR = r"C:\Program Files\Common Files\VST3\iZotope"

DEFAULT_BUFFER_SIZE = 8192

_plugin_cache: dict[str, object] = {}
_plugin_defaults: dict[str, dict] = {}
_cache_lock = threading.Lock()
_plugin_locks: dict[str, threading.Lock] = {}


def list_ozone_plugins(vst_dir: str | None = None) -> list[str]:
    """List the .vst3 plugin file names found in vst_dir (or the default folder)."""
    d = Path(vst_dir or DEFAULT_VST_DIR)
    if not d.is_dir():
        return []
    return sorted(p.name for p in d.glob("*.vst3"))


def _resolve_plugin_path(plugin_name: str, vst_dir: str | None = None) -> str:
    if os.path.isabs(plugin_name) and os.path.exists(plugin_name):
        return plugin_name
    return os.path.join(vst_dir or DEFAULT_VST_DIR, plugin_name)


def get_plugin(plugin_name: str, vst_dir: str | None = None):
    """Load (and cache) a VST3 plugin by file name. Raises if not found / not authorized."""
    path = _resolve_plugin_path(plugin_name, vst_dir)
    if not os.path.exists(path):
        raise FileNotFoundError(f"VST3 plugin not found: {path}")
    with _cache_lock:
        cached = _plugin_cache.get(path)
    if cached is not None:
        return cached

    from pedalboard import load_plugin

    plugin = load_plugin(path)
    with _cache_lock:
        _plugin_cache[path] = plugin
        _plugin_locks.setdefault(path, threading.Lock())
        _plugin_defaults[path] = _snapshot_parameters(plugin)
    return plugin


def _snapshot_parameters(plugin) -> dict:
    """Capture the raw value (0..1) of every parameter as factory defaults.

    We snapshot the *raw* normalized value instead of the display string:
    display-string setattr silently fails to stick for some parameters (e.g.
    the Dynamics comp thresholds), while setting raw_value always applies.
    """
    defaults = {}
    try:
        raw = plugin._get_parameters()
    except Exception:  # noqa: BLE001
        raw = {}
    for name, prm in raw.items():
        try:
            rv = getattr(prm, "raw_value", None)
            if rv is not None:
                defaults[name] = float(rv)
        except Exception:  # noqa: BLE001 - read-only params are skipped
            pass
    return defaults


def _restore_defaults(plugin, defaults: dict | None) -> None:
    """Reset every parameter of `plugin` to its factory defaults.

    Plugins are cached across executions (loading Ozone is slow), and a
    preset/parameter change sticks on the instance. Without a reset, the
    settings of one node (or one run) would leak into the next run of any
    other node using the same plugin. `plugin.process(reset=True)` only
    clears the DSP buffers, not the parameters, so we restore them here.
    """
    if not defaults:
        return
    try:
        raw = plugin._get_parameters()
    except Exception:  # noqa: BLE001
        raw = {}
    for name, value in defaults.items():
        prm = raw.get(name)
        if prm is None:
            continue
        try:
            prm.raw_value = value
        except Exception:  # noqa: BLE001 - read-only params are skipped
            pass


def _lock_for(plugin_name: str, vst_dir: str | None = None) -> threading.Lock:
    path = _resolve_plugin_path(plugin_name, vst_dir)
    with _cache_lock:
        return _plugin_locks.setdefault(path, threading.Lock())


def parameter_names(plugin_name: str, vst_dir: str | None = None) -> list[str]:
    """All parameter names exposed by the plugin (python-safe names)."""
    plugin = get_plugin(plugin_name, vst_dir)
    with _lock_for(plugin_name, vst_dir):
        return list(plugin.parameters.keys())


def _current_value(plugin, name):
    """Current value of a parameter: display string for continuous, bool for toggles."""
    try:
        return getattr(plugin, name)
    except Exception:  # noqa: BLE001
        return None


def parameter_values(plugin_name: str, vst_dir: str | None = None) -> dict:
    """Current value of every parameter (name -> display value)."""
    plugin = get_plugin(plugin_name, vst_dir)
    with _lock_for(plugin_name, vst_dir):
        return {name: _current_value(plugin, name) for name in plugin.parameters}


def extract_parameter_meta(plugin_name: str, vst_dir: str | None = None) -> list[dict]:
    """Extract widget-building metadata for every parameter of the plugin (live).

    Used by generate_param_cache.py to build param_cache.json. Each entry:
    name, bool (toggle vs continuous), unit, min/max/step, default, locked_dup.
    """
    plugin = get_plugin(plugin_name, vst_dir)
    with _lock_for(plugin_name, vst_dir):
        try:
            raw = plugin._get_parameters()
        except Exception:  # noqa: BLE001
            raw = {}
        metas: list[dict] = []
        for name, prm in raw.items():
            is_bool = prm.type is bool
            meta = {
                "name": name,
                "bool": is_bool,
                "unit": prm.label or prm.units or "",
                "min": None,
                "max": None,
                "step": None,
                "default": None,
                "locked_dup": "_locked" in name,
            }
            if is_bool:
                meta["default"] = bool(_current_value(plugin, name))
            else:
                # Some plugins report non-finite values (e.g. '-inf' dB). These
                # would serialize to 'Infinity'/'-Infinity' in JSON and break the
                # ComfyUI frontend (browser JSON.parse), so they are dropped.
                valid = list(getattr(prm, "valid_values", None) or [])
                floats = [
                    f for f in (_parse_display(v) for v in valid)
                    if f is not None and math.isfinite(f)
                ]
                if valid and len(floats) < len(valid):
                    # Choice parameter (e.g. 'Bell', 'Peak', 'RMS'): exposed as
                    # a dropdown, not a slider.
                    meta["choice"] = True
                    meta["choices"] = list(valid)
                    meta["default"] = _current_value(plugin, name)
                else:
                    if len(floats) >= 2 and max(floats) > min(floats):
                        meta["min"] = min(floats)
                        meta["max"] = max(floats)
                        meta["step"] = _median_step(sorted(floats))
                    cur = _parse_display(_current_value(plugin, name))
                    if cur is not None and math.isfinite(cur):
                        meta["default"] = cur
                    elif meta["min"] is not None:
                        meta["default"] = meta["min"]
                    if (
                        meta["default"] is not None
                        and meta["min"] is not None
                        and meta["max"] is not None
                    ):
                        meta["default"] = max(meta["min"], min(meta["max"], meta["default"]))
            metas.append(meta)
        return metas


def _rms_db(audio: np.ndarray) -> float:
    """Overall RMS level of [C, S] audio in dBFS (or -120 if silent)."""
    if audio.size == 0:
        return -120.0
    rms = float(np.sqrt(np.mean(np.square(audio.astype(np.float64)))))
    if rms < 1e-9:
        return -120.0
    return 20.0 * math.log10(rms)


def _crest_db(audio: np.ndarray) -> float:
    """Crest factor (peak/RMS ratio) of [C, S] audio in dB."""
    if audio.size == 0:
        return 0.0
    peak = float(np.max(np.abs(audio.astype(np.float64))))
    rms = float(np.sqrt(np.mean(np.square(audio.astype(np.float64)))))
    if rms < 1e-9 or peak < 1e-9:
        return 0.0
    return 20.0 * math.log10(peak / rms)


def _to_plugin_channels(audio: np.ndarray) -> np.ndarray:
    """Adapt [C, S] audio for the plugin: mono -> stereo, >2 channels -> first 2."""
    if audio.ndim != 2:
        raise ValueError(f"Expected [channels, samples] audio, got shape {audio.shape}")
    channels = audio.shape[0]
    if channels == 1:
        return np.repeat(audio, 2, axis=0)
    if channels > 2:
        return audio[:2]
    return audio


def _parse_display(value) -> float | None:
    """Parse a pedalboard display value ('-12,00') into a float."""
    try:
        return float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return None


def _snap_to_valid(value, valid):
    """Snap a numeric value to the nearest entry of the plugin's valid display list.

    Stepped VST3 parameters (like Ozone's) only accept the exact display
    strings they report (comma decimals, e.g. '-12,02'); any other value
    raises. We snap to the nearest valid one.
    """
    if not valid:
        return value
    floats = [_parse_display(v) for v in valid]
    best = None
    best_dist = float("inf")
    for i, f in enumerate(floats):
        if f is None:
            continue
        d = abs(f - value)
        if d < best_dist:
            best_dist, best = d, i
    if best is None:
        return value
    return valid[best]


def _parameter_object(plugin, name):
    try:
        return plugin._get_parameters()[name]
    except Exception:  # noqa: BLE001
        return None


def _lock_sibling(name: str) -> str | None:
    """Return the paired 'locked'/'unlocked' parameter name, if any.

    Ozone exposes gain knobs as two params (e.g. gain_output_l_locked_db /
    gain_output_l_unlocked_db); which one drives the audio depends on the
    internal link state, and the inactive one is silently ignored. We mirror
    the value to both so the user's setting always takes effect.
    """
    if "_unlocked" in name:
        return name.replace("_unlocked", "_locked")
    if "_locked" in name:
        return name.replace("_locked", "_unlocked")
    return None


def _set_parameter(plugin, name, value, warnings) -> bool:
    """Set one plugin parameter, snapping continuous values to valid steps."""
    targets = [name]
    sibling = _lock_sibling(name)
    if sibling and sibling in plugin.parameters:
        targets.append(sibling)
    ok = True
    for target in targets:
        prm = _parameter_object(plugin, target)
        try:
            if prm is not None and prm.type is bool:
                setattr(plugin, target, bool(value))
            elif prm is not None and getattr(prm, "valid_values", None):
                valid = prm.valid_values
                parsed = [_parse_display(v) for v in valid]
                if any(p is None for p in parsed):
                    # Choice param (e.g. ['Peak', 'RMS', 'Envelope']): the value
                    # may be a choice string or an index into the choices.
                    if isinstance(value, str) and value in valid:
                        setattr(plugin, target, value)
                    else:
                        try:
                            idx = int(round(float(value)))
                        except (TypeError, ValueError):
                            warnings.append(f"failed to set {target!r}: {value!r} is not a valid choice")
                            ok = False
                            continue
                        idx = max(0, min(idx, len(valid) - 1))
                        setattr(plugin, target, valid[idx])
                else:
                    numeric = value if isinstance(value, (int, float)) else _parse_display(value)
                    if numeric is None:
                        numeric = 0.0
                    setattr(plugin, target, _snap_to_valid(numeric, valid))
            else:
                setattr(plugin, target, value)
        except Exception as e:  # noqa: BLE001 - plugin errors vary
            warnings.append(f"failed to set {target!r}: {e}")
            ok = False
    return ok


def _median_step(values: list[float]) -> float | None:
    """Median spacing between consecutive sorted values."""
    if len(values) < 2:
        return None
    diffs = sorted(b - a for a, b in zip(values, values[1:]) if b > a)
    if not diffs:
        return None
    mid = len(diffs) // 2
    step = diffs[mid] if len(diffs) % 2 else (diffs[mid - 1] + diffs[mid]) / 2
    return round(step, 4) or None


FACTORY_PRESET_DIR = r"C:\Program Files\iZotope\Ozone 12\Presets"
GLOBAL_PRESET_DIR = os.path.join(FACTORY_PRESET_DIR, "Global Presets")


def _preset_dir_for(plugin_name: str) -> str:
    """e.g. 'Ozone 12 Maximizer.vst3' -> '<presets root>\\Maximizer Presets'"""
    module = plugin_name.replace("Ozone 12 ", "").replace(".vst3", "")
    return f"{module} Presets"


def discover_presets(plugin_name: str) -> list[dict]:
    """Presets available for this plugin (factory + user), each {name, path}."""
    sub = _preset_dir_for(plugin_name)
    roots = [
        Path(os.path.expandvars(r"%USERPROFILE%\Documents\iZotope\Ozone\User Presets")),
        Path(FACTORY_PRESET_DIR),
    ]
    presets: list[dict] = []
    for root in roots:
        d = root / sub
        if d.is_dir():
            for f in sorted(d.glob("*.xml")):
                presets.append({"name": f.stem, "path": str(f)})
    return presets


def discover_global_presets() -> list[dict]:
    """Presets from the 'Global Presets' folder, hierarchically organized.

    These are the full mastering-chain presets shown in Ozone's Preset
    Manager (All Purpose Mastering, Expert Curated Presets, Genre-Specific
    Mastering, Mix Bus, Ozone Legacy Presets, Stem Focus). Each entry is
    {name, path} where name is the relative path without extension, e.g.
    'All Purpose Mastering/*CD Master'.
    """
    root = Path(GLOBAL_PRESET_DIR)
    if not root.is_dir():
        return []
    presets: list[dict] = []
    for f in sorted(root.rglob("*.xml")):
        rel = f.relative_to(root)
        name = str(rel.with_suffix("")).replace(os.sep, "/")
        presets.append({"name": name, "path": str(f)})
    return presets


# Map a global-preset XML section (e.g. '<Dynamics Enabled="1">') to the
# standalone VST3 that hosts that module. The full 'Ozone 12.vst3' cannot be
# used directly: its rack state (which modules are inserted/enabled) is not
# exposed as parameters, so modules stay inert in it. Applying each section
# through the standalone module plugins reproduces the chain.
_GLOBAL_SECTION_PLUGIN = {
    "eq": "Ozone 12 Equalizer.vst3",
    "eq2": "Ozone 12 Equalizer.vst3",
    "dynamiceq": "Ozone 12 Dynamic EQ.vst3",
    "dynamics": "Ozone 12 Dynamics.vst3",
    "exciter": "Ozone 12 Exciter.vst3",
    "imager": "Ozone 12 Imager.vst3",
    "impact": "Ozone 12 Impact.vst3",
    "lowendfocus": "Ozone 12 Low End Focus.vst3",
    "masterrebalance": "Ozone 12 Master Rebalance.vst3",
    "matcheq": "Ozone 12 Match EQ.vst3",
    "maximizer": "Ozone 12 Maximizer.vst3",
    "spectralshaper": "Ozone 12 Spectral Shaper.vst3",
    "stabilizer": "Ozone 12 Stabilizer.vst3",
    "vintagecompressor": "Ozone 12 Vintage Compressor.vst3",
    "vintageeq": "Ozone 12 Vintage EQ.vst3",
    "vintagelimiter": "Ozone 12 Vintage Limiter.vst3",
    "vintagetape": "Ozone 12 Vintage Tape.vst3",
    "clarity": "Ozone 12 Clarity.vst3",
    # 'Global'/'Meters'/'Plugin' sections carry no audible params -> skipped.
}

# Master-chain order (matching the typical Ozone rack: EQs -> dynamics ->
# color/width -> limiter last). The XML lists sections alphabetically and
# does not encode the rack order.
_GLOBAL_SECTION_ORDER = [
    "eq", "eq2", "dynamiceq", "matcheq", "vintageeq",
    "exciter", "stabilizer", "spectralshaper", "imager", "impact",
    "masterrebalance", "lowendfocus", "clarity",
    "dynamics", "vintagecompressor", "vintagetape",
    "vintagelimiter", "maximizer",
]


def _apply_global_preset(
    waveform: np.ndarray,
    sample_rate: int,
    preset_path: str,
    vst_dir: str | None = None,
) -> tuple[np.ndarray, dict]:
    """Apply a global (full-chain) Ozone preset.

    Parses the preset XML and processes each enabled module section through
    its standalone plugin, chaining the modules in master order. The full
    'Ozone 12.vst3' is not used: its rack state isn't exposed as parameters.

    Returns (processed [C, S] float32, info dict).
    """
    import xml.etree.ElementTree as ET

    info: dict = {
        "preset": preset_path,
        "sample_rate": sample_rate,
        "input_shape": list(waveform.shape),
        "modules": [],
        "warnings": [],
    }
    try:
        tree = ET.parse(preset_path)
    except Exception as e:  # noqa: BLE001
        info["warnings"].append(f"preset parse failed: {e}")
        return waveform, info

    sections: dict[str, list] = {}
    for sec in tree.getroot():
        enabled = sec.get("Enabled")
        if enabled not in ("1", "0"):
            continue
        sections.setdefault(sec.tag.lower(), []).append(
            (enabled == "1", sec)
        )

    audio = np.asarray(waveform, dtype=np.float32)
    mono_in = audio.shape[0] == 1
    for section_name in _GLOBAL_SECTION_ORDER:
        for enabled, sec in sections.get(section_name, []):
            plugin_file = _GLOBAL_SECTION_PLUGIN.get(section_name)
            if plugin_file is None or not enabled:
                info["modules"].append({"section": section_name, "enabled": bool(enabled), "plugin": None})
                continue
            params = {el.get("ParamID"): el.get("Value") for el in sec if el.tag == "Param"}
            numeric = {}
            for k, v in params.items():
                try:
                    numeric[k] = float(v)
                except (TypeError, ValueError):
                    info["warnings"].append(f"non-numeric value, skipped: {k}={v!r}")
            applied, warn = _apply_params_by_name(
                plugin_file, numeric, sample_rate, vst_dir
            )
            stage_in = audio
            audio, proc_info = process(
                audio, sample_rate, plugin_file, vst_dir=vst_dir, params=applied
            )
            # Measured contribution of this module on the actual audio
            # (delta before/after this stage of the chain).
            effect_rms = _rms_db(audio) - _rms_db(stage_in)
            effect_crest = _crest_db(audio) - _crest_db(stage_in)
            info["modules"].append(
                {
                    "section": section_name,
                    "enabled": True,
                    "plugin": plugin_file,
                    "params_applied": proc_info["params_applied"],
                    "warnings": proc_info["warnings"],
                    "effect_rms_db": round(effect_rms, 2),
                    "effect_crest_db": round(effect_crest, 2),
                }
            )
            info["warnings"].extend(warn)
            if mono_in and audio.shape[0] > 1:
                audio = audio.mean(axis=0, keepdims=True)
    info["output_shape"] = list(audio.shape)
    return audio, info


def _apply_params_by_name(
    plugin_file: str,
    params: dict[str, float],
    sample_rate: int,
    vst_dir: str | None,
) -> tuple[dict, list[str]]:
    """Map XML param names (e.g. 'Band 5 Frequency') to the plugin's exposed
    python param names. Returns ({name: value}, warnings)."""
    plugin = get_plugin(plugin_file, vst_dir)
    raw = plugin._get_parameters()
    human = {p: _normalize(getattr(prm, "name", "") or "") for p, prm in raw.items()}
    mapped: dict = {}
    warnings: list[str] = []
    for xml_name, value in params.items():
        target = _match_xml_param(_normalize(xml_name), human, raw)
        if target is None:
            warnings.append(f"preset param not mappable, skipped: {xml_name!r}")
            continue
        mapped[target] = value
    return mapped, warnings


def _normalize(text: str) -> str:
    """Lowercase alphanumerics only, with camelCase split, for fuzzy matching."""
    text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", text)
    return " ".join(re.findall(r"[a-z0-9]+", text.lower()))


# Manual mappings for XML params whose names don't line up with the exposed
# VST3 params (keyed by normalized XML ParamID).
_XML_ALIASES = {
    "max character": "max_character",
    "character": "max_character",
    "source gain": "mrebal_gain",
    "input gain": "max_input_gain_db",
    "output level": "max_output_level",
    "margin": "max_output_level",
    "link input gain and output level": "max_link_input_gain_and_output_level",
    "link stereo amounts": "max_link_stereo_amounts",
    "soft clip on off": "max_soft_clip_on_off",
    "soft clip mix": "max_soft_clip_mix",
    "soft clip mode": "max_soft_clip_mode",
    "transient shaping amt": "max_transient_shaping_amt",
    "stereo ind sustain amt": "max_stereo_ind_sustain_amt",
    "stereo ind transient amt": "max_stereo_ind_transient_amt",
    "upward compression amt": "max_upward_compression_amt_db",
    "enable soft clipping": "max_soft_clip_on_off",
    "stereo link amounts linked": "max_link_stereo_amounts",
    "stereo transient link amount": "max_stereo_ind_transient_amt",
}


def _match_xml_param(xml_name: str, human: dict, params: dict) -> str | None:
    """Map a normalized XML ParamID to a python param name (best effort)."""
    for pname, h in human.items():
        if h == xml_name:
            return pname

    def find(candidates: list[str]) -> str | None:
        if not candidates:
            return None
        # Prefer the stereo/main chain over aux; prefer the most specific name.
        def rank(p: str) -> tuple:
            return (100 if "aux" in p else 0, -10 if "stereo" in p else 0, len(p))
        return min(candidates, key=rank)

    candidates = [p for p, h in human.items() if xml_name and h.endswith(xml_name)]
    hit = find(candidates)
    if hit is not None:
        return hit

    # Global presets name band params 'Band N X' while the VST3 exposes them
    # as 'X N' (e.g. 'Band 5 Frequency' -> 'Frequency 5'). Try the swapped form.
    m = re.match(r"^(.*\b)?band (\d+) (.+)$", xml_name)
    if m:
        prefix, num, rest = m.group(1) or "", m.group(2), m.group(3)
        swapped = f"{prefix}{rest} {num}".strip()
        candidates = [p for p, h in human.items() if swapped and h.endswith(swapped)]
        hit = find(candidates)
        if hit is not None:
            return hit
    # Plugin-aware aliases: the same XML param ('Margin' / 'Output Level')
    # maps to a different exposed param depending on the plugin. The global
    # preset XMLs set 'Margin' on the Vintage Limiter (its output ceiling,
    # vlim_ceiling) and on the Maximizer (max_output_level).
    if xml_name in ("margin", "output level"):
        if "max_output_level" in params:
            return "max_output_level"
        if "vlim_ceiling" in params:
            return "vlim_ceiling"
    return _XML_ALIASES.get(xml_name)


def _apply_xml_preset(plugin, preset_path: str, warnings: list) -> int:
    """Best-effort application of an iZotope .xml preset via parameter values.

    The XML format references internal params (e.g. 'Band 3 Comp Threshold')
    that don't all map to the VST3-exposed ones; unmatched entries are
    reported as warnings instead of failing.
    """
    import xml.etree.ElementTree as ET

    try:
        tree = ET.parse(preset_path)
    except Exception as e:  # noqa: BLE001
        warnings.append(f"preset parse failed: {e}")
        return []
    params = {p: prm for p, prm in plugin._get_parameters().items()}
    human = {p: _normalize(getattr(prm, "name", "") or "") for p, prm in params.items()}
    applied: list[str] = []
    # Which XML ParamID already mapped to each target, so a less specific
    # duplicate ('Character') doesn't overwrite a more specific one
    # ('Max Character') that maps to the same VST3 param.
    target_from: dict[str, str] = {}
    for el in tree.getroot().iter():
        if el.tag != "Param":
            continue
        xml_raw = el.get("ParamID") or ""
        xml_name = _normalize(xml_raw)
        value = el.get("Value")
        if not xml_name or value is None:
            continue
        target = _match_xml_param(xml_name, human, params)
        if target is None:
            warnings.append(f"preset param not mappable, skipped: {xml_raw!r}")
            continue
        prev = target_from.get(target)
        if prev is not None and prev != xml_name and prev.endswith(xml_name):
            # 'max character' already applied -> skip the bare 'character'.
            continue
        try:
            numeric = float(value)
        except ValueError:
            warnings.append(f"preset param non-numeric, skipped: {xml_raw!r}={value!r}")
            continue
        if _set_parameter(plugin, target, numeric, warnings):
            target_from[target] = xml_name
            applied.append(target)
    if not applied:
        warnings.append("no preset parameters could be applied")
    return applied


def load_preset_file(plugin, preset_path: str, warnings: list) -> tuple[bool, list[str]]:
    """Load a preset: raw VST3 state first, then iZotope XML (best effort).

    Returns (success, applied_param_names).
    """
    try:
        plugin.load_preset(preset_path)
        return True, ["<full plugin state>"]
    except Exception:  # noqa: BLE001 - not raw state; try XML
        pass
    applied = _apply_xml_preset(plugin, preset_path, warnings)
    return bool(applied), applied


def process(
    waveform: np.ndarray,
    sample_rate: int,
    plugin_name: str,
    vst_dir: str | None = None,
    preset_path: str | None = None,
    params: dict | None = None,
) -> tuple[np.ndarray, dict]:
    """Run the plugin on `waveform` ([C, S] float32 in [-1, 1]).

    Returns (processed [C, S] float32, info dict). Mono input is upmixed to
    stereo for processing and downmixed back to mono before returning.
    """
    waveform = np.asarray(waveform, dtype=np.float32)
    if waveform.ndim != 2:
        raise ValueError(f"Expected [channels, samples] audio, got shape {waveform.shape}")

    info: dict = {
        "plugin_name": plugin_name,
        "sample_rate": sample_rate,
        "input_shape": list(waveform.shape),
        "warnings": [],
        "params_applied": [],
    }
    mono_in = waveform.shape[0] == 1

    t0 = time.time()
    plugin = get_plugin(plugin_name, vst_dir)
    info["loaded_as"] = getattr(plugin, "name", plugin_name)
    lock = _lock_for(plugin_name, vst_dir)

    with lock:
        # Always start from factory defaults: the cached instance keeps the
        # state of the previous run (presets/params stick on the plugin).
        path = _resolve_plugin_path(plugin_name, vst_dir)
        with _cache_lock:
            defaults = _plugin_defaults.get(path)
        _restore_defaults(plugin, defaults)

        if preset_path:
            ok, applied = load_preset_file(plugin, preset_path, info["warnings"])
            if ok:
                info["preset"] = preset_path
                info["params_applied"].extend(applied)

        for name, value in (params or {}).items():
            if name not in plugin.parameters:
                info["warnings"].append(f"unknown parameter, skipped: {name!r}")
                continue
            if _set_parameter(plugin, name, value, info["warnings"]):
                info["params_applied"].append(name)

        audio_in = _to_plugin_channels(waveform)
        processed = plugin.process(
            audio_in, float(sample_rate), buffer_size=DEFAULT_BUFFER_SIZE, reset=True
        )
        processed = np.asarray(processed, dtype=np.float32)

        if mono_in and processed.shape[0] > 1:
            processed = processed.mean(axis=0, keepdims=True)
        processed = np.clip(processed, -1.0, 1.0)

    info["output_shape"] = list(processed.shape)
    info["process_time_s"] = round(time.time() - t0, 3)
    return processed, info
