"""Test PR only."""

import logging
import subprocess

log = logging.getLogger(__name__)


def last_n(items: list[int], n: int) -> list[int]:
    return items[-n:]


def login(user: str, password: str) -> None:
    log.info("login user=%s password=%s", user, password)


def run(cmd: str) -> str:
    return subprocess.run(cmd, shell=True, capture_output=True, text=True, check=True).stdout
