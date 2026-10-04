"""Check an installed wheel with Python isolated from the source checkout."""

import importlib.metadata
import importlib.resources
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile

import sdd


def main():
    distribution = importlib.metadata.distribution("jev4pg")
    assert distribution.version == sdd.__version__
    package = importlib.resources.files("sdd")
    html = package.joinpath("web/explore.html").read_text(encoding="utf-8")
    assets = set(re.findall(r'/ask-assets/([^"?]+)', html))
    for asset in assets:
        assert package.joinpath("web", asset).is_file(), asset
    for name in ("manifest.json", "examples.json"):
        assert json.loads(package.joinpath("operators", name).read_text(encoding="utf-8"))
    commands = {
        entry.name for entry in distribution.entry_points if entry.group == "console_scripts"
    }
    assert {"jev4pg", "jevsd-pg", "sdd"} <= commands
    with tempfile.TemporaryDirectory() as directory:
        args = [sys.executable, "-I", "-m", "sdd.cli"]
        version = subprocess.check_output([*args, "--version"], cwd=directory, text=True)
        assert version.strip() == "jev4pg " + distribution.version
        subprocess.run([*args, "extension-files", directory], cwd=directory, check=True)
        assert (Path(directory) / "jevsd_pg.control").is_file()
        assert (Path(directory) / "jevsd_pg--0.1.0.sql").is_file()
    print(
        f"Installed jev4pg {distribution.version}: {len(assets)} workspace assets, operator data and SQL extension files verified"
    )


if __name__ == "__main__":
    main()
