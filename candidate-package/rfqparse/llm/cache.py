"""Content-addressed cache for model responses. Stdlib only.

Key = sha256 over canonical JSON of (model, prompt version, schema version, payload). Images are
passed as raw bytes and enter the key as their own sha256, never as base64. Nothing in the key
depends on the machine: no paths, no mtimes, no dict order, no directory listing order.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .. import config

SECRET_KEYS = {"authorization", "api_key", "apikey", "x-api-key", "headers", "openai_api_key"}


class CacheMiss(Exception):
    """Nothing cached for this key and there is no client (or no key) to ask the model."""

    def __init__(self, key: str, model: str):
        super().__init__(f"cache miss for model {model!r} (key {key[:12]}...) and no client available")
        self.key, self.model = key, model


def default_cache_dir() -> Path:
    """cache/ at the repo root, located from this file (rfqparse/llm/cache.py), not from the cwd."""
    return Path(__file__).resolve().parents[2] / "cache"


# ------------------------------------------------------------------ the key


def canonical(obj):
    """Plain JSON-able structure with bytes replaced by their sha256. Anything machine-dependent or
    unknown (Path, datetime, set ...) is refused rather than silently stringified."""
    if obj is None or isinstance(obj, (bool, int, str)):
        return obj
    if isinstance(obj, float):
        if not math.isfinite(obj):
            raise ValueError("NaN/Infinity cannot be part of a cache key")
        return obj
    if isinstance(obj, (bytes, bytearray, memoryview)):
        return {"$sha256": hashlib.sha256(bytes(obj)).hexdigest()}
    if isinstance(obj, dict):
        bad = [k for k in obj if not isinstance(k, str)]
        if bad:
            raise TypeError(f"payload keys must be strings, got {bad[0]!r}")
        return {k: canonical(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [canonical(v) for v in obj]
    raise TypeError(f"{type(obj).__name__} cannot be part of a cache key (convert it to str/number/bytes first)")


def generation_settings(model: str) -> dict:
    """The settings that shape a reply (they come from config, per model). They are part of the cache key, so a
    changed temperature or output limit never serves an old reply (for example one cut off by a smaller limit)."""
    st = {**config.DEFAULT_MODEL_SETTINGS, **config.MODEL_SETTINGS.get(model, {})}
    return {"temperature": st.get("temperature"), "max_output_tokens": st.get("max_completion_tokens")}


def canonical_json(model: str, prompt_version: str, schema_version: str, payload, settings: dict | None = None) -> str:
    settings = generation_settings(model) if settings is None else settings
    return json.dumps(
        {"model": model, "prompt_version": prompt_version, "schema_version": schema_version,
         "settings": canonical(settings), "payload": canonical(payload)},
        sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def make_key(model: str, prompt_version: str, schema_version: str, payload, settings: dict | None = None) -> str:
    return hashlib.sha256(canonical_json(model, prompt_version, schema_version, payload, settings).encode("utf-8")).hexdigest()


# ------------------------------------------------------------------ entries


@dataclass
class Entry:
    key: str
    model: str
    prompt_version: str
    schema_version: str
    response: object
    usage: dict
    created_at: str
    latency_s: float | None = None
    email_id: str | None = None
    generation: dict | None = None


def _strip_secrets(obj):
    if isinstance(obj, dict):
        return {k: _strip_secrets(v) for k, v in obj.items() if str(k).lower() not in SECRET_KEYS}
    if isinstance(obj, list):
        return [_strip_secrets(v) for v in obj]
    return obj


class Cache:
    def __init__(self, directory: Path | str | None = None):
        chosen = directory if directory is not None else config.CACHE_DIR
        self.dir = Path(chosen) if chosen is not None else default_cache_dir()
        self.corrupt_seen = 0   # unreadable entries met while looking up (each counted as a miss)

    def path_for(self, key: str) -> Path:
        return self.dir / f"{key}.json"

    def get(self, key: str) -> Entry | None:
        """The entry, or None. A missing, unreadable or malformed file is a miss, never an error."""
        p = self.path_for(key)
        try:
            raw = p.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except Exception:
            self.corrupt_seen += 1
            return None
        try:
            d = json.loads(raw)
            if not (isinstance(d, dict) and d.get("key") == key and "response" in d and isinstance(d.get("usage"), dict)
                    and isinstance(d.get("model"), str) and isinstance(d.get("prompt_version"), str)):
                raise ValueError("malformed entry")
            return Entry(key=key, model=d["model"], prompt_version=d["prompt_version"],
                         schema_version=str(d.get("schema_version", "")), response=d["response"], usage=d["usage"],
                         created_at=str(d.get("created_at", "")), latency_s=d.get("latency_s"), email_id=d.get("email_id"),
                         generation=d.get("generation"))
        except Exception:
            self.corrupt_seen += 1
            return None

    def put(self, key: str, *, model: str, prompt_version: str, schema_version: str, response, usage: dict,
            latency_s: float | None = None, email_id: str | None = None, generation: dict | None = None) -> Path:
        """Write the entry atomically: temp file in the same folder, then rename over the final name."""
        entry = {
            "key": key, "model": model, "prompt_version": prompt_version, "schema_version": schema_version,
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "email_id": email_id, "generation": generation, "latency_s": None if latency_s is None else round(latency_s, 3),
            "usage": _strip_secrets(usage), "response": _strip_secrets(response),
        }
        text = json.dumps(entry, indent=2, ensure_ascii=False, allow_nan=False, sort_keys=True) + "\n"
        self.dir.mkdir(parents=True, exist_ok=True)
        final = self.path_for(key)
        fd, tmp = tempfile.mkstemp(prefix=f".{key[:12]}.", suffix=".tmp", dir=self.dir)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
                f.write(text)
                f.flush()
                os.fsync(f.fileno())
            for attempt in range(4):  # Windows can hold the target briefly (antivirus, indexer)
                try:
                    os.replace(tmp, final)
                    break
                except PermissionError:
                    if attempt == 3:
                        raise
                    time.sleep(0.05 * (attempt + 1))
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return final

    def entries(self) -> tuple[list[Entry], int]:
        """All readable entries in a stable order (sorted by key), plus how many files were unreadable."""
        out, bad = [], 0
        if not self.dir.is_dir():
            return out, bad
        for p in sorted(self.dir.glob("*.json"), key=lambda q: q.name):
            e = self.get(p.stem)
            if e is None:
                bad += 1
            else:
                out.append(e)
        return out, bad
