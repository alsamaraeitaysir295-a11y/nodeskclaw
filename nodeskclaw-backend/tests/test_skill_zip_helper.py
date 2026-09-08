"""build_gene_zip 单测：ZIP 结构（SKILL.md/scripts/assets/references）、路径安全、二进制条目。"""

import io
import json
import zipfile

from app.services.skill_package_service import BINARY_MARKER, build_gene_zip


class _FakeGene:
    """只带 build_gene_zip 所需字段的轻量替身。"""

    def __init__(self, slug: str, manifest: dict):
        self.slug = slug
        self.manifest = json.dumps(manifest, ensure_ascii=False)


def _names(data: bytes) -> set[str]:
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        return set(zf.namelist())


def test_build_gene_zip_contains_all_sections():
    gene = _FakeGene("demo-skill", {
        "skill": {"content": "# Demo\n说明"},
        "scripts": {"main.py": "print('hi')", "nested/evil.py": "x"},
        "assets": {"assets/data.json": "{}", "../escape.txt": "bad"},
        "references": {"references/guide.md": "guide"},
    })
    buf, size = build_gene_zip(gene)
    data = buf.getvalue()
    assert size == len(data)
    names = _names(data)
    assert "demo-skill/SKILL.md" in names
    assert "demo-skill/main.py" in names              # scripts 取 basename
    assert "demo-skill/nested/evil.py" not in names   # basename 丢弃子目录
    assert "demo-skill/assets/data.json" in names
    assert "demo-skill/references/guide.md" in names
    assert not any("escape" in n for n in names)      # .. 路径被拒


def test_build_gene_zip_decodes_binary_entry():
    import base64

    raw = b"PK\x03\x04binary"
    entry = {BINARY_MARKER: True, "b64": base64.b64encode(raw).decode("ascii")}
    gene = _FakeGene("bin-skill", {"skill": {"content": "x"}, "assets": {"assets/a.docx": entry}})
    buf, _ = build_gene_zip(gene)
    with zipfile.ZipFile(io.BytesIO(buf.getvalue())) as zf:
        assert zf.read("bin-skill/assets/a.docx") == raw


def test_build_gene_zip_corrupt_manifest_raises():
    class _Bad:
        slug = "bad"
        manifest = "{not-json"

    import pytest
    with pytest.raises(json.JSONDecodeError):
        build_gene_zip(_Bad())
