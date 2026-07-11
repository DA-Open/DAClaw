from types import SimpleNamespace

from agent.data_agent.engine import _build_sqlalchemy_url, _redact_connection_error
from daclaw_cli import data_agent_web
from tools.data_agent_tools import _allowed_source_path


def test_connection_url_encodes_credentials():
    url = _build_sqlalchemy_url("postgres", "db.local", 5432, "user@example.com", "p/a:ss@word", "sales")
    assert "user%40example.com" in url
    assert "p%2Fa%3Ass%40word" in url


def test_connection_errors_are_redacted():
    exc = RuntimeError("postgresql://alice:secret-value@db.local/sales password=hunter2")
    text = _redact_connection_error(exc)
    assert "secret-value" not in text
    assert "hunter2" not in text
    assert "***" in text


def test_uploaded_path_stays_inside_workspace(tmp_path, monkeypatch):
    root = tmp_path / "workspace"
    root.mkdir()
    inside = root / "data.sqlite"
    inside.write_bytes(b"SQLite format 3\0")
    outside = tmp_path / "secret.sqlite"
    outside.write_bytes(b"SQLite format 3\0")
    import daclaw_cli.web_server as web_server

    monkeypatch.setattr(web_server, "_workspace_upload_root", lambda: root)
    assert data_agent_web._uploaded_path(str(inside), suffixes={".sqlite"}) == inside.resolve()
    try:
        data_agent_web._uploaded_path(str(outside), suffixes={".sqlite"})
    except Exception as exc:
        assert getattr(exc, "status_code", None) == 400
    else:
        raise AssertionError("outside path was accepted")


def test_agent_source_path_is_scoped(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    inside = tmp_path / "data.csv"
    inside.write_text("a\n1\n", encoding="utf-8")
    assert _allowed_source_path(str(inside)) == str(inside.resolve())
    outside = tmp_path.parent / "outside.csv"
    outside.write_text("a\n1\n", encoding="utf-8")
    try:
        _allowed_source_path(str(outside))
    except ValueError as exc:
        assert "Upload/copy" in str(exc)
    else:
        raise AssertionError("outside source was accepted")
