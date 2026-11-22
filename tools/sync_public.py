"""Copy web_app/static into public/ (served by the Vercel CDN). Run after changing the front end."""
import shutil
from pathlib import Path
R = Path(__file__).resolve().parents[1]
shutil.rmtree(R / "public", ignore_errors=True)
shutil.copytree(R / "web_app/static", R / "public/static")
shutil.copy(R / "web_app/static/index.html", R / "public/index.html")
print("public/ synced")
