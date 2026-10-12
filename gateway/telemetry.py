"""Read-only host telemetry; unavailable counters are null, never invented."""

from pathlib import Path

import psutil


def telemetry():
    memory = psutil.virtual_memory()
    gpus = []
    for card in sorted(Path("/sys/class/drm").glob("card[0-9]*")):
        if not card.name[4:].isdigit():
            continue
        device = card / "device"
        if not device.exists():
            continue

        def number(name, device=device):
            try:
                return int((device / name).read_text().strip())
            except (OSError, ValueError):
                return None

        temps = []
        for p in device.glob("hwmon/hwmon*/temp*_input"):
            try:
                temps.append(int(p.read_text()) / 1000)
            except (OSError, ValueError):
                pass
        gpus.append(
            {
                "name": card.name,
                "source": "Linux DRM sysfs",
                "utilization_percent": number("gpu_busy_percent"),
                "vram_used_bytes": number("mem_info_vram_used"),
                "vram_total_bytes": number("mem_info_vram_total"),
                "gtt_used_bytes": number("mem_info_gtt_used"),
                "temperature_c": max(temps) if temps else None,
            }
        )
    return {
        "memory": {
            "total_bytes": memory.total,
            "available_bytes": memory.available,
            "pressure_bytes": memory.total - memory.available,
        },
        "cpu_percent": psutil.cpu_percent(),
        "swap_used_bytes": psutil.swap_memory().used,
        "gpus": gpus,
        "note": "Whole-system pressure, not engine RSS. Unified-memory GPU counters may overlap RAM.",
    }
