import unittest

from hazardous_ledger_core.acceptance import run


class AcceptanceTest(unittest.TestCase):
    def test_offline_acceptance(self):
        result = run()
        self.assertEqual("ok", result["status"])
        self.assertTrue(result["audit_valid"])
        self.assertFalse(result["first_replayed"])
        self.assertTrue(result["second_replayed"])
        self.assertEqual(1, result["records"])
        self.assertTrue(result["discrepancy_raised"])
        self.assertTrue(result["reopen_blocked"])
        self.assertEqual("bin-001", result["trace_source"])
        self.assertEqual("carrier-001", result["trace_destination"])
        self.assertEqual(50.4, result["final_weight"])


if __name__ == "__main__":
    unittest.main()
