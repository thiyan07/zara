"""P5 feature-hunt ⑬ (2026-10-23) — laptop health reading.

`system.health`: instantaneous read-only facts (home disk, RAM,
load averages, cores, uptime). Plain /proc + statvfs numbers, no
judgments or predictions; PUBLIC/SAFE like the battery read,
laptop-side (parity-pinned), routed by honest laptop aliases.
"""
import unittest


class TestSystemHealthContract(unittest.TestCase):
    def test_registry_contract(self):
        from core.device_tools import DEVICE_TOOL_DEFS
        from core.models import PermissionLevel, RiskLevel
        got = {d.name: d for d in DEVICE_TOOL_DEFS}
        d = got["system.health"]
        self.assertEqual(d.permission, PermissionLevel.PUBLIC)
        self.assertEqual(d.risk, RiskLevel.SAFE)
        self.assertFalse(d.never_auto_approve)
        self.assertFalse(d.read_only_auto_allow)
        self.assertEqual(d.supported_devices, ["linux"])
        self.assertEqual(d.required_capabilities, ["system.health"])
        from device.linux.tools_linux import LINUX_TOOLS
        self.assertIn("system.health", {t.name for t, _ in LINUX_TOOLS})

    def test_deferred_loader_matches(self):
        # hyperlarge tool file: import cost guard
        import core.device_tools as dt
        self.assertTrue(any(
            d.name == "system.health" for d in DEVICE_TOOL_DEFS_OVERRIDE(dt)))

    def test_handler_is_pure_read_and_plausible(self):
        from core.builtin_tools import system_health
        r = system_health({}, {})
        self.assertTrue(r["ok"], r)
        for key in ("disk_total_gb", "disk_free_gb", "ram_total_gb",
                    "load_1m", "cpu_cores", "uptime_days"):
            self.assertIn(key, r, r)
        self.assertGreater(r["disk_total_gb"], 0)
        self.assertGreater(r["ram_total_gb"], 0)
        self.assertGreaterEqual(r["ram_available_gb"], 0)
        self.assertTrue(0 <= r["disk_used_pct"] <= 100, r)
        self.assertTrue(0 <= r["ram_used_pct"] <= 100, r)
        self.assertGreater(r["cpu_cores"], 0)
        self.assertGreater(r["uptime_days"], 0)
        self.assertIsInstance(r["load_1m"], float)
        self.assertIn("no judgments", r["detail"])


def DEVICE_TOOL_DEFS_OVERRIDE(dt):
    return dt.DEVICE_TOOL_DEFS


if __name__ == "__main__":
    unittest.main()
