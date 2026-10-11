"""The viewer's vendored JavaScript must be in the repo: CI runs on a fresh checkout, so
a file that's missing (e.g. caught by .gitignore) fails here instead of in people's browsers."""

import json
import re
from pathlib import Path

VIEWER = Path(__file__).resolve().parents[2] / "viewer"
IMPORT_RE = re.compile(r"""(?:from|import)\s*['"]([^'"]+)['"]""")


def _import_map() -> dict:
    html = (VIEWER / "3d.html").read_text()
    return json.loads(re.search(r'<script type="importmap">(.*?)</script>', html, re.S).group(1))["imports"]


def _local(url: str) -> Path:
    assert url.startswith("/viewer/"), url
    return VIEWER / url.removeprefix("/viewer/")


def test_import_map_targets_exist():
    for name, url in _import_map().items():
        target = _local(url)
        assert target.exists(), f"{name} -> {target} is missing (vendor/update.sh, and check .gitignore)"


def test_vendored_modules_resolve_their_imports():
    imports = _import_map()
    pending = [_local(u) for u in imports.values() if not u.endswith("/")]
    pending += [VIEWER / "town3d.js"]
    seen = set()
    while pending:
        file = pending.pop()
        if file in seen:
            continue
        seen.add(file)
        for spec in IMPORT_RE.findall(file.read_text(encoding="utf-8")):
            if spec.startswith("."):
                target = (file.parent / spec).resolve()
            elif spec in imports:
                target = _local(imports[spec])
            elif any(spec.startswith(p) for p in imports if p.endswith("/")):
                prefix = next(p for p in imports if p.endswith("/") and spec.startswith(p))
                target = _local(imports[prefix] + spec[len(prefix):])
            else:
                continue  # a URL in a comment or docstring
            assert target.exists(), f"{file.relative_to(VIEWER)} imports {spec!r}, but {target} is missing"
            pending.append(target)
    assert len(seen) >= 8
