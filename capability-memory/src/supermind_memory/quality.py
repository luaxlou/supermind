"""Project-owned capability admission checks and evidence-bounded library audits."""
from __future__ import annotations

from pathlib import Path
from collections.abc import Sequence
import subprocess
from urllib.parse import unquote, urlsplit

from supermind_memory.source_resolution import resolve_source, source_available
from supermind_memory.types import Capability, Evidence


def quality_issues(capability: Capability) -> tuple[str, ...]:
    """Cheap deterministic gates shared by admission and reuse; no semantic approval."""
    issues = [f"{field}_missing" for field in ("name", "summary", "contract", "source_uri", "source_revision", "content_hash")
              if not getattr(capability, field).strip()]
    source = resolve_source(capability.source_uri)
    if source is None:
        issues.append("source_uri_invalid")
    filename = (Path(unquote(urlsplit(source.canonical_uri).path)).name.casefold()
                if source is not None else "")
    if capability.category_path[0] == "code" and filename in {"plugin.json", "package.json", "pyproject.toml", "cargo.toml", "go.mod"}:
        issues.append("code_asset_is_manifest")
    return tuple(issues)


def audit_capability(capability: Capability, evidence: Sequence[Evidence]) -> dict:
    issues = list(quality_issues(capability))
    if not source_available(capability, evidence):
        issues.append("source_unavailable")
    source = resolve_source(capability.source_uri)
    asset = {"source": capability.source_uri, "revision": capability.source_revision,
             "repository": None, "relative_path": None, "revision_file_matches": None}
    if source is not None and source.path is not None and source_available(capability, evidence):
        path = source.path
        def git(*args):
            return subprocess.run(["git", "-C", str(path.parent), *args], check=True,
                                  capture_output=True, timeout=5).stdout.decode().strip()
        try:
            root = Path(git("rev-parse", "--show-toplevel"))
            relative = path.relative_to(root).as_posix()
            asset.update(repository=str(root), relative_path=relative)
            revision = git("rev-parse", "--verify", "--end-of-options", capability.source_revision + "^{commit}")
            historical = git("rev-parse", "--verify", "--end-of-options", f"{revision}:{relative}")
            current = git("hash-object", "--", str(path))
            asset["revision_file_matches"] = historical == current
            if historical != current:
                issues.append("source_revision_drift")
        except (subprocess.SubprocessError, OSError, ValueError):
            issues.append("source_revision_unverified")
    elif source is not None and source.kind == "remote":
        issues.append("remote_revision_requires_review")
    proofs = [item for item in evidence if item.capability_id == capability.id and
              item.evidence_type == "verification" and item.outcome.casefold() in {"passed", "success", "verified"}]
    if not proofs:
        issues.append("verification_evidence_missing")
    return {"id": capability.id, "name": capability.name, "issues": sorted(set(issues)), "asset": asset,
            "status": "needs_attention" if issues else "needs_human_review",
            "abstraction_status": capability.abstraction_status.value,
            "verification_evidence_ids": [item.id for item in proofs],
            "human_review_required": ["necessity", "summary_and_contract_accuracy", "business_independent_boundary",
                                      "capability_specific_verification", "specific_reuse_confirmation"],
            "reuse_authorized": False}
