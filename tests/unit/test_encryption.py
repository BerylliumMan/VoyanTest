"""Tests for app/security/encryption.py — Fernet encrypt/decrypt."""
from unittest.mock import patch, MagicMock
import pytest
from app.security.encryption import encrypt_value, decrypt_value, get_fernet


class TestEncryptDecrypt:
    @pytest.fixture(autouse=True)
    def _hermetic_key(self, monkeypatch):
        """本次测试自给密钥，不依赖机器上 ``data/.db_encryption_key`` 的属主。

        该文件被有意设为 0600（``app/security/encryption.py`` 显式 chmod），当
        pytest 以非属主 uid 运行时它不可读，会让本类用例因 PermissionError 失败 ——
        那是环境问题，不该表现为「加密功能回归」。绝不能为此放开密钥权限；
        ``DB_ENCRYPTION_KEY`` 优先级最高，注入后完全不会碰磁盘上的密钥。
        """
        from cryptography.fernet import Fernet

        monkeypatch.setenv("DB_ENCRYPTION_KEY", Fernet.generate_key().decode())

    def test_roundtrip(self):
        plain = "test-value-123"
        encrypted = encrypt_value(plain)
        assert encrypted != plain
        assert encrypted.startswith("gAAAAA")
        decrypted = decrypt_value(encrypted)
        assert decrypted == plain

    def test_decrypt_plaintext_returns_as_is(self):
        plain = "not-a-fernet-token"
        assert decrypt_value(plain) == plain

    def test_decrypt_empty_string(self):
        assert decrypt_value("") == ""

    def test_multiple_roundtrips_different(self):
        v1 = encrypt_value("value1")
        v2 = encrypt_value("value2")
        assert v1 != v2
        assert decrypt_value(v1) == "value1"
        assert decrypt_value(v2) == "value2"

    def test_long_value_roundtrip(self):
        long_val = "x" * 1000
        assert decrypt_value(encrypt_value(long_val)) == long_val


class TestGetFernet:
    @patch.dict("os.environ", {}, clear=True)
    def test_from_env_var(self):
        from cryptography.fernet import Fernet as F
        valid_key = F.generate_key().decode()
        with patch.dict("os.environ", {"DB_ENCRYPTION_KEY": valid_key}, clear=True):
            f = get_fernet()
            assert f is not None
            token = f.encrypt(b"test")
            assert f.decrypt(token) == b"test"

    @patch.dict("os.environ", {}, clear=True)
    @patch("app.security.encryption.KEY_FILE")
    def test_from_key_file(self, mock_key_file):
        from cryptography.fernet import Fernet as F
        valid_key = F.generate_key().decode()
        mock_key_file.exists.return_value = True
        mock_key_file.read_text.return_value = valid_key
        f = get_fernet()
        assert f is not None

    @patch.dict("os.environ", {}, clear=True)
    @patch("app.security.encryption.KEY_FILE")
    @patch("app.security.encryption._LEGACY_KEY_FILE")
    @patch("app.security.encryption.warnings")
    def test_auto_generate(self, mock_warn, mock_legacy_key_file, mock_key_file):
        mock_key_file.exists.return_value = False
        mock_legacy_key_file.exists.return_value = False
        f = get_fernet()
        assert f is not None
        mock_key_file.write_text.assert_called_once()
        mock_warn.warn.assert_called_once()
