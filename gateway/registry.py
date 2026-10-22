"""Confined metadata inspection and a data-only, atomic model registry.

The administrator owns every executable, argument template and capability. Registry
records can supply only a model ID, engine/profile IDs and a confined artifact path.
"""

import copy
import fcntl
import json
import logging
import os
import re
import stat
import struct
import tempfile
from pathlib import Path

from fastapi import HTTPException

from .settings import select, summary

SAFE_ID = re.compile(r"[a-zA-Z0-9_.-]+\Z")
FIELDS = {"engine", "profile", "path", "model", "artifacts", "context", "reasoning"}
FORMATS = {"gguf", "strata", "kyojin", "native"}
UNAVAILABLE = "Registration unavailable; inspect its files, roots and profile"
logger = logging.getLogger(__name__)


def invalid(message):
    raise HTTPException(422, message)


class Registry:
    def __init__(self, config, state_dir):
        self.config = config
        self.path = Path(state_dir) / "models.json"
        self.roots = {
            name: Path(path).resolve() for name, path in config.get("model_roots", {}).items()
        }
        self.static = {
            (engine, model) for engine, e in config["engines"].items() for model in e["models"]
        }
        self.registered = set()
        self.unavailable = {}
        if self.path.exists():
            try:
                records = self._read()
            except (OSError, ValueError) as exc:
                raise ValueError("Persisted model registry is invalid; startup refused") from exc
            for record in records:
                try:
                    candidate, spec, _ = self.inspect(record)
                    self._install(candidate, spec)
                except (OSError, ValueError, HTTPException) as exc:
                    engine, model = record["engine"], record["model"]
                    detail = (
                        exc.detail
                        if isinstance(exc, HTTPException) and isinstance(exc.detail, str)
                        else UNAVAILABLE
                    )
                    logger.warning(
                        "Saved registration %s/%s unavailable: %s",
                        engine,
                        model,
                        " ".join(detail.split())[:240],
                    )
                    self.unavailable[(engine, model)] = UNAVAILABLE
                    models = config["engines"].get(engine, {}).get("models")
                    if models is not None and not any(
                        model in e["models"] for e in config["engines"].values()
                    ):
                        models[model] = {"enabled": False, "unavailable_reason": UNAVAILABLE}
                        self.registered.add((engine, model))

    def confined(self, path):
        try:
            resolved = Path(path).resolve(strict=True)
        except (OSError, ValueError, RuntimeError):
            invalid("Artifact does not exist or cannot be resolved")
        if not any(resolved.is_relative_to(root) for root in self.roots.values()):
            invalid("Artifact is outside configured model roots")
        return resolved

    def files(self, root=None, path=""):
        if root is None:
            return {"roots": [{"id": name, "label": name} for name in self.roots]}
        if root not in self.roots:
            raise HTTPException(404, "Unknown model root")
        relative = Path(path)
        if relative.is_absolute() or ".." in relative.parts:
            invalid("A confined relative directory is required")
        base = self.roots[root]
        directory = self.confined(base / relative)
        if not directory.is_relative_to(base) or not directory.is_dir():
            invalid("A directory inside the selected model root is required")
        entries = []
        try:
            children = sorted(
                directory.iterdir(), key=lambda p: (not p.is_dir(), p.name.casefold())
            )
            if len(children) > 10000:
                invalid("Directory has too many entries; choose a narrower model root")
            for child in children:
                # Never reveal names of escaping symlinks or special files.
                try:
                    resolved = child.resolve(strict=True)
                    if not resolved.is_relative_to(base) or not (
                        resolved.is_dir() or resolved.is_file()
                    ):
                        continue
                    entries.append(
                        {
                            "name": child.name,
                            "path": str(child.relative_to(base)),
                            "kind": "directory" if resolved.is_dir() else "file",
                        }
                    )
                except (OSError, RuntimeError):
                    continue
        except OSError:
            invalid("Model directory is unreadable")
        rel = str(directory.relative_to(base))
        return {
            "root": root,
            "path": "" if rel == "." else rel,
            "absolute_path": str(directory),
            "parent": None
            if directory == base
            else ("" if directory.parent == base else str(directory.parent.relative_to(base))),
            "entries": entries,
        }

    def profile(self, engine, name=None):
        if engine not in self.config["engines"]:
            invalid("Unknown engine")
        profiles = self.config["engines"][engine].get("profiles", {})
        if name is None and len(profiles) == 1:
            name = next(iter(profiles))
        if name not in profiles:
            invalid("Select an administrator launch profile")
        return name, profiles[name]

    def _json(self, path):
        path = self.confined(path)
        if not path.is_file() or path.stat().st_size > 4 * 1024 * 1024:
            invalid("Metadata must be a regular JSON file below 4 MiB")
        try:
            with path.open("rb") as file:
                value = json.load(file)
            if not isinstance(value, dict):
                invalid("Metadata must be a JSON object")
            return value
        except (ValueError, OSError, RecursionError):
            invalid("Invalid or unreadable JSON metadata")

    def _gguf(self, path):
        if not path.is_file() or path.suffix.lower() != ".gguf":
            invalid("A GGUF model file is required")
        artifacts = [path]
        split = re.fullmatch(r"(.+)-(\d{5})-of-(\d{5})\.gguf", path.name, re.I)
        if split:
            prefix, part, count = split.groups()
            count = int(count)
            if int(part) != 1 or not 1 <= count <= 1024:
                invalid("Select the first shard of a supported split GGUF")
            artifacts = [
                self.confined(path.with_name(f"{prefix}-{i:05d}-of-{count:05d}.gguf"))
                for i in range(1, count + 1)
            ]
        for artifact in artifacts:
            try:
                if not artifact.is_file():
                    invalid("GGUF shards must be regular files")
                with artifact.open("rb") as file:
                    header = file.read(24)
                if (
                    len(header) != 24
                    or header[:4] != b"GGUF"
                    or struct.unpack("<I", header[4:8])[0] not in (2, 3)
                ):
                    invalid("Invalid or unsupported GGUF header")
            except OSError:
                invalid("GGUF artifact is unreadable")
        return artifacts

    def _gguf_metadata(self, path):
        # Bound both I/O and work. Tensor bytes are never read or deserialized.
        metadata = {}
        with path.open("rb") as file:

            def read(size):
                if size < 0 or size > 16 * 1024 * 1024 or file.tell() + size > 16 * 1024 * 1024:
                    invalid("GGUF metadata exceeds inspection limits")
                data = file.read(size)
                if len(data) != size:
                    invalid("Truncated GGUF metadata")
                return data

            def number(fmt):
                return struct.unpack("<" + fmt, read(struct.calcsize("<" + fmt)))[0]

            def string():
                size = number("Q")
                if size > 65536:
                    invalid("GGUF metadata string exceeds inspection limits")
                return read(size).decode("utf-8")

            def value(kind, depth=0):
                if depth > 4:
                    invalid("GGUF metadata nesting is unsupported")
                scalar = {
                    0: "B",
                    1: "b",
                    2: "H",
                    3: "h",
                    4: "I",
                    5: "i",
                    6: "f",
                    7: "?",
                    10: "Q",
                    11: "q",
                    12: "d",
                }
                if kind in scalar:
                    return number(scalar[kind])
                if kind == 8:
                    return string()
                if kind == 9:
                    subtype, count = number("I"), number("Q")
                    if count > 1000000:
                        invalid("GGUF metadata array exceeds inspection limits")
                    if subtype in scalar:
                        read(count * struct.calcsize("<" + scalar[subtype]))
                    else:
                        for _ in range(count):
                            value(subtype, depth + 1)
                    return None
                invalid("Unsupported GGUF metadata type")

            read(16)
            count = number("Q")
            if count > 10000:
                invalid("GGUF metadata entry count exceeds inspection limits")
            for _ in range(count):
                key = string()
                item = value(number("I"))
                if key == "general.architecture" and isinstance(item, str):
                    metadata["architecture"] = item
                elif key.endswith(".context_length") and type(item) is int and item > 0:
                    metadata["metadata_context"] = min(metadata.get("metadata_context", item), item)
        return metadata

    def _pack(self, path, profile):
        if not path.is_dir():
            invalid("This launch profile requires a model pack directory")
        # Validate every companion (including otherwise unused sidecars). No code,
        # pickle, framework loading, remote configuration, or artifact execution.
        artifacts = []
        todo = [path]
        visited = set()
        while todo:
            directory = todo.pop()
            directory = self.confined(directory)
            if directory in visited:
                invalid("Cyclic or duplicate directory symlinks are unsupported")
            visited.add(directory)
            for child in directory.iterdir():
                resolved = self.confined(child)
                if resolved.is_dir():
                    todo.append(resolved)
                elif resolved.is_file() and stat.S_ISREG(resolved.stat().st_mode):
                    artifacts.append(resolved)
                else:
                    invalid("Special artifact files are unsupported")
                if len(artifacts) + len(todo) + len(visited) > 10000:
                    invalid("Model pack contains too many files")
        required = profile.get("required_files")
        if required is None and profile.get("format") == "strata":
            required = ["dense.bin", "index.txt", "native_experts.txt", "tokenizer/tokenizer.json"]
        if required is None:
            required = ["config.json"]
            if not any((path / name).is_file() for name in ("tokenizer.json", "tokenizer.model")):
                invalid("Model pack needs tokenizer.json or tokenizer.model")
            if not any(p.suffix in {".safetensors", ".exl3"} for p in artifacts):
                invalid("Model pack needs safe tensor weights")
        for name in required:
            item = self.confined(path / name)
            if not item.is_file():
                invalid("Required model pack companion is missing")
        for artifact in artifacts:
            trusted_dense = (
                profile.get("format") == "strata"
                and artifact == (path / "dense.bin")
                and "dense.bin" in required
            )
            if (
                artifact.suffix.lower() in {".pkl", ".pickle", ".pt", ".pth", ".bin"}
                and not trusted_dense
            ):
                invalid("Pickle or unknown binary model packs are unsupported")
            if artifact.name in {"config.json", "tokenizer_config.json"} or artifact.name.endswith(
                ".index.json"
            ):
                metadata = self._json(artifact)
                mapping = metadata.get("weight_map", {})
                if not isinstance(mapping, dict):
                    invalid("Invalid shard index")
                for sidecar in mapping.values():
                    if (
                        not isinstance(sidecar, str)
                        or Path(sidecar).is_absolute()
                        or ".." in Path(sidecar).parts
                    ):
                        invalid("Invalid tensor shard reference")
                    if not self.confined(path / sidecar).is_file():
                        invalid("Tensor shard is missing")
        return artifacts

    def inspect(self, record):
        try:
            return self._inspect(record)
        except (OSError, ValueError, RuntimeError, RecursionError):
            invalid("Artifact metadata is missing, unreadable or unsupported")

    def _inspect(self, record):
        if (
            not isinstance(record, dict)
            or set(record) - FIELDS
            or not {"engine", "path"} <= set(record)
        ):
            invalid("Registration accepts only engine, profile, path and model")
        if any(
            not isinstance(v, str) or not v or "\0" in v
            for k, v in record.items()
            if k not in {"artifacts", "context"}
        ):
            invalid("Registration values must be nonempty strings")
        engine = record["engine"]
        name, profile = self.profile(engine, record.get("profile"))
        if not Path(record["path"]).is_absolute():
            invalid("An absolute host model path is required")
        path = self.confined(record["path"])
        model = record.get("model") or re.sub(r"[^a-zA-Z0-9_.-]+", "-", path.stem).strip("-")
        if not model or not SAFE_ID.fullmatch(model) or len(model) > 128:
            invalid("Invalid model ID")
        supplied_artifacts = record.get("artifacts", {})
        declared_artifacts = profile.get("artifacts", {})
        if not isinstance(supplied_artifacts, dict) or set(supplied_artifacts) != set(
            declared_artifacts
        ):
            invalid("Named companion artifacts must match the administrator profile")
        named = {}
        native_shards = []
        for artifact_name, definition in declared_artifacts.items():
            value = supplied_artifacts[artifact_name]
            if not isinstance(value, str) or not Path(value).is_absolute():
                invalid("Companion artifacts need absolute host paths")
            companion = self.confined(value)
            if definition.get("kind") == "directory":
                if not companion.is_dir():
                    invalid("Companion artifact must be a directory")
                # Validate all sidecars, not only the files bound to argv.
                self._pack(companion, {"required_files": definition.get("required_files", [])})
            elif definition.get("kind") == "file":
                if not companion.is_file():
                    invalid("Companion artifact must be a file")
                if definition.get("format") == "gguf":
                    native_shards.extend(self._gguf(companion))
                if (
                    definition.get("extensions")
                    and companion.suffix not in definition["extensions"]
                ):
                    invalid("Unsupported companion artifact extension")
            else:
                invalid("Invalid administrator companion artifact declaration")
            named[artifact_name] = str(companion)
        fmt = profile["format"]
        if fmt == "gguf":
            artifacts = self._gguf(path)
        elif fmt in {"strata", "kyojin"}:
            artifacts = self._pack(path, profile)
        elif fmt == "native":
            if path.is_dir():
                artifacts = self._pack(path, profile)
            elif path.suffix.lower() == ".gguf":
                artifacts = self._gguf(path)
            elif path.is_file() and path.suffix.lower() in profile.get(
                "extensions", [".safetensors"]
            ):
                artifacts = [path]
                for companion in profile.get("required_files", []):
                    target = self.confined(path.parent / companion)
                    if not target.is_file():
                        invalid("Required native companion is missing")
                    artifacts.append(target)
            else:
                invalid("Unsupported native model artifact")
        else:
            invalid("Unsupported model pack format")
        if fmt == "strata" and (path / "native_experts.txt").exists():
            if not native_shards:
                invalid("Strata native-expert packs require an explicit GGUF native companion")
            index = self.confined(path / "native_experts.txt")
            if index.stat().st_size > 4 * 1024 * 1024:
                invalid("Strata native-expert index is too large")
            shard_names = {shard.name for shard in native_shards}
            for line in index.read_text().splitlines():
                if not line.strip() or line.lstrip().startswith("#"):
                    continue
                columns = line.split()
                if len(columns) not in {8, 9} or any(
                    not column.isdecimal() for column in columns[:8]
                ):
                    invalid("Unsupported Strata native-expert index")
                if len(columns) == 9:
                    refs = columns[8].split(",")
                    if any(ref not in shard_names for ref in refs):
                        invalid("Strata expert index references an undeclared native shard")
        candidate = {"engine": engine, "profile": name, "path": str(path), "model": model}
        if named:
            candidate["artifacts"] = named
        for key in ("context", "reasoning"):
            if key in record:
                candidate[key] = record[key]
        metadata = (
            self._gguf_metadata(path)
            if path.is_file() and path.suffix.lower() == ".gguf"
            else self._gguf_metadata(native_shards[0])
            if native_shards
            else {}
        )
        supported = profile.get("supported_architectures")
        if supported and metadata.get("architecture") not in supported:
            invalid("GGUF architecture is unknown or unsupported by this launch profile")
        spec = copy.deepcopy(
            {
                key: value
                for key, value in profile.items()
                if key
                not in {
                    "format",
                    "required_files",
                    "extensions",
                    "artifacts",
                    "supported_architectures",
                }
            }
        )
        bindings = {
            "path": str(path),
            "model": model,
            **{"artifact." + k: v for k, v in named.items()},
        }
        # Substitute once; braces inside supplied paths never become template code.
        spec["argv"] = [
            re.sub(r"\{([^{}]+)\}", lambda m: bindings.get(m[1], m[0]), arg) for arg in spec["argv"]
        ]
        if any("{" in str(v) or "}" in str(v) for v in [path, *named.values()]):
            invalid("Artifact paths containing template braces are unsupported")
        if "config_template" in spec:

            def bind(value):
                if isinstance(value, str):
                    return re.sub(r"\{([^{}]+)\}", lambda m: bindings.get(m[1], m[0]), value)
                if isinstance(value, list):
                    return [bind(item) for item in value]
                if isinstance(value, dict):
                    return {key: bind(item) for key, item in value.items()}
                return value

            spec["config_template"] = bind(spec["config_template"])
        if metadata.get("metadata_context") and "context_limits" in spec:
            limits = spec["context_limits"]
            limits["max"] = min(limits["max"], metadata["metadata_context"])
            if limits["min"] > limits["max"]:
                invalid("GGUF native context is below this profile's minimum")
            limits["presets"] = [n for n in limits["presets"] if n <= limits["max"]]
            spec["context"] = min(spec["context"], limits["max"])
        if metadata.get("metadata_context") and spec.get("context"):
            spec["context"] = min(spec["context"], metadata["metadata_context"])
        _, selected = select(spec, record.get("context"), record.get("reasoning"))
        if "context" in record:
            spec["context"] = selected["context"]
        if "reasoning" in record:
            spec["reasoning"] = selected["reasoning"]
        spec["upstream_model"] = spec.get("upstream_model", "{model}").replace("{model}", model)
        return candidate, spec, {"format": fmt, "artifact_count": len(artifacts), **metadata}

    def _read(self):
        if not self.path.exists():
            return []
        if self.path.stat().st_size > 8 * 1024 * 1024:
            raise ValueError("Registry too large")
        records = json.loads(self.path.read_text())
        if not isinstance(records, list) or len(records) > 10000:
            raise ValueError("Invalid registry")
        ids = set()
        for record in records:
            if (
                not isinstance(record, dict)
                or any(
                    not isinstance(record.get(key), str)
                    or len(record[key]) > 128
                    or not SAFE_ID.fullmatch(record[key])
                    for key in ("engine", "model")
                )
                or record["model"] in ids
            ):
                raise ValueError("Invalid registry identity")
            ids.add(record["model"])
        return records

    def unavailable_summary(self):
        return [
            {"engine": engine, "id": model, "reason": reason}
            for (engine, model), reason in sorted(self.unavailable.items())
        ]

    def _install(self, record, spec):
        model = record["model"]
        if any(model in e["models"] for e in self.config["engines"].values()):
            raise HTTPException(409, "Model ID is already registered")
        self.config["engines"][record["engine"]]["models"][model] = spec
        self.registered.add((record["engine"], model))

    def register(self, record):
        candidate, spec = self.persist(record)
        self._install(candidate, spec)
        return {"engine": candidate["engine"], **summary(candidate["model"], spec)}

    def uninstall(self, engine, model):
        if (engine, model) in self.registered:
            self.config["engines"][engine]["models"].pop(model, None)
            self.registered.discard((engine, model))
        self.unavailable.pop((engine, model), None)

    def persist_remove(self, engine, model):
        # Remove saved data even when its profile, engine or files no longer exist.
        with (self.path.parent / "models.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            records = self._read()
            kept = [r for r in records if (r["engine"], r["model"]) != (engine, model)]
            if len(kept) == len(records):
                raise HTTPException(
                    404, "Saved registration not found; static models use configuration"
                )
            self._write(kept)

    def _write(self, records):
        fd, temp = tempfile.mkstemp(prefix=".models-", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w") as out:
                json.dump(records, out, indent=2, allow_nan=False)
                out.write("\n")
                out.flush()
                os.fsync(out.fileno())
            os.replace(temp, self.path)
            directory = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if os.path.exists(temp):
                os.unlink(temp)

    def persist(self, record):
        """Perform blocking validation/durable writes without mutating the live catalog.

        The async API installs the returned spec on its event loop after this worker
        finishes, including when a caller disconnects during the durable write.
        """
        # flock also serializes separate gateway processes, avoiding lost updates.
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with (self.path.parent / "models.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            candidate, spec, _ = self.inspect(record)
            records = self._read()
            model = candidate["model"]
            if any(r["model"] == model for r in records) or any(
                model in e["models"] for e in self.config["engines"].values()
            ):
                raise HTTPException(409, "Model ID is already registered")
            records.append(candidate)
            self._write(records)
        return candidate, spec
