"""Summarize `docker stats` samples: peak and median memory per container, and the peak of
the total across containers at one moment. Input lines: "<name> <used> / <limit> <cpu%>"."""

import re
import statistics
import sys
from collections import defaultdict

UNITS = {"B": 1 / 1024**2, "KiB": 1 / 1024, "kB": 1 / 1024, "MiB": 1, "MB": 1,
         "GiB": 1024, "GB": 1024}  # fmt: skip


def mib(text: str) -> float:
    number, unit = re.fullmatch(r"([\d.]+)\s*([A-Za-z]+)", text.strip()).groups()
    return float(number) * UNITS[unit]


samples, per, cpu = defaultdict(float), defaultdict(list), defaultdict(list)
for line in open(sys.argv[1]):
    parts = line.split()
    if len(parts) < 5 or parts[0] == "---":
        continue
    stamp, name, used, cpu_pct = parts[0], parts[1], parts[2], parts[-1]
    value = mib(used)
    per[name].append(value)
    samples[stamp] += value
    cpu[name].append(float(cpu_pct.rstrip("%")))

print(f"{'container':40} {'peak MiB':>9} {'median':>8} {'peak CPU%':>10}")
for name in sorted(per):
    print(f"{name:40} {max(per[name]):9.0f} {statistics.median(per[name]):8.0f} "
          f"{max(cpu[name]):10.1f}")  # fmt: skip
print(f"{'peak of the total at one moment':40} {max(samples.values()):9.0f}")
print(f"{'samples':40} {len(samples):9d}")
