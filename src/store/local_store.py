from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any


def write_json(path: str | Path, data: Any) -> Path:
    file_path = Path(path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, ensure_ascii=True, indent=2)
    tmp_path = file_path.with_suffix(".tmp")
    tmp_path.write_text(payload + "\n", encoding="utf-8")
    os.replace(tmp_path, file_path)
    return file_path


def read_json(path: str | Path, default: Any | None = None) -> Any:
    file_path = Path(path)
    if not file_path.exists():
        return default
    try:
        return json.loads(file_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        logging.critical(
            "State file corrupted (JSONDecodeError) at %s: %s — returning empty default. "
            "De-duplication history may be lost for this run.",
            file_path,
            exc,
        )
        return default
