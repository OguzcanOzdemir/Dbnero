import logging

from dbnero.core.logging_setup import SecretMaskingFilter, mask_secrets


def test_mask_kv():
    s = "Driver=x;Server=h;UID=sa;PWD=Hunter2;Encrypt=yes"
    assert "Hunter2" not in mask_secrets(s)
    assert "password='a b'" not in mask_secrets("password='a b' user=x")


def test_mask_url():
    out = mask_secrets("postgresql://admin:topsecret@db:5432/crm")
    assert "topsecret" not in out and "admin" in out


def test_filter_registered_secret():
    f = SecretMaskingFilter()
    f.register("zz-plain-zz")
    rec = logging.LogRecord("t", logging.INFO, "", 0, "tool said %s", ("zz-plain-zz",), None)
    f.filter(rec)
    assert "zz-plain-zz" not in rec.getMessage()


def test_configure_logging_never_fails(tmp_path, monkeypatch):
    import logging
    import sys

    from dbnero.core.logging_setup import configure_logging

    root = logging.getLogger()
    before = list(root.handlers)
    blocked = tmp_path / "file-not-dir"
    blocked.write_text("x")  # a file where the log folder should be
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path / "tmp"))
    monkeypatch.setattr(sys, "stderr", None)  # windowed app without a console
    try:
        log_file = configure_logging(blocked / "logs")
        assert log_file == tmp_path / "tmp" / "dbnero-logs" / "dbnero.log"
        added = [h for h in root.handlers if h not in before]
        assert len(added) == 1  # file only, no stderr handler
        logging.getLogger("t").warning("hello")
        assert "hello" in log_file.read_text("utf-8")
    finally:
        for h in [h for h in root.handlers if h not in before]:
            root.removeHandler(h)
            h.close()
