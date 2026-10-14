from __future__ import annotations

import contextlib
import io
import json
import os
import pathlib
import re
import subprocess
import sys
import unittest
from unittest import mock

from socials_homes import VERSION
from socials_homes.cli import EXIT_FAILED, EXIT_OK, EXIT_USAGE, USAGE, CliIo, entry, main, printable

from .helpers import FakeOpener, cli, fixture

SRC = pathlib.Path(__file__).resolve().parent.parent / "src"
CONTROL = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f]")


class Commands(unittest.TestCase):
    def test_supply_prints_the_counts_the_budget_line_the_note_and_the_signup_link(self) -> None:
        r = cli(["supply", "amsterdam", "--budget", "900"])
        self.assertEqual(r.code, EXIT_OK)
        s = fixture("supply-amsterdam-900")["data"]
        self.assertRegex(r.out, r"^Amsterdam, Netherlands\n")
        self.assertIn(f"Live listings: {s['supply']['live_listings']:,}", r.out)
        self.assertIn(f"Within EUR 900 a month: {s['supply']['listings_within_budget']:,} (ok)", r.out)
        self.assertIn(s["note"], r.out)
        self.assertIn(f"Sign up: {s['signup_url']}", r.out)
        self.assertEqual(r.opener.calls[0].search, "?budget_max_eur=900")

    def test_supply_prints_exactly_what_the_javascript_cli_prints(self) -> None:
        r = cli(["supply", "amsterdam", "--budget", "900"])
        self.assertEqual(
            r.out,
            "Amsterdam, Netherlands\n\nLive listings: 1,187\nWithin EUR 900 a month: 193 (ok)\n\n"
            f"{fixture('supply-amsterdam-900')['data']['note']}\n"
            "Sign up: https://www.socials.homes/signup?city=amsterdam",
        )

    def test_supply_suggests_a_budget_when_the_market_is_thin(self) -> None:
        body = fixture("supply-amsterdam-900")
        body["data"]["supply"].update(listings_within_budget=3, level="critical", suggested_budget_eur=1250)
        r = cli(
            ["supply", "amsterdam", "--budget", "900"], FakeOpener({"/api/v1/cities/amsterdam/supply": {"body": body}})
        )
        self.assertIn(
            "Within EUR 900 a month: 3 (critical)\nA budget of EUR 1,250 would reach about 30 listings.", r.out
        )

    def test_supply_says_plainly_when_counts_are_unavailable_never_zero(self) -> None:
        body = fixture("supply-amsterdam-900")
        body["data"]["supply"] = {"available": False, "reason": "Live counts are temporarily unavailable."}
        r = cli(["supply", "amsterdam"], FakeOpener({"/api/v1/cities/amsterdam/supply": {"body": body}}))
        self.assertEqual(r.code, EXIT_OK)
        self.assertRegex(
            r.out, r"unavailable right now\. That is not zero\.\nLive counts are temporarily unavailable\."
        )
        self.assertNotRegex(r.out, r"Live listings: 0")

    def test_plans_prints_each_plan_with_its_price_and_the_apis_billing_sentence(self) -> None:
        r = cli(["plans"])
        self.assertEqual(r.code, EXIT_OK)
        for p in fixture("plans")["data"]:
            self.assertIn(p["id"], r.out)
            self.assertIn(f"EUR {p['price_eur']:.2f}", r.out)
            self.assertIn(p["billing"], r.out)
        self.assertIn("EUR 39.90", r.out, "two decimals, as toFixed(2)")
        self.assertRegex(r.out, r"^PLAN {10}NAME {21}PRICE {7}BILLING\n")

    def test_cities_lists_every_city_with_a_count_and_country_filters(self) -> None:
        r = cli(["cities", "--country", "NL"])
        self.assertEqual(r.code, EXIT_OK)
        self.assertRegex(r.out, r"^2 cities in Netherlands")
        self.assertRegex(r.out, r"almere\s+Almere\s+NL Netherlands")
        self.assertEqual(r.opener.calls[0].param("country"), "NL")
        none = cli(["cities", "--country", "XX"])
        self.assertEqual(none.out, 'No covered city in "XX".')
        self.assertEqual(none.code, EXIT_OK)

    def test_cities_without_a_country_has_no_in_clause(self) -> None:
        r = cli(["cities"])
        self.assertRegex(r.out, r"^2 cities\n\nSLUG {7}NAME {7}COUNTRY\nalmere {5}Almere {5}NL Netherlands\n")
        self.assertEqual(r.opener.calls[0].search, "?limit=200")

    def test_city_and_info_print_their_essentials(self) -> None:
        c = cli(["city", "amsterdam"])
        self.assertRegex(c.out, r"^Amsterdam, Netherlands \(amsterdam\)")
        self.assertRegex(c.out, r"Sign up\s+https://www\.socials\.homes/signup\?city=amsterdam")
        i = cli(["info"])
        self.assertRegex(i.out, r"^Socials Homes API v1")
        self.assertRegex(i.out, r"GET /api/v1/plans")
        self.assertRegex(i.out, r"Limit api: 300 requests per minute\.")
        self.assertRegex(i.out, r"Limit supply: 600 requests per hour\.")
        self.assertRegex(i.out, r"MCP: Socials Homes, https://www\.socials\.homes/mcp")

    def test_json_prints_the_apis_json(self) -> None:
        r = cli(["supply", "amsterdam", "--json"])
        self.assertEqual(json.loads(r.out), fixture("supply-amsterdam-900")["data"])
        p = cli(["plans", "--json"])
        self.assertEqual(json.loads(p.out), fixture("plans"))
        self.assertIn("€24.95", p.out, "not escaped to \\u20ac")
        self.assertTrue(p.out.startswith('{\n  "data": [\n    {\n      "id": "explorer"'), "indented by 2")
        c = cli(["cities", "--json"])
        self.assertEqual(json.loads(c.out), fixture("cities-nl-page1")["data"])


class Failures(unittest.TestCase):
    def test_an_unknown_city_exits_1_with_the_apis_detail_the_suggestions_and_the_fix(self) -> None:
        r = cli(["city", "den_haag"])
        self.assertEqual(r.code, EXIT_FAILED)
        self.assertEqual(r.out, "")
        self.assertRegex(r.err, r'^socials-homes: "den_haag" is not a city')
        self.assertRegex(r.err, r"Did you mean: the_hague \(The Hague, NL\)\?")
        self.assertRegex(r.err, r"Use a slug from GET")

    def test_usage_mistakes_exit_2_before_any_request(self) -> None:
        for args in (
            ["supply"],
            ["city", "a", "b"],
            ["plans", "extra"],
            ["nope"],
            ["supply", "amsterdam", "--budget", "cheap"],
            ["supply", "amsterdam", "--budget", "-5"],
            ["supply", "amsterdam", "--budget", "9.5"],
            ["plans", "--country", "NL"],
            ["cities", "--budget", "900"],
            ["plans", "--unknown"],
            ["plans", "--jso"],
            ["cities", "--country"],
        ):
            with self.subTest(args=args):
                r = cli(args)
                self.assertEqual(r.code, EXIT_USAGE)
                self.assertRegex(r.err, r"Run socials-homes --help for usage\.")
                self.assertEqual(len(r.opener.calls), 0)

    def test_an_empty_country_a_dot_slug_and_a_bad_base_url_are_usage_mistakes_before_any_request(self) -> None:
        for args in (
            ["cities", "--country", ""],
            ["cities", "--country", "  "],
            ["city", "."],
            ["supply", ".."],
            ["plans", "--base-url", "not a url"],
            ["plans", "--base-url", "file:///etc"],
            ["plans", "--base-url", ""],
        ):
            with self.subTest(args=args):
                r = cli(args)
                self.assertEqual(r.code, EXIT_USAGE)
                self.assertEqual(len(r.opener.calls), 0)
                self.assertNotRegex(r.err, r"Traceback|File \".+\.py\", line \d+")

    def test_a_parameter_the_api_rejects_exits_2_and_the_message_is_printed_once(self) -> None:
        opener = FakeOpener(
            {"/api/v1/cities/amsterdam/supply": {"status": 400, "body": fixture("problem-invalid-limit")}}
        )
        r = cli(["supply", "amsterdam", "--budget", "1"], opener)
        self.assertEqual(r.code, EXIT_USAGE)
        detail = fixture("problem-invalid-limit")["detail"]
        self.assertEqual(r.err.count(detail), 1, "detail printed once")

    def test_each_rejected_parameter_gets_its_own_line_unless_the_detail_says_it(self) -> None:
        problem = fixture("problem-invalid-limit")
        problem["detail"] = "Two parameters are wrong."
        problem["errors"].append({"parameter": "country", "in": "query", "message": "country is unknown."})
        r = cli(["cities"], FakeOpener({"/api/v1/cities": {"status": 400, "body": problem}}))
        self.assertEqual(r.code, EXIT_USAGE)
        self.assertIn(
            "socials-homes: Two parameters are wrong.\n  limit: limit must be a whole number from 1 to 200.\n"
            "  country: country is unknown.\nCorrect the parameters",
            r.err,
        )

    def test_a_rate_limit_says_when_to_try_again(self) -> None:
        body = {
            "type": "x",
            "title": "t",
            "status": 429,
            "detail": "The api rate limit is used up.",
            "code": "rate_limited",
            "resolution": "Wait.",
        }
        r = cli(
            ["plans"], FakeOpener({"/api/v1/plans": {"status": 429, "headers": {"Retry-After": "120"}, "body": body}})
        )
        self.assertEqual(r.code, EXIT_FAILED)
        self.assertEqual(r.err, "socials-homes: The api rate limit is used up.\nTry again in 120 seconds.")

    def test_an_answer_of_the_wrong_shape_exits_1_without_a_traceback(self) -> None:
        r = cli(["plans"], FakeOpener({"/api/v1/plans": {"body": {"data": [{"id": "x"}]}}}))
        self.assertEqual(r.code, EXIT_FAILED)
        self.assertRegex(r.err, r"^socials-homes: The answer could not be printed \(KeyError: .+\)\. Try --json\.$")

    def test_terminal_control_sequences_in_an_answer_never_reach_the_terminal(self) -> None:
        body = fixture("supply-amsterdam-900")
        body["data"]["city"]["name"] = "Amster\x1b[31mdam\x1b]0;pwned\x07"
        body["data"]["note"] = "note\x9b2J"
        r = cli(["supply", "amsterdam"], FakeOpener({"/api/v1/cities/amsterdam/supply": {"body": body}}))
        self.assertEqual(r.code, EXIT_OK)
        self.assertIsNone(CONTROL.search(r.out))
        self.assertRegex(r.out, r"^Amster\[31mdam\]0;pwned, Netherlands\n")
        j = cli(["supply", "amsterdam", "--json"], FakeOpener({"/api/v1/cities/amsterdam/supply": {"body": body}}))
        self.assertNotIn("\x1b", j.out, "JSON escapes it")
        self.assertEqual(json.loads(j.out)["city"]["name"], body["data"]["city"]["name"], "and keeps the data exact")

    def test_control_sequences_in_an_error_never_reach_the_terminal_either(self) -> None:
        problem = fixture("problem-city-not-found")
        problem["detail"] = "bad\x1b[2J city"
        r = cli(["city", "den_haag"], FakeOpener({"/api/v1/cities/den_haag": {"status": 404, "body": problem}}))
        self.assertEqual(r.code, EXIT_FAILED)
        self.assertIsNone(CONTROL.search(r.err))
        self.assertIn("socials-homes: bad[2J city", r.err)

    def test_printable_keeps_newlines_and_tabs(self) -> None:
        self.assertEqual(printable("a\tb\nc\rd\x00e\x7ff\x85g\x9fh"), "a\tb\ncdefgh")

    def test_help_and_version(self) -> None:
        for args in ([], ["help"], ["--help"], ["-h"], ["plans", "--help"], ["nope", "--help"]):
            with self.subTest(args=args):
                r = cli(args)
                self.assertEqual(r.code, EXIT_OK)
                self.assertEqual(r.out, USAGE)
                self.assertEqual(len(r.opener.calls), 0)
        self.assertEqual(cli(["--version"]).out, VERSION)
        self.assertEqual(cli(["-v"]).out, VERSION)
        self.assertEqual(cli(["plans", "-v"]).out, VERSION)

    def test_the_help_text_matches_the_javascript_cli(self) -> None:
        self.assertTrue(
            USAGE.startswith(
                f"socials-homes {VERSION}, the Socials Homes API from the terminal. Read-only, no API key.\n"
            )
        )
        self.assertIn(
            "  socials-homes supply <slug> [--budget <eur>]       live rental listings in a city now\n", USAGE
        )
        self.assertIn("  --base-url <url>   another API origin (default https://www.socials.homes)\n", USAGE)
        self.assertTrue(USAGE.endswith("\n\nDocs: https://www.socials.homes/developers"))

    def test_help_text_has_no_em_or_en_dashes(self) -> None:
        self.assertNotRegex(USAGE, "[\u2013\u2014]")

    def test_the_cli_sends_its_own_user_agent(self) -> None:
        r = cli(["plans"])
        self.assertRegex(
            r.opener.calls[0].request.get_header("User-agent"), rf" socials-homes-cli/{re.escape(VERSION)}$"
        )


UNKNOWN = (
    "Unknown option '{0}'. To specify a positional argument starting with a '-', "
    "place it at the end of the command after '--', as in '-- \"{0}\""
)


class ParsingAsNodeDoes(unittest.TestCase):
    """The same argv gives the same message and exit code as util.parseArgs in the JavaScript CLI."""

    def test_option_mistakes_get_nodes_own_words(self) -> None:
        for args, message in (
            (["plans", "--bogus"], UNKNOWN.format("--bogus")),
            (["plans", "--bogus=1"], UNKNOWN.format("--bogus")),
            (["plans", "-x"], UNKNOWN.format("-x")),
            (["plans", "-hx"], UNKNOWN.format("-x")),
            (["plans", "---x"], UNKNOWN.format("---x")),
            (["city", "-5"], UNKNOWN.format("-5")),
            (["city", "-a b"], UNKNOWN.format("-a")),
            (["cities", "--country"], "Option '--country <value>' argument missing"),
            (["plans", "--base-url"], "Option '--base-url <value>' argument missing"),
            (
                ["cities", "--country", "-1"],
                "Option '--country' argument is ambiguous.\nDid you forget to specify the option argument for "
                "'--country'?\nTo specify an option argument starting with a dash use '--country=-XYZ'.",
            ),
            (["plans", "--json=1"], "Option '--json' does not take an argument"),
            (["plans", "--help=x"], "Option '-h, --help' does not take an argument"),
            (["--", "--help"], 'Unknown command "--help".'),
            (["--=a=b"], UNKNOWN.format("--")),
            (["--=x"], UNKNOWN.format("--=x")),
            (["city", "--"], "city takes one city slug, for example: socials-homes city amsterdam"),
        ):
            with self.subTest(args=args):
                r = cli(args)
                self.assertEqual(r.code, EXIT_USAGE)
                self.assertEqual(r.err, f"socials-homes: {message}\nRun socials-homes --help for usage.")
                self.assertEqual(len(r.opener.calls), 0)

    def test_values_and_groups_parse_as_in_node(self) -> None:
        self.assertEqual(cli(["plans", "-vh"]).out, VERSION)
        self.assertEqual(cli(["-h-"]).out, USAGE, "the - ends the options, as -- does")
        self.assertEqual(cli(["plans", "-h-", "--bogus"]).out, USAGE, "--bogus is a positional after it")
        self.assertEqual(cli(["-v-json"]).out, VERSION)
        self.assertEqual(cli(["-hv"]).out, VERSION)
        r = cli(["cities", "--country=NL"])
        self.assertEqual((r.code, r.opener.calls[0].param("country")), (EXIT_OK, "NL"))
        r = cli(["--json", "cities", "--country", "NL", "--country", "XX"])
        self.assertEqual((r.code, r.out, r.opener.calls[0].param("country")), (EXIT_OK, "[]", "XX"))
        r = cli(["supply", "--budget=900", "amsterdam"])
        self.assertEqual(r.opener.calls[0].search, "?budget_max_eur=900")
        r = cli(["city", "--", "-amsterdam"])
        self.assertEqual(r.opener.calls[0].path, "/api/v1/cities/-amsterdam", "after -- a dash is a positional")

    def test_a_budget_of_more_than_18_digits_is_a_usage_mistake_on_every_python(self) -> None:
        for digits in (19, 5000):
            r = cli(["supply", "amsterdam", "--budget", "9" * digits])
            self.assertEqual(r.code, EXIT_USAGE, digits)
            self.assertIn("--budget is a whole number of EUR", r.err)
            self.assertEqual(len(r.opener.calls), 0)
        r = cli(["supply", "amsterdam", "--budget", "9" * 18])
        self.assertEqual(r.opener.calls[0].search, f"?budget_max_eur={'9' * 18}")
        r = cli(["supply", "amsterdam", "--budget", "0" * 20 + "900"])
        self.assertEqual(r.opener.calls[0].search, "?budget_max_eur=900", "leading zeros, as Number() reads them")

    def test_json_that_cannot_be_written_exits_1_without_a_traceback(self) -> None:
        with mock.patch("socials_homes.cli._json", side_effect=RecursionError("maximum recursion depth exceeded")):
            r = cli(["plans", "--json"])
        self.assertEqual(r.code, EXIT_FAILED)
        self.assertRegex(r.err, r"^socials-homes: The answer could not be printed \(RecursionError: ")

    def test_columns_line_up_as_padEnd_counts_them(self) -> None:
        page = {**fixture("cities-nl-page1"), "next_cursor": None}
        page["data"][0]["name"] = "\U0001d538lmere"  # one code point, two UTF-16 units
        r = cli(["cities"], FakeOpener({"/api/v1/cities": {"body": page}}))
        self.assertIn("almere     \U0001d538lmere    NL Netherlands\namsterdam  Amsterdam  NL Netherlands", r.out)

    def test_a_price_past_1e21_prints_as_to_fixed_prints_it(self) -> None:
        plans = fixture("plans")
        plans["data"][0]["price_eur"] = 1e21
        r = cli(["plans"], FakeOpener({"/api/v1/plans": {"body": plans}}))
        self.assertIn("EUR 1e+21", r.out)


class TheInstalledCommand(unittest.TestCase):
    def env(self) -> dict:
        return {**os.environ, "PYTHONPATH": os.pathsep.join(filter(None, [str(SRC), os.environ.get("PYTHONPATH")]))}

    def test_python_m_socials_homes_runs_and_sets_the_exit_code(self) -> None:
        ok = subprocess.run(
            [sys.executable, "-m", "socials_homes", "--version"],
            capture_output=True,
            text=True,
            env=self.env(),
            check=False,
        )
        self.assertEqual((ok.returncode, ok.stdout.strip()), (EXIT_OK, VERSION))
        bad = subprocess.run(
            [sys.executable, "-m", "socials_homes", "nope"], capture_output=True, text=True, env=self.env(), check=False
        )
        self.assertEqual(bad.returncode, EXIT_USAGE)
        self.assertIn('Unknown command "nope"', bad.stderr)
        self.assertEqual(bad.stdout, "")

    def test_the_output_is_utf_8_whatever_the_locale(self) -> None:
        env = {**self.env(), "PYTHONIOENCODING": "ascii", "LC_ALL": "C"}
        # The escapes reach the child as text; a lone surrogate cannot travel in argv.
        script = "from socials_homes.cli import _writer\n_writer('stdout')('EUR \\u20ac24.95, Malm\\u00f6 \\ud800')\n"
        run = subprocess.run([sys.executable, "-c", script], capture_output=True, env=env, check=False)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(run.stdout, "EUR \u20ac24.95, Malm\u00f6 \\ud800\n".encode())

    def test_entry_exits_with_mains_code(self) -> None:
        with (
            mock.patch.object(sys, "argv", ["socials-homes", "--version"]),
            contextlib.redirect_stdout(io.StringIO()) as out,
            self.assertRaises(SystemExit) as done,
        ):
            entry()
        self.assertEqual(done.exception.code, EXIT_OK)
        self.assertEqual(out.getvalue(), f"{VERSION}\n")

    def test_entry_turns_ctrl_c_into_130(self) -> None:
        with mock.patch("socials_homes.cli.main", side_effect=KeyboardInterrupt), self.assertRaises(SystemExit) as done:
            entry()
        self.assertEqual(done.exception.code, 130)

    def test_main_reads_sys_argv_by_default(self) -> None:
        out: list = []
        with mock.patch.object(sys, "argv", ["socials-homes", "-v"]):
            self.assertEqual(main(io=CliIo(stdout=out.append, stderr=out.append)), EXIT_OK)
        self.assertEqual(out, [VERSION])


if __name__ == "__main__":
    unittest.main()
