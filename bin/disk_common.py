"""Shared collection logic for TA-disk scripted inputs (SMART + NVMe)."""
import json
import os
import re
import subprocess
import sys

BLOCK = "/sys/class/block"
NVME = "/sys/class/nvme"

SMARTCTL = os.environ.get("TA_DISK_SMARTCTL", "smartctl")

ATA_ATTRS = {
    5: "realloc_raw",
    10: "spin_retry_raw",
    187: "unc_raw",
    194: "temperature",
    196: "offline_unc_raw",
    198: "ud_unc_raw",
    199: "crc_raw",
    177: "wearing_level",
    201: "ssd_life_left",
    233: "ssd_life_used",
}

DISK_RE = re.compile(r"^(?:nvme\d+n\d|(?:sd|vd|xvd|hd)[a-z]+)$")


def read_file(path):
    try:
        with open(path) as fh:
            return fh.read().strip()
    except OSError:
        return None


def run_json(cmd, timeout=20):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if p.returncode != 0:
            return None
        return json.loads(p.stdout)
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None


def _nvme_info(name):
    m = re.match(r"^nvme(\d+)n(\d+)$", name)
    if not m:
        return None
    ctrl = os.path.join(NVME, "nvme%s" % m.group(1))
    return {
        "model": read_file(os.path.join(ctrl, "model")) or "nvme",
        "serial": read_file(os.path.join(ctrl, "serial")) or "",
        "firmware": read_file(os.path.join(ctrl, "firmware_rev")) or "",
        "type": "nvme",
    }


def _ata_info(name):
    dev = os.path.join("/sys/block", name, "device")
    return {
        "model": (read_file(os.path.join(dev, "model")) or read_file(os.path.join(dev, "subsystem")) or name).replace(" ", "_"),
        "serial": read_file(os.path.join(dev, "serial")) or "",
        "firmware": read_file(os.path.join(dev, "firmware_rev")) or "",
        "type": "ata",
        "rotational": read_file(os.path.join("/sys/block", name, "queue", "rotational")) == "1",
    }


def _virtio_info(name):
    return {"model": "virtio_blk", "serial": "", "firmware": "", "type": "virtio"}


def discover_disks():
    try:
        names = sorted(os.listdir(BLOCK))
    except OSError:
        return {}
    disks = {}
    for name in names:
        if not DISK_RE.match(name):
            continue
        devdir = os.path.join(BLOCK, name)
        if not os.path.islink(os.path.join(devdir, "device")) and not name.startswith("nvme"):
            continue
        if name.startswith("nvme"):
            info = _nvme_info(name)
            if info is None:
                continue
        elif os.path.exists(os.path.join("/sys/block", name, "device")):
            info = _ata_info(name)
        else:
            info = _virtio_info(name)
        rec = dict(info)
        rec.update(collect_smart(name))
        disks[name] = rec
    return disks


def collect_smart(name):
    dev = "/dev/%s" % name
    ctrl = dev if (name.startswith("nvme") and not re.search(r"n\d+$", name)) else dev
    data = run_json([SMARTCTL, "-a", "-j", dev])
    if data is None:
        return {"smart": None, "status": "ok"}
    rec = {"smart": True}
    smart = data.get("smart") or {}
    power_on = data.get("power_on") or {}
    try:
        rec["power_on_hours"] = int(power_on.get("hours"))
    except (TypeError, ValueError):
        pass

    if rec.get("type") == "nvme":
        rec["health"] = (smart.get("overall_health") or {}).get("value")
        try:
            rec["temperature"] = float(smart["temperature"]["value"])
        except (KeyError, TypeError, ValueError):
            pass
        for src, dst in (
            ("percentage_used", "percentage_used"),
            ("available_spare", "available_spare"),
            ("spare_threshold", "spare_threshold"),
            ("media_errors", "media_errors"),
            ("critical_warn", "critical_warn"),
        ):
            v = (smart.get(src) or {}).get("value")
            if v is not None:
                try:
                    rec[dst] = int(v)
                except (TypeError, ValueError):
                    pass
        for unit in ("data_units_read", "data_units_written"):
            v = (smart.get(unit) or {}).get("value")
            if v is not None:
                try:
                    rec[unit + "_raw"] = int(v)
                except (TypeError, ValueError):
                    pass
    else:
        rec["health"] = smart.get("health_assessment")
        for attr in data.get("attributes") or []:
            try:
                aid = int(attr.get("id"))
            except (TypeError, ValueError):
                continue
            dst = ATA_ATTRS.get(aid)
            if dst is None:
                continue
            raw = ((attr.get("raw") or {}).get("value"))
            if raw is None:
                raw = attr.get("value")
            try:
                rec[dst] = float(raw)
                if dst == "temperature":
                    rec[dst] = float(rec[dst])
            except (TypeError, ValueError):
                pass
    rec["status"] = _status(rec)
    return rec


def _status(r):
    health = (r.get("health") or "").upper()
    if health == "FAILED":
        return "fail"
    spare = r.get("available_spare")
    thr = r.get("spare_threshold")
    if spare is not None and thr is not None and spare < thr:
        return "fail"
    if (r.get("media_errors") or 0) > 0:
        return "fail"
    for key in ("realloc_raw", "unc_raw", "offline_unc_raw", "ud_unc_raw", "crc_raw", "spin_retry_raw"):
        if (r.get(key) or 0) > 0:
            return "alarm"
    if r.get("critical_warn") and r["critical_warn"] > 0:
        return "alarm"
    if spare is not None and thr and thr > 0 and spare < 3 * thr:
        return "alarm"
    return "ok"


METRIC_KEYS = (
    "temperature",
    "power_on_hours",
    "percentage_used",
    "available_spare",
    "media_errors",
    "critical_warn",
    "realloc_raw",
    "spin_retry_raw",
    "unc_raw",
    "offline_unc_raw",
    "ud_unc_raw",
    "crc_raw",
    "wearing_level",
    "ssd_life_left",
    "ssd_life_used",
)


def metrics_payload(disks):
    payload = {"total": len(disks), "smart_available": 1 if any(d.get("smart") for d in disks.values()) else 0}
    for name, d in disks.items():
        st = d.get("status", "unknown")
        payload[name + "_status"] = st
        for key in METRIC_KEYS:
            if key in d and d[key] is not None:
                v = d[key]
                payload["%s_%s" % (name, key)] = int(v) if float(v).is_integer() else float(v)
    return payload


def emit(payload):
    sys.stdout.write(json.dumps(payload, sort_keys=True) + "\n")
    sys.stdout.flush()


def fail(message):
    sys.stderr.write("TA-disk collection error: %s\n" % message)
    return 1
