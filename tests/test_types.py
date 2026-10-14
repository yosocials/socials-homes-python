from __future__ import annotations

import json
import unittest

import socials_homes as sh
from socials_homes import types as types_module

from .helpers import FIXTURES, fixture
from .spec import INLINE, NAMED, conforms, mismatches


class TypesMatchTheSpec(unittest.TestCase):
    def test_every_schema_field_required_key_type_and_null_matches_the_openapi_document(self) -> None:
        spec = json.loads((FIXTURES / "openapi.json").read_text(encoding="utf-8"))
        self.assertEqual(mismatches(spec), [])

    def test_the_check_notices_a_difference(self) -> None:
        spec = json.loads((FIXTURES / "openapi.json").read_text(encoding="utf-8"))
        plan = spec["components"]["schemas"]["Plan"]["properties"]
        plan["renews_every"]["type"] = "string"
        plan["new_field"] = {"type": "string"}
        spec["components"]["schemas"]["City"]["required"].remove("links")
        found = mismatches(spec)
        self.assertEqual(len(found), 3, found)

    def test_every_typeddict_is_exported(self) -> None:
        for td in [*NAMED.values(), *INLINE.values()]:
            if hasattr(td, "__required_keys__"):
                self.assertIs(getattr(sh, td.__name__), td, td.__name__)
        self.assertIs(sh.Supply, NAMED["Supply"])
        self.assertEqual(sorted(types_module.__all__), sorted(n for n in sh.__all__ if hasattr(types_module, n)))

    def test_the_fixtures_have_the_documented_shapes(self) -> None:
        for name, hint in (
            ("index", sh.ApiIndex),
            ("cities-nl-page1", sh.CityList),
            ("cities-nl-page2", sh.CityList),
            ("city-amsterdam", sh.CityResponse),
            ("supply-amsterdam-900", sh.SupplyResponse),
            ("plans", sh.PlansResponse),
            ("problem-city-not-found", sh.Problem),
            ("problem-invalid-limit", sh.Problem),
        ):
            with self.subTest(fixture=name):
                self.assertEqual(conforms(fixture(name), hint), [])

    def test_the_shape_check_notices_a_wrong_answer(self) -> None:
        body = fixture("supply-amsterdam-900")
        body["data"]["supply"]["level"] = "plenty"
        body["data"]["supply"]["live_listings"] = None
        del body["data"]["note"]
        self.assertEqual(
            len(conforms(body, sh.SupplyResponse)), 2, "the supply fits no union member, and note is missing"
        )
        self.assertEqual(conforms({**fixture("city-amsterdam"), "added_in_v1_1": True}, sh.CityResponse), [])


if __name__ == "__main__":
    unittest.main()
