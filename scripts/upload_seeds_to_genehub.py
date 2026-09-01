#!/usr/bin/env python3
"""Upload local seed gene/genome templates to GeneHub Registry.

Usage:
    export GENEHUB_REGISTRY_URL=https://genehub.nodeskai.com
    export GENEHUB_API_KEY=ghb_xxx
    python scripts/upload_seeds_to_genehub.py

Or with --dry-run to preview without uploading:
    python scripts/upload_seeds_to_genehub.py --dry-run
"""

import json
import os
import sys
from pathlib import Path

import httpx

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "nodeskclaw-backend" / "app" / "data" / "gene_templates"

SKIP_FILES = {"workflow_step_template.json", "README.md"}

GENE_FILES = [
    "mcp_blackboard_tools.json",
    "mcp_proposals.json",
    "mcp_gene_discovery.json",
    "mcp_performance_reader.json",
    "mcp_topology_awareness.json",
    "mcp_shared_files.json",
    "meta_gene_ai_hc.json",
    "meta_gene_reorg.json",
    "meta_gene_culture.json",
    "meta_gene_self_improve.json",
    "meta_gene_innovation.json",
    "meta_gene_akr_decomposer.json",
]

GENOME_FILES = [
    "genome_self_management.json",
    "genome_ai_employee_basics.json",
    "workflow_genome_example.json",
]

VALID_CATEGORIES = {
    "development", "data", "operations", "network",
    "creative", "communication", "security", "efficiency",
}
VALID_TAGS = {"ability", "personality", "knowledge", "tool"}

CATEGORY_MAP = {
    "tools": "efficiency",
    "meta": "efficiency",
    "culture": "communication",
}

TAG_MAP = {
    "tools": "tool",
    "meta": "ability",
    "culture": "personality",
    "communication": "ability",
    "self-management": "ability",
    "blackboard": "tool",
    "proposals": "tool",
    "trust": "ability",
    "gene-market": "tool",
    "learning": "ability",
    "topology": "tool",
    "performance": "tool",
    "organization": "ability",
    "hiring": "ability",
    "innovation": "ability",
    "improvement": "ability",
    "self-improvement": "ability",
    "akr": "ability",
    "planning": "ability",
    "objectives": "ability",
    "shared-files": "tool",
    "genome": "ability",
    "autonomy": "ability",
}


def _map_category(raw: str) -> str:
    if raw in VALID_CATEGORIES:
        return raw
    return CATEGORY_MAP.get(raw, "efficiency")


def _map_tags(raw_tags: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for t in raw_tags:
        mapped = t if t in VALID_TAGS else TAG_MAP.get(t, "ability")
        if mapped not in seen:
            seen.add(mapped)
            result.append(mapped)
    if not result:
        result.append("ability")
    return result


def build_gene_manifest(tpl: dict) -> dict:
    """Convert a local gene template to the flat GeneHub manifest format."""
    inner = tpl.get("manifest", {})

    default_compat = [{"product": "openclaw", "min_version": "0.5.0"}]
    manifest = {
        "name": tpl["name"],
        "slug": tpl["slug"],
        "description": tpl.get("description", ""),
        "short_description": tpl.get("short_description", ""),
        "category": _map_category(tpl.get("category", "")),
        "tags": _map_tags(tpl.get("tags", [])),
        "version": "1.0.0",
        "author": {"ref": "", "name": "NoDeskAI", "type": "human"},
        "compatibility": tpl.get("compatibility", inner.get("compatibility", default_compat)),
        "dependencies": [],
        "synergies": [],
        "rules": [],
        "mcp_servers": inner.get("mcp_servers", []),
    }
    if "skill" in inner:
        manifest["skill"] = inner["skill"]
    # 能力标签（任务空间匹配器数据源，设计 docs/mission-space-p1-design.md §5/T4a）：
    # 白名单字段必须与模板源同步，否则标注到不了 GeneHub
    if "capabilities" in inner:
        manifest["capabilities"] = inner["capabilities"]
    if "tool_allow" in inner:
        manifest["tool_allow"] = inner["tool_allow"]
    if "scripts" in inner:
        scripts = inner["scripts"]
        if isinstance(scripts, list):
            scripts_dict = {}
            scripts_dir = TEMPLATES_DIR.parent / "gene_scripts"
            for name in scripts:
                script_path = scripts_dir / name
                if script_path.exists():
                    scripts_dict[name] = script_path.read_text()
                else:
                    print(f"Warning: Script {name} not found at {script_path}")
            manifest["scripts"] = scripts_dict
        else:
            manifest["scripts"] = scripts
    return manifest


def build_genome_payload(tpl: dict) -> dict:
    """Convert a local genome template to the GeneHub genome POST payload."""
    gene_slugs = tpl.get("gene_slugs", [])
    genes = [{"slug": s, "version": ">=1.0.0", "required": True} for s in gene_slugs]

    default_compat = [{"product": "openclaw", "min_version": "0.5.0"}]
    return {
        "name": tpl["name"],
        "slug": tpl["slug"],
        "description": tpl.get("description", ""),
        "short_description": tpl.get("short_description", ""),
        "category": _map_category(tpl.get("category", "efficiency")),
        "tags": _map_tags(tpl.get("tags", [])) if tpl.get("tags") else ["ability"],
        "version": "1.0.0",
        "author": {"ref": "", "name": "NoDeskAI", "type": "organization"},
        "genes": genes,
        "compatibility": tpl.get("compatibility", default_compat),
    }


def main() -> None:
    dry_run = "--dry-run" in sys.argv

    registry_url = os.environ.get("GENEHUB_REGISTRY_URL", "").rstrip("/")
    api_key = os.environ.get("GENEHUB_API_KEY", "")

    if not registry_url:
        print("ERROR: GENEHUB_REGISTRY_URL not set")
        sys.exit(1)
    if not api_key and not dry_run:
        print("ERROR: GENEHUB_API_KEY not set")
        sys.exit(1)

    print(f"Registry: {registry_url}")
    print(f"Templates: {TEMPLATES_DIR}")
    print(f"Mode: {'DRY RUN' if dry_run else 'LIVE UPLOAD'}")
    print()

    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }

    ok, fail, skip = 0, 0, 0

    def _publish_gene(slug: str, manifest: dict | None = None) -> bool:
        """PUT to approve + publish a gene, optionally updating its manifest."""
        body: dict = {"is_published": True, "review_status": "approved"}
        if manifest:
            body["manifest"] = manifest
        try:
            resp = httpx.put(
                f"{registry_url}/api/v1/genes/{slug}",
                json=body,
                headers=headers,
                timeout=10.0,
            )
            return resp.status_code < 300
        except Exception:
            return False

    print("=== Uploading genes ===")
    for fname in GENE_FILES:
        fpath = TEMPLATES_DIR / fname
        if not fpath.exists():
            print(f"  SKIP  {fname} (file not found)")
            skip += 1
            continue

        tpl = json.loads(fpath.read_text())
        manifest = build_gene_manifest(tpl)
        slug = tpl["slug"]

        if dry_run:
            print(f"  DRY   {slug} ({fname})")
            ok += 1
            continue

        try:
            resp = httpx.post(
                f"{registry_url}/api/v1/genes",
                json={"manifest": manifest},
                headers=headers,
                timeout=15.0,
            )
            body = resp.json()
            if resp.status_code < 300 and body.get("code") == 0:
                gene_id = body.get("data", {}).get("id", "?")
                published = _publish_gene(slug)
                status = "OK+PUB" if published else "OK(unpublished)"
                print(f"  {status}  {slug} -> id={gene_id}")
                ok += 1
            elif resp.status_code == 409:
                published = _publish_gene(slug, manifest=manifest)
                status = "EXIST+UPDATE" if published else "EXIST"
                print(f"  {status}  {slug} (already on GeneHub)")
                ok += 1
            else:
                msg = body.get("message", resp.text[:120])
                print(f"  FAIL  {slug} — {resp.status_code}: {msg}")
                fail += 1
        except Exception as e:
            print(f"  FAIL  {slug} — {e}")
            fail += 1

    print()
    print("=== Uploading genomes ===")
    for fname in GENOME_FILES:
        fpath = TEMPLATES_DIR / fname
        if not fpath.exists():
            print(f"  SKIP  {fname} (file not found)")
            skip += 1
            continue

        tpl = json.loads(fpath.read_text())
        payload = build_genome_payload(tpl)

        if dry_run:
            print(f"  DRY   {tpl['slug']} ({fname})")
            ok += 1
            continue

        try:
            resp = httpx.post(
                f"{registry_url}/api/v1/genomes",
                json=payload,
                headers=headers,
                timeout=15.0,
            )
            body = resp.json()
            if resp.status_code < 300 and body.get("code") == 0:
                genome_id = body.get("data", {}).get("id", "?")
                print(f"  OK    {tpl['slug']} -> id={genome_id}")
                ok += 1
            else:
                msg = body.get("message", resp.text[:120])
                print(f"  WARN  {tpl['slug']} — {resp.status_code}: {msg}")
                print(f"        (Genome POST may not be supported; try GeneHub Web UI)")
                fail += 1
        except Exception as e:
            print(f"  WARN  {tpl['slug']} — {e}")
            print(f"        (Genome POST may not be supported; try GeneHub Web UI)")
            fail += 1

    print()
    print(f"Done. OK={ok}  FAIL={fail}  SKIP={skip}")
    if fail > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
