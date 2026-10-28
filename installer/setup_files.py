"""Copy public installation files and prepare (never activate) service units."""

import argparse
import grp
import os
import pwd
import shutil
from pathlib import Path

PUBLIC_FILES = ("pyproject.toml", "README.md", "LICENSE")
PUBLIC_DIRS = ("gateway", "examples", "docs", "skills", "installer")


def check_target(source, target):
    source, target = Path(source).resolve(), Path(target).resolve()
    if target != source and (target.is_relative_to(source) or source.is_relative_to(target)):
        raise ValueError("Choose an install directory separate from the source checkout")
    for name in ("config.local.json", ".local", ".venv"):
        path = target / name
        if path.exists() or path.is_symlink():
            raise ValueError("Existing installation detected; use the documented upgrade procedure")
    if target != source and target.exists() and any(target.iterdir()):
        raise ValueError("A separate install target must be empty")


def copy_public_files(source, target):
    source, target = Path(source).resolve(), Path(target).resolve()
    check_target(source, target)
    if source == target:
        return
    target.mkdir(parents=True, exist_ok=True)

    def ignore(directory, names):
        return [
            name
            for name in names
            if name in {"__pycache__", ".local", ".venv", ".git", ".pytest_cache", ".ruff_cache"}
            or name.endswith((".local.json", ".log", ".pyc"))
            or name.startswith(".env")
            or (Path(directory) / name).is_symlink()
        ]

    for name in PUBLIC_FILES:
        path = source / name
        if path.is_symlink():
            raise ValueError("Public install files must not be symlinks")
        shutil.copy2(path, target / name)
    for name in PUBLIC_DIRS:
        path = source / name
        if path.is_symlink():
            raise ValueError("Public install directories must not be symlinks")
        shutil.copytree(path, target / name, ignore=ignore)


def quote_unit(value):
    # systemd specifiers are expanded even inside quotes. ExecStart additionally
    # expands $, which this installer rejects in install paths below.
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"'


def validate_path(value):
    path = Path(value).expanduser()
    if not path.is_absolute() or any(c in str(path) for c in "\n\r\0$"):
        raise ValueError("Use an absolute path without newlines, NUL or dollar signs")
    return path.resolve()


def render_unit(source, install, system=False):
    install = validate_path(install)
    template = "inference-gateway-manager" + ("-system" if system else "") + ".service"
    lines = (Path(source) / "examples" / template).read_text().splitlines()
    account = pwd.getpwuid(os.getuid())
    group = grp.getgrgid(os.getgid()).gr_name
    replacements = {
        "WorkingDirectory": quote_unit(install),
        "EnvironmentFile": quote_unit(install / ".local/credentials.env"),
        "ExecStart": (
            f"{quote_unit(install / '.venv/bin/python')} -m gateway serve "
            f"--config {quote_unit(install / 'config.local.json')} "
            f"--state-dir {quote_unit(install / '.local/state')}"
        ),
        "User": account.pw_name,
        "Group": group,
    }
    return (
        "\n".join(
            key + "=" + replacements[key]
            if (key := line.split("=", 1)[0]) in replacements
            else line
            for line in lines
        )
        + "\n"
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("check", "copy", "unit"))
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--target", required=True, type=Path)
    parser.add_argument("--system", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        target = validate_path(args.target)
        if args.action == "check":
            check_target(args.source, target)
        elif args.action == "copy":
            copy_public_files(args.source, target)
        else:
            if args.output is None:
                parser.error("unit needs --output")
            with args.output.open("x") as file:
                file.write(render_unit(args.source, target, args.system))
    except (ValueError, OSError) as error:
        parser.exit(1, str(error) + "\n")


if __name__ == "__main__":
    main()
