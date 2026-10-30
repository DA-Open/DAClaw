from pathlib import Path
import tomllib


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_faster_whisper_is_not_a_base_dependency():
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    deps = data["project"]["dependencies"]

    assert not any(dep.startswith("faster-whisper") for dep in deps)

    voice_extra = data["project"]["optional-dependencies"]["voice"]
    assert any(dep.startswith("faster-whisper") for dep in voice_extra)


def test_data_extra_declares_python_docx():
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    # agent/data_agent/document.py imports docx for Word export
    assert any(dep.startswith("python-docx") for dep in data["project"]["optional-dependencies"]["data"])


def test_acp_extra_stays_below_0_11():
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    (acp_dep,) = data["project"]["optional-dependencies"]["acp"]

    # 0.11+ removed SetSessionModelResponse, which acp_adapter/server.py imports
    assert "<0.11" in acp_dep


def test_manifest_includes_bundled_skills():
    manifest = (REPO_ROOT / "MANIFEST.in").read_text(encoding="utf-8")

    assert "graft skills" in manifest
    assert "graft optional-skills" in manifest
