"""Test process configuration shared by optional offscreen Qt tests."""

from __future__ import annotations

import os


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
