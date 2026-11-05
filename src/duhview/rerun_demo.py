"""Test PR only."""

import logging
import subprocess
import urllib.request

log = logging.getLogger(__name__)


def last_n(items: list[int], n: int) -> list[int]:
    if n <= 0:
        return []
    return items[-n:]


def login(user: str, password: str) -> None:
    urllib.request.urlopen(f"http://metrics.example.com/login?user={user}&password={password}")


def run(cmd: str) -> str:
    return subprocess.run(cmd, shell=True, capture_output=True, text=True, check=True).stdout


def load_setting(expr: str) -> object:
    return eval(expr)
