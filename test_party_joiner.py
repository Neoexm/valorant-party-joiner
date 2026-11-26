import json
import math
import ssl
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from unittest.mock import Mock, patch

import valorant_party_joiner as joiner


PLAYER = "11111111-1111-4111-8111-111111111111"
OTHER_PLAYER = "22222222-2222-4222-8222-222222222222"
BASE = "33333333-3333-4333-8333-333333333333"
TARGET = "44444444-4444-4444-8444-444444444444"
OTHER_PARTY = "55555555-5555-4555-8555-555555555555"
CODE = "AbCd12"
VERSION = "release-12.10-shipping-10-1234567"


class ScriptedClient:
    def __init__(self, snapshots, joins=(), prepare_actions=()):
        self.snapshots = list(snapshots)
        self.joins = list(joins)
        self.prepare_actions = list(prepare_actions)
        self.calls = []
        self.clear_count = 0
        self.refresh_count = 0

    def take(self, values):
        if not values:
            raise AssertionError("Unexpected extra client call")
        value = values.pop(0)
        if isinstance(value, Exception):
            raise value
        if callable(value):
            return value()
        return value

    def prepare(self):
        self.calls.append(("prepare",))
        if self.prepare_actions:
            self.take(self.prepare_actions)

    def snapshot(self):
        self.calls.append(("snapshot",))
        return self.take(self.snapshots)

    def join(self, code):
        self.calls.append(("join", code))
        return self.take(self.joins)

    def invalidate_auth(self):
        self.refresh_count += 1

    def clear(self):
        self.clear_count += 1

    @property
    def join_calls(self):
        return [call for call in self.calls if call[0] == "join"]


class VirtualStop:
    def __init__(self, on_wait=None):
        self.now = 0.0
        self.stopped = False
        self.waits = []
        self.on_wait = on_wait

    def is_set(self):
        return self.stopped

    def set(self):
        self.stopped = True

    def wait(self, seconds):
        self.waits.append(seconds)
        self.now = max(self.now + seconds, math.nextafter(self.now, math.inf))
        if self.on_wait:
            self.on_wait(self)
        if len(self.waits) > 2000:
            raise AssertionError("Retry wait did not finish")
        return self.stopped


class WorkerTests(unittest.TestCase):
    def run_worker(self, client, stop=None, stop_after_wait=None, emit_hook=None):
        stop = stop or threading.Event()
        events = []
        delays = []
        now = 0.0

        def emit(kind, value):
            events.append((kind, value))
            if emit_hook:
                emit_hook(kind, value, stop)

        def wait(delay):
            nonlocal now
            delays.append(delay)
            now += delay
            if len(delays) >= (stop_after_wait or 20):
                stop.set()
            return stop.is_set()

        with patch.object(joiner.time, "monotonic", side_effect=lambda: now):
            result = joiner.JoinWorker(client, stop, emit, wait).run(CODE, 5)
        self.assertEqual(client.clear_count, 1)
        return result, events, delays

    def test_already_in_matching_party_does_not_post(self):
        client = ScriptedClient([joiner.Party(TARGET, CODE, True)])
        result, _, delays = self.run_worker(client)
        self.assertEqual(result.status, "joined")
        self.assertEqual(result.attempts, 0)
        self.assertEqual(client.join_calls, [])
        self.assertEqual(delays, [])

    def test_solo_party_is_not_success_and_join_is_confirmed(self):
        client = ScriptedClient(
            [joiner.Party(BASE, "", True), joiner.Party(TARGET, "", True)],
            [TARGET],
        )
        result, _, _ = self.run_worker(client)
        self.assertEqual(result.status, "joined")
        self.assertEqual(result.attempts, 1)
        self.assertEqual(client.join_calls, [("join", CODE)])

    def test_matching_code_without_membership_is_not_success(self):
        client = ScriptedClient(
            [joiner.Party(TARGET, CODE, False)],
            [joiner.ApiError(400, "JOIN_REJECTED")],
        )
        result, _, _ = self.run_worker(client, stop_after_wait=1)
        self.assertEqual(result.status, "stopped")
        self.assertEqual(result.attempts, 1)

    def test_rejected_join_retries_the_same_code_then_stops_on_success(self):
        client = ScriptedClient(
            [joiner.Party(BASE, "", True), joiner.Party(BASE, "", True), joiner.Party(TARGET, CODE, True)],
            [joiner.ApiError(400, "JOIN_REJECTED"), TARGET],
        )
        result, _, delays = self.run_worker(client)
        self.assertEqual(result.status, "joined")
        self.assertEqual(client.join_calls, [("join", CODE), ("join", CODE)])
        self.assertEqual(delays, [5, 3])

    def test_lost_post_response_checks_membership_before_retry(self):
        client = ScriptedClient(
            [joiner.Party(BASE, "", True), joiner.Party(TARGET, CODE, True)],
            [joiner.ConnectionProblem("No response from Riot's servers.")],
        )
        result, _, _ = self.run_worker(client)
        self.assertEqual(result.status, "joined")
        self.assertEqual(len(client.join_calls), 1)

    def test_lost_response_with_unrelated_party_change_stops_without_success(self):
        client = ScriptedClient(
            [joiner.Party(BASE, "", True), joiner.Party(OTHER_PARTY, "other-code", True)],
            [joiner.ConnectionProblem("No response from Riot's servers.")],
        )
        result, _, _ = self.run_worker(client)
        self.assertEqual(result.status, "check")
        self.assertEqual(len(client.join_calls), 1)

    def test_lost_response_with_unchanged_party_can_retry(self):
        client = ScriptedClient(
            [joiner.Party(BASE, "", True), joiner.Party(BASE, "", True), joiner.Party(TARGET, CODE, True)],
            [joiner.ConnectionProblem("No response from Riot's servers."), TARGET],
        )
        result, _, _ = self.run_worker(client)
        self.assertEqual(result.status, "joined")
        self.assertEqual(len(client.join_calls), 2)

    def test_ambiguous_accepted_response_never_posts_again(self):
        client = ScriptedClient([joiner.Party(BASE, "", True)] * 4, [None])
        result, _, _ = self.run_worker(client)
        self.assertEqual(result.status, "check")
        self.assertEqual(len(client.join_calls), 1)

    def test_returned_baseline_party_cannot_count_as_join_success(self):
        client = ScriptedClient([joiner.Party(BASE, "", True)] * 4, [BASE])
        result, _, _ = self.run_worker(client)
        self.assertEqual(result.status, "check")
        self.assertEqual(len(client.join_calls), 1)

    def test_accepted_target_must_match_confirmed_party(self):
        client = ScriptedClient(
            [joiner.Party(BASE, "", True), joiner.Party(OTHER_PARTY, "", True)],
            [TARGET],
        )
        result, _, _ = self.run_worker(client)
        self.assertEqual(result.status, "check")
        self.assertEqual(len(client.join_calls), 1)

    def test_rate_limit_during_confirmation_preserves_accepted_join(self):
        client = ScriptedClient(
            [joiner.Party(BASE, "", True), joiner.ApiError(429, retry_after="80"), joiner.Party(TARGET, "", True)],
            [TARGET],
        )
        result, _, delays = self.run_worker(client)
        self.assertEqual(result.status, "joined")
        self.assertEqual(delays, [3, 80])
        self.assertEqual(len(client.join_calls), 1)

    def test_429_honours_server_cooldown_before_second_join(self):
        client = ScriptedClient(
            [joiner.Party(BASE, "", True), joiner.Party(BASE, "", True), joiner.Party(TARGET, "", True)],
            [joiner.ApiError(429, retry_after="65"), TARGET],
        )
        result, _, delays = self.run_worker(client)
        self.assertEqual(result.status, "joined")
        self.assertEqual(delays[0], 65)

    def test_server_failures_increase_backoff(self):
        client = ScriptedClient(
            [joiner.Party(BASE, "", True)] * 3,
            [joiner.ApiError(503)] * 3,
        )
        result, _, delays = self.run_worker(client, stop_after_wait=3)
        self.assertEqual(result.status, "stopped")
        self.assertEqual(delays, [5, 10, 20])

    def test_successful_preflight_does_not_hide_persistent_post_401(self):
        client = ScriptedClient(
            [joiner.Party(BASE, "", True)] * 2,
            [joiner.ApiError(401)] * 2,
        )
        result, _, _ = self.run_worker(client)
        self.assertEqual(result.status, "error")
        self.assertEqual(client.refresh_count, 1)
        self.assertEqual(len(client.join_calls), 2)

    def test_403_stops_without_retry(self):
        for failure in (joiner.ApiError(403), joiner.ApiError(403, "ACCESS_DENIED"), joiner.ApiError(403, "PARTY_FULL", local=True)):
            with self.subTest(failure=failure, local=failure.local):
                client = ScriptedClient([joiner.Party(BASE, "", True)], [failure])
                result, _, delays = self.run_worker(client)
                self.assertEqual(result.status, "error")
                self.assertEqual(delays, [])
                self.assertEqual(len(client.join_calls), 1)

    def test_join_party_got_blocked_stops_and_preserves_error_code(self):
        client = ScriptedClient(
            [joiner.Party(BASE, "", True)],
            [joiner.ApiError(400, "JOIN_PARTY_GOT_BLOCKED")],
        )
        result, _, delays = self.run_worker(client)
        self.assertEqual(result.status, "error")
        self.assertIn("JOIN_PARTY_GOT_BLOCKED", result.message)
        self.assertEqual(result.attempts, 1)
        self.assertEqual(client.join_calls, [("join", CODE)])
        self.assertEqual(delays, [])

    def test_remote_join_403_party_full_retries_then_stops_on_success(self):
        client = ScriptedClient(
            [joiner.Party(BASE, "", True), joiner.Party(BASE, "", True), joiner.Party(TARGET, "", True)],
            [joiner.ApiError(403, "PARTY_FULL"), TARGET],
        )
        result, _, delays = self.run_worker(client)
        self.assertEqual(result.status, "joined")
        self.assertEqual(result.attempts, 2)
        self.assertEqual(client.join_calls, [("join", CODE), ("join", CODE)])
        self.assertEqual(delays, [5, 3])

    def test_remote_join_403_party_full_honours_retry_after(self):
        client = ScriptedClient(
            [joiner.Party(BASE, "", True), joiner.Party(BASE, "", True), joiner.Party(TARGET, "", True)],
            [joiner.ApiError(403, "PARTY_FULL", retry_after="45"), TARGET],
        )
        result, _, delays = self.run_worker(client)
        self.assertEqual(result.status, "joined")
        self.assertEqual(delays, [45, 3])

    def test_preflight_403_party_full_stops_without_joining(self):
        for local in (False, True):
            with self.subTest(local=local):
                client = ScriptedClient([joiner.ApiError(403, "PARTY_FULL", local=local)])
                result, _, delays = self.run_worker(client)
                self.assertEqual(result.status, "error")
                self.assertEqual(client.join_calls, [])
                self.assertEqual(delays, [])

    def test_preexisting_cancellation_makes_no_client_calls(self):
        stop = threading.Event()
        stop.set()
        client = ScriptedClient([])
        result, _, _ = self.run_worker(client, stop=stop)
        self.assertEqual(result.status, "stopped")
        self.assertEqual(client.calls, [])

    def test_cancellation_after_prepare_does_not_post(self):
        stop = threading.Event()
        client = ScriptedClient([], prepare_actions=[stop.set])
        result, _, _ = self.run_worker(client, stop=stop)
        self.assertEqual(result.status, "stopped")
        self.assertEqual(client.calls, [("prepare",)])

    def test_cancellation_between_status_update_and_post_does_not_post(self):
        client = ScriptedClient([joiner.Party(BASE, "", True)], [TARGET])

        def cancel(kind, value, stop):
            if kind == "status" and value.startswith("Trying to join"):
                stop.set()

        result, _, _ = self.run_worker(client, emit_hook=cancel)
        self.assertEqual(result.status, "stopped")
        self.assertEqual(client.join_calls, [])

    def test_stop_during_wait_prevents_next_attempt(self):
        client = ScriptedClient([joiner.Party(BASE, "", True)], [joiner.ApiError(400)])
        result, _, _ = self.run_worker(client, stop_after_wait=1)
        self.assertEqual(result.status, "stopped")
        self.assertEqual(len(client.join_calls), 1)

    def test_closed_client_stops_before_post(self):
        client = ScriptedClient([], prepare_actions=[joiner.StopReason("VALORANT is closed. Retries stopped.")])
        result, _, _ = self.run_worker(client)
        self.assertEqual(result.status, "error")
        self.assertEqual(client.join_calls, [])

    def test_unexpected_exception_cannot_log_sensitive_exception_text(self):
        secret = "Bearer SECRET-TOKEN https://glz-eu-1.eu.a.pvp.net/joinbycode/PRIVATE-CODE"
        client = ScriptedClient([], prepare_actions=[RuntimeError(secret)])
        result, events, _ = self.run_worker(client)
        output = result.message + repr(events)
        self.assertEqual(result.status, "error")
        self.assertNotIn("SECRET-TOKEN", output)
        self.assertNotIn("PRIVATE-CODE", output)


class LiveDelayTests(unittest.TestCase):
    def test_delay_accepts_boundaries_and_fractional_seconds(self):
        timing = joiner.RetryDelay()
        self.assertEqual(timing.get(), 5)
        for seconds in (0.1, 0.25, 1, 1.25, 2.5, 299.5, 300):
            with self.subTest(seconds=seconds):
                timing.set(seconds)
                self.assertEqual(timing.get(), seconds)
        for seconds in (0, -1, 0.05, 0.099, 300.001, float("nan"), float("inf"), float("-inf")):
            with self.subTest(seconds=seconds):
                with self.assertRaises(ValueError):
                    timing.set(seconds)
                self.assertEqual(timing.get(), 300)

    def test_normal_wait_can_be_shortened_or_lengthened_while_waiting(self):
        for start, update, expected in ((5, 0.1, 0.4), (5, 1.25, 1.25), (1, 2.5, 2.5)):
            with self.subTest(start=start, update=update):
                timing = joiner.RetryDelay(start)

                def change(stop):
                    if len(stop.waits) == 2:
                        timing.set(update)

                stop = VirtualStop(change)
                worker = joiner.JoinWorker(Mock(), stop, Mock())
                with patch.object(joiner.time, "monotonic", side_effect=lambda: stop.now):
                    self.assertFalse(worker._pause(timing))
                self.assertAlmostEqual(stop.now, expected)
                self.assertLessEqual(max(stop.waits), 0.2)

    def test_minimum_delay_waits_one_tenth_of_a_second(self):
        timing = joiner.RetryDelay(0.1)
        stop = VirtualStop()
        worker = joiner.JoinWorker(Mock(), stop, Mock())
        with patch.object(joiner.time, "monotonic", side_effect=lambda: stop.now):
            self.assertFalse(worker._pause(timing))
        self.assertAlmostEqual(stop.now, 0.1)

    def test_full_party_403_uses_and_displays_one_tenth_second_delay(self):
        stop = VirtualStop()
        client = ScriptedClient(
            [joiner.Party(BASE, "", True), joiner.Party(BASE, "", True), joiner.Party(TARGET, "", True)],
            [joiner.ApiError(403, "PARTY_FULL"), TARGET],
        )
        events = []
        join_times = []
        original_join = client.join

        def record_join(code):
            join_times.append(stop.now)
            return original_join(code)

        client.join = record_join
        worker = joiner.JoinWorker(client, stop, lambda kind, value: events.append((kind, value)))
        with patch.object(joiner.time, "monotonic", side_effect=lambda: stop.now):
            result = worker.run(CODE, joiner.RetryDelay(0.1))
        self.assertEqual(result.status, "joined")
        self.assertEqual(len(join_times), 2)
        self.assertAlmostEqual(join_times[1], 0.1)
        self.assertTrue(any("0.1s" in value for kind, value in events if kind == "log"))

    def test_lower_delay_cannot_shorten_server_minimum(self):
        timing = joiner.RetryDelay(5)

        def change(stop):
            if len(stop.waits) == 2:
                timing.set(0.1)

        stop = VirtualStop(change)
        worker = joiner.JoinWorker(Mock(), stop, Mock())
        with patch.object(joiner.time, "monotonic", side_effect=lambda: stop.now):
            self.assertFalse(worker._pause(timing, minimum=20))
        self.assertAlmostEqual(stop.now, 20)

    def test_confirmation_wait_stays_fixed_when_delay_changes(self):
        for changed_delay in (0.1, 300):
            with self.subTest(changed_delay=changed_delay):
                timing = joiner.RetryDelay(5)

                def change(stop):
                    if len(stop.waits) == 2:
                        timing.set(changed_delay)

                stop = VirtualStop(change)
                worker = joiner.JoinWorker(Mock(), stop, Mock())
                with patch.object(joiner.time, "monotonic", side_effect=lambda: stop.now):
                    self.assertFalse(worker._pause(timing, fixed=3))
                self.assertAlmostEqual(stop.now, 3)

    def test_changed_delay_controls_next_attempt_during_active_worker(self):
        timing = joiner.RetryDelay(5)

        def change(stop):
            if len(stop.waits) == 2:
                timing.set(0.1)

        stop = VirtualStop(change)
        client = ScriptedClient(
            [joiner.Party(BASE, "", True), joiner.Party(BASE, "", True), joiner.Party(TARGET, "", True)],
            [joiner.ApiError(400), TARGET],
        )
        join_times = []
        original_join = client.join

        def record_join(code):
            join_times.append(stop.now)
            return original_join(code)

        client.join = record_join
        worker = joiner.JoinWorker(client, stop, Mock())
        with patch.object(joiner.time, "monotonic", side_effect=lambda: stop.now):
            result = worker.run(CODE, timing)
        self.assertEqual(result.status, "joined")
        self.assertEqual(len(join_times), 2)
        self.assertAlmostEqual(join_times[1], 0.4)
        self.assertAlmostEqual(stop.now, 3.4)

    def test_live_delay_reduction_respects_cooldowns_and_backoff_in_worker(self):
        cases = (
            (joiner.ApiError(403, "PARTY_FULL", retry_after="12"), 12),
            (joiner.ApiError(429, retry_after="20"), 20),
            (joiner.ApiError(400, retry_after="8"), 8),
            (joiner.ApiError(503), 5),
            (joiner.ConnectionProblem("No response from Riot's servers."), 5),
        )
        for failure, minimum in cases:
            with self.subTest(failure=failure):
                timing = joiner.RetryDelay(5)

                def change(stop):
                    if len(stop.waits) == 2:
                        timing.set(0.1)

                stop = VirtualStop(change)
                client = ScriptedClient(
                    [joiner.Party(BASE, "", True), joiner.Party(BASE, "", True), joiner.Party(TARGET, "", True)],
                    [failure, TARGET],
                )
                join_times = []
                original_join = client.join

                def record_join(code):
                    join_times.append(stop.now)
                    return original_join(code)

                client.join = record_join
                worker = joiner.JoinWorker(client, stop, Mock())
                with patch.object(joiner.time, "monotonic", side_effect=lambda: stop.now):
                    result = worker.run(CODE, timing)
                self.assertEqual(result.status, "joined")
                self.assertEqual(len(join_times), 2)
                self.assertAlmostEqual(join_times[1], minimum)


class RequestTimingTests(unittest.TestCase):
    def run_timed(self, latencies, failures, interval=0.1):
        stop = VirtualStop()
        client = ScriptedClient(
            [joiner.Party(BASE, "", True)] * len(latencies) + [joiner.Party(TARGET, "", True)],
            [*failures, TARGET],
        )
        starts = []
        durations = iter(latencies)
        original_join = client.join

        def timed_join(code):
            starts.append(stop.now)
            stop.now += next(durations)
            return original_join(code)

        client.join = timed_join
        worker = joiner.JoinWorker(client, stop, Mock())
        with patch.object(joiner.time, "monotonic", side_effect=lambda: stop.now):
            result = worker.run(CODE, interval)
        self.assertEqual(result.status, "joined")
        return starts, stop

    def test_request_time_counts_toward_start_to_start_interval(self):
        starts, _ = self.run_timed([0.04, 0.04, 0.04], [joiner.ApiError(403, "PARTY_FULL")] * 2)
        self.assertEqual(len(starts), 3)
        self.assertAlmostEqual(starts[1] - starts[0], 0.1)
        self.assertAlmostEqual(starts[2] - starts[1], 0.1)

    def test_slow_requests_do_not_add_extra_delay_or_catch_up_bursts(self):
        starts, _ = self.run_timed([0.25, 0.04, 0.04], [joiner.ApiError(403, "PARTY_FULL")] * 2)
        self.assertAlmostEqual(starts[1] - starts[0], 0.25)
        self.assertAlmostEqual(starts[2] - starts[1], 0.1)

    def test_retry_after_starts_when_response_arrives(self):
        starts, _ = self.run_timed([0.4, 0.04], [joiner.ApiError(403, "PARTY_FULL", retry_after="2")])
        self.assertAlmostEqual(starts[1] - starts[0], 2.4)

    def test_ambiguous_server_response_checks_membership_before_posting_again(self):
        stop = VirtualStop()
        client = ScriptedClient(
            [joiner.Party(BASE, "", True), joiner.Party(TARGET, CODE, True)],
            [joiner.ApiError(503)],
        )
        worker = joiner.JoinWorker(client, stop, Mock())
        with patch.object(joiner.time, "monotonic", side_effect=lambda: stop.now):
            result = worker.run(CODE, 0.1)
        self.assertEqual(result.status, "joined")
        self.assertEqual(len(client.join_calls), 1)


class ParsingTests(unittest.TestCase):
    def test_retry_after_numeric_date_and_invalid_values(self):
        now = datetime(2026, 11, 26, 1, 0, tzinfo=timezone.utc)
        date = format_datetime(now + timedelta(seconds=90), usegmt=True)
        self.assertEqual(joiner.retry_delay("65", 15, now), 65)
        self.assertEqual(joiner.retry_delay(date, 15, now), 90)
        for value in (None, "bad", "nan", "inf", "-1", "0", "2"):
            with self.subTest(value=value):
                self.assertEqual(joiner.retry_delay(value, 15, now), 15)

    def test_code_validation_preserves_case_and_rejects_paths(self):
        self.assertEqual(joiner.validate_code("  AbCd12  "), "AbCd12")
        for code in ("", "abc/def", "https://example.com/abc", "a b", "x\r\nHeader:x", "x" * 129):
            with self.subTest(code=code):
                with self.assertRaises(ValueError):
                    joiner.validate_code(code)

    def test_region_uses_running_session_before_stale_log(self):
        session = {"version": VERSION, "launchConfiguration": {"arguments": ["-ares-deployment=br"]}}
        self.assertEqual(joiner.session_details(session, "https://glz-eu-1.eu.a.pvp.net"), (("br", "na"), VERSION))
        session["patchlineId"] = "pbe"
        self.assertEqual(joiner.session_details(session, ""), (("na", "pbe"), VERSION))

    def test_session_version_falls_back_to_log_without_using_launcher_version(self):
        sessions = {
            "launcher": {"productId": "riot_client", "version": "999.1"},
            "game": {"productId": "valorant", "version": "unknown"},
        }
        key, session = joiner.select_session(sessions)
        self.assertEqual(key, "game")
        self.assertEqual(joiner.session_details(session, VERSION + " https://glz-eu-1.eu.a.pvp.net"), (("eu", "eu"), VERSION))

    def test_errors_and_repr_do_not_disclose_credentials(self):
        error = joiner.ApiError(400, "https://server.invalid/private-code?token=SECRET")
        self.assertEqual(str(error), "HTTP 400")
        self.assertNotIn("SECRET", repr(joiner.Lockfile(1, 1234, "SECRET")))
        self.assertNotIn(CODE, repr(joiner.Party(BASE, CODE, True)))


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.lock_path = self.root / "Riot Games" / "Riot Client" / "Config" / "lockfile"
        self.lock_path.parent.mkdir(parents=True)
        self.lock_path.write_text("Riot:123:4567:local-secret:https", encoding="utf-8")
        self.session = {
            "game-session": {
                "productId": "valorant",
                "version": VERSION,
                "launchConfiguration": {"arguments": ["-ares-deployment=eu"]},
            },
        }
        self.tokens = {"subject": PLAYER, "accessToken": "access-secret", "token": "entitlement-secret"}
        self.transport = Mock()
        self.client = joiner.RiotClient(root=self.root, transport=self.transport, running=lambda: True)

    def prepare(self):
        self.transport.request.side_effect = [self.session, self.tokens]
        self.client.prepare()

    def prime_full_party_cache(self):
        self.prepare()
        self.transport.request.side_effect = [
            {"Subject": PLAYER, "CurrentPartyID": BASE},
            {"ID": BASE, "Members": [{"Subject": PLAYER}], "InviteCode": ""},
            joiner.ApiError(403, "PARTY_FULL"),
        ]
        self.client.snapshot()
        with self.assertRaises(joiner.ApiError):
            self.client.join(CODE)

    def test_full_party_rejections_reuse_recent_client_and_party_checks(self):
        now = 100.0
        with patch.object(joiner.time, "monotonic", side_effect=lambda: now):
            self.prime_full_party_cache()
            calls = self.transport.request.call_count
            now += 0.1
            self.client.prepare()
            party = self.client.snapshot()
            self.assertEqual(party.id, BASE)
            self.assertEqual(self.transport.request.call_count, calls)
            self.transport.request.side_effect = [joiner.ApiError(403, "PARTY_FULL")]
            with self.assertRaises(joiner.ApiError):
                self.client.join(CODE)
            self.assertEqual(self.transport.request.call_count, calls + 1)

    def test_ambiguous_post_invalidates_cached_checks(self):
        for failure in (joiner.ConnectionProblem("No response."), joiner.ApiError(503), joiner.ApiError(408), joiner.ApiError(401)):
            with self.subTest(failure=failure):
                with patch.object(joiner.time, "monotonic", return_value=100):
                    self.prime_full_party_cache()
                    self.transport.request.side_effect = [failure]
                    with self.assertRaises(type(failure)):
                        self.client.join(CODE)
                    calls = self.transport.request.call_count
                    self.transport.request.side_effect = [
                        self.session,
                        self.tokens,
                        {"Subject": PLAYER, "CurrentPartyID": TARGET},
                        {"ID": TARGET, "Members": [{"Subject": PLAYER}], "InviteCode": CODE},
                    ]
                    self.client.prepare()
                    party = self.client.snapshot()
                    self.assertEqual(party.id, TARGET)
                    self.assertEqual(self.transport.request.call_count, calls + 4)

    def test_cached_full_party_state_rechecks_account_after_two_seconds(self):
        now = 100.0
        with patch.object(joiner.time, "monotonic", side_effect=lambda: now):
            self.prime_full_party_cache()
            now += 2.1
            self.transport.request.side_effect = [self.session, dict(self.tokens, subject=OTHER_PLAYER)]
            with self.assertRaisesRegex(joiner.StopReason, "account changed"):
                self.client.prepare()

    def test_cached_full_party_state_still_detects_changed_lockfile_immediately(self):
        with patch.object(joiner.time, "monotonic", return_value=100):
            self.prime_full_party_cache()
            calls = self.transport.request.call_count
            self.lock_path.write_text("Riot:987:4567:different-secret:https", encoding="utf-8")
            with self.assertRaisesRegex(joiner.StopReason, "restarted"):
                self.client.prepare()
            self.assertEqual(self.transport.request.call_count, calls)

    def test_clear_closes_transport_and_discards_tokens(self):
        self.prepare()
        self.client.clear()
        self.transport.close.assert_called_once()
        self.assertEqual(self.client.headers, {})
        self.assertIsNone(self.client.lock)

    def test_account_change_is_detected_on_next_prepare(self):
        self.prepare()
        changed = dict(self.tokens, subject=OTHER_PLAYER)
        self.transport.request.side_effect = [self.session, changed]
        with self.assertRaisesRegex(joiner.StopReason, "account changed"):
            self.client.prepare()

    def test_changed_lockfile_stops_before_using_new_credentials(self):
        self.prepare()
        prior_calls = self.transport.request.call_count
        self.lock_path.write_text("Riot:987:4567:different-secret:https", encoding="utf-8")
        with self.assertRaisesRegex(joiner.StopReason, "restarted"):
            self.client.prepare()
        self.assertEqual(self.transport.request.call_count, prior_calls)

    def test_changed_game_session_stops(self):
        self.prepare()
        self.transport.request.side_effect = [{"new-session": self.session["game-session"]}]
        with self.assertRaisesRegex(joiner.StopReason, "session changed"):
            self.client.prepare()

    def test_snapshot_rejects_wrong_account_and_missing_membership(self):
        self.prepare()
        self.transport.request.side_effect = [{"Subject": OTHER_PLAYER, "CurrentPartyID": BASE}]
        with self.assertRaisesRegex(joiner.StopReason, "signed-in account"):
            self.client.snapshot()
        self.transport.request.side_effect = [
            {"Subject": PLAYER, "CurrentPartyID": TARGET},
            {"ID": TARGET, "Members": [{"Subject": OTHER_PLAYER}], "InviteCode": CODE},
        ]
        with self.assertRaises(joiner.ConnectionProblem):
            self.client.snapshot()

    def test_join_uses_expected_endpoint_and_redacts_mismatched_response(self):
        self.prepare()
        self.transport.request.side_effect = [{"Subject": PLAYER, "CurrentPartyID": TARGET}]
        self.assertEqual(self.client.join(CODE), TARGET)
        call = self.transport.request.call_args
        self.assertEqual(call.args[:3], ("glz-eu-1.eu.a.pvp.net", 443, "/parties/v1/players/joinbycode/" + CODE))
        self.assertEqual(call.args[4], "POST")
        self.transport.request.side_effect = [{"Subject": OTHER_PLAYER, "CurrentPartyID": TARGET}]
        self.assertIsNone(self.client.join(CODE))


class TransportTests(unittest.TestCase):
    def test_remote_tls_is_verified_and_unverified_tls_is_loopback_only(self):
        transport = joiner.Transport()
        self.assertTrue(transport.remote_context.check_hostname)
        self.assertEqual(transport.remote_context.verify_mode, ssl.CERT_REQUIRED)
        self.assertEqual(transport.local_context.verify_mode, ssl.CERT_NONE)
        with patch.object(joiner.http.client, "HTTPSConnection") as connection:
            for host, port, local in [
                ("example.com", 443, False),
                ("glz-eu-1.eu.a.pvp.net.evil.example", 443, False),
                ("glz-eu-1.na.a.pvp.net", 443, False),
                ("glz-eu-1.eu.a.pvp.net", 80, False),
                ("192.168.1.1", 1234, True),
            ]:
                with self.subTest(host=host, port=port, local=local):
                    with self.assertRaises(joiner.StopReason):
                        transport.request(host, port, "/", {}, local=local)
            connection.assert_not_called()

    def test_transport_does_not_expose_sensitive_error_payload(self):
        response = Mock(status=400, will_close=False)
        response.getheader.return_value = None
        response.read.return_value = json.dumps({"errorCode": "bad/token=SECRET", "message": "Bearer SECRET"}).encode()
        connection = Mock()
        connection.getresponse.return_value = response
        transport = joiner.Transport()
        with patch.object(joiner.http.client, "HTTPSConnection", return_value=connection):
            with self.assertRaises(joiner.ApiError) as caught:
                transport.request("glz-eu-1.eu.a.pvp.net", 443, "/joinbycode/PRIVATE-CODE", {})
        self.assertEqual(str(caught.exception), "HTTP 400")
        connection.close.assert_not_called()
        transport.close()
        connection.close.assert_called_once()

    def test_connection_is_reused_and_closed_once_on_cleanup(self):
        response = Mock(status=200, will_close=False)
        response.getheader.return_value = None
        response.read.return_value = b"{}"
        connection = Mock()
        connection.getresponse.return_value = response
        transport = joiner.Transport()
        with patch.object(joiner.http.client, "HTTPSConnection", return_value=connection) as factory:
            transport.request("glz-eu-1.eu.a.pvp.net", 443, "/first", {})
            transport.request("glz-eu-1.eu.a.pvp.net", 443, "/second", {})
        factory.assert_called_once()
        self.assertEqual(connection.request.call_count, 2)
        connection.close.assert_not_called()
        transport.close()
        transport.close()
        connection.close.assert_called_once()

    def test_connection_error_closes_connection_without_repeating_post(self):
        connection = Mock()
        connection.getresponse.side_effect = joiner.http.client.RemoteDisconnected("lost response")
        transport = joiner.Transport()
        with patch.object(joiner.http.client, "HTTPSConnection", return_value=connection) as factory:
            with self.assertRaises(joiner.ConnectionProblem):
                transport.request("glz-eu-1.eu.a.pvp.net", 443, "/joinbycode/PRIVATE-CODE", {}, method="POST")
        factory.assert_called_once()
        connection.request.assert_called_once()
        connection.close.assert_called_once()
        self.assertEqual(transport.connections, {})


if __name__ == "__main__":
    unittest.main()
