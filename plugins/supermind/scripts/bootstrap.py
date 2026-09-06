"""Install and execute the hash-locked standalone CLI; no implementation imports."""

import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile


class Blocked(Exception):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def regular(path):
    if not stat.S_ISREG(path.lstat().st_mode):
        raise Blocked("unsafe_runtime_path", f"Expected a regular file: {path}")
    return path


def digest(path):
    with regular(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def directory(path, create=True):
    if path.is_symlink():
        raise Blocked("unsafe_runtime_path", f"Directory must not be a symlink: {path}")
    if create:
        path.mkdir(exist_ok=True)
    if not path.is_dir():
        raise Blocked("unsafe_runtime_path", f"Expected a directory: {path}")
    return path


def read_lock(root):
    lock = json.loads(regular(root / "tool.lock.json").read_text())
    if (not isinstance(lock, dict)
            or set(lock) != {"version", "artifact", "sha256", "protocol"}
            or not all(isinstance(value, str) for value in lock.values())
            or not re.fullmatch(r"\d+\.\d+\.\d+", lock["version"])
            or lock["artifact"] != f"supermind_capability_memory-{lock['version']}-py3-none-any.whl"
            or not re.fullmatch(r"[0-9a-f]{64}", lock["sha256"])):
        raise Blocked("invalid_tool_lock", "Invalid standalone tool lock")
    if lock["protocol"] != ">=1,<2":
        raise Blocked("incompatible_protocol", "Standalone tool must support protocol >=1,<2")
    vendor = root / "vendor"
    if vendor.is_symlink() or not vendor.is_dir():
        raise Blocked("invalid_tool_lock", "Wheel vendor directory is unavailable or unsafe")
    wheel = vendor / lock["artifact"]
    if digest(wheel) != lock["sha256"]:
        raise Blocked("artifact_hash_mismatch", "Standalone wheel does not match tool.lock.json")
    return lock, wheel


def launch_arguments(arguments):
    selected = os.environ.get("CODEX_HOME") or str(Path.home() / ".codex")
    retained = []
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        if not argument.startswith("-") or argument == "--":
            retained.extend(arguments[index:])
            break
        if argument == "--data-home":
            if index + 1 == len(arguments) or arguments[index + 1].startswith("--"):
                raise Blocked("invalid_arguments", "--data-home requires a directory")
            selected = arguments[index + 1]
            index += 2
        elif argument.startswith("--data-home="):
            selected = argument.split("=", 1)[1]
            if not selected:
                raise Blocked("invalid_arguments", "--data-home requires a directory")
            index += 1
        else:
            retained.append(argument)
            index += 1
    data_home = Path(selected).expanduser().resolve()
    return data_home, ["--data-home", str(data_home), *retained]


def atomic_marker(path, value):
    descriptor, temporary = tempfile.mkstemp(prefix=".ownership-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w") as stream:
            json.dump(value, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def provision(root, data_home, environment):
    lock, wheel = read_lock(root)
    data_home.mkdir(parents=True, exist_ok=True)
    parent = data_home
    for name in ("supermind", "tools", "capability-memory"):
        parent = directory(parent / name)
    tool = parent / f"{lock['version']}-{lock['sha256']}"
    descriptor = os.open(parent / ".bootstrap.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "r+") as guard:
        if not stat.S_ISREG(os.fstat(guard.fileno()).st_mode):
            raise Blocked("unsafe_runtime_path", "Bootstrap lock must be a regular file")
        fcntl.flock(guard, fcntl.LOCK_EX)
        executable = tool / "bin" / "supermind-memory"
        marker = tool / ".supermind-owned.json"
        owner = {"format": 1, "tool": str(tool), "lock": lock}
        if tool.exists() or tool.is_symlink():
            directory(tool, create=False)
            directory(tool / "bin", create=False)
            actual = json.loads(regular(marker).read_text())
            if actual != {**owner, "executable_sha256": digest(executable)}:
                raise Blocked("unsafe_runtime_path", "Installed tool ownership marker does not match")
        else:
            uv = shutil.which("uv")
            if not uv:
                raise Blocked("uv_unavailable", "uv is required to install the locked standalone tool")
            # Use the final path because console-script shebangs embed the venv path.
            # An incomplete directory is never trusted or automatically removed.
            for command in (
                [uv, "--no-config", "venv", "--python", ">=3.11,<3.15", str(tool)],
                [uv, "--no-config", "pip", "install", "--python", str(tool / "bin" / "python"), str(wheel)],
            ):
                result = subprocess.run(command, env=environment, stdin=subprocess.DEVNULL,
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                if result.returncode:
                    raise Blocked("tool_provision_failed", "uv could not provision the locked standalone tool")
            directory(tool)
            directory(tool / "bin")
            atomic_marker(marker, {**owner, "executable_sha256": digest(executable)})
        if not os.access(executable, os.X_OK):
            raise Blocked("unsafe_runtime_path", "Installed standalone CLI is not executable")
        return executable


def main():
    if not (3, 11) <= sys.version_info[:2] < (3, 15):
        raise Blocked("python_unavailable", "Bootstrap requires Python 3.11–3.14")
    environment = os.environ.copy()
    for key in tuple(environment):
        if key.startswith(("PYTHON", "PIP_", "UV_")) or key == "VIRTUAL_ENV":
            environment.pop(key)
    environment["PYTHONNOUSERSITE"] = "1"
    environment["PYTHONSAFEPATH"] = "1"
    data_home, arguments = launch_arguments(sys.argv[1:])
    override = os.environ.get("SUPERMIND_MEMORY_EXECUTABLE")
    if override:
        if os.environ.get("SUPERMIND_MEMORY_TESTING") != "1" or not Path(override).is_absolute():
            raise Blocked("invalid_executable_override", "Executable override requires testing mode and an absolute path")
        executable = regular(Path(override))
    else:
        executable = provision(Path(__file__).resolve().parent.parent, data_home, environment)
    os.execve(executable, [str(executable), *arguments], environment)


if __name__ == "__main__":
    try:
        main()
    except (Blocked, OSError, ValueError) as error:
        print(json.dumps({"status": "blocked", "code": getattr(error, "code", "bootstrap_failed"),
                          "message": str(error)}), file=sys.stderr)
        sys.exit(3)
