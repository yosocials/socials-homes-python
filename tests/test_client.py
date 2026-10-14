from __future__ import annotations

import email.message
import email.utils
import io
import pickle
import re
import signal
import socket
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest import mock

from socials_homes import DEFAULT_BASE_URL, VERSION, SocialsHomes, SocialsHomesError
from socials_homes import client as client_module

from .helpers import Call, DeafOpener, FailingOpener, FakeOpener, HangingOpener, LocalApi, fixture, request_threads

FAST = {"retry_base_delay": 0.001}


def rate_limited(seconds: int) -> dict:
    return {
        "status": 429,
        "headers": {"Retry-After": str(seconds)},
        "body": {
            "type": "x",
            "title": "Rate limited",
            "status": 429,
            "detail": "The api rate limit is used up.",
            "code": "rate_limited",
            "resolution": "Wait.",
            "retry_after_seconds": seconds,
        },
    }


def limited(headers: dict, seconds: object = None) -> dict:
    body = {"type": "x", "title": "t", "status": 429, "detail": "d", "code": "rate_limited", "resolution": "r"}
    if seconds is not None:
        body["retry_after_seconds"] = seconds
    return {"status": 429, "headers": headers, "body": body}


INTERNAL_ERROR = {
    "status": 500,
    "body": {
        "type": "x",
        "title": "Internal error",
        "status": 500,
        "detail": "Failed.",
        "code": "internal_error",
        "resolution": "Retry.",
    },
}


def raises(fn, *args, **kwargs) -> BaseException:
    try:
        fn(*args, **kwargs)
    except BaseException as err:
        return err
    raise AssertionError("expected an exception")


class Requests(unittest.TestCase):
    def test_gets_the_production_origin_under_api_v1_asking_for_json_with_a_versioned_user_agent(self) -> None:
        opener = FakeOpener({"/api/v1": {"body": fixture("index")}})
        index = SocialsHomes(opener=opener).get_index()
        self.assertEqual(DEFAULT_BASE_URL, "https://www.socials.homes")
        call = opener.calls[0]
        self.assertEqual(call.href, "https://www.socials.homes/api/v1")
        self.assertEqual(call.request.get_method(), "GET")
        self.assertEqual(call.request.get_header("Accept"), "application/json")
        agent = call.request.get_header("User-agent")
        self.assertRegex(agent, rf"^socials-homes-python/{re.escape(VERSION)} python/\d+\.\d+\.\d+$")
        self.assertIs(index["authentication"]["required"], False)
        self.assertIs(index["read_only"], True)

    def test_never_sends_credentials_or_a_body(self) -> None:
        opener = FakeOpener({"/api/v1/plans": {"body": fixture("plans")}})
        SocialsHomes(opener=opener, user_agent="my-agent/2").list_plans()
        request = opener.calls[0].request
        self.assertIsNone(request.data)
        self.assertEqual(sorted(k for k, _ in request.header_items()), ["Accept", "User-agent"])
        self.assertRegex(request.get_header("User-agent"), r" my-agent/2$")

    def test_the_timeout_reaches_the_opener(self) -> None:
        opener = FakeOpener({"/api/v1/plans": {"body": fixture("plans")}})
        SocialsHomes(opener=opener, timeout=7.5).list_plans()
        self.assertEqual(opener.calls[0].timeout, 7.5)

    def test_a_custom_base_url_loses_its_trailing_slash(self) -> None:
        opener = FakeOpener({"/api/v1/plans": {"body": fixture("plans")}})
        sh = SocialsHomes("http://localhost:3000///", opener=opener)
        self.assertEqual(sh.base_url, "http://localhost:3000")
        sh.list_plans()
        self.assertEqual(opener.calls[0].href, "http://localhost:3000/api/v1/plans")

    def test_bad_options_raise_at_construction(self) -> None:
        self.assertRaises(ValueError, SocialsHomes, timeout=-1)
        self.assertRaises(TypeError, SocialsHomes, max_retries=float("nan"))
        self.assertRaises(TypeError, SocialsHomes, opener=42)
        self.assertRaises(ValueError, SocialsHomes, timeout=float("nan"))
        self.assertRaises(TypeError, SocialsHomes, timeout="15")
        self.assertRaises(TypeError, SocialsHomes, max_retries=True)
        self.assertRaises(ValueError, SocialsHomes, max_retries=-1)
        self.assertRaises(ValueError, SocialsHomes, retry_base_delay=float("inf"))
        self.assertRaises(TypeError, SocialsHomes, user_agent=3)
        self.assertRaises(ValueError, SocialsHomes, user_agent="me\r\nX-Injected: 1")

    def test_the_default_transport_is_urllib(self) -> None:
        sh = SocialsHomes()
        self.assertIsInstance(sh._opener, urllib.request.OpenerDirector)


class Cities(unittest.TestCase):
    def test_list_cities_maps_its_params_to_the_query_and_returns_the_page_as_the_api_sends_it(self) -> None:
        opener = FakeOpener({"/api/v1/cities": {"body": fixture("cities-nl-page1")}})
        page = SocialsHomes(opener=opener).list_cities(country="NL", limit=2)
        self.assertEqual(opener.calls[0].search, "?country=NL&limit=2")
        self.assertEqual(len(page["data"]), 2)
        self.assertEqual(page["data"][0]["links"]["supply"], "https://www.socials.homes/api/v1/cities/almere/supply")
        self.assertIsInstance(page["next_cursor"], str)

    def test_cities_walks_every_page_through_next_cursor_and_stops_at_none(self) -> None:
        p1 = fixture("cities-nl-page1")
        p2 = {**fixture("cities-nl-page2"), "next_cursor": None}
        opener = FakeOpener(
            {"/api/v1/cities": lambda call: {"body": p2 if call.param("cursor") == p1["next_cursor"] else p1}}
        )
        slugs = [c["slug"] for c in SocialsHomes(opener=opener).cities(country="NL", page_size=2)]
        self.assertEqual(slugs, [c["slug"] for c in p1["data"] + p2["data"]])
        self.assertEqual(len(opener.calls), 2)
        self.assertIsNone(opener.calls[0].param("cursor"))
        self.assertEqual(opener.calls[1].param("cursor"), p1["next_cursor"])
        self.assertEqual(opener.calls[0].param("limit"), "2")

    def test_cities_asks_for_the_largest_page_by_default(self) -> None:
        opener = FakeOpener({"/api/v1/cities": {"body": {"data": [], "next_cursor": None}}})
        self.assertEqual(list(SocialsHomes(opener=opener).cities()), [])
        self.assertEqual(opener.calls[0].param("limit"), "200")

    def test_cities_is_lazy_but_checks_its_arguments_at_once(self) -> None:
        opener = FakeOpener({"/api/v1/cities": {"body": {"data": [], "next_cursor": None}}})
        sh = SocialsHomes(opener=opener)
        sh.cities()
        self.assertEqual(len(opener.calls), 0, "no request until iterated")
        self.assertRaises(TypeError, sh.cities, page_size="2")
        self.assertRaises(TypeError, sh.cities, country=7)

    def test_get_city_unwraps_data_and_encodes_the_slug(self) -> None:
        opener = FakeOpener({"/api/v1/cities/amsterdam": {"body": fixture("city-amsterdam")}})
        city = SocialsHomes(opener=opener).get_city(" amsterdam ")
        self.assertEqual(city["slug"], "amsterdam")
        self.assertEqual(city["country"], "NL")
        odd = FakeOpener({})
        self.assertRaises(SocialsHomesError, SocialsHomes(opener=odd).get_city, "a/b?c")
        self.assertEqual(odd.calls[0].request.selector, "/api/v1/cities/a%2Fb%3Fc")

    def test_the_slug_is_encoded_as_encode_uri_component_does(self) -> None:
        odd = FakeOpener({})
        self.assertRaises(SocialsHomesError, SocialsHomes(opener=odd).get_city, "it's (ok)!*~ é")
        self.assertEqual(odd.calls[0].request.selector, "/api/v1/cities/it's%20(ok)!*~%20%C3%A9")

    def test_an_empty_slug_raises_before_any_request(self) -> None:
        opener = FakeOpener({})
        self.assertRaises(ValueError, SocialsHomes(opener=opener).get_city, "")
        self.assertRaises(ValueError, SocialsHomes(opener=opener).get_city_supply, "  ")
        self.assertRaises(TypeError, SocialsHomes(opener=opener).get_city, None)
        self.assertEqual(len(opener.calls), 0)

    def test_list_cities_type_checks_its_params(self) -> None:
        opener = FakeOpener({})
        sh = SocialsHomes(opener=opener)
        self.assertRaises(TypeError, sh.list_cities, limit="2")
        self.assertRaises(TypeError, sh.list_cities, limit=True)
        self.assertRaises(TypeError, sh.list_cities, limit=2.0)
        self.assertRaises(TypeError, sh.list_cities, cursor=1)
        self.assertRaises(TypeError, sh.get_city_supply, "amsterdam", budget_max_eur="900")
        self.assertEqual(len(opener.calls), 0)


class SupplyAndPlans(unittest.TestCase):
    def test_get_city_supply_sends_budget_max_eur_and_returns_data(self) -> None:
        opener = FakeOpener({"/api/v1/cities/amsterdam/supply": {"body": fixture("supply-amsterdam-900")}})
        s = SocialsHomes(opener=opener).get_city_supply("amsterdam", budget_max_eur=900)
        self.assertEqual(opener.calls[0].search, "?budget_max_eur=900")
        self.assertEqual(s["city"]["slug"], "amsterdam")
        supply = s["supply"]
        assert supply["available"]
        self.assertEqual(supply["budget_max_eur"], 900)
        self.assertIsInstance(supply["listings_within_budget"], int)

    def test_without_a_budget_no_query_is_sent(self) -> None:
        opener = FakeOpener({"/api/v1/cities/amsterdam/supply": {"body": fixture("supply-amsterdam-900")}})
        SocialsHomes(opener=opener).get_city_supply("amsterdam")
        self.assertEqual(opener.calls[0].search, "")

    def test_unavailable_counts_come_through_as_available_false_never_as_zero(self) -> None:
        body = fixture("supply-amsterdam-900")
        body["data"]["supply"] = {"available": False, "reason": "Live counts are temporarily unavailable."}
        opener = FakeOpener({"/api/v1/cities/amsterdam/supply": {"body": body}})
        s = SocialsHomes(opener=opener).get_city_supply("amsterdam")
        self.assertEqual(s["supply"], {"available": False, "reason": "Live counts are temporarily unavailable."})

    def test_list_plans_returns_the_whole_answer_plans_notes_and_the_vip_guarantee(self) -> None:
        opener = FakeOpener({"/api/v1/plans": {"body": fixture("plans")}})
        r = SocialsHomes(opener=opener).list_plans()
        self.assertEqual(r["currency"], "EUR")
        self.assertGreaterEqual(len(r["data"]), 1)
        for p in r["data"]:
            self.assertIsInstance(p["price_eur"], float)
        self.assertGreater(len(r["vip_guarantee"]["conditions"]), 0)

    def test_an_answer_without_data_is_invalid_response(self) -> None:
        opener = FakeOpener({"/api/v1/cities/amsterdam": {"body": {"city": {}}}})
        err = raises(SocialsHomes(opener=opener).get_city, "amsterdam")
        self.assertIsInstance(err, SocialsHomesError)
        self.assertEqual(err.code, "invalid_response")
        self.assertEqual(len(opener.calls), 1)


class Errors(unittest.TestCase):
    def test_a_404_problem_is_a_socials_homes_error_with_the_apis_code_detail_suggestions_and_resolution(self) -> None:
        problem = fixture("problem-city-not-found")
        opener = FakeOpener({"/api/v1/cities/den_haag": {"status": 404, "body": problem}})
        err = raises(SocialsHomes(opener=opener).get_city, "den_haag")
        self.assertIsInstance(err, SocialsHomesError)
        self.assertIsInstance(err, Exception)
        self.assertEqual(err.status, 404)
        self.assertEqual(err.code, "city_not_found")
        self.assertEqual(err.message, problem["detail"])
        self.assertEqual(str(err), problem["detail"])
        self.assertEqual(err.resolution, problem["resolution"])
        self.assertEqual(err.problem["suggestions"][0]["slug"], "the_hague")
        self.assertEqual(err.url, "https://www.socials.homes/api/v1/cities/den_haag")
        self.assertIsNone(err.retry_after_seconds)
        self.assertEqual(len(opener.calls), 1, "a 404 is not retried")

    def test_an_error_status_returned_instead_of_raised_reads_the_same(self) -> None:
        problem = fixture("problem-city-not-found")
        opener = FakeOpener({"/api/v1/cities/den_haag": {"status": 404, "body": problem}}, raise_errors=False)
        err = raises(SocialsHomes(opener=opener).get_city, "den_haag")
        self.assertEqual((err.status, err.code, err.message), (404, "city_not_found", problem["detail"]))

    def test_a_400_names_each_bad_parameter_and_is_not_retried(self) -> None:
        opener = FakeOpener({"/api/v1/cities": {"status": 400, "body": fixture("problem-invalid-limit")}})
        err = raises(SocialsHomes(opener=opener).list_cities, limit=0)
        self.assertEqual(err.code, "invalid_parameter")
        self.assertEqual(err.problem["errors"][0]["parameter"], "limit")
        self.assertEqual(len(opener.calls), 1)
        self.assertEqual(opener.calls[0].search, "?limit=0", "0 is sent, not dropped")

    def test_a_5xx_without_a_problem_body_is_invalid_response_retried_then_raised(self) -> None:
        opener = FakeOpener({"/api/v1/plans": {"status": 502, "raw": "<html>Bad gateway</html>"}})
        err = raises(SocialsHomes(opener=opener, **FAST).list_plans)
        self.assertEqual(err.code, "invalid_response")
        self.assertEqual(err.status, 502)
        self.assertIsNone(err.problem)
        self.assertIsNone(err.resolution)
        self.assertEqual(len(opener.calls), 3, "1 try + 2 retries")

    def test_a_200_that_is_not_json_is_invalid_response_and_not_retried(self) -> None:
        opener = FakeOpener({"/api/v1/plans": {"status": 200, "raw": "<html>captive portal</html>"}})
        err = raises(SocialsHomes(opener=opener, **FAST).list_plans)
        self.assertEqual(err.code, "invalid_response")
        self.assertEqual(err.status, 200)
        self.assertEqual(len(opener.calls), 1)

    def test_a_200_with_json_that_is_not_an_object_is_invalid_response(self) -> None:
        for raw in ("[1, 2]", "null", '"text"', "42"):
            opener = FakeOpener({"/api/v1/plans": {"raw": raw}})
            err = raises(SocialsHomes(opener=opener, **FAST).list_plans)
            self.assertEqual(err.code, "invalid_response", raw)
            self.assertEqual(len(opener.calls), 1, raw)

    def test_a_byte_order_mark_is_dropped_as_fetch_does(self) -> None:
        opener = FakeOpener({"/api/v1/plans": {"raw": "\ufeff" + '{"currency": "EUR", "data": []}'}})
        self.assertEqual(SocialsHomes(opener=opener).list_plans()["currency"], "EUR")

    def test_the_error_survives_pickling(self) -> None:
        problem = fixture("problem-city-not-found")
        err = SocialsHomesError("d", status=404, code="city_not_found", url="u", problem=problem, retry_after_seconds=3)
        copy = pickle.loads(pickle.dumps(err))
        self.assertIsInstance(copy, SocialsHomesError)
        self.assertEqual(
            (copy.message, copy.status, copy.code, copy.url, copy.problem, copy.retry_after_seconds),
            ("d", 404, "city_not_found", "u", problem, 3),
        )
        self.assertEqual(copy.resolution, problem["resolution"])


class Retries(unittest.TestCase):
    def test_a_429_waits_retry_after_then_succeeds(self) -> None:
        opener = FakeOpener({"/api/v1/plans": [rate_limited(0), {"body": fixture("plans")}]})
        r = SocialsHomes(opener=opener).list_plans()
        self.assertEqual(r["currency"], "EUR")
        self.assertEqual(len(opener.calls), 2)

    def test_the_wait_is_retry_after_exactly(self) -> None:
        opener = FakeOpener({"/api/v1/plans": [rate_limited(3), {"body": fixture("plans")}]})
        with mock.patch.object(client_module, "_sleep") as sleep:
            SocialsHomes(opener=opener).list_plans()
        sleep.assert_called_once_with(3.0)

    def test_a_retry_after_longer_than_max_retry_delay_is_raised_at_once_with_retry_after_seconds(self) -> None:
        opener = FakeOpener({"/api/v1/plans": [rate_limited(120), {"body": fixture("plans")}]})
        started = time.monotonic()
        err = raises(SocialsHomes(opener=opener).list_plans)
        self.assertLess(time.monotonic() - started, 1, "did not wait two minutes")
        self.assertEqual(err.code, "rate_limited")
        self.assertEqual(err.retry_after_seconds, 120)
        self.assertEqual(len(opener.calls), 1)

    def test_a_500_is_retried_with_backoff_and_then_succeeds(self) -> None:
        opener = FakeOpener({"/api/v1/plans": [INTERNAL_ERROR, INTERNAL_ERROR, {"body": fixture("plans")}]})
        r = SocialsHomes(opener=opener, **FAST).list_plans()
        self.assertEqual(r["currency"], "EUR")
        self.assertEqual(len(opener.calls), 3)

    def test_the_backoff_doubles_from_retry_base_delay_with_a_little_jitter_and_is_capped(self) -> None:
        opener = FakeOpener({"/api/v1/plans": INTERNAL_ERROR})
        with mock.patch.object(client_module, "_sleep") as sleep:
            raises(SocialsHomes(opener=opener, max_retries=4, retry_base_delay=1, max_retry_delay=5).list_plans)
        delays = [c.args[0] for c in sleep.call_args_list]
        self.assertEqual(len(delays), 4)
        for delay, base in zip(delays[:3], (1, 2, 4)):
            self.assertGreaterEqual(delay, base)
            self.assertLessEqual(delay, min(base * 1.25, 5))
        self.assertEqual(delays[3], 5, "8 is capped at max_retry_delay")

    def test_max_retries_0_raises_on_the_first_failure(self) -> None:
        opener = FakeOpener({"/api/v1/plans": [rate_limited(0), {"body": fixture("plans")}]})
        err = raises(SocialsHomes(opener=opener, max_retries=0).list_plans)
        self.assertEqual(err.code, "rate_limited")
        self.assertEqual(len(opener.calls), 1)

    def test_a_network_failure_is_connection_error_retried_and_keeps_the_cause(self) -> None:
        opener = FailingOpener(urllib.error.URLError(ConnectionRefusedError(61, "Connection refused")))
        err = raises(SocialsHomes(opener=opener, **FAST).list_plans)
        self.assertEqual(err.code, "connection_error")
        self.assertEqual(err.status, 0)
        self.assertEqual(err.message, "Could not reach https://www.socials.homes/api/v1/plans.")
        self.assertIsInstance(err.__cause__, urllib.error.URLError)
        self.assertIsInstance(err.__cause__.reason, ConnectionRefusedError)
        self.assertEqual(opener.calls, 3)

    def test_a_slow_answer_is_a_timeout(self) -> None:
        opener = HangingOpener()
        err = raises(SocialsHomes(opener=opener, timeout=0.02, max_retries=1, **FAST).list_plans)
        self.assertEqual(err.code, "timeout")
        self.assertEqual(err.status, 0)
        self.assertRegex(err.message, r"within 0\.02 seconds")
        self.assertEqual(opener.calls, 2)

    def test_a_socket_timeout_inside_urllib_is_a_timeout_too(self) -> None:
        for raised in (socket.timeout("timed out"), urllib.error.URLError(socket.timeout("timed out"))):
            opener = FailingOpener(raised)
            err = raises(SocialsHomes(opener=opener, max_retries=0).list_plans)
            self.assertEqual(err.code, "timeout", repr(raised))
            self.assertIs(err.__cause__, raised)


class Interrupting(unittest.TestCase):
    """Python's cancel is Ctrl-C: KeyboardInterrupt goes straight through, never retried or wrapped."""

    def test_an_interrupt_in_the_transport_is_not_retried(self) -> None:
        opener = FailingOpener(KeyboardInterrupt())
        self.assertRaises(KeyboardInterrupt, SocialsHomes(opener=opener, **FAST).list_plans)
        self.assertEqual(opener.calls, 1)

    def test_an_interrupt_during_a_retry_wait_stops_the_wait(self) -> None:
        opener = FakeOpener({"/api/v1/plans": [limited({"Retry-After": "20"})]})
        with mock.patch.object(client_module, "_sleep", side_effect=KeyboardInterrupt):
            self.assertRaises(KeyboardInterrupt, SocialsHomes(opener=opener).list_plans)
        self.assertEqual(len(opener.calls), 1)


class Hardening(unittest.TestCase):
    def test_dot_slugs_are_refused_they_would_reach_another_endpoint(self) -> None:
        opener = FakeOpener({})
        for slug in (".", "..", " .. "):
            self.assertRaises(ValueError, SocialsHomes(opener=opener).get_city, slug)
            self.assertRaises(ValueError, SocialsHomes(opener=opener).get_city_supply, slug)
        self.assertEqual(len(opener.calls), 0)

    def test_base_url_must_be_a_plain_http_or_https_origin_checked_when_the_client_is_built(self) -> None:
        for bad in (
            "",
            "not a url",
            "file:///etc",
            "javascript:alert(1)",
            "ftp://x.y",
            "https://x.y/?a=1",
            "https://x.y/#f",
            "https://u:p@x.y",
            "https://u@x.y",
            "https://",
            "https://x.y:port",
            "https://exa mple.com",
            "localhost:3000",
        ):
            with self.subTest(bad=bad):
                self.assertRaises(ValueError, SocialsHomes, bad)
        self.assertRaises(TypeError, SocialsHomes, 42)
        self.assertEqual(SocialsHomes("https://proxy.example/socials/").base_url, "https://proxy.example/socials")
        self.assertEqual(SocialsHomes("HTTPS://WWW.SOCIALS.HOMES").base_url, "https://www.socials.homes")
        self.assertEqual(SocialsHomes(" https://x.y:443/ ").base_url, "https://x.y")
        self.assertEqual(SocialsHomes("http://x.y:80").base_url, "http://x.y")
        self.assertEqual(SocialsHomes("https://x.y:8443/").base_url, "https://x.y:8443")
        self.assertEqual(SocialsHomes("http://[::1]:3000/").base_url, "http://[::1]:3000")
        self.assertEqual(SocialsHomes("https://xn--bcher-kva.example").base_url, "https://xn--bcher-kva.example")
        self.assertEqual(SocialsHomes(None).base_url, DEFAULT_BASE_URL)

    def test_base_url_is_normalised_as_a_url_parser_does(self) -> None:
        for given, expected in (
            ("https://x.y/a/..", "https://x.y"),
            ("https://x.y/.", "https://x.y"),
            ("https://x.y/a/%2e%2E/b/./c/", "https://x.y/b/c"),
            ("https://x.y/a/b/../../..", "https://x.y"),
            ("http://2130706433", "http://127.0.0.1"),
            ("http://0x7f.1:8080/p", "http://127.0.0.1:8080/p"),
            ("http://1.2.3.4.", "http://1.2.3.4"),
            ("http://[0:0:0:0:0:0:0:1]", "http://[::1]"),
        ):
            with self.subTest(given=given):
                self.assertEqual(SocialsHomes(given).base_url, expected)
        for given, expected in (
            ("http://[::ffff:1.2.3.4]", "http://[::ffff:102:304]"),
            ("http://[2001:db8:0:0:1:0:0:1]", "http://[2001:db8::1:0:0:1]"),
            ("http://[1:0:0:2:0:0:0:3]", "http://[1:0:0:2::3]"),
            ("http://x.y:", "http://x.y"),
            ("http://x.y:0080", "http://x.y"),
        ):
            with self.subTest(given=given):
                self.assertEqual(SocialsHomes(given).base_url, expected)
        for bad in (
            "http://999.1.1.1",
            "http://1.2.3.4.5",
            "http://[::g]",
            "http://08",
            "http://4294967297",
            "http://0x1ffffffff",
            "http://8589934593:8080",
            "http://[fe80::1%25en0]",
            "http://evil.com[::1]",
            "http://x.y:+80",
            "http://x.y:8_0",
            "http://x.y:65536",
        ):
            with self.subTest(bad=bad):
                self.assertRaises(ValueError, SocialsHomes, bad)

    def test_an_international_host_must_come_in_its_xn_form(self) -> None:
        # Python's idna codec would send straße.de to strasse.de, a different host than a browser picks.
        for host in ("https://straße.de", "https://bücher.example"):
            err = raises(SocialsHomes, host)
            self.assertIsInstance(err, ValueError)
            self.assertIn("xn--", str(err))

    def test_timeout_must_be_above_0_and_fit_a_timer(self) -> None:
        self.assertRaises(ValueError, SocialsHomes, timeout=0)
        self.assertRaises(ValueError, SocialsHomes, timeout=2**31 / 1000)
        self.assertRaises(ValueError, SocialsHomes, max_retry_delay=2**31 / 1000)
        SocialsHomes(timeout=(2**31 - 1) / 1000)

    def test_retry_after_as_an_http_date_in_the_past_or_a_negative_body_value_retries_at_once(self) -> None:
        past = email.utils.formatdate(time.time() - 60, usegmt=True)
        for step in (limited({"Retry-After": past}), limited({}, -5)):
            opener = FakeOpener({"/api/v1/plans": [step, {"body": fixture("plans")}]})
            with mock.patch.object(client_module, "_sleep") as sleep:
                SocialsHomes(opener=opener).list_plans()
            sleep.assert_called_once_with(0.0)
            self.assertEqual(len(opener.calls), 2)

    def test_retry_after_as_an_http_date_in_the_future_is_waited_out_or_raised_when_too_long(self) -> None:
        future = email.utils.formatdate(time.time() + 3600, usegmt=True)
        opener = FakeOpener({"/api/v1/plans": [limited({"Retry-After": future})]})
        err = raises(SocialsHomes(opener=opener).list_plans)
        self.assertEqual(err.code, "rate_limited")
        self.assertTrue(3500 < err.retry_after_seconds <= 3600, err.retry_after_seconds)
        self.assertEqual(len(opener.calls), 1)
        soon = email.utils.formatdate(time.time() + 10, usegmt=True)
        opener = FakeOpener({"/api/v1/plans": [limited({"Retry-After": soon}), {"body": fixture("plans")}]})
        with mock.patch.object(client_module, "_sleep") as sleep:
            SocialsHomes(opener=opener).list_plans()
        self.assertTrue(9 <= sleep.call_args.args[0] <= 10, sleep.call_args)

    def test_an_unreadable_retry_after_falls_back_to_the_body_then_to_one_second(self) -> None:
        for step, wait in ((limited({"Retry-After": "soon"}, 4), 4.0), (limited({"Retry-After": "soon"}), 1.0)):
            opener = FakeOpener({"/api/v1/plans": [step, {"body": fixture("plans")}]})
            with mock.patch.object(client_module, "_sleep") as sleep:
                SocialsHomes(opener=opener).list_plans()
            sleep.assert_called_once_with(wait)

    def test_an_opener_that_ignores_its_timeout_still_times_out(self) -> None:
        opener = DeafOpener()
        started = time.monotonic()
        err = raises(SocialsHomes(opener=opener, timeout=0.03, max_retries=0).list_plans)
        self.assertEqual(err.code, "timeout")
        self.assertLess(time.monotonic() - started, 1)

    def test_cities_stops_with_invalid_response_when_a_server_repeats_a_cursor(self) -> None:
        opener = FakeOpener({"/api/v1/cities": {"body": {**fixture("cities-nl-page1"), "next_cursor": "c1.same"}}})
        seen = []
        err = raises(lambda: [seen.append(c["slug"]) for c in SocialsHomes(opener=opener).cities()])
        self.assertIsInstance(err, SocialsHomesError)
        self.assertEqual(err.code, "invalid_response")
        self.assertEqual(len(opener.calls), 2)
        self.assertEqual(len(seen), 4, "both pages were yielded before the repeat was seen")

    def test_an_empty_country_is_sent_for_the_api_to_answer_with_its_400(self) -> None:
        opener = FakeOpener({"/api/v1/cities": {"body": {"data": [], "next_cursor": None}}})
        SocialsHomes(opener=opener).list_cities(country="")
        self.assertEqual(opener.calls[0].search, "?country=")

    def test_the_default_opener_follows_redirects_to_http_and_https_only(self) -> None:
        handler = client_module._HttpOnlyRedirects()
        request = urllib.request.Request("https://www.socials.homes/api/v1/plans")
        headers = email.message.Message()
        fp = io.BytesIO(b"")
        for target in ("ftp://x.y/file", "file:///etc/passwd", "data:,hi"):
            self.assertIsNone(handler.redirect_request(request, fp, 302, "Found", headers, target), target)
        followed = handler.redirect_request(request, fp, 302, "Found", headers, "https://socials.homes/api/v1/plans")
        self.assertIsNotNone(followed)
        self.assertEqual(followed.get_method(), "GET")

    def test_nan_and_infinity_are_not_json(self) -> None:
        for raw in ('{"price": NaN}', '{"price": Infinity}', '{"price": -Infinity}', '{"price": 1e999}'):
            opener = FakeOpener({"/api/v1/plans": {"raw": raw}})
            err = raises(SocialsHomes(opener=opener).list_plans)
            self.assertEqual(err.code, "invalid_response", raw)

    def test_a_retry_after_of_thousands_of_digits_is_still_a_rate_limit(self) -> None:
        for digits, seconds in ((5000, sys.maxsize), (19, sys.maxsize), (18, int("9" * 18))):
            opener = FakeOpener({"/api/v1/plans": [limited({"Retry-After": "9" * digits})]})
            err = raises(SocialsHomes(opener=opener).list_plans)
            self.assertIsInstance(err, SocialsHomesError)
            self.assertEqual((err.code, err.retry_after_seconds), ("rate_limited", seconds), digits)
            self.assertEqual(len(opener.calls), 1)

    def test_retry_after_reads_leading_zeros_and_any_size_of_body_value(self) -> None:
        for step, wait in ((limited({"Retry-After": "0005"}), 5.0), (limited({"Retry-After": "0" * 30 + "3"}), 3.0)):
            opener = FakeOpener({"/api/v1/plans": [step, {"body": fixture("plans")}]})
            with mock.patch.object(client_module, "_sleep") as sleep:
                SocialsHomes(opener=opener).list_plans()
            sleep.assert_called_once_with(wait)
        opener = FakeOpener({"/api/v1/plans": [limited({}, 10**400)]})
        err = raises(SocialsHomes(opener=opener).list_plans)
        self.assertIsInstance(err, SocialsHomesError)
        self.assertEqual((err.code, err.retry_after_seconds), ("rate_limited", sys.maxsize))

    def test_an_http_error_without_a_body_or_headers_reads_as_an_empty_answer(self) -> None:
        calls = []

        class Opener:
            # A fresh error each time, as urllib raises one per request.
            def open(self, fullurl: urllib.request.Request, timeout: float = 0) -> None:
                calls.append(fullurl)
                raise urllib.error.HTTPError(fullurl.full_url, 503, "busy", None, None)

        err = raises(SocialsHomes(opener=Opener(), **FAST).list_plans)
        self.assertEqual((err.code, err.status), ("invalid_response", 503))
        self.assertEqual(len(calls), 3, "a 503 is retried")

    def test_the_query_is_written_as_url_search_params_writes_it(self) -> None:
        opener = FakeOpener({"/api/v1/cities": {"body": {"data": [], "next_cursor": None}}})
        SocialsHomes(opener=opener).list_cities(country="a*b~ c&d=é")
        self.assertEqual(opener.calls[0].search, "?country=a*b%7E+c%26d%3D%C3%A9")

    def test_a_cursor_with_a_lone_surrogate_is_sent_as_u_fffd_as_fetch_does(self) -> None:
        page = '{"data": [], "next_cursor": "c1.\\ud800"}'
        last = {"body": {"data": [], "next_cursor": None}}
        opener = FakeOpener({"/api/v1/cities": lambda call: last if call.param("cursor") else {"raw": page}})
        self.assertEqual(list(SocialsHomes(opener=opener).cities()), [])
        self.assertEqual(opener.calls[1].search, "?limit=200&cursor=c1.%EF%BF%BD")

    def test_a_slug_is_trimmed_as_javascripts_trim_does(self) -> None:
        opener = FakeOpener({})
        self.assertRaises(ValueError, SocialsHomes(opener=opener).get_city, "\ufeff")
        self.assertEqual(len(opener.calls), 0)
        self.assertRaises(SocialsHomesError, SocialsHomes(opener=opener).get_city, "\x1f")
        self.assertEqual(opener.calls[0].request.selector, "/api/v1/cities/%1F")

    def test_without_threads_the_call_runs_inline(self) -> None:
        # A subinterpreter refuses daemon threads; the call still works, bounded by the socket timeout.
        opener = FakeOpener({"/api/v1/plans": {"body": fixture("plans")}})
        with mock.patch.object(client_module.threading.Thread, "start", side_effect=RuntimeError("no daemon threads")):
            self.assertEqual(SocialsHomes(opener=opener).list_plans()["currency"], "EUR")

    def test_the_fake_opener_records_what_was_sent(self) -> None:
        # The helpers must read requests the way the assertions above assume.
        call = Call(urllib.request.Request("https://x.y/a?b=1&c="), None)
        self.assertEqual(
            (call.path, call.search, call.param("b"), call.param("c"), call.param("d")),
            ("/a", "?b=1&c=", "1", "", None),
        )


class RealSockets(unittest.TestCase):
    """The default urllib transport against a server on 127.0.0.1."""

    def test_a_308_is_followed_on_every_python(self) -> None:
        with LocalApi() as base:
            self.assertEqual(SocialsHomes(f"{base}/moved").get_index(), {"ok": True})

    def test_a_redirect_to_ftp_is_not_followed(self) -> None:
        with LocalApi() as base:
            err = raises(SocialsHomes(f"{base}/ftp", max_retries=0).get_index)
        self.assertEqual((err.code, err.status), ("invalid_response", 302))

    def test_a_body_sent_a_byte_at_a_time_is_cut_off_at_the_deadline_and_its_thread_ends(self) -> None:
        with LocalApi() as base:
            before = request_threads()
            started = time.monotonic()
            err = raises(SocialsHomes(f"{base}/slow", timeout=0.4, max_retries=1, **FAST).list_plans)
            self.assertLess(time.monotonic() - started, 2)
            self.assertEqual(err.code, "timeout")
            deadline = time.monotonic() + 3
            while request_threads() > before and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertLessEqual(request_threads(), before, "abandoned exchanges stopped at their next read")

    def test_a_body_cut_short_of_its_content_length_is_a_connection_error_and_retried(self) -> None:
        api = LocalApi()
        with api as base:
            err = raises(SocialsHomes(f"{base}/short", max_retries=1, **FAST).get_index)
        self.assertEqual((err.code, err.status), ("connection_error", 0))
        self.assertEqual(type(err.__cause__).__name__, "IncompleteRead")
        self.assertEqual(api.hits, ["/short/api/v1", "/short/api/v1"], "retried once")

    @unittest.skipUnless(hasattr(signal, "pthread_kill"), "a real SIGINT for the main thread needs POSIX")
    def test_ctrl_c_during_the_wait_abandons_the_exchange_too(self) -> None:
        def ctrl_c() -> None:
            signal.pthread_kill(threading.main_thread().ident, signal.SIGINT)

        with LocalApi() as base:
            before = request_threads()
            started = time.monotonic()
            timer = threading.Timer(0.3, ctrl_c)
            timer.start()
            try:
                self.assertRaises(KeyboardInterrupt, SocialsHomes(f"{base}/slow", timeout=20).list_plans)
            finally:
                timer.cancel()
            self.assertLess(time.monotonic() - started, 5, "the wait stopped at the Ctrl-C")
            deadline = time.monotonic() + 3
            while request_threads() > before and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertLessEqual(request_threads(), before)

    def test_a_refused_connection_is_connection_error_with_its_cause(self) -> None:
        with LocalApi() as base:
            port = int(base.rsplit(":", 1)[1])
        err = raises(SocialsHomes(f"http://127.0.0.1:{port}", max_retries=0).get_index)
        self.assertEqual((err.code, err.status), ("connection_error", 0))
        self.assertIsInstance(err.__cause__, urllib.error.URLError)


if __name__ == "__main__":
    unittest.main()
