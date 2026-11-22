"""Write the synthetic demo field (seeded generator in web_app/demo_data.py) to web_app/demo_static/.

Run from the repository root after changing the generator:   python tools/build_demo_static.py
The app serves these files for "Load demo data", so every server instance returns byte-identical demo data.
The data is synthetic: it is not field data and must not be presented as such.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "web_app"))

from web_app import demo_data            # noqa: E402
from web_app.main import model           # noqa: E402

if __name__ == "__main__":
    names = demo_data.write_static(model())
    print(f"wrote {len(names)} files to {demo_data.STATIC_DIR}: " + ", ".join(names))
