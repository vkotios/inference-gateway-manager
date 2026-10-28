"""Opt-in source updates with immutable revisions, rollback and private receipts.

Only administrator recipes supply repositories, commands and test models. Remote
controllers select a discovered revision; they cannot supply code or launch argv.
"""

import asyncio
import copy
import hashlib
import json
import os
import platform
import re
import secrets
import signal
import time
from pathlib import Path
from urllib.parse import quote, urlencode

import httpx
import psutil
from fastapi import HTTPException

from . import __version__
from .core import finish_cleanup, identity, signal_identity
from .settings import apply_reasoning

SAFE = re.compile(r"[a-zA-Z0-9_.-]+\Z")
SHA = re.compile(r"[0-9a-f]{40}\Z")
REPO = re.compile(r"[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+\Z")
ISSUES = "https://github.com/vkotios/inference-gateway-manager/issues/new"
FAILURE_CODES = {
    "command_output_limit",
    "command_failed",
    "missing_release_executable",
    "incompatible_command_contract",
    "stream_validation_failed",
    "answer_validation_failed",
    "test_output_limit",
    "test_occupancy_unknown",
    "native_context_mismatch",
}


def failure_code(error):
    if isinstance(error, TimeoutError):
        return "timeout"
    if isinstance(error, HTTPException):
        return "busy_or_unavailable"
    if isinstance(error, RuntimeError) and str(error) in FAILURE_CODES:
        return str(error)
    return "runtime_or_protocol_failure"


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def private_json(path, value):
    """Durable replace; never follow an existing state symlink."""
    path = Path(path)
    temp = path.with_name(path.name + "." + secrets.token_hex(8))
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w") as output:
            json.dump(value, output, allow_nan=False)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temp, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temp.unlink(missing_ok=True)


def confined_relative(value):
    return (
        isinstance(value, str)
        and value
        and not Path(value).is_absolute()
        and ".." not in Path(value).parts
    )


def validate_options(config):
    recipes = config.get("engine_updates", {})
    if not isinstance(recipes, dict):
        raise ValueError("engine_updates must map engine IDs to administrator recipes")
    for engine, recipe in recipes.items():
        if engine not in config["engines"] or not isinstance(recipe, dict):
            raise ValueError("Update recipe needs a configured engine")
        fields = {
            "repository",
            "installed_root",
            "channel",
            "branch",
            "build",
            "checks",
            "executable",
            "tests",
            "build_env",
            "build_timeout",
            "test_timeout",
            "version_argv",
        }
        if (
            set(recipe) - fields
            or not isinstance(recipe.get("repository"), str)
            or not REPO.fullmatch(recipe["repository"])
        ):
            raise ValueError("Update source must be a public GitHub owner/repository")
        root = recipe.get("installed_root")
        if (
            not isinstance(root, str)
            or not Path(root).is_absolute()
            or Path(root).resolve() == Path("/")
        ):
            raise ValueError("Update installed_root must be a dedicated absolute directory")
        for model_root in config.get("model_roots", {}).values():
            if Path(model_root).resolve().is_relative_to(Path(root).resolve()) or Path(
                root
            ).resolve().is_relative_to(Path(model_root).resolve()):
                raise ValueError("Keep model roots separate from updatable engine installations")
        if not isinstance(recipe.get("channel", "releases"), str) or recipe.get(
            "channel", "releases"
        ) not in {"releases", "tags", "branch"}:
            raise ValueError("Update channel must be releases, tags or branch")
        if not isinstance(recipe.get("branch", "main"), str) or not re.fullmatch(
            r"[a-zA-Z0-9_./-]+", recipe.get("branch", "main")
        ):
            raise ValueError("Invalid update branch")
        if not confined_relative(recipe.get("executable")):
            raise ValueError("Update executable must be confined to its release")
        for field in ("build", "checks"):
            commands = recipe.get(field)
            if not isinstance(commands, list) or not commands or len(commands) > 20:
                raise ValueError("Update build/checks need bounded administrator argv lists")
            for command in commands:
                argv = command.get("argv") if isinstance(command, dict) else command
                if (
                    not isinstance(argv, list)
                    or not argv
                    or any(not isinstance(a, str) or not a or "\0" in a for a in argv)
                ):
                    raise ValueError("Invalid update command argv")
                if not (Path(argv[0]).is_absolute() or argv[0].startswith("{release}/")):
                    raise ValueError("Update executables must be absolute or release-local")
                if field == "checks" and (
                    not isinstance(command, dict)
                    or set(command) != {"argv", "contains"}
                    or not isinstance(command["contains"], list)
                    or not command["contains"]
                    or any(not isinstance(s, str) or not s for s in command["contains"])
                ):
                    raise ValueError("Compatibility checks need argv and required output strings")
                if field == "build" and not isinstance(command, list):
                    raise ValueError("Build commands must be argv arrays")
        tests = recipe.get("tests")
        if not isinstance(tests, list) or not 1 <= len(tests) <= 8:
            raise ValueError(
                "Declare one test model per engine, plus any additional affected profiles"
            )
        ids = []
        for test in tests:
            if (
                not isinstance(test, dict)
                or set(test) != {"model", "context", "expected"}
                or not isinstance(test.get("model"), str)
                or not SAFE.fullmatch(test["model"])
                or type(test.get("context")) is not int
                or not 1024 <= test["context"] <= 32768
                or not isinstance(test.get("expected"), str)
                or not re.fullmatch(r"[A-Z0-9_]{3,40}", test["expected"])
            ):
                raise ValueError(
                    "Test needs a model ID, modest context and an uppercase expected token"
                )
            ids.append(test["model"])
        if len(set(ids)) != len(ids):
            raise ValueError("Update test model IDs must be unique")
        env = recipe.get("build_env", {})
        if not isinstance(env, dict) or any(
            not isinstance(k, str)
            or not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]*", k)
            or not isinstance(v, str)
            or "\0" in v
            for k, v in env.items()
        ):
            raise ValueError("Invalid update build environment")
        probe = recipe.get("version_argv")
        if probe is not None and (
            not isinstance(probe, list)
            or not probe
            or any(not isinstance(v, str) or not v or "\0" in v for v in probe)
            or not (Path(probe[0]).is_absolute() or probe[0].startswith("{installed}/"))
        ):
            raise ValueError("version_argv must be an administrator-owned absolute argv")
        for name, default, upper in (("build_timeout", 1800, 7200), ("test_timeout", 120, 600)):
            value = recipe.get(name, default)
            if type(value) is not int or not 1 <= value <= upper:
                raise ValueError("Invalid update timeout")
    return recipes


def rewrite_root(value, old, new):
    old = old.rstrip("/")
    if isinstance(value, str) and (value == old or value.startswith(old + "/")):
        return str(new) + value[len(old) :]
    if isinstance(value, list):
        return [rewrite_root(v, old, new) for v in value]
    if isinstance(value, dict):
        return {k: rewrite_root(v, old, new) for k, v in value.items()}
    return value


def command_env(extra=None):
    # Build tools must not inherit control/inference credentials, session tokens
    # or watchdog ownership. Recipes may deliberately provide runtime tool paths.
    env = {
        k: v
        for k, v in os.environ.items()
        if not re.search(
            r"KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|^IGM_|^NOTIFY_|^WATCHDOG_|^GIT_", k, re.I
        )
    }
    env.update(extra or {})
    env.update(GIT_TERMINAL_PROMPT="0", GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL="/dev/null")
    return env


async def run(argv, cwd, timeout, env=None):
    """Bound command output and terminate tagged owned children on timeout/cancel."""
    run_id = secrets.token_hex(24)
    environment = {**command_env(env), "IGM_UPDATE_RUN_ID": run_id}
    process = await asyncio.create_subprocess_exec(
        *argv,
        cwd=cwd,
        env=environment,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        start_new_session=True,
    )
    try:
        async with asyncio.timeout(timeout):
            chunks, size = [], 0
            while chunk := await process.stdout.read(65536):
                size += len(chunk)
                if size > 1024 * 1024:
                    raise RuntimeError("command_output_limit")
                chunks.append(chunk)
            await process.wait()
        if process.returncode:
            raise RuntimeError("command_failed")
        return b"".join(chunks).decode(errors="replace")
    finally:
        # Signal only identities bearing this command's private ownership tag.
        # A detached child or a recycled PID must not become unrelated work.
        owned = set()
        for candidate in psutil.process_iter(["pid"]):
            try:
                if candidate.environ().get("IGM_UPDATE_RUN_ID") == run_id:
                    item = identity(candidate.pid)
                    if item:
                        owned.add(item)
            except psutil.Error:
                continue
        for item in owned:
            signal_identity(item, signal.SIGKILL)
        await process.wait()


class Updates:
    def __init__(self, config, state_dir):
        self.config = config
        self.recipes = validate_options(config)
        self.root = Path(state_dir) / "engine-updates"
        self.path = self.root / "state.json"
        self.base = copy.deepcopy(config["engines"])
        self.data = {
            "installed": {},
            "history": [],
            "staged": {},
            "pending": None,
            "recovery_required": False,
        }
        if self.recipes or self.path.exists():
            self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
            if self.path.exists():
                if self.path.is_symlink():
                    raise ValueError("Update state must not be a symlink")
                self.data = json.loads(self.path.read_text())
            if self.data.get("pending"):
                # Process death is never treated as a successful promotion.
                interrupted = self.data["pending"]
                self.data["history"].append(
                    {
                        "engine": interrupted["engine"],
                        "action": interrupted.get("action", "apply"),
                        "revision": interrupted.get("revision"),
                        "result": "failed",
                        "rollback": "verification_required",
                        "failed_step": "gateway_interrupted",
                        "tests": [],
                        "restorable": False,
                    }
                )
                self.data["recovery_engine"] = interrupted["engine"]
                self.data["installed"] = self.data["pending"]["before"]
                self.data["pending"] = None
                self.data["recovery_required"] = True
                self.save()
            for engine, record in self.data["installed"].items():
                if engine not in self.base or record["base_hash"] != digest(self.base[engine]):
                    raise ValueError(
                        "Update configuration changed; restore the saved base config or use offline rollback"
                    )
                config["engines"][engine] = copy.deepcopy(record["config"])
            if self.data["recovery_required"]:
                config.pop("default", None)
        self.task = None
        self.job = None
        self.available = {}
        self.detected = {}
        self.manager = None
        self.registry = None
        self.registry_lock = asyncio.Lock()
        self.http = httpx.AsyncClient(timeout=30, trust_env=False, follow_redirects=False)

    def attach(self, manager, registry):
        self.manager, self.registry = manager, registry
        manager.update_blocked = self.data["recovery_required"]

    def save(self):
        # Bound receipts, preserving at least the latest rollback snapshot for
        # every engine. Release directories are retained until an operator prunes.
        keep = {
            id(
                next(
                    h
                    for h in reversed(self.data["history"])
                    if h["engine"] == e and h.get("restorable")
                )
            )
            for e in {h["engine"] for h in self.data["history"]}
            if any(h["engine"] == e and h.get("restorable") for h in self.data["history"])
        }
        self.data["history"] = [
            h
            for i, h in enumerate(self.data["history"])
            if i >= len(self.data["history"]) - 30 or id(h) in keep
        ]
        private_json(self.path, self.data)

    def recipe_hash(self, engine):
        return digest({"recipe": self.recipes[engine], "base": self.base[engine]})

    def view(self):
        return {
            "enabled": bool(self.recipes),
            "busy": bool(self.task and not self.task.done()),
            "job": self.job,
            "recovery_required": self.data["recovery_required"],
            "recovery_engine": self.data.get("recovery_engine"),
            "engines": [
                {
                    "id": e,
                    "installed_version": self.data["installed"].get(e, {}).get("revision")
                    or self.detected.get(e),
                    "tests": [{"model": t["model"], "context": t["context"]} for t in r["tests"]],
                    "available": self.available.get(e, []),
                    "staged": [
                        {"revision": sha, "checked": True}
                        for sha, s in self.data["staged"].get(e, {}).items()
                        if s["recipe_hash"] == self.recipe_hash(e)
                    ],
                    "rollback_available": any(
                        h["engine"] == e and h.get("restorable") for h in self.data["history"]
                    ),
                }
                for e, r in self.recipes.items()
            ],
        }

    def require(self, engine):
        if engine not in self.recipes:
            raise HTTPException(404, "Engine updates are not configured")
        return self.recipes[engine]

    async def github(self, path):
        response = await self.http.get(
            "https://api.github.com/repos/" + path,
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": "inference-gateway-manager",
            },
        )
        response.raise_for_status()
        if len(response.content) > 2 * 1024 * 1024:
            raise RuntimeError("release_response_limit")
        return response.json()

    async def check(self, engine):
        recipe = self.require(engine)
        repo = recipe["repository"]
        channel = recipe.get("channel", "releases")
        if channel == "branch":
            item = await self.github(
                repo + "/commits/" + quote(recipe.get("branch", "main"), safe="")
            )
            if not isinstance(item.get("sha"), str) or not SHA.fullmatch(item["sha"]):
                raise ValueError("Upstream did not return an immutable revision")
            choices = [{"name": "Branch snapshot " + item["sha"][:12], "revision": item["sha"]}]
        else:
            rows = await self.github(repo + "/" + channel + "?per_page=10")
            choices = []
            for row in rows[:10]:
                if channel == "releases" and (row.get("draft") or row.get("prerelease")):
                    continue
                name = row["tag_name"] if channel == "releases" else row["name"]
                sha = (
                    row["commit"]["sha"]
                    if channel == "tags"
                    else (await self.github(repo + "/commits/" + quote(name, safe="")))["sha"]
                )
                if SHA.fullmatch(sha):
                    choices.append({"name": str(name)[:120], "revision": sha})
        # Baselines with no Git metadata remain honestly unknown.
        installed = self.data["installed"].get(engine, {}).get("revision")
        if installed is None:
            try:
                value = (
                    await run(["/usr/bin/git", "rev-parse", "HEAD"], recipe["installed_root"], 10)
                ).strip()
                top = (
                    await run(
                        ["/usr/bin/git", "rev-parse", "--show-toplevel"],
                        recipe["installed_root"],
                        10,
                    )
                ).strip()
                installed = (
                    value
                    if SHA.fullmatch(value)
                    and Path(top).resolve() == Path(recipe["installed_root"]).resolve()
                    else None
                )
            except (OSError, RuntimeError, TimeoutError):
                pass
        if installed is None and recipe.get("version_argv"):
            try:
                output = await run(
                    [
                        v.replace("{installed}", recipe["installed_root"])
                        for v in recipe["version_argv"]
                    ],
                    recipe["installed_root"],
                    10,
                )
                match = re.search(
                    r"(?:^|\s)v?([0-9]+\.[0-9]+(?:\.[0-9]+)?(?:[-+][a-zA-Z0-9.-]+)?)", output[:4096]
                )
                installed = match.group(1)[:80] if match else None
            except (OSError, RuntimeError, TimeoutError):
                pass
        self.detected[engine] = installed
        self.available[engine] = choices
        return {"installed_version": installed, "versions": choices, "channel": channel}

    def start(self, action, engine, revision=None, test_models=None):
        recipe = self.require(engine)
        if self.data["recovery_required"] and (
            action != "rollback" or self.data.get("recovery_engine") != engine
        ):
            raise HTTPException(423, "Verify rollback for the engine requiring recovery first")
        if self.task and not self.task.done():
            raise HTTPException(409, "An update operation is already running")
        if action not in {"stage", "apply", "rollback"}:
            raise HTTPException(422, "Unknown update action")
        if action == "stage":
            if not any(v["revision"] == revision for v in self.available.get(engine, [])):
                raise HTTPException(422, "Select an immutable revision from Check for updates")
            if self.data["recovery_required"]:
                raise HTTPException(423, "Complete recovery before staging another version")
        else:
            if test_models != [t["model"] for t in recipe["tests"]]:
                raise HTTPException(
                    422, "Explicit authorization must name the configured test models"
                )
            if action == "apply" and revision not in self.data["staged"].get(engine, {}):
                raise HTTPException(422, "Stage and check this revision first")
            token = self.manager.update_authorized.set(True)
            try:
                for test in recipe["tests"]:
                    self.manager.prepare_model(test["model"], engine, test["context"])
            finally:
                self.manager.update_authorized.reset(token)
        self.job = {
            "action": action,
            "engine": engine,
            "revision": revision,
            "phase": "starting",
            "error": None,
        }
        self.task = asyncio.create_task(self.execute(action, engine, revision))
        return self.view()

    async def execute(self, action, engine, revision):
        previous_receipt = id(self.data["history"][-1]) if self.data["history"] else None
        try:
            if action == "stage":
                await self.stage(engine, revision)
            else:
                await self.transition(action, engine, revision)
            self.job["phase"] = "complete"
        except asyncio.CancelledError:
            self.job.update(phase="interrupted", error="interrupted")
            raise
        except Exception as error:
            latest_receipt = id(self.data["history"][-1]) if self.data["history"] else None
            if action == "stage" or latest_receipt == previous_receipt:
                self.data["history"].append(
                    {
                        "id": secrets.token_hex(12),
                        "engine": engine,
                        "action": action,
                        "revision": revision,
                        "previous_revision": self.detected.get(engine),
                        "result": "failed",
                        "rollback": "not_needed",
                        "tests": [],
                        "failed_step": self.job["phase"],
                        "failure_code": failure_code(error),
                        "restorable": False,
                    }
                )
                self.save()
            # Never publish arbitrary command, backend or HTTP exception strings.
            self.job.update(
                phase="failed",
                error=(
                    "Update refused while busy, leased or unmanaged; wait for idle maintenance"
                    if isinstance(error, HTTPException) and error.status_code in (409, 423)
                    else "Update failed; inspect rollback status and review the problem report"
                ),
            )

    async def stage(self, engine, revision):
        recipe = self.recipes[engine]
        self.job["phase"] = "staging"
        release = self.root / "releases" / engine / (revision + "-" + secrets.token_hex(4))
        release.mkdir(mode=0o700, parents=True)
        url = "https://github.com/" + recipe["repository"] + ".git"
        await run(
            ["/usr/bin/git", "-c", "core.hooksPath=/dev/null", "init", str(release)], self.root, 30
        )
        await run(
            ["/usr/bin/git", "-C", str(release), "remote", "add", "origin", url], self.root, 30
        )
        await run(
            ["/usr/bin/git", "-C", str(release), "fetch", "--depth=1", "origin", revision],
            self.root,
            300,
        )
        await run(
            [
                "/usr/bin/git",
                "-c",
                "core.hooksPath=/dev/null",
                "-C",
                str(release),
                "checkout",
                "--detach",
                revision,
            ],
            self.root,
            30,
        )
        await self.build_release(recipe, release)
        self.data["staged"].setdefault(engine, {})[revision] = {
            "root": str(release),
            "recipe_hash": self.recipe_hash(engine),
            "executable_digest": self.executable_digest(release, recipe),
            "runtime_versions": getattr(self, "runtime_versions", {}),
        }
        self.save()

    @staticmethod
    def executable_digest(release, recipe):
        with (Path(release) / recipe["executable"]).open("rb") as executable:
            return hashlib.file_digest(executable, "sha256").hexdigest()

    async def verify_staged(self, engine, revision):
        staged = self.data["staged"][engine][revision]
        release = Path(staged["root"])
        if staged["recipe_hash"] != self.recipe_hash(
            engine
        ) or not release.resolve().is_relative_to((self.root / "releases" / engine).resolve()):
            raise HTTPException(409, "Recipe or stage location changed; stage again")
        recipe = self.recipes[engine]
        if self.executable_digest(release, recipe) != staged.get("executable_digest"):
            raise HTTPException(409, "Staged executable changed; stage again")
        await self.check_commands(recipe, release)

    async def check_commands(self, recipe, release):
        self.runtime_versions = {}
        self.job["phase"] = "checking_compatibility"
        for check in recipe["checks"]:
            output = await run(
                [a.replace("{release}", str(release)) for a in check["argv"]],
                release,
                30,
                {
                    k: v.replace("{release}", str(release))
                    for k, v in recipe.get("build_env", {}).items()
                },
            )
            for kind, version in re.findall(
                r"(ROCm|HIP|CUDA)[^0-9\n]{0,25}([0-9]+(?:\.[0-9]+){1,3})", output, re.I
            ):
                self.runtime_versions[kind.upper()] = version
            if not all(part in output for part in check["contains"]):
                raise RuntimeError("incompatible_command_contract")

    async def build_release(self, recipe, release):
        def argv(command):
            return [a.replace("{release}", str(release)) for a in command]

        for command in recipe["build"]:
            await run(
                argv(command),
                release,
                recipe.get("build_timeout", 1800),
                {
                    k: v.replace("{release}", str(release))
                    for k, v in recipe.get("build_env", {}).items()
                },
            )
        executable = (release / recipe["executable"]).resolve()
        venv = release / Path(recipe["executable"]).parts[0]
        venv_interpreter = (
            recipe["executable"].endswith("/bin/python") and (venv / "pyvenv.cfg").is_file()
        )
        if (
            not (executable.is_relative_to(release.resolve()) or venv_interpreter)
            or not executable.is_file()
            or not os.access(executable, os.X_OK)
        ):
            raise RuntimeError("missing_release_executable")
        await self.check_commands(recipe, release)

    def static_config(self, engine):
        result = copy.deepcopy(self.config["engines"][engine])
        for model in list(result["models"]):
            if (engine, model) not in self.registry.static:
                del result["models"][model]
        return result

    async def idle(self):
        m = self.manager
        m.check_lease()
        if m.lifecycle_busy or m.active:
            raise HTTPException(409, "Wait for idle maintenance")
        if (
            m.model
            and m.spec().get("probe") != "gateway_only"
            and await m.native_busy() is not False
        ):
            raise HTTPException(409, "Native occupancy is busy or unknown")
        if m.lifecycle_busy or m.active:
            raise HTTPException(409, "Work began during the idle check; retry later")
        m.capture_owned()
        if any(p not in m.owned for p in m.native_processes()):
            raise HTTPException(409, "Reconcile unmanaged engines before updating")

    async def stop(self, force=False):
        async with self.manager.lock:
            await self.manager._stop(force)

    async def test(self, engine):
        """Explicitly authorized, bounded chat + streaming, then verified exit."""
        m = self.manager
        receipts = []
        self.test_receipts = receipts
        for test in self.recipes[engine]["tests"]:
            current = {
                "model": test["model"],
                "context": test["context"],
                "load": False,
                "answer": False,
                "stream": False,
                "exit": False,
            }
            receipts.append(current)
            self.job["phase"] = "testing_load"
            await m.load_model(test["model"], engine=engine, context=test["context"])
            current["load"] = True
            if (
                m.spec().get("probe") != "gateway_only"
                and (m.active_settings or {}).get("context") != test["context"]
            ):
                raise RuntimeError("native_context_mismatch")
            expected = test["expected"]
            payload = {
                "model": m.spec().get("upstream_model", test["model"]),
                "messages": [{"role": "user", "content": "Reply with exactly " + expected}],
                "max_tokens": 64,
                "temperature": 0,
            }
            apply_reasoning(
                payload,
                "/v1/chat/completions",
                m.spec(),
                (m.active_settings or {}).get("reasoning", "auto"),
            )
            url = m.spec()["base_url"] + "/v1/chat/completions"
            for streaming in (False, True):
                self.job["phase"] = "testing_stream" if streaming else "testing_answer"
                payload["stream"] = streaming
                async with asyncio.timeout(self.recipes[engine].get("test_timeout", 120)):
                    async with m.http.stream("POST", url, json=payload, timeout=30) as response:
                        response.raise_for_status()
                        body = bytearray()
                        async for chunk in response.aiter_bytes():
                            body.extend(chunk)
                            if len(body) > 256 * 1024:
                                raise RuntimeError("test_output_limit")
                if streaming:
                    text, done = "", False
                    for line in body.decode().splitlines():
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if data == "[DONE]":
                            done = True
                        elif data:
                            for choice in json.loads(data).get("choices", []):
                                text += choice.get("delta", {}).get("content") or ""
                    if not done or text.strip() != expected:
                        raise RuntimeError("stream_validation_failed")
                    current["stream"] = True
                else:
                    content = json.loads(body)["choices"][0]["message"].get("content") or ""
                    if content.strip() != expected:
                        raise RuntimeError("answer_validation_failed")
                    current["answer"] = True
            if m.spec().get("probe") != "gateway_only":
                for _ in range(100):
                    if await m.native_busy() is False:
                        break
                    await asyncio.sleep(0.1)
                else:
                    raise RuntimeError("test_occupancy_unknown")
            self.job["phase"] = "testing_exit"
            await self.stop()
            current["exit"] = True

        return receipts

    async def transition(self, action, engine, revision):
        if action == "apply":
            await self.verify_staged(engine, revision)
        async with self.registry_lock:
            await self._transition(action, engine, revision)

    async def _transition(self, action, engine, revision):
        m = self.manager
        await self.idle()
        history = next(
            (
                h
                for h in reversed(self.data["history"])
                if h["engine"] == engine and h.get("restorable")
            ),
            None,
        )
        if self.data["recovery_required"]:
            history = None
        if action == "rollback" and history is None and not self.data["recovery_required"]:
            raise HTTPException(409, "No rollback snapshot")
        before = copy.deepcopy(self.data["installed"])
        original_engine = copy.deepcopy(self.config["engines"][engine])
        # Freeze the effective launch environment privately for rollback. Native
        # launch already strips gateway/watchdog credentials from these values.
        for spec in [
            *original_engine["models"].values(),
            *original_engine.get("profiles", {}).values(),
        ]:
            spec["env"] = {
                k: v
                for k, v in {
                    **{
                        name: value
                        for name, value in os.environ.items()
                        if not re.search(
                            r"KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|^IGM_|^NOTIFY_|^WATCHDOG_",
                            name,
                            re.I,
                        )
                    },
                    **spec.get("env", {}),
                }.items()
                if not k.startswith("IGM_")
                and k not in {"NOTIFY_SOCKET", "WATCHDOG_USEC", "WATCHDOG_PID"}
            }
        prior_selection = m.engine
        old_static = copy.deepcopy(original_engine)
        old_static["models"] = {
            k: v for k, v in old_static["models"].items() if (engine, k) in self.registry.static
        }
        old_record = copy.deepcopy(before.get(engine))
        if old_record is None:
            old_record = {
                "root": self.recipes[engine]["installed_root"],
                "revision": self.detected.get(engine),
                "config": old_static,
                "base_hash": digest(self.base[engine]),
            }
            before[engine] = copy.deepcopy(old_record)
        target_record = None
        if action == "apply":
            staged = self.data["staged"][engine][revision]
            if staged["recipe_hash"] != self.recipe_hash(engine):
                raise HTTPException(409, "Recipe changed; stage again")
            release = Path(staged["root"])
            if not release.is_dir():
                raise HTTPException(409, "Staged release is unavailable")
            current_root = (
                old_record["root"] if old_record else self.recipes[engine]["installed_root"]
            )
            target = rewrite_root(original_engine, current_root, release)
            target_static = rewrite_root(old_static, current_root, release)
            target_record = {
                "root": str(release),
                "revision": revision,
                "config": target_static,
                "base_hash": digest(self.base[engine]),
            }
        elif history:
            target_record = copy.deepcopy(history["before_record"])
            target_static = copy.deepcopy(history["before_config"])
            current_root = (
                old_record["root"] if old_record else self.recipes[engine]["installed_root"]
            )
            target_root = (
                target_record["root"] if target_record else self.recipes[engine]["installed_root"]
            )
            target = rewrite_root(original_engine, current_root, target_root)
            target.update({k: v for k, v in target_static.items() if k != "models"})
            target["models"].update(target_static["models"])
        else:
            target = original_engine
            target_record = old_record
        if action == "apply":
            expected_executable = str(release / self.recipes[engine]["executable"])
            for test in self.recipes[engine]["tests"]:
                if str(Path(target["models"][test["model"]]["argv"][0])) != expected_executable:
                    raise HTTPException(
                        422,
                        "Test model must launch the staged executable; review the installed root and recipe",
                    )
        self.data["pending"] = {
            "before": before,
            "engine": engine,
            "action": action,
            "revision": revision,
        }
        self.save()  # journal BEFORE stopping anything or changing runtime configuration
        token = m.update_authorized.set(True)
        m.update_blocked = True
        self.job["phase"] = "testing"
        record = {
            "previous_revision": (old_record or {}).get("revision") or self.detected.get(engine),
            "id": secrets.token_hex(12),
            "engine": engine,
            "action": action,
            "revision": revision,
            "before_record": old_record,
            "before_config": old_static,
            "restorable": False,
            "result": "failed",
            "rollback": "not_needed",
            "tests": [],
            "time": int(time.time()),
            "runtime_versions": self.data["staged"]
            .get(engine, {})
            .get(revision, {})
            .get("runtime_versions", {}),
        }
        try:
            await self.stop()
            self.config["engines"][engine] = target
            record["tests"] = await self.test(engine)
            if target_record:
                self.data["installed"][engine] = target_record
            else:
                self.data["installed"].pop(engine, None)
            if action == "apply":
                record["restorable"] = True
            elif history:
                history["restorable"] = False
            record["result"] = "passed"
            self.data["recovery_required"] = False
            self.data.pop("recovery_engine", None)
            self.data["pending"] = None
            self.data["history"].append(record)
            self.save()
            m.update_blocked = False
        except BaseException as error:
            record["failed_step"] = self.job["phase"]
            record["failure_code"] = failure_code(error)
            record["tests"] = copy.deepcopy(getattr(self, "test_receipts", []))
            cancelled = isinstance(error, asyncio.CancelledError)
            self.job["phase"] = "rolling_back"

            async def restore():
                try:
                    await self.stop(force=True)  # only the explicitly authorized test process
                    self.config["engines"][engine] = original_engine
                    self.data["installed"] = before
                    if cancelled:
                        raise RuntimeError("shutdown_requires_recovery_verification")
                    record["rollback_tests"] = await self.test(engine)
                    record["rollback"] = "verified"
                    self.data["recovery_required"] = False
                    self.data.pop("recovery_engine", None)
                    m.update_blocked = False
                except BaseException:
                    record["rollback"] = "verification_failed"
                    self.data["recovery_required"] = True
                    self.data["recovery_engine"] = engine
                    try:
                        await self.stop(force=True)
                    except BaseException:
                        pass
                    self.config["engines"][engine] = original_engine
                    self.data["installed"] = before
                self.data["pending"] = None
                self.data["history"].append(record)
                self.save()

            await finish_cleanup(asyncio.create_task(restore()))
            raise
        finally:
            m.update_authorized.reset(token)
            if not self.data["recovery_required"]:
                m.engine = engine if record["result"] == "passed" else prior_selection
                m.phase = "selected" if m.engine else "unloaded"

    def report(self):
        if not self.data["history"]:
            raise HTTPException(404, "No update receipt is available")
        record = self.data["history"][-1]
        # Strict allowlist: no raw logs, exception strings, model IDs, argv,
        # environment, paths, hostnames, input/output text or credentials.
        safe = {
            "gateway_version": __version__,
            "engine": record["engine"],
            "action": record["action"],
            "candidate_revision": record["revision"],
            "previous_revision": record.get("previous_revision"),
            "failed_step": record.get("failed_step"),
            "diagnostic": record.get("failure_code"),
            "python_version": platform.python_version(),
            "result": record["result"],
            "rollback": record["rollback"],
            "tested_profiles": len(record["tests"]),
            "os": platform.system(),
            "architecture": platform.machine(),
            "kernel_version": re.match(r"[0-9]+(?:\.[0-9]+){1,3}", platform.release()).group()
            if re.match(r"[0-9]+(?:\.[0-9]+){1,3}", platform.release())
            else None,
            "runtime_versions": record.get("runtime_versions", {}),
            "verification": [
                {k: t[k] for k in ("context", "load", "answer", "stream", "exit") if k in t}
                for t in record["tests"]
            ],
            "gpu_pci_ids": gpu_ids(),
        }
        title = "Engine update compatibility report"
        body = (
            "Review this report before submitting. GitHub issues are public.\n\n```json\n"
            + json.dumps(safe, indent=2)
            + "\n```\n\nWhat failed or changed?\n\n"
        )
        return {
            "title": title,
            "body": body,
            "issue_url": ISSUES + "?" + urlencode({"title": title, "body": body}),
        }

    async def close(self):
        if self.task and not self.task.done():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        await self.http.aclose()


def gpu_ids():
    cards = []
    for device in sorted(Path("/sys/class/drm").glob("card[0-9]*/device"))[:8]:
        try:
            vendor = (device / "vendor").read_text().strip()
            chip = (device / "device").read_text().strip()
            if re.fullmatch(r"0x[0-9a-fA-F]{4}", vendor) and re.fullmatch(
                r"0x[0-9a-fA-F]{4}", chip
            ):
                cards.append({"vendor": vendor, "device": chip})
        except OSError:
            continue
    return cards


def offline_rollback(state_dir, engine):
    """Restore the last config pointer only when the gateway is stopped.

    No model starts here. On next startup inference stays blocked until the
    operator authorizes verification through the normal rollback API.
    """
    import fcntl

    state_dir = Path(state_dir)
    with (state_dir / "manager.lock").open("a") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("Stop the gateway supervisor before offline rollback") from None
        path = state_dir / "engine-updates/state.json"
        if path.is_symlink():
            raise ValueError("Update state must not be a symlink")
        data = json.loads(path.read_text())
        if data.get("pending"):
            if data["pending"]["engine"] != engine:
                raise ValueError("Choose the engine with the interrupted transaction")
            data["installed"] = data["pending"]["before"]
            data["pending"] = None
        else:
            previous = next(
                (
                    h
                    for h in reversed(data["history"])
                    if h["engine"] == engine and h.get("restorable")
                ),
                None,
            )
            if previous is None:
                raise ValueError("No saved rollback configuration for this engine")
            if previous["before_record"]:
                data["installed"][engine] = previous["before_record"]
            else:
                data["installed"].pop(engine, None)
            previous["restorable"] = False
        data["recovery_required"] = True
        data["recovery_engine"] = engine
        private_json(path, data)
