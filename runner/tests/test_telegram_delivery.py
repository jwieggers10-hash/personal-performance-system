"""Tests for runner/telegram_delivery.py.

Standard library only (unittest + unittest.mock). No real HTTP request is
ever made -- every test that exercises sending injects a fake http_post
callable, consistent with telegram_delivery.py's own test-mode support
(http_post / sleep_fn / log are all injectable).
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import telegram_delivery as td  # noqa: E402


REAL_LOOKING_TOKEN = "123456789:AAFakeTokenValueForTestsOnlyNotReal"
REAL_LOOKING_CHAT_ID = "987654321"


def make_config() -> td.TelegramConfig:
    return td.TelegramConfig(bot_token=REAL_LOOKING_TOKEN, chat_id=REAL_LOOKING_CHAT_ID)


class RecordingHttpPost:
    """Fake http_post that records every call and returns scripted
    (status, body) responses in order, repeating the last one once
    exhausted."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, url, data, timeout=10.0):
        self.calls.append((url, dict(data)))
        if len(self.responses) > 1:
            return self.responses.pop(0)
        return self.responses[0]


def raising_network_post(*_args, **_kwargs):
    raise td.TelegramNetworkError("Temporary failure in name resolution")


def no_sleep(_seconds):
    return None


class LoadTelegramConfigTests(unittest.TestCase):
    def test_missing_both_returns_none(self):
        self.assertIsNone(td.load_telegram_config({}))

    def test_missing_one_returns_none(self):
        self.assertIsNone(td.load_telegram_config({"TELEGRAM_BOT_TOKEN": REAL_LOOKING_TOKEN}))
        self.assertIsNone(td.load_telegram_config({"TELEGRAM_CHAT_ID": REAL_LOOKING_CHAT_ID}))

    def test_blank_strings_treated_as_missing(self):
        env = {"TELEGRAM_BOT_TOKEN": "   ", "TELEGRAM_CHAT_ID": ""}
        self.assertIsNone(td.load_telegram_config(env))

    def test_both_present_returns_config(self):
        env = {"TELEGRAM_BOT_TOKEN": REAL_LOOKING_TOKEN, "TELEGRAM_CHAT_ID": REAL_LOOKING_CHAT_ID}
        config = td.load_telegram_config(env)
        self.assertEqual(config.bot_token, REAL_LOOKING_TOKEN)
        self.assertEqual(config.chat_id, REAL_LOOKING_CHAT_ID)


class RedactSecretsTests(unittest.TestCase):
    def test_redacts_bot_token_in_url(self):
        url = f"https://api.telegram.org/bot{REAL_LOOKING_TOKEN}/sendMessage"
        redacted = td.redact_secrets(url)
        self.assertNotIn(REAL_LOOKING_TOKEN, redacted)
        self.assertIn("/bot[REDACTED]", redacted)

    def test_redacts_bearer_token(self):
        redacted = td.redact_secrets("Authorization: Bearer abc123XYZ")
        self.assertNotIn("abc123XYZ", redacted)

    def test_redacts_apikey_query_param(self):
        redacted = td.redact_secrets("error calling ...?apiKey=sk_soma_shouldnotleak")
        self.assertNotIn("sk_soma_shouldnotleak", redacted)

    def test_empty_string_passthrough(self):
        self.assertEqual(td.redact_secrets(""), "")


class SplitMessageTests(unittest.TestCase):
    def test_short_message_returns_single_untagged_part(self):
        parts = td.split_message("short text", limit=100)
        self.assertEqual(parts, ["short text"])

    def test_long_message_splits_within_limit_and_preserves_order(self):
        # Build recognizably-ordered content so we can verify sequence.
        words = [f"word{i:03d}" for i in range(200)]
        text = " ".join(words)
        parts = td.split_message(text, limit=80)

        self.assertGreater(len(parts), 1)
        for part in parts:
            self.assertLessEqual(len(part), 80)

        # Strip the "(i/N) " ordering prefix and reassemble; word order
        # must be unchanged and nothing lost or duplicated.
        reassembled = " ".join(
            part.split(") ", 1)[1] if part.startswith("(") else part for part in parts
        )
        self.assertEqual(reassembled.split(), words)

    def test_parts_are_labeled_in_order(self):
        text = "x" * 500
        parts = td.split_message(text, limit=100)
        total = len(parts)
        for i, part in enumerate(parts, start=1):
            self.assertTrue(part.startswith(f"({i}/{total}) "))

    def test_never_exceeds_limit_even_with_ordering_prefix(self):
        text = "y" * 1000
        limit = 60
        parts = td.split_message(text, limit=limit)
        for part in parts:
            self.assertLessEqual(len(part), limit)


class BuildMessageTests(unittest.TestCase):
    def test_includes_confidence_and_notification_level(self):
        message = td.build_message(
            workflow="daily-review",
            local_date="2026-08-23",
            notification_level="attention",
            confidence="moderate",
            review_message="Proceed with caution.",
            review_file="reviews/daily/2026-08-23.md",
        )
        self.assertIn("Confidence: moderate", message)
        self.assertIn("Notification: attention", message)
        self.assertIn("[ATTENTION]", message)
        self.assertIn("reviews/daily/2026-08-23.md", message)
        self.assertIn("Proceed with caution.", message)

    def test_routine_has_no_marker(self):
        message = td.build_message(
            workflow="daily-review",
            local_date="2026-08-23",
            notification_level="routine",
            confidence="high",
            review_message="All good.",
        )
        self.assertNotIn("[ATTENTION]", message)
        self.assertNotIn("[URGENT]", message)

    def test_failed_status_produces_failure_message_not_review_content(self):
        message = td.build_message(
            workflow="daily-review",
            local_date="2026-08-23",
            notification_level="urgent",
            confidence=None,
            review_message="this should not appear",
            status="failed",
            error="Claude execution timed out",
        )
        self.assertIn("could not be completed", message)
        self.assertIn("Claude execution timed out", message)
        self.assertNotIn("this should not appear", message)


class SendTelegramNotificationTests(unittest.TestCase):
    def _base_kwargs(self, **overrides):
        kwargs = dict(
            workflow="daily-review",
            local_date="2026-08-23",
            notification_level="routine",
            notification_required=True,
            confidence="moderate",
            review_message="Proceed as planned.",
            review_file="reviews/daily/2026-08-23.md",
            sleep_fn=no_sleep,
        )
        kwargs.update(overrides)
        return kwargs

    def test_successful_single_part_send(self):
        fake = RecordingHttpPost([(200, json.dumps({"ok": True}))])
        result = td.send_telegram_notification(make_config(), http_post=fake, **self._base_kwargs())

        self.assertTrue(result["notification_delivered"])
        self.assertIsNone(result["notification_delivery_error"])
        self.assertFalse(result["notification_skipped"])
        self.assertEqual(result["parts_sent"], 1)
        self.assertEqual(len(fake.calls), 1)

    def test_skips_cleanly_when_not_configured(self):
        fake = RecordingHttpPost([(200, "{}")])
        result = td.send_telegram_notification(None, http_post=fake, **self._base_kwargs())

        self.assertFalse(result["notification_delivered"])
        self.assertTrue(result["notification_skipped"])
        self.assertIsNotNone(result["notification_delivery_error"])
        self.assertEqual(fake.calls, [])  # never attempted an HTTP call

    def test_skips_when_notification_not_required(self):
        fake = RecordingHttpPost([(200, "{}")])
        result = td.send_telegram_notification(
            make_config(), http_post=fake, **self._base_kwargs(notification_required=False)
        )

        self.assertTrue(result["notification_skipped"])
        self.assertFalse(result["notification_delivered"])
        self.assertEqual(fake.calls, [])

    def test_api_error_4xx_is_not_retried(self):
        fake = RecordingHttpPost([(400, json.dumps({"description": "Bad Request: chat not found"}))])
        result = td.send_telegram_notification(make_config(), http_post=fake, **self._base_kwargs())

        self.assertFalse(result["notification_delivered"])
        self.assertIn("chat not found", result["notification_delivery_error"])
        self.assertEqual(len(fake.calls), 1)  # no retry for a config-level error

    def test_api_error_5xx_is_retried_then_succeeds(self):
        fake = RecordingHttpPost([(503, "Service Unavailable"), (200, json.dumps({"ok": True}))])
        result = td.send_telegram_notification(make_config(), http_post=fake, **self._base_kwargs())

        self.assertTrue(result["notification_delivered"])
        self.assertEqual(len(fake.calls), 2)

    def test_network_failure_retries_then_fails_without_raising(self):
        result = td.send_telegram_notification(
            make_config(), http_post=raising_network_post, **self._base_kwargs()
        )

        self.assertFalse(result["notification_delivered"])
        self.assertIn("network error", result["notification_delivery_error"])
        self.assertNotIn(REAL_LOOKING_TOKEN, result["notification_delivery_error"])

    def test_bounded_retry_does_not_retry_indefinitely(self):
        call_count = {"n": 0}

        def always_failing(url, data, timeout=10.0):
            call_count["n"] += 1
            return 500, "still failing"

        result = td.send_telegram_notification(
            make_config(), http_post=always_failing, **self._base_kwargs()
        )

        self.assertFalse(result["notification_delivered"])
        self.assertEqual(call_count["n"], td.MAX_SEND_ATTEMPTS)

    def test_long_message_sends_all_parts_in_order(self):
        long_review = " ".join(f"line{i:03d}" for i in range(800))  # forces a split (well over the 4096 limit)
        fake = RecordingHttpPost([(200, json.dumps({"ok": True}))])
        result = td.send_telegram_notification(
            make_config(), http_post=fake, **self._base_kwargs(review_message=long_review)
        )

        self.assertTrue(result["notification_delivered"])
        self.assertGreater(result["parts_sent"], 1)
        self.assertEqual(len(fake.calls), result["parts_sent"])

        sent_texts = [call[1]["text"] for call in fake.calls]
        for i, text in enumerate(sent_texts, start=1):
            self.assertTrue(text.startswith(f"({i}/{len(sent_texts)}) "))

    def test_stops_on_first_failed_part_without_sending_later_parts(self):
        long_review = " ".join(f"line{i:03d}" for i in range(800))
        # First part succeeds, second part hits a non-retryable 4xx.
        fake = RecordingHttpPost([
            (200, json.dumps({"ok": True})),
            (400, json.dumps({"description": "bad request"})),
        ])
        result = td.send_telegram_notification(
            make_config(), http_post=fake, **self._base_kwargs(review_message=long_review)
        )

        self.assertFalse(result["notification_delivered"])
        self.assertEqual(result["parts_sent"], 1)
        self.assertEqual(len(fake.calls), 2)  # attempted part 2, did not skip ahead to part 3+

    def test_invalid_notification_level_is_skipped_not_sent(self):
        fake = RecordingHttpPost([(200, "{}")])
        result = td.send_telegram_notification(
            make_config(), http_post=fake, **self._base_kwargs(notification_level="critical")
        )

        self.assertTrue(result["notification_skipped"])
        self.assertEqual(fake.calls, [])

    def test_never_logs_the_bot_token(self):
        logged = []

        def capturing_log(**fields):
            logged.append(fields)

        fake = RecordingHttpPost([(200, json.dumps({"ok": True}))])
        td.send_telegram_notification(
            make_config(), http_post=fake, log=capturing_log, **self._base_kwargs()
        )

        for entry in logged:
            self.assertNotIn(REAL_LOOKING_TOKEN, json.dumps(entry))

    def test_delivery_result_has_expected_keys_and_types(self):
        fake = RecordingHttpPost([(200, json.dumps({"ok": True}))])
        result = td.send_telegram_notification(make_config(), http_post=fake, **self._base_kwargs())

        self.assertEqual(
            set(result.keys()),
            {"notification_delivered", "notification_delivery_error", "notification_skipped", "parts_sent"},
        )
        self.assertIsInstance(result["notification_delivered"], bool)
        self.assertIsInstance(result["notification_skipped"], bool)
        self.assertIsInstance(result["parts_sent"], int)
        self.assertTrue(result["notification_delivery_error"] is None or isinstance(result["notification_delivery_error"], str))


class DeliverReviewAdapterTests(unittest.TestCase):
    def test_extracts_fields_from_runner_result_dict(self):
        runner_result = {
            "workflow": "daily-review",
            "local_date": "2026-08-23",
            "notification_level": "attention",
            "notification_required": True,
            "confidence": "moderate",
            "recommendation_summary": "Reduce volume today.",
            "review_file": "reviews/daily/2026-08-23.md",
            "status": "completed_with_limitations",
            "error": None,
        }
        fake = RecordingHttpPost([(200, json.dumps({"ok": True}))])

        result = td.deliver_review(
            runner_result, REAL_LOOKING_TOKEN, REAL_LOOKING_CHAT_ID, http_post=fake, sleep_fn=no_sleep
        )

        self.assertTrue(result["notification_delivered"])
        self.assertEqual(len(fake.calls), 1)
        self.assertIn("Reduce volume today.", fake.calls[0][1]["text"])

    def test_missing_secrets_skips_without_error(self):
        runner_result = {"workflow": "daily-review", "notification_required": True, "notification_level": "routine"}
        fake = RecordingHttpPost([(200, "{}")])

        result = td.deliver_review(runner_result, None, None, http_post=fake, sleep_fn=no_sleep)

        self.assertTrue(result["notification_skipped"])
        self.assertFalse(result["notification_delivered"])
        self.assertEqual(fake.calls, [])


if __name__ == "__main__":
    unittest.main()
