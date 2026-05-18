"""FAS bundle smoke test.

Validates that the FAS detection pipeline's static assets are reachable
from the running process — works both from source and from a PyInstaller
--onedir bundle. Loads each ONNX model with onnxruntime and runs fpcalc
--version so a packaging regression (missing DLL, broken path resolution,
mis-bundled binary) fails the build instead of crashing in production.

Run:
    python -m noc_beam --fas-smoke
    python -m noc_beam --fas-smoke --fas-smoke-output report.json

Exit codes:
    0 - all assets present and load cleanly
    1 - one or more assets missing or failed to load
    2 - onnxruntime not importable
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from noc_beam._native.chromaprint import fpcalc_path
from noc_beam.audio.models import model_path

# Keep in sync with build/MODELS.lock
EXPECTED_MODELS = [
    ("silero_vad", "silero_vad.onnx"),
    ("aasist", "aasist.onnx"),
    ("panns_cnn14", "Cnn14_16k.onnx"),
]


def _check_onnxruntime() -> tuple[bool, str]:
    try:
        import onnxruntime as ort  # type: ignore[import-untyped]

        return True, ort.__version__
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"


def _check_model(name: str, filename: str) -> dict[str, Any]:
    path = model_path(filename)
    entry: dict[str, Any] = {
        "name": name,
        "path": str(path),
        "exists": path.exists(),
        "loaded": False,
        "load_ms": None,
        "size_bytes": None,
        "error": None,
    }
    if not path.exists():
        entry["error"] = "file not present (run build/fetch_fas_models.py)"
        return entry

    entry["size_bytes"] = path.stat().st_size

    try:
        import onnxruntime as ort  # type: ignore[import-untyped]

        t0 = time.perf_counter()
        sess = ort.InferenceSession(
            str(path),
            providers=["CPUExecutionProvider"],
        )
        entry["load_ms"] = round((time.perf_counter() - t0) * 1000, 1)
        entry["loaded"] = True
        entry["inputs"] = [
            {"name": i.name, "shape": list(i.shape), "type": str(i.type)}
            for i in sess.get_inputs()
        ]
        entry["outputs"] = [
            {"name": o.name, "shape": list(o.shape), "type": str(o.type)}
            for o in sess.get_outputs()
        ]
    except Exception as e:  # noqa: BLE001
        entry["error"] = f"{type(e).__name__}: {e}"
    return entry


def _check_fpcalc() -> dict[str, Any]:
    path = fpcalc_path()
    entry: dict[str, Any] = {
        "path": str(path),
        "exists": path.exists(),
        "ok": False,
        "version": None,
        "error": None,
    }
    if not path.exists():
        entry["error"] = "fpcalc binary not present (run build/fetch_fas_models.py)"
        return entry
    try:
        # CREATE_NO_WINDOW: suppress console flash if smoke is ever run
        # from the windowed PyInstaller build (only fires in --fas-smoke
        # mode but worth keeping consistent with fas_fingerprint).
        proc = subprocess.run(
            [str(path), "-version"],
            capture_output=True,
            text=True,
            timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if proc.returncode == 0:
            entry["ok"] = True
            entry["version"] = (proc.stdout + proc.stderr).strip().splitlines()[0] if (
                proc.stdout or proc.stderr
            ) else ""
        else:
            entry["error"] = f"exit {proc.returncode}: {proc.stderr.strip()}"
    except Exception as e:  # noqa: BLE001
        entry["error"] = f"{type(e).__name__}: {e}"
    return entry


def run_fas_smoke() -> tuple[int, dict[str, Any]]:
    report: dict[str, Any] = {
        "ok": False,
        "frozen": bool(getattr(sys, "frozen", False)),
        "meipass": getattr(sys, "_MEIPASS", None),
        "onnxruntime": None,
        "models": [],
        "chromaprint": None,
        "errors": [],
    }

    ort_ok, ort_info = _check_onnxruntime()
    report["onnxruntime"] = {"importable": ort_ok, "info": ort_info}
    if not ort_ok:
        report["errors"].append(f"onnxruntime not importable: {ort_info}")
        return 2, report

    for name, filename in EXPECTED_MODELS:
        entry = _check_model(name, filename)
        report["models"].append(entry)
        if entry.get("error"):
            report["errors"].append(f"model {name}: {entry['error']}")

    report["chromaprint"] = _check_fpcalc()
    if not report["chromaprint"]["ok"]:
        report["errors"].append(
            f"chromaprint: {report['chromaprint'].get('error')}"
        )

    all_models_loaded = all(m.get("loaded") for m in report["models"])
    cp_ok = report["chromaprint"]["ok"]
    report["ok"] = all_models_loaded and cp_ok

    return (0 if report["ok"] else 1), report


def write_smoke_report(path: str | Path, report: dict[str, Any]) -> None:
    Path(path).write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
