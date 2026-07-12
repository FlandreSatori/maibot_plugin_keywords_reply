from pathlib import Path
import shutil

src = Path(__file__).resolve().parent / "tools" / "keywords.imported.json"
targets = [
    Path(__file__).resolve().parent / "keywords.json",
    Path(__file__).resolve().parents[1] / "maibot_plugin.keywords_reply" / "keywords.json",
]
if not src.exists():
    raise SystemExit(f"missing: {src}")
for dst in targets:
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    print(f"copied -> {dst} ({dst.stat().st_size} bytes)")
