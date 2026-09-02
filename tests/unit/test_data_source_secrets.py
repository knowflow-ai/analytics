"""连接串加密的合同。

这里保护的是**别人数据库的凭据**，所以每一条失败路径都必须是 fail-closed。
"""

from __future__ import annotations

import pytest

from knowflow_analytics.catalog.secrets import (
    DataSourceSecretBox,
    SecretDecryptionError,
)

_SECRET = "local-dev-only-analytics-service-secret-change-me"
_DSN = "postgresql+psycopg://alice:hunter2@10.0.0.7:5432/warehouse"


class TestRoundTrip:
    def test_encrypt_then_decrypt_returns_the_original(self):
        box = DataSourceSecretBox(_SECRET)

        assert box.decrypt(box.encrypt(_DSN)) == _DSN

    def test_non_ascii_survives(self):
        # 库名/账号带中文是会有的，UTF-8 不能在往返里丢。
        box = DataSourceSecretBox(_SECRET)
        dsn = "mysql+pymysql://用户:密码@host/仓库?charset=utf8mb4"

        assert box.decrypt(box.encrypt(dsn)) == dsn

    def test_two_boxes_from_the_same_secret_interoperate(self):
        """必须能跨进程解开。

        密钥派生带随机性的话，重启后所有已存的连接串就都解不开了——而且只会在
        用户下一次提问时才暴露。
        """

        written = DataSourceSecretBox(_SECRET).encrypt(_DSN)

        assert DataSourceSecretBox(_SECRET).decrypt(written) == _DSN


class TestCiphertext:
    def test_ciphertext_does_not_contain_the_plaintext(self):
        box = DataSourceSecretBox(_SECRET)

        ciphertext = box.encrypt(_DSN)

        assert "hunter2" not in ciphertext
        assert "10.0.0.7" not in ciphertext
        assert "warehouse" not in ciphertext

    def test_same_plaintext_encrypts_differently_each_time(self):
        """相同明文不能得到相同密文。

        上游 SuperSonic 用的 AES-ECB 就会——同样的密码在库里长得一模一样，
        谁复用了密码一眼就能看出来。Fernet 带随机 IV。
        """

        box = DataSourceSecretBox(_SECRET)

        assert box.encrypt(_DSN) != box.encrypt(_DSN)


class TestFailClosed:
    def test_a_different_secret_cannot_decrypt(self):
        ciphertext = DataSourceSecretBox(_SECRET).encrypt(_DSN)
        other = DataSourceSecretBox("a-completely-different-service-secret-value!!")

        with pytest.raises(SecretDecryptionError):
            other.decrypt(ciphertext)

    def test_tampered_ciphertext_is_refused(self):
        """密文被改过要报错，而不是解出垃圾。

        Fernet 带 HMAC，所以能发现；ECB 不能——改过的密文会解出一段乱码，然后被
        当成连接串拿去连库。
        """

        box = DataSourceSecretBox(_SECRET)
        ciphertext = box.encrypt(_DSN)
        tampered = ciphertext[:-4] + ("AAAA" if not ciphertext.endswith("AAAA") else "BBBB")

        with pytest.raises(SecretDecryptionError):
            box.decrypt(tampered)

    def test_plaintext_stored_by_mistake_is_refused(self):
        """一条没加密就写进去的脏数据不能被当成连接串用。

        解不开就返回原文兜底，等于把"忘了加密"变成"照样能连"，问题会一直藏着。
        """

        box = DataSourceSecretBox(_SECRET)

        with pytest.raises(SecretDecryptionError):
            box.decrypt(_DSN)

    @pytest.mark.parametrize("value", ["", "   ", "\n"])
    def test_empty_connection_strings_are_refused(self, value: str):
        box = DataSourceSecretBox(_SECRET)

        with pytest.raises(ValueError):
            box.encrypt(value)

    def test_short_service_secret_is_refused(self):
        # 与服务密钥自身的下限一致：短密钥派生出的密钥同样短命。
        with pytest.raises(ValueError):
            DataSourceSecretBox("too-short")
