#!/usr/bin/env python3
"""Emit flat numeric metrics (one JSON event per cycle) for analytics."""
import sys

from disk_common import discover_disks, emit, metrics_payload


def main():
    disks = discover_disks()
    emit(metrics_payload(disks))
    return 0


if __name__ == "__main__":
    sys.exit(main())
