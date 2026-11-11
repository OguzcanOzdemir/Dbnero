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
