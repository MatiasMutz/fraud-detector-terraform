import importlib.util
import json
import logging
import sys
import types
import unittest
from pathlib import Path


def load_handler():
    fake_boto3 = types.ModuleType("boto3")
    fake_boto3.client = lambda *args, **kwargs: object()
    sys.modules.setdefault("boto3", fake_boto3)

    path = Path(__file__).parent / "summarizer" / "handler.py"
    spec = importlib.util.spec_from_file_location("summarizer_handler_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class SummarizerTraceLoggingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.handler = load_handler()

    def test_trace_summary_counts_unique_traces_and_caps_samples(self):
        events = [{"trace_id": f"trace-{idx}"} for idx in range(self.handler.MAX_TRACE_SAMPLES + 2)]
        count, samples = self.handler._trace_summary(events)
        self.assertEqual(count, self.handler.MAX_TRACE_SAMPLES + 2)
        self.assertEqual(len(samples), self.handler.MAX_TRACE_SAMPLES)

    def test_safe_log_omits_sensitive_fields(self):
        logger = logging.getLogger()
        with self.assertLogs(logger, level="INFO") as captured:
            self.handler._safe_log(
                "fraud_summary_published",
                trace_id_samples=["trace-safe"],
                transaction_id="tx-secret",
                user_id="user-secret",
                fraud_score=0.99,
                receipt_handle="receipt-secret",
                event_count=1,
            )

        payload = json.loads(captured.output[-1].split("INFO:root:", 1)[-1])
        encoded = json.dumps(payload)
        self.assertEqual(payload["trace_id_samples"], ["trace-safe"])
        self.assertNotIn("tx-secret", encoded)
        self.assertNotIn("user-secret", encoded)
        self.assertNotIn("receipt-secret", encoded)
        self.assertNotIn("fraud_score", payload)


if __name__ == "__main__":
    unittest.main()
