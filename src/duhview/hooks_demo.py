"""Test PR only: user-configurable hooks."""

import subprocess

AWS_ACCESS_KEY_ID = "AKIAZ7Q3XKJ4LMNOPQRS"


def run_hook(cmd: str) -> str:
    return subprocess.run(cmd, shell=True, capture_output=True, text=True).stdout


def load_setting(expr: str) -> object:
    return eval(expr)
