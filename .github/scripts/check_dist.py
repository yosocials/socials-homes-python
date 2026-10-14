"""Fails unless dist/ holds one sdist and one wheel carrying only the package, README, LICENSE and pyproject.

Run after uv build: python .github/scripts/check_dist.py
"""

from __future__ import annotations

import pathlib
import sys
import tarfile
import zipfile

DIST = pathlib.Path("dist")


def main() -> int:
    sdists = sorted(DIST.glob("*.tar.gz"))
    wheels = sorted(DIST.glob("*.whl"))
    if len(sdists) != 1 or len(wheels) != 1:
        print(f"Want one sdist and one wheel in dist/, found {[p.name for p in sdists + wheels]}")
        return 1
    stray: list[str] = []

    with tarfile.open(sdists[0]) as sdist:
        names = sorted(m.name for m in sdist.getmembers() if m.isfile())
    root = sdists[0].name[: -len(".tar.gz")] + "/"
    # PKG-INFO is the metadata every sdist carries.
    allowed = {f"{root}{n}" for n in ("README.md", "LICENSE", "pyproject.toml", "PKG-INFO")}
    stray += [n for n in names if n not in allowed and not n.startswith(f"{root}src/socials_homes/")]
    print(f"{sdists[0].name}: {len(names)} files")
    for n in names:
        print(f"  {n}")

    with zipfile.ZipFile(wheels[0]) as wheel:
        wnames = sorted(i.filename for i in wheel.infolist() if not i.is_dir())
    dist_info = wheels[0].name.split("-")[0] + "-" + wheels[0].name.split("-")[1] + ".dist-info/"
    # METADATA carries the README; the LICENSE sits under licenses/.
    meta = {f"{dist_info}{n}" for n in ("METADATA", "WHEEL", "RECORD", "entry_points.txt", "licenses/LICENSE")}
    stray += [n for n in wnames if n not in meta and not n.startswith("socials_homes/")]
    print(f"{wheels[0].name}: {len(wnames)} files")
    for n in wnames:
        print(f"  {n}")

    for must in ("socials_homes/py.typed", f"{dist_info}licenses/LICENSE", f"{dist_info}entry_points.txt"):
        if must not in wnames:
            stray.append(f"missing {must}")
    if any("__pycache__" in n or n.endswith(".pyc") for n in names + wnames):
        stray.append("compiled files")
    if stray:
        print(f"Would publish more or less than the package: {stray}")
        return 1
    print("Only the package, README, LICENSE and pyproject.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
