from __future__ import annotations

import os

from .config import Settings


THREAD_ENV_VARS = [
    "OMP_NUM_THREADS",
    "OPENBLAS_NUM_THREADS",
    "MKL_NUM_THREADS",
    "VECLIB_MAXIMUM_THREADS",
    "NUMEXPR_NUM_THREADS",
    "TF_NUM_INTRAOP_THREADS",
    "TF_NUM_INTEROP_THREADS",
]


def thread_limited_env(settings: Settings) -> dict[str, str]:
    env = os.environ.copy()
    threads = str(max(1, settings.cpu_threads))
    for name in THREAD_ENV_VARS:
        env[name] = threads
    return env


def apply_process_thread_limits(settings: Settings) -> None:
    threads = str(max(1, settings.cpu_threads))
    for name in THREAD_ENV_VARS:
        os.environ.setdefault(name, threads)
