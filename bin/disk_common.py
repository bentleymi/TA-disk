"""Shared collection logic for TA-disk scripted inputs (SMART + NVMe)."""
import json
import os
import re
import subprocess
import sys

BLOCK = "/sys/class/block"
NVME = "/sys/class/nvme"

def _smart_cmds():
    env = os.environ.get("TA_DISK_SMARTCTL")
    if env:
        return [env]
    cands = []
    if os.path.exists("/usr/local/sbin/ta_disk_smartctl"):
        cands.append("/usr/local/sbin/ta_disk_smartctl")
    for p in ("/usr/sbin/smartctl", "/usr/bin/smartctl"):
        if os.path.exists(p):
            cands.append(p)
    cands.append("smartctl")
    return cands


SMARTCMDS = _smart_cmds()
_SMART_KEYS = ("smart_status", "temperature", "nvme_smart_health_information_log",
               "ata_smart_attributes", "scsi_smart_attributes")


def _smartctl_json(name):
    last = None
    for cmd in SMARTCMDS:
        data = run_json([cmd, "-a", "-j", "/dev/%s" % name])
        if not data:
            continue
        last = data
        if any(k in data for k in _SMART_KEYS):
            return data
    return last

# ATA attribute id -> metric key (raw values are the meaningful ones)
ATA_ATTRS = {
    5: "realloc_raw",
    10: "spin_retry_raw",
    187: "unc_raw",
    196: "offline_unc_raw",
    198: "ud_unc_raw",
    199: "crc_raw",
    177: "wearing_level",
    201: "ssd_life_left",
    233: "ssd_life_used",
}

METRIC_KEYS = (
    "temperature",
    "power_on_hours",
    "percentage_used",
    "available_spare",
    "spare_threshold",
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
    "data_units_read_raw",
    "data_units_written_raw",
)

DISK_RE = re.compile(r"^(?:nvme\d+n\d|sd[a-z]+|xvd[a-z]+|hd[a-z]+|vd[a-z]+)$")


def read_file(path):
    try:
        with open(path) as fh:
            return fh.read().strip()
    except OSError:
        return None


def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def run_json(cmd, timeout=25):
    """smartctl exits non-zero on partial reads but still emits valid JSON."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None
    try:
        return json.loads(p.stdout)
    except ValueError:
        return None


def _info_nvme(name):
    m = re.match(r"^nvme(\d+)n(\d+)$", name)
    if not m:
        return None
    ctrl = os.path.join(NVME, "nvme%s" % m.group(1))
    if not os.path.isdir(ctrl):
        return None
    return {
        "model": (read_file(os.path.join(ctrl, "model")) or "nvme").strip().replace(" ", "_"),
        "serial": read_file(os.path.join(ctrl, "serial")) or "",
        "firmware": read_file(os.path.join(ctrl, "firmware_rev")) or "",
        "type": "nvme",
    }


def _info_sata(name):
    dev = os.path.join("/sys/block", name, "device")
    model = read_file(os.path.join(dev, "model")) or read_file(os.path.join(dev, "subsystem")) or name
    return {
        "model": model.replace(" ", "_"),
        "serial": read_file(os.path.join(dev, "serial")) or "",
        "firmware": read_file(os.path.join(dev, "firmware_rev")) or "",
        "type": "ata",
        "rotational": read_file(os.path.join("/sys/block", name, "queue", "rotational")) == "1",
    }


def discover_disks():
    try:
        names = sorted(os.listdir(BLOCK))
    except OSError:
        return {}
    disks = {}
    for name in names:
        if not DISK_RE.match(name):
            continue
        if name.startswith("nvme"):
            info = _info_nvme(name)
            if info is None:
                continue
        elif os.path.exists(os.path.join("/sys/block", name, "device")):
            info = _info_sata(name)
        else:
            continue
        rec = dict(info)
        rec.update(collect_smart(name))
        disks[name] = rec
    return disks


def collect_smart(name):
    data = _smartctl_json(name)
    if data is None:
        return {"smart_available": 0, "status": "ok"}
    rec = {}
    st = data.get("smart_status")
    s = data.get("smart") or {}
    if isinstance(st, dict) and "passed" in st:
        rec["health"] = "PASSED" if st.get("passed") else "FAILED"
    if not rec.get("health") and s.get("health_assessment"):
        rec["health"] = s["health_assessment"]
    if not rec.get("health") and isinstance(s.get("overall_health"), dict):
        rec["health"] = s["overall_health"].get("value")

    pot = data.get("power_on_time")
    if isinstance(pot, dict):
        h = pot.get("hours")
        if h is None and pot.get("seconds") is not None:
            h = int(pot["seconds"]) // 3600
        if h is not None and _num(h) is not None:
            rec["power_on_hours"] = int(_num(h))
    elif (data.get("power_on") or {}).get("hours") is not None:
        h = _num((data.get("power_on") or {}).get("hours"))
        if h is not None:
            rec["power_on_hours"] = int(h)

    t = data.get("temperature")
    if isinstance(t, dict):
        t = t.get("current", t.get("celsius"))
    tv = _num(t)
    if tv is not None:
        rec["temperature"] = tv

    if rec.get("type") == "nvme":
        log = data.get("nvme_smart_health_information_log") or {}
        if not log:
            # old nested format fallback
            for src, dst in (
                ("percentage_used", "percentage_used"),
                ("available_spare", "available_spare"),
                ("spare_threshold", "spare_threshold"),
                ("media_errors", "media_errors"),
                ("critical_warn", "critical_warn"),
            ):
                v = (s.get(src) or {}).get("value") if isinstance(s.get(src), dict) else s.get(src)
                if _num(v) is not None:
                    rec[dst] = int(_num(v))
            v = (s.get("data_units_read") or {}).get("value") if isinstance(s.get("data_units_read"), dict) else None
            if _num(v) is not None:
                rec["data_units_read_raw"] = int(_num(v))
            v = (s.get("data_units_written") or {}).get("value") if isinstance(s.get("data_units_written"), dict) else None
            if _num(v) is not None:
                rec["data_units_written_raw"] = int(_num(v))
        else:
            for src, dst in (
                ("percentage_used", "percentage_used"),
                ("available_spare", "available_spare"),
                ("available_spare_threshold", "spare_threshold"),
                ("media_errors", "media_errors"),
                ("critical_warning", "critical_warn"),
                ("power_on_hours", "power_on_hours"),
            ):
                v = log.get(src)
                if _num(v) is not None:
                    rec[dst] = int(_num(v))
            for unit, dst in (
                ("data_units_read", "data_units_read_raw"),
                ("data_units_written", "data_units_written_raw"),
            ):
                v = log.get(unit)
                if _num(v) is not None:
                    rec[dst] = int(_num(v))
    else:
        attrs = data.get("ata_smart_attributes")
        if isinstance(attrs, dict):
            items = list(attrs.items())
        else:
            items = [(a.get("id"), a) for a in (attrs or []) if isinstance(a, dict)]
        for aid, a in items:
            try:
                aid = int(aid)
            except (TypeError, ValueError):
                continue
            dst = ATA_ATTRS.get(aid)
            if dst is None:
                continue
            if not isinstance(a, dict):
                continue
            raw = a.get("raw_value", a.get("raw"))
            if isinstance(raw, dict):
                raw = raw.get("value")
            if raw is None:
                raw = a.get("value")
            rv = _num(raw)
            if rv is None:
                continue
            if dst == "temperature":
                if rec.get("temperature") is None:
                    rec["temperature"] = rv
            else:
                rec[dst] = rv
    rec["status"] = _status(rec)
    got = any(k in rec for k in ("health", "temperature", "power_on_hours", "percentage_used"))
    rec["smart_available"] = 1 if got else 0
    return rec


def _status(r):
    health = (r.get("health") or "").upper()
    if health == "FAILED":
        return "fail"
    spare = r.get("available_spare")
    thr = r.get("spare_threshold")
    if spare is not None and thr is not None and thr > 0 and spare <= thr:
        return "fail"
    if (r.get("media_errors") or 0) > 0:
        return "fail"
    for key in ("realloc_raw", "unc_raw", "offline_unc_raw", "ud_unc_raw", "crc_raw", "spin_retry_raw"):
        if (r.get(key) or 0) > 0:
            return "alarm"
    if (r.get("critical_warn") or 0) > 0:
        return "alarm"
    if spare is not None and thr and thr > 0 and spare <= 3 * thr:
        return "alarm"
    if (r.get("percentage_used") or 0) >= 90:
        return "alarm"
    return "ok"


def metrics_payload(disks):
    payload = {"total": len(disks)}
    payload["smart_available"] = 1 if any(d.get("smart_available") for d in disks.values()) else 0
    for name, d in disks.items():
        st = d.get("status", "unknown")
        if st in ("fail", "alarm"):
            payload[name + "_status"] = "alarm"
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
