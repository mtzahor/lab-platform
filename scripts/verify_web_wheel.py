from __future__ import annotations

import argparse
import re
import tomllib
from pathlib import Path
from zipfile import ZipFile

_ASSET_REFERENCE = re.compile(r"(?:src|href)=[\"'](?P<path>/assets/[^\"']+)[\"']")
_WEB_ROOT = "lab_platform/control_plane/web_dist/"


def _select_current_wheel(wheels: list[Path]) -> Path:
    if len(wheels) == 1:
        return wheels[0]

    project_path = Path(__file__).resolve().parents[1] / "pyproject.toml"
    project = tomllib.loads(project_path.read_text(encoding="utf-8"))["project"]
    distribution = project["name"].replace("-", "_")
    version = project["version"]
    marker = f"{distribution}-{version}-"
    current_wheels = [wheel for wheel in wheels if marker in wheel.name]
    if len(current_wheels) != 1:
        raise ValueError(
            f"expected exactly one {distribution} wheel for current version {version}; "
            f"found {len(current_wheels)} among {len(wheels)} candidates"
        )
    return current_wheels[0]


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify that a Lab Platform wheel contains a usable dashboard bundle."
    )
    parser.add_argument("wheels", nargs="+", type=Path)
    args = parser.parse_args()

    try:
        wheel = _select_current_wheel(args.wheels)
    except ValueError as error:
        parser.error(str(error))

    if not wheel.is_file():
        parser.error(f"wheel does not exist: {wheel}")

    with ZipFile(wheel) as archive:
        names = set(archive.namelist())
        index_name = _WEB_ROOT + "index.html"
        if index_name not in names:
            raise SystemExit(f"Dashboard entry point is missing from {wheel}: {index_name}")

        index = archive.read(index_name).decode("utf-8")
        references = {
            match.group("path").removeprefix("/") for match in _ASSET_REFERENCE.finditer(index)
        }
        if not references:
            raise SystemExit("Dashboard index.html does not reference any compiled assets")

        missing = sorted(
            _WEB_ROOT + reference for reference in references if _WEB_ROOT + reference not in names
        )
        if missing:
            raise SystemExit("Dashboard assets are missing from the wheel: " + ", ".join(missing))

        if not any(
            name.startswith(_WEB_ROOT + "assets/") and name.endswith(".js") for name in names
        ):
            raise SystemExit("Dashboard JavaScript bundle is missing from the wheel")
        if any("node_modules/" in name for name in names):
            raise SystemExit("The wheel unexpectedly contains frontend node_modules")
        source_maps = sorted(
            name for name in names if name.startswith(_WEB_ROOT) and name.endswith(".map")
        )
        if source_maps:
            raise SystemExit(
                "The wheel unexpectedly contains frontend source maps: " + ", ".join(source_maps)
            )

    print(f"Verified packaged dashboard in {wheel}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
