from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))
from app.data import ingest


if __name__ == "__main__":
    print(json.dumps(ingest(), ensure_ascii=False, indent=2))
