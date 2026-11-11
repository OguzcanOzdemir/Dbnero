from dbnero.core.settings import Settings, load_settings, save_settings


def test_roundtrip(tmp_path):
    p = tmp_path / "s.json"
    s = Settings(archive_root=str(tmp_path / "a"), tool_paths={"pg_dump": "/x"})
    save_settings(s, p)
    loaded = load_settings(p)
    assert loaded == s
    assert loaded.resolved_archive_root() == tmp_path / "a"


def test_defaults_on_missing_or_corrupt(tmp_path):
    assert load_settings(tmp_path / "none.json") == Settings()
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert load_settings(bad) == Settings()


def test_unknown_keys_ignored(tmp_path):
    p = tmp_path / "s.json"
    p.write_text('{"max_parallel_jobs": 4, "removed_option": true}')
    assert load_settings(p).max_parallel_jobs == 4
