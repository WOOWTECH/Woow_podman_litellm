#!/usr/bin/env python3
"""Reject a plugin drift from the reviewed HA/PaaS lock."""
import hashlib
import json
from pathlib import Path

root = Path(__file__).resolve().parents[1]
lock = json.loads((root / "image/plugin.lock.json").read_text())
assert lock["source"]["commit"] == "b3e8371487794d8531d1b5c01f11969088cd166d"
assert lock["source"]["chart_version"] == "0.3.16"
for name, entry in lock["files"].items():
    data = (root / "image/plugin" / name).read_bytes()
    assert hashlib.sha256(data).hexdigest() == entry["sha256"], name
    assert len(data) == entry["size"], name
print(f"verified {len(lock['files'])} locked Woow plugin files")
overlay = json.loads((root / "image/overlay/overlay.lock.json").read_text())
for entry in overlay["packages"]:
    wheel = root / "image/overlay/wheels" / entry["wheel"]
    assert hashlib.sha256(wheel.read_bytes()).hexdigest() == entry["sha256"]
print(f"verified {len(overlay['packages'])} locked Python wheel")
