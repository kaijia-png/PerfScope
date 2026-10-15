"""Thin wrapper around the adb command line."""
import os
import shutil
import subprocess
import sys


class AdbError(RuntimeError):
    pass


def adb_path() -> str:
    return os.environ.get("ADB_PATH") or shutil.which("adb") or "adb"


def _base():
    p = adb_path()
    # A .py "adb" (tools/fake_adb.py, demo mode) is run with the current interpreter
    return [sys.executable, p] if p.endswith(".py") else [p]


def run(args, serial=None, timeout=15) -> str:
    cmd = _base() + (["-s", serial] if serial else []) + list(args)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           errors="replace")
    except FileNotFoundError:
        raise AdbError("找不到 adb，请安装 Android platform-tools 或设置 ADB_PATH 环境变量")
    except subprocess.TimeoutExpired:
        raise AdbError(f"adb 命令超时: {' '.join(args)}")
    if r.returncode != 0:
        raise AdbError((r.stderr or r.stdout).strip() or f"adb 返回码 {r.returncode}")
    return r.stdout


def run_bytes(args, serial=None, timeout=15) -> bytes:
    cmd = _base() + (["-s", serial] if serial else []) + list(args)
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout)
    except FileNotFoundError:
        raise AdbError("找不到 adb，请安装 Android platform-tools 或设置 ADB_PATH 环境变量")
    except subprocess.TimeoutExpired:
        raise AdbError(f"adb 命令超时: {' '.join(args)}")
    if r.returncode != 0:
        raise AdbError(r.stderr.decode(errors="replace").strip() or f"adb 返回码 {r.returncode}")
    return r.stdout


def shell(serial, command: str, timeout=15) -> str:
    return run(["shell", command], serial=serial, timeout=timeout)


def popen(args, serial=None) -> subprocess.Popen:
    cmd = _base() + (["-s", serial] if serial else []) + list(args)
    return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            text=True, bufsize=1, errors="replace")
