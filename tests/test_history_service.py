import importlib.util
from pathlib import Path


MODULE_PATH = Path(__file__).parents[1] / "src" / "service" / "history_service.py"
spec = importlib.util.spec_from_file_location("usage_history_service", MODULE_PATH)
service = importlib.util.module_from_spec(spec)
spec.loader.exec_module(service)


def check(name, actual, expected):
    if actual != expected:
        raise AssertionError(f"{name}: expected {expected!r}, got {actual!r}")


now = 1_000_000
gib = 1024**3

# Ordinary traffic is unchanged.
check("normal", service.calculate_delta(3 * gib, 10, (2 * gib, 10, 0, 0), now), (gib, 0, 0))

# A counter decrease starts a reset guard and adds nothing by itself.
delta, high, until = service.calculate_delta(0, 10, (22 * gib, 10, 0, 0), now)
check("reset delta", delta, 0)
check("reset high-water", high, 22 * gib)
check("reset guard", until, now + service.RESET_GUARD_SECONDS)

# A full or partial restoration of the old Xray counter is not new traffic.
check(
    "full rebound",
    service.calculate_delta(22 * gib, 10, (0, 10, high, until), now + 60)[0],
    0,
)
check(
    "partial rebound",
    service.calculate_delta(18 * gib, 10, (0, 10, high, until), now + 60)[0],
    0,
)
check(
    "multi-step rebound",
    service.calculate_delta(22 * gib, 10, (18 * gib, 10, high, until), now + 120)[0],
    0,
)

# Small, gradual post-renewal traffic is still counted.
check(
    "gradual post-reset traffic",
    service.calculate_delta(300 * 1024**2, 10, (200 * 1024**2, 10, high, until), now + 120)[0],
    100 * 1024**2,
)

# Client edits/renewals rebaseline the collector before a reset becomes visible.
delta, edit_high, edit_until = service.calculate_delta(
    22 * gib, 11, (22 * gib, 10, 0, 0), now
)
check("renewal rebaseline", delta, 0)
check("renewal high-water", edit_high, 22 * gib)
check("renewal guard", edit_until, now + service.RESET_GUARD_SECONDS)

print("all reset/rebound tests passed")
