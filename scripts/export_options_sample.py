"""Publish already collected historical samples for the read-only options page.

No broker connection or new market-data request. Atomic file replacement lets
the existing local web server serve the page without a backend restart.
"""
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from server.options_sample import es_sample


if __name__ == "__main__":
    latest = es_sample()
    payload = {"samples": [es_sample(start) for start in latest["available_times"]]}
    path = ROOT / "frontend/public/assets/options/es-sample.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")))
    temp.replace(path)
    print(f"Exported {len(payload['samples'])} historical samples ({path.stat().st_size} bytes).")
