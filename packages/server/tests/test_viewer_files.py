"""The viewer's vendored JavaScript must be in the repo: CI runs on a fresh checkout, so
a file that's missing (e.g. caught by .gitignore) fails here instead of in people's browsers."""

import json
import re
from pathlib import Path

import pytest

VIEWER = Path(__file__).resolve().parents[2] / "viewer"
IMPORT_RE = re.compile(r"""(?:from|import)\s*['"]([^'"]+)['"]""")
MODULE_RE = re.compile(r'<script type="module" src="([^"]+)"')
PAGES = sorted(p for p in VIEWER.glob("*.html") if '<script type="importmap">' in p.read_text())


def _import_map(page: Path) -> dict:
    html = page.read_text()
    return json.loads(re.search(r'<script type="importmap">(.*?)</script>', html, re.S).group(1))["imports"]


def _local(url: str) -> Path:
    assert url.startswith("/viewer/"), url
    return VIEWER / url.removeprefix("/viewer/")


def test_pages_with_import_maps_are_found():
    assert {p.name for p in PAGES} >= {"3d.html", "avatar.html"}


@pytest.mark.parametrize("page", PAGES, ids=lambda p: p.name)
def test_import_map_targets_exist(page):
    for name, url in _import_map(page).items():
        target = _local(url)
        assert target.exists(), f"{page.name}: {name} -> {target} is missing (vendor/update.sh, and check .gitignore)"


@pytest.mark.parametrize("page", PAGES, ids=lambda p: p.name)
def test_modules_resolve_their_imports(page):
    imports = _import_map(page)
    pending = [_local(u) for u in imports.values() if not u.endswith("/")]
    pending += [_local(src) for src in MODULE_RE.findall(page.read_text())]
    seen = set()
    while pending:
        file = pending.pop()
        if file in seen:
            continue
        seen.add(file)
        assert file.exists(), f"{page.name} needs {file}, which is missing"
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
    assert len(seen) >= 5
