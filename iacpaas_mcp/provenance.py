import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

SNAPSHOTS = Path(__file__).resolve().parent.parent / "logs" / "exports"


def capture(graph, path, backend, depth):
    # Both backends pass a fresh export object; avoid duplicating a large graph in RAM.
    graph.pop("_source", None)
    raw = json.dumps(graph, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    checksum = hashlib.sha256(raw.encode()).hexdigest()
    SNAPSHOTS.mkdir(parents=True, exist_ok=True)
    filename = SNAPSHOTS / (checksum + ".json")
    if not filename.exists() or not verify_snapshot({"export_sha256":checksum}):
        filename.write_text(raw, encoding="utf-8")
    graph["_source"] = {"export_sha256":checksum, "path":path, "backend":backend,
                        "retrieved_at":datetime.now(timezone.utc).isoformat(), "export_depth":depth,
                        "snapshot":checksum + ".json"}
    return graph


def verify_snapshot(source):
    checksum = source.get("export_sha256", "")
    if len(checksum) != 64 or any(char not in "0123456789abcdef" for char in checksum):
        return False
    try:
        with (SNAPSHOTS / (checksum + ".json")).open("rb") as handle:
            return hashlib.file_digest(handle,"sha256").hexdigest() == checksum
    except OSError:
        return False
