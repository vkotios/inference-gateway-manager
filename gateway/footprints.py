"""Recorded memory footprints and the model-agnostic fit test.

A footprint is how much host memory a loaded model (a chat engine or an auxiliary
worker) holds. Records come from measurement (see ``footprint_recorder``) or from an
administrator-supplied estimate. Whether a combination of models fits is computed from
these records only, so no model family is special-cased.
"""

import hashlib
import json
import os
import tempfile
import time
from dataclasses import dataclass, replace
from pathlib import Path

CHAT = "chat"
KINDS = (CHAT, "transcription", "embeddings")
SOURCES = ("measured", "estimate")
VERSION = 1


@dataclass(frozen=True)
class Footprint:
    kind: str
    key: str
    steady_bytes: int
    peak_bytes: int = 0
    request_bytes: int = 0
    context: int | None = None
    fingerprint: str | None = None
    source: str = "measured"
    recorded_at: float = 0.0
    note: str = ""
    requests_observed: int = 0

    @property
    def request_estimate_bytes(self):
        """Extra memory one request may need.

        Until a request has actually been observed an auxiliary model is assumed to
        double its resident size while serving (conservative); chat entries have none.
        """
        if self.kind == CHAT:
            return self.request_bytes
        return self.request_bytes if self.requests_observed else self.steady_bytes

    @property
    def needs_bytes(self):
        """Conservative memory this entry can hold: the high-water mark, never below steady."""
        return max(
            self.steady_bytes, self.peak_bytes, self.steady_bytes + self.request_estimate_bytes
        )

    def public(self):
        return {
            "kind": self.kind,
            "key": self.key,
            "steady_bytes": self.steady_bytes,
            "peak_bytes": self.peak_bytes,
            "request_bytes": self.request_bytes,
            "requests_observed": self.requests_observed,
            "context": self.context,
            "source": self.source,
            "recorded_at": self.recorded_at,
            "note": self.note,
        }


def _valid(record):
    return (
        isinstance(record, Footprint)
        and record.kind in KINDS
        and isinstance(record.key, str)
        and bool(record.key)
        and record.source in SOURCES
        and all(
            type(v) is int and v >= 0
            for v in (
                record.steady_bytes,
                record.peak_bytes,
                record.request_bytes,
                record.requests_observed,
            )
        )
        and record.steady_bytes > 0
        and (record.context is None or (type(record.context) is int and record.context > 0))
        and (record.fingerprint is None or isinstance(record.fingerprint, str))
    )


class FootprintStore:
    """Durable records plus in-memory administrator seeds.

    A measured record is valid only for the launch fingerprint it was taken under and
    covers loads at the same or a smaller context. Seeds carry no fingerprint: they stay
    valid until replaced by a measurement, and are always marked as estimates.
    """

    def __init__(self, path=None, seeds=()):
        self.path = Path(path) if path else None
        self.seeds = [s for s in seeds if _valid(s)]
        self.records = {}
        self.error = None
        self._load()

    @staticmethod
    def _identity(record):
        return (record.kind, record.key, record.fingerprint, record.context, record.source)

    def _load(self):
        if self.path is None or not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text())
            if data.get("version") != VERSION or not isinstance(data.get("records"), list):
                raise ValueError("unsupported footprint file")
            for item in data["records"]:
                record = Footprint(**item)
                if _valid(record):
                    self.records[self._identity(record)] = record
        except (OSError, ValueError, TypeError, AttributeError):
            # A damaged file must never grant admission; start with no measurements.
            self.records = {}
            self.error = "Footprint records could not be read; all models are uncalibrated"

    def _save(self):
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": VERSION,
            "records": [r.__dict__ for r in self.records.values()],
        }
        fd, name = tempfile.mkstemp(prefix=".footprints-", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w") as out:
                json.dump(payload, out, allow_nan=False, indent=1)
                out.flush()
                os.fsync(out.fileno())
            os.chmod(name, 0o600)
            os.replace(name, self.path)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def lookup(self, kind, key, fingerprint, context=None):
        """Best valid record for this model and launch, or None when uncalibrated."""
        candidates = []
        for record in (*self.records.values(), *self.seeds):
            if record.kind != kind or record.key != key:
                continue
            if record.source == "measured" and record.fingerprint != fingerprint:
                continue  # taken under different launch settings
            if context is not None and record.context is not None and record.context < context:
                continue  # does not cover this context
            candidates.append(record)
        if not candidates:
            return None
        candidates.sort(
            key=lambda r: (r.source != "measured", r.context if r.context is not None else 0)
        )
        return candidates[0]

    def record(self, footprint):
        """Store a measurement; the same launch and context keep their highest peaks."""
        if not _valid(footprint):
            raise ValueError("Invalid footprint")
        if not footprint.recorded_at:
            footprint = replace(footprint, recorded_at=time.time())
        previous = self.records.get(self._identity(footprint))
        if previous is not None:
            footprint = replace(
                footprint,
                peak_bytes=max(previous.peak_bytes, footprint.peak_bytes),
                request_bytes=max(previous.request_bytes, footprint.request_bytes),
                requests_observed=max(previous.requests_observed, footprint.requests_observed),
            )
        self.records[self._identity(footprint)] = footprint
        self._save()
        return footprint

    def forget(self, kind, key):
        removed = [i for i in self.records if i[0] == kind and i[1] == key]
        for ident in removed:
            del self.records[ident]
        if removed:
            self._save()
        return len(removed)

    def listing(self, fingerprints=None):
        """Public view; marks measured records whose launch fingerprint no longer matches."""
        fingerprints = fingerprints or {}
        rows = []
        for record in (*self.records.values(), *self.seeds):
            row = record.public()
            expected = fingerprints.get((record.kind, record.key))
            row["stale"] = bool(
                record.source == "measured"
                and expected is not None
                and expected != record.fingerprint
            )
            rows.append(row)
        return rows


def seeds_from_config(config):
    """Administrator estimates from ``memory_footprints``: always marked as estimates."""
    seeds = []
    for item in config.get("memory_footprints", []):
        seeds.append(
            Footprint(
                item["kind"],
                item["key"],
                item["steady_bytes"],
                item.get("peak_bytes", item["steady_bytes"]),
                item.get("request_bytes", 0),
                item.get("context"),
                None,
                "estimate",
                0.0,
                item.get("note", "administrator estimate"),
                1 if item.get("request_bytes") else 0,
            )
        )
    return seeds


def fingerprint(parts):
    """Stable short hash of the launch-defining values (never includes credentials)."""
    blob = json.dumps(parts, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()[:24]


def chat_fingerprint(engine, model, spec):
    """Launch fingerprint independent of the context (context is tracked separately)."""
    return fingerprint(
        {
            "engine": engine,
            "model": model,
            "argv": spec.get("_argv_template", spec.get("argv")),
            "env": spec.get("env", {}),
            "cwd": spec.get("cwd"),
            "config": spec.get("config_template", spec.get("_config_template")),
        }
    )


def path_identity(path):
    """``[total bytes, file count]`` of a model file or directory, or None if unreadable.

    Deliberately ignores modification times: caches or lock files appearing beside the
    weights must not invalidate a measurement; a change to the weights changes the size.
    """
    path = Path(path)
    try:
        if path.is_file():
            return [path.stat().st_size, 1]
        if not path.is_dir():
            return None
        size = count = 0
        for root, dirs, files in os.walk(path):
            dirs[:] = [d for d in dirs if not d.startswith(".") and d != "__pycache__"]
            for name in files:
                if name.startswith("."):
                    continue  # hidden cache / lock files are not part of the model
                try:
                    size += os.stat(os.path.join(root, name)).st_size
                    count += 1
                except OSError:
                    continue
        return [size, count]
    except OSError:
        return None


def aux_fingerprint(kind, model, spec):
    path = Path(spec["path"])
    return fingerprint(
        {
            "kind": kind,
            "model": model,
            "path": str(path),
            "threads": spec.get("threads", 4),
            "id": path_identity(path),
        }
    )


@dataclass(frozen=True)
class FitDecision:
    fits: bool
    reason: str  # ok | over_budget | uncalibrated
    needed_bytes: int
    budget_bytes: int
    total_bytes: int
    reserve_bytes: int
    baseline_bytes: int
    parts: tuple = ()
    missing: tuple = ()

    @property
    def short_bytes(self):
        return max(0, self.needed_bytes - self.budget_bytes)

    def public(self):
        return {
            "fits": self.fits,
            "reason": self.reason,
            "needed_bytes": self.needed_bytes,
            "budget_bytes": self.budget_bytes,
            "total_bytes": self.total_bytes,
            "reserve_bytes": self.reserve_bytes,
            "baseline_bytes": self.baseline_bytes,
            "parts": [{"kind": k, "key": n, "bytes": b} for k, n, b in self.parts],
            "missing": list(self.missing),
        }


def fit(total_bytes, reserve_bytes, baseline_bytes, entries):
    """Do these models fit together?  ``entries`` are ``(kind, key, Footprint | None)``.

    Needed memory is the sum of each entry's conservative need. A missing footprint makes
    the combination unverifiable and therefore not fitting (fail closed).
    """
    budget = max(0, total_bytes - reserve_bytes - baseline_bytes)
    parts, missing, needed = [], [], 0
    for kind, key, record in entries:
        if record is None:
            missing.append(f"{kind}:{key}")
            continue
        parts.append((kind, key, record.needs_bytes))
        needed += record.needs_bytes
    if missing:
        reason, ok = "uncalibrated", False
    elif needed > budget:
        reason, ok = "over_budget", False
    else:
        reason, ok = "ok", True
    return FitDecision(
        ok,
        reason,
        needed,
        budget,
        total_bytes,
        reserve_bytes,
        baseline_bytes,
        tuple(parts),
        tuple(missing),
    )
