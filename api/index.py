"""Vercel entrypoint: exposes the FastAPI app as an ASGI function.

Deploy-time configuration may be baked into api/_env.py (never committed —
see .gitignore); real deployments should prefer Vercel project env vars,
which take precedence since we only setdefault here.
"""

import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

try:
    from api._env import ENV
except ImportError:
    try:
        from _env import ENV  # Vercel sometimes mounts api/ as the cwd
    except ImportError:
        ENV = {}

for _k, _v in ENV.items():
    os.environ.setdefault(_k, _v)

from darwin.server import app  # noqa: E402  (env must be set before import)
