#!/usr/bin/env python3
"""Emit one JSON event per cycle with per-disk SMART/NVMe health."""
import sys

from disk_common import discover_disks, emit


def main():
    disks = discover_disks()
    if not disks:
        emit({"status": "ok", "disks": {}, "note": "no block devices found"})
        return 0
    order = {"fail": 2, "alarm": 1}
    worst = "ok"
    for d in disks.values():
        st = d.get("status", "ok")
        if order.get(st, 0) > order.get(worst, 0):
            worst = st
    emit({"status": worst, "disks": disks})
    return 0


if __name__ == "__main__":
    sys.exit(main())
