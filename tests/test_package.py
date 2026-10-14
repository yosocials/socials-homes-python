from __future__ import annotations

import json
import pathlib
import re
import sys
import unittest

import socials_homes
from socials_homes import VERSION

ROOT = pathlib.Path(__file__).resolve().parent.parent
PACKAGE = ROOT / "src" / "socials_homes"


def read_toml(text: str) -> dict:
    """pyproject.toml as a dict: tomllib from Python 3.11, else a reader for the subset this file uses."""
    try:
        import tomllib
    except ImportError:
        return _subset_toml(text)
    return tomllib.loads(text)


def _subset_toml(text: str) -> dict:
    # Tables, and keys whose values are strings, arrays of strings or inline tables of strings.
    data: dict = {}
    table = data
    pending = ""
    for raw in text.splitlines():
        line = re.sub(r'\s+#[^"]*$', "", raw).strip()
        if line.startswith("#"):
            continue
        if not pending and line.startswith("["):
            table = data
            for part in line.strip("[]").split("."):
                table = table.setdefault(part.strip('"'), {})
            continue
        pending = f"{pending} {line}".strip()
        if not pending or pending.count("[") > pending.count("]") or pending.count("{") > pending.count("}"):
            continue
        key, _, value = pending.partition("=")
        value = value.strip()
        if value.startswith(("{", "[{")):
            value = re.sub(r"([{,]\s*)([A-Za-z_-]+)\s*=", r'\1"\2":', value)
        table[key.strip().strip('"')] = json.loads(re.sub(r",\s*([\]}])", r"\1", value))
        pending = ""
    return data


PYPROJECT_TEXT = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
PYPROJECT = read_toml(PYPROJECT_TEXT)
PROJECT = PYPROJECT["project"]


class Package(unittest.TestCase):
    def test_the_version_the_client_reports_is_the_package_version(self) -> None:
        self.assertEqual(VERSION, PROJECT["version"])
        self.assertEqual(socials_homes.__version__, PROJECT["version"])
        self.assertEqual(VERSION, "1.0.0")

    def test_no_runtime_dependencies_installing_it_pulls_in_nothing_else(self) -> None:
        self.assertEqual(PROJECT["dependencies"], [])
        self.assertNotIn("optional-dependencies", PROJECT)
        imported = set()
        for source in PACKAGE.glob("*.py"):
            for match in re.finditer(
                r"^\s*(?:from|import) ([a-zA-Z_][\w.]*)", source.read_text(encoding="utf-8"), re.M
            ):
                imported.add(match.group(1).split(".")[0])
        stdlib = {
            "__future__", "collections", "contextlib", "dataclasses", "datetime", "decimal", "email", "http",
            "ipaddress", "json", "math", "os", "random", "re", "socket", "sys", "threading", "time", "typing", "urllib",
        }  # fmt: skip
        if hasattr(sys, "stdlib_module_names"):  # Python 3.10 and later can say for itself
            self.assertLessEqual(stdlib, set(sys.stdlib_module_names))
        imported.discard("socials_homes")  # the usage example in the docstring
        self.assertLessEqual(imported, stdlib, f"imports outside the standard library: {sorted(imported - stdlib)}")

    def test_nothing_runs_on_a_users_machine_at_install(self) -> None:
        self.assertEqual(PYPROJECT["build-system"]["build-backend"], "hatchling.build")
        # 1.27 is the last hatchling that runs on Python 3.9, so the sdist builds wherever the package installs.
        self.assertEqual(PYPROJECT["build-system"]["requires"], ["hatchling>=1.27"])
        for name in ("setup.py", "setup.cfg", "MANIFEST.in"):
            self.assertFalse((ROOT / name).exists(), name)
        self.assertNotIn("hooks", PYPROJECT.get("tool", {}).get("hatch", {}).get("build", {}))

    def test_the_package_names_the_socials_homes_domain_and_its_source_repository_so_agents_can_verify_it_is_official(
        self,
    ) -> None:
        self.assertEqual(PROJECT["name"], "socials-homes")
        urls = PROJECT["urls"]
        self.assertRegex(urls["Homepage"], r"^https://www\.socials\.homes/")
        self.assertRegex(urls["Documentation"], r"^https://www\.socials\.homes/")
        self.assertEqual(urls["Source"], "https://github.com/yosocials/socials-homes-python")
        self.assertEqual(urls["Issues"], "https://github.com/yosocials/socials-homes-python/issues")
        self.assertEqual(PROJECT["authors"], [{"name": "Socials BV", "email": "hello@socials.homes"}])
        self.assertEqual(PROJECT["maintainers"], PROJECT["authors"])
        self.assertEqual(PROJECT["license"], "MIT")
        self.assertIn("https://www.socials.homes/terms", (ROOT / "LICENSE").read_text(encoding="utf-8"))

    def test_only_the_package_is_shipped_and_the_cli_is_the_console_script(self) -> None:
        build = PYPROJECT["tool"]["hatch"]["build"]["targets"]
        self.assertEqual(build["wheel"]["packages"], ["src/socials_homes"])
        self.assertEqual(
            sorted(build["sdist"]["only-include"]), ["LICENSE", "README.md", "pyproject.toml", "src/socials_homes"]
        )
        self.assertEqual(PROJECT["scripts"], {"socials-homes": "socials_homes.cli:entry"})
        self.assertTrue((PACKAGE / "py.typed").is_file())
        self.assertTrue((PACKAGE / "__main__.py").is_file())
        self.assertEqual(PROJECT["requires-python"], ">=3.9")

    def test_the_fallback_toml_reader_agrees_with_tomllib(self) -> None:
        try:
            import tomllib
        except ImportError:
            self.skipTest("tomllib arrived in Python 3.11")
        real = tomllib.loads(PYPROJECT_TEXT)
        subset = _subset_toml(PYPROJECT_TEXT)
        for key in ("build-system", "project"):
            self.assertEqual(subset[key], real[key], key)
        self.assertEqual(subset["tool"]["hatch"], real["tool"]["hatch"])

    def test_copy_has_no_em_or_en_dashes_and_no_emoji(self) -> None:
        files = [ROOT / "README.md", ROOT / "LICENSE", ROOT / "pyproject.toml", *PACKAGE.glob("*.py")]
        files += [*(ROOT / "tests").glob("*.py"), *(ROOT / "tests" / "fixtures").glob("*.json")]
        files += [
            *(ROOT / ".github").rglob("*.yml"),
            *(ROOT / ".github").rglob("*.py"),
            *(ROOT / ".github").rglob("*.txt"),
        ]
        banned = re.compile("[\u2013\u2014\U0001f000-\U0001faff\u2600-\u27bf\ufe0f]")
        for path in files:
            with self.subTest(path=path.name):
                self.assertIsNone(banned.search(path.read_text(encoding="utf-8")))


if __name__ == "__main__":
    unittest.main()
