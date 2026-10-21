"""Maintenance lease: a co-located job runner pauses model loads through a private directory.

The runner writes lease.json, the gateway writes status.json, both inside a directory owned
by the gateway's Unix user. Every invalid lease is ignored (fail open). The gateway never
follows symlinks in the directory and replaces status.json by rename, never by writing
through an existing entry.
"""

import json
import logging
import math
import os
import re
import secrets
import stat
import time
from datetime import UTC, datetime
from pathlib import Path

log = logging.getLogger("gateway.maintenance")

LEASE_KEYS = {"version", "id", "holder", "reason", "acquired_at", "expires_at"}
ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{8,64}")
HOLDER_PATTERN = re.compile(r"[a-zA-Z0-9_.-]{1,64}")
MAX_FILE_BYTES = 4096
MAX_REVOKED = 64
NO_LEASE = {
    "held": False,
    "id": None,
    "holder": None,
    "reason": None,
    "expires_at": None,
    "invalid": False,
    "problem": None,
}


def validate_options(options):
    """Raise ValueError unless the maintenance block is well formed; returns normalised."""
    if not isinstance(options, dict) or set(options) - {"dir", "holders", "max_ttl_seconds"}:
        raise ValueError("maintenance accepts only dir, holders and max_ttl_seconds")
    directory = options.get("dir")
    if not isinstance(directory, str) or not Path(directory).is_absolute() or "\0" in directory:
        raise ValueError("maintenance.dir must be an absolute directory path")
    holders = options.get("holders")
    if (
        not isinstance(holders, list)
        or not holders
        or any(not isinstance(h, str) or not HOLDER_PATTERN.fullmatch(h) for h in holders)
    ):
        raise ValueError("maintenance.holders must be a nonempty list of safe holder names")
    ttl = options.get("max_ttl_seconds", 5400)
    if type(ttl) is not int or not 60 <= ttl <= 86400:
        raise ValueError("maintenance.max_ttl_seconds must be an integer from 60 to 86400")
    return {"dir": directory, "holders": list(holders), "max_ttl_seconds": ttl}


class DirectoryError(Exception):
    pass


def number(value):
    return type(value) in (int, float) and math.isfinite(value)


class Maintenance:
    def __init__(self, options, state_dir, clock=time.time):
        options = validate_options(options)
        self.path = options["dir"]
        self.holders = frozenset(options["holders"])
        self.max_ttl = options["max_ttl_seconds"]
        self.clock = clock
        self.revoked_path = Path(state_dir) / "maintenance-revoked.json"
        self.error = None
        self._revoked = self._load_revoked()
        try:
            os.lstat(self.path)
        except FileNotFoundError:
            try:
                Path(self.path).parent.mkdir(parents=True, exist_ok=True)
                os.mkdir(self.path, 0o700)
            except OSError as exc:
                self.error = f"cannot create maintenance directory: {exc.strerror}"
                log.error(self.error)
                return
        except OSError as exc:
            self.error = f"cannot inspect maintenance directory: {exc.strerror}"
            log.error(self.error)
            return
        try:
            os.close(self._open_dir())
        except DirectoryError as exc:
            log.error("%s", exc)

    # -- directory -----------------------------------------------------------------

    def _open_dir(self):
        try:
            fd = os.open(self.path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        except OSError as exc:
            self.error = f"cannot open maintenance directory: {exc.strerror}"
            raise DirectoryError(self.error) from None
        info = os.fstat(fd)
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            os.close(fd)
            self.error = "maintenance directory must be owned by the gateway user with mode 0700"
            raise DirectoryError(self.error)
        self.error = None
        return fd

    # -- revocations ---------------------------------------------------------------

    def _load_revoked(self):
        try:
            data = json.loads(self.revoked_path.read_text())
            ids = data["ids"]
            if isinstance(ids, list) and all(isinstance(i, str) for i in ids):
                return ids[-MAX_REVOKED:]
        except (OSError, ValueError, KeyError, TypeError):
            pass
        return []

    def _save_revoked(self):
        self.revoked_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.revoked_path.with_name(f".{self.revoked_path.name}.{secrets.token_hex(6)}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(fd, "w") as handle:
                json.dump({"version": 1, "ids": self._revoked}, handle)
                handle.flush()
                os.fsync(handle.fileno())
            os.rename(tmp, self.revoked_path)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise

    def revoked_ids(self):
        return list(self._revoked)

    # -- lease ---------------------------------------------------------------------

    def _read(self, dfd):
        """Return (document, None), (None, None) when absent, or (None, problem)."""
        try:
            fd = os.open(
                "lease.json",
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                dir_fd=dfd,
            )
        except FileNotFoundError:
            return None, None
        except OSError as exc:
            return None, f"lease file cannot be opened safely: {exc.strerror}"
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                return None, "lease file is not a regular file"
            if info.st_uid != os.getuid():
                return None, "lease file is not owned by the gateway user"
            if info.st_mode & ~0o600 & 0o7777:
                return None, "lease file mode must be 0600 or narrower"
            if info.st_size > MAX_FILE_BYTES:
                return None, "lease file is larger than 4 KiB"
            raw = os.read(fd, MAX_FILE_BYTES + 1)
        finally:
            os.close(fd)
        if len(raw) > MAX_FILE_BYTES:
            return None, "lease file is larger than 4 KiB"
        try:
            doc = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError, RecursionError):
            return None, "lease file is not valid JSON"
        if not isinstance(doc, dict):
            return None, "lease file must contain a JSON object"
        return doc, None

    def _check(self, doc, now):
        """Return a problem string, or None when the document is a structurally valid lease."""
        if set(doc) != LEASE_KEYS:
            return "lease keys must be exactly " + ", ".join(sorted(LEASE_KEYS))
        if type(doc["version"]) is not int or doc["version"] != 1:
            return "unsupported lease version"
        if not isinstance(doc["id"], str) or not ID_PATTERN.fullmatch(doc["id"]):
            return "lease id must be 8-64 characters of A-Z a-z 0-9 _ -"
        if doc["holder"] not in self.holders:
            return "lease holder is not allowed"
        reason = doc["reason"]
        if not isinstance(reason, str) or len(reason) > 200 or not reason.isprintable():
            return "lease reason must be at most 200 printable characters"
        if not number(doc["acquired_at"]) or not number(doc["expires_at"]):
            return "lease times must be finite epoch seconds"
        if doc["acquired_at"] >= doc["expires_at"]:
            return "lease acquired_at must precede expires_at"
        if doc["expires_at"] - now > self.max_ttl:
            return f"lease expires more than {self.max_ttl} seconds from now"
        return None

    def lease(self):
        try:
            dfd = self._open_dir()
        except DirectoryError:
            return dict(NO_LEASE)
        try:
            doc, problem = self._read(dfd)
        finally:
            os.close(dfd)
        now = self.clock()
        if doc is not None:
            problem = self._check(doc, now)
        if problem:
            log.warning("ignoring maintenance lease: %s", problem)
            return {**NO_LEASE, "invalid": True, "problem": problem}
        if doc is None or doc["expires_at"] <= now or doc["id"] in self._revoked:
            return dict(NO_LEASE)
        return {
            "held": True,
            "id": doc["id"],
            "holder": doc["holder"],
            "reason": doc["reason"],
            "expires_at": doc["expires_at"],
            "invalid": False,
            "problem": None,
        }

    def break_lease(self):
        lease = self.lease()
        if not lease["held"]:
            return None
        self._revoked = [*(i for i in self._revoked if i != lease["id"]), lease["id"]][
            -MAX_REVOKED:
        ]
        self._save_revoked()
        return lease

    def block_detail(self):
        """None when loads are allowed, else (detail, retry_after_seconds)."""
        lease = self.lease()
        if not lease["held"]:
            return None
        until = datetime.fromtimestamp(lease["expires_at"], UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        detail = f"Model loads are paused by '{lease['holder']}' until {until} ({lease['reason']})"
        return detail, max(1, math.ceil(lease["expires_at"] - self.clock()))

    # -- status --------------------------------------------------------------------

    def write_status(self, snapshot):
        document = {
            "version": 1,
            "updated_at": self.clock(),
            "phase": snapshot.get("phase"),
            "loaded_model": snapshot.get("loaded_model"),
            "active_requests": snapshot.get("active_requests", 0),
            "native_busy": snapshot.get("native_busy"),
            "lifecycle_busy": snapshot.get("lifecycle_busy", False),
            "lease": self.lease(),
            "revoked_ids": self.revoked_ids(),
        }
        try:
            dfd = self._open_dir()
        except DirectoryError:
            return False
        tmp = f".status.{os.getpid()}.{secrets.token_hex(6)}.tmp"
        try:
            fd = os.open(
                tmp,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600,
                dir_fd=dfd,
            )
            try:
                os.fchmod(fd, 0o600)
                data = json.dumps(document, allow_nan=False).encode()
                if os.write(fd, data) != len(data):
                    raise OSError("short write")
            finally:
                os.close(fd)
            os.rename(tmp, "status.json", src_dir_fd=dfd, dst_dir_fd=dfd)
            return True
        except (OSError, ValueError) as exc:
            log.warning("cannot write maintenance status: %s", exc)
            try:
                os.unlink(tmp, dir_fd=dfd)
            except OSError:
                pass
            return False
        finally:
            os.close(dfd)
