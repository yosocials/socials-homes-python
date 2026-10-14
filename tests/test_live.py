"""Against the real API, read-only GETs.

Runs only with SOCIALS_HOMES_LIVE=1, so the normal suite never needs the network.

SOCIALS_HOMES_LIVE=1 python -m unittest tests.test_live
"""

from __future__ import annotations

import json
import os
import unittest
import urllib.request

import socials_homes as sh
from socials_homes import SocialsHomes, SocialsHomesError
from socials_homes.cli import CliIo, main

from .spec import conforms, mismatches

LIVE = os.environ.get("SOCIALS_HOMES_LIVE") == "1"
BASE_URL = os.environ.get("SOCIALS_HOMES_BASE_URL")


@unittest.skipUnless(LIVE, "set SOCIALS_HOMES_LIVE=1 to run")
class TheLiveApi(unittest.TestCase):
    client: SocialsHomes

    @classmethod
    def setUpClass(cls) -> None:
        cls.client = SocialsHomes(BASE_URL, user_agent="socials-homes-python-tests")

    def test_the_index_lists_the_five_endpoints_this_sdk_wraps(self) -> None:
        index = self.client.get_index()
        self.assertEqual(index["version"], "1")
        paths = sorted(e["path"] for e in index["endpoints"])
        self.assertEqual(
            paths,
            sorted(
                ["/api/v1", "/api/v1/cities", "/api/v1/cities/{slug}", "/api/v1/cities/{slug}/supply", "/api/v1/plans"]
            ),
        )
        self.assertEqual(conforms(index, sh.ApiIndex), [])

    def test_cities_visits_every_city_once(self) -> None:
        slugs = [c["slug"] for c in self.client.cities(page_size=50)]
        self.assertGreater(len(slugs), 50, "more than one page")
        self.assertEqual(len(set(slugs)), len(slugs))

    def test_a_city_its_supply_and_the_plans_have_the_documented_shapes(self) -> None:
        city = self.client.get_city("amsterdam")
        self.assertEqual(city["country"], "NL")
        self.assertEqual(conforms(city, sh.City), [])
        s = self.client.get_city_supply("amsterdam", budget_max_eur=900)
        self.assertEqual(conforms(s, sh.CitySupply), [])
        supply = s["supply"]
        if supply["available"]:
            self.assertIsInstance(supply["live_listings"], int)
            self.assertEqual(supply["budget_max_eur"], 900)
        else:
            self.assertIsInstance(supply["reason"], str)
        self.assertEqual(conforms(self.client.get_city_supply("amsterdam"), sh.CitySupply), [])
        plans = self.client.list_plans()
        self.assertEqual(plans["currency"], "EUR")
        for p in plans["data"]:
            self.assertIsInstance(p["price_eur"], (int, float))
        self.assertEqual(conforms(plans, sh.PlansResponse), [])
        self.assertEqual(conforms(self.client.list_cities(country="NL", limit=2), sh.CityList), [])

    def test_an_unknown_city_is_city_not_found_with_suggestions(self) -> None:
        with self.assertRaises(SocialsHomesError) as caught:
            self.client.get_city("den_haag")
        err = caught.exception
        self.assertEqual(err.code, "city_not_found")
        self.assertEqual(err.status, 404)
        self.assertTrue(any(x["slug"] == "the_hague" for x in err.problem.get("suggestions", [])))
        self.assertEqual(conforms(err.problem, sh.Problem), [])

    def test_a_bad_parameter_is_invalid_parameter(self) -> None:
        with self.assertRaises(SocialsHomesError) as caught:
            self.client.list_cities(country="")
        self.assertEqual((caught.exception.status, caught.exception.code), (400, "invalid_parameter"))
        self.assertEqual(conforms(caught.exception.problem, sh.Problem), [])

    def test_the_types_match_the_live_openapi_document(self) -> None:
        url = f"{self.client.base_url}/openapi.json"
        request = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(request, timeout=15) as response:
            spec = json.loads(response.read())
        self.assertEqual(mismatches(spec), [])

    def test_the_cli_prints_the_live_supply(self) -> None:
        out: list[str] = []
        err: list[str] = []
        args = ["supply", "amsterdam", "--budget", "900"] + (["--base-url", BASE_URL] if BASE_URL else [])
        code = main(args, CliIo(stdout=out.append, stderr=err.append))
        self.assertEqual(code, 0, err)
        self.assertRegex(out[0], r"^Amsterdam, Netherlands\n\n(Live listings: [\d,]+|Live counts are unavailable)")


if __name__ == "__main__":
    unittest.main()
