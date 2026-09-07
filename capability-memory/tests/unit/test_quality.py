from dataclasses import replace
import subprocess

from supermind_memory.types import ArtifactType, Capability, Lifecycle


def candidate(source="https://github.com/example/auth"):
    return Capability(id="auth", name="登录", summary="校验凭证并建立会话。", category_path=("code", "Authentication"),
        facets=(), contract="credentials -> session", constraints=(), artifact_type=ArtifactType.CODE,
        source_uri=source, source_revision="abc123", content_hash="a" * 64, owner="team", license="MIT",
        stack=("python",), runtime=("python",), platform=("local",), dependencies=(), compatibility=(),
        lifecycle=Lifecycle.VERIFIED, confidence=1, expected_net_value=10, embedding_generation="test",
        created_at="2026-09-06T12:00:00Z", updated_at="2026-09-06T12:00:00Z", last_verified_at="2026-09-06T12:00:00Z")


def test_missing_summary_and_manifest_are_not_code_capabilities():
    from supermind_memory.quality import quality_issues
    issues = quality_issues(replace(candidate("file:///tmp/plugin.json"), summary=" "))
    assert "summary_missing" in issues
    assert "code_asset_is_manifest" in issues


def test_invalid_source_is_reported_without_crashing():
    from supermind_memory.quality import quality_issues
    assert "source_uri_invalid" in quality_issues(candidate("https://[invalid"))


def test_positive_score_does_not_prove_necessity():
    from supermind_memory.quality import audit_capability
    report = audit_capability(candidate(), ())
    assert report["reuse_authorized"] is False
    assert "necessity" in report["human_review_required"]
    assert "summary_and_contract_accuracy" in report["human_review_required"]


def test_reuse_gate_rejects_legacy_record_with_empty_summary():
    from supermind_memory.decision import eligibility_reasons
    from supermind_memory.types import CandidateMatch, RequirementProfile
    match = CandidateMatch("auth", 1, 1, 1, 1, 1, 1, 0, 0, 1, source_available=True)
    reasons = eligibility_reasons(RequirementProfile(id="r", project_id="p", intent="login"), match, replace(candidate(), summary=""))
    assert "summary_missing" in reasons


def test_audit_detects_revision_drift_without_editing_source(tmp_path):
    from supermind_memory.quality import audit_capability
    def git(*args):
        return subprocess.check_output(["git", "-C", str(tmp_path), *args], stderr=subprocess.DEVNULL).decode().strip()
    git("init")
    source = tmp_path / "auth.py"
    source.write_text("def login(): return 1\n")
    git("add", "auth.py")
    git("-c", "user.name=Test", "-c", "user.email=test@localhost", "commit", "-m", "initial")
    item = replace(candidate(source.as_uri()), source_revision=git("rev-parse", "HEAD"))
    assert audit_capability(item, ())["asset"]["revision_file_matches"] is True
    source.write_text("def login(): return 2\n")
    report = audit_capability(item, ())
    assert "source_revision_drift" in report["issues"]
    assert source.read_text() == "def login(): return 2\n"
