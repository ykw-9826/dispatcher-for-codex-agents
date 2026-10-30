"""Local, offline syntax validation and reproducible source-only ZIP."""

import ast
import zipfile
from pathlib import Path

root = Path(__file__).resolve().parent
sources = sorted((root / "src").glob("*.py"))
for path in sources:
    ast.parse(path.read_text(), filename=path.name)
(root / "build").mkdir(exist_ok=True)
with zipfile.ZipFile(root / "build/labels.zip", "w") as archive:
    for path in sources:
        archive.writestr(path.relative_to(root).as_posix(), path.read_bytes())
print("BUILD_PASS")
