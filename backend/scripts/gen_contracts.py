"""Regenerate the import-linter contracts in pyproject.toml from apps/ (run from backend/)."""

from pathlib import Path

root = Path(__file__).resolve().parent.parent
sources = sorted(p.name for p in (root / "apps").iterdir() if (p / "__init__.py").exists())
targets = [a for a in sources if a != "core" and (root / "apps" / a / "models.py").exists()]
blocks = []
for app in sources:
    others = ", ".join(f'"apps.{o}.models"' for o in targets if o != app)
    blocks.append(
        f"""
[[tool.importlinter.contracts]]
name = "{app} does not import other apps' models"
type = "forbidden"
source_modules = ["apps.{app}"]
forbidden_modules = [{others}]
allow_indirect_imports = true
unmatched_ignore_imports_alerting = "none"
ignore_imports = [
    "apps.{app}.tests.** -> **",
    "apps.{app}.factories -> **",
    "apps.{app}.migrations.** -> **",
    "apps.{app}.management.** -> **",
    "apps.{app}.admin -> **",
]
"""
    )
path = root / "pyproject.toml"
text = path.read_text()
head = text.split("\n[[tool.importlinter.contracts]]")[0].rstrip("\n") + "\n"
text = head + "".join(blocks)
path.write_text(text)
print(f"{len(blocks)} contracts")
