"""USB serial device autodetection.

Scans /dev for serial ports and enriches them via /dev/serial/by-id/
symlinks so the operator can pick a port without restarting the webapp.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

COMMON = ("/dev/ttyUSB", "/dev/ttyACM", "/dev/ttyAMA", "/dev/ttyS",
          "/dev/ttyXRUSB", "/dev/ttyOP")

_PORT_RE = re.compile(r"^/dev/serial/by-id/(?:usb-)?(.+)$")


def _describe(path: Path) -> tuple[str, str]:
    """Return (description, hardware_id) for a by-id symlink name."""
    name = path.name
    m = _PORT_RE.match(name)
    if not m:
        return str(name), ""
    # "XYZ-manufacturer-product-serial-if00-port0" -> "manufacturer product"
    parts = m.group(1).split("-")
    # strip trailing "ifNN-portN"
    while parts and (parts[-1].startswith("if") or parts[-1].startswith("port")):
        parts.pop()
    desc = " ".join(parts[1:]) if len(parts) > 1 else parts[0] if parts else str(name)
    return desc.replace("_", " "), name


def scan() -> list[dict]:
    """Return sorted list of available serial ports with metadata.

    Each entry: {"path", "description", "hardware_id"}
    """
    seen: set[str] = set()
    out: list[dict] = []

    # by-id symlinks carry vendor/product/serial - richest info first
    by_id = Path("/dev/serial/by-id")
    if by_id.is_dir():
        for p in sorted(by_id.iterdir()):
            if not p.is_symlink():
                continue
            try:
                target = os.readlink(p)
            except OSError:
                continue
            real = Path("/dev/serial/by-id") / target if not target.startswith("/") else Path(target)
            # Some distros link to ../../ttyUSB0 - resolve it
            try:
                resolved = real.resolve()
            except OSError:
                continue
            if not str(resolved).startswith("/dev/tty"):
                continue
            rstr = str(resolved)
            if rstr in seen:
                continue
            seen.add(rstr)
            desc, hw = _describe(p)
            out.append({"path": str(resolved), "description": desc, "hardware_id": hw})

    # Direct /dev entries not covered by by-id
    for prefix in COMMON:
        stem = prefix.rsplit("/", 1)[1]
        for p in sorted(Path("/dev").glob(stem + "*")):
            rstr = str(p)
            if rstr not in seen:
                seen.add(rstr)
                out.append({"path": rstr, "description": p.name, "hardware_id": ""})

    return out