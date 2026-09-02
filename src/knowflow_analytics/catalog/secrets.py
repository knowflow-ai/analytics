"""数据源连接串的静态加密。

连接串里带着别人数据库的账号密码。目录库被 dump、被备份、被误授权时，这些凭据
不应该跟着一起走——它们打开的是**第三方的库**，不是我们自己的。

上游 SuperSonic 也加密（``AESEncryptionUtil.aesDecryptECB``），但用的是 ECB：同样
的明文块得到同样的密文块，会泄露结构；而且没有完整性校验，密文被改了也发现不了。
这里用 ``cryptography`` 的 Fernet（AES-128-CBC + HMAC-SHA256 + 随机 IV + 时间戳），
成熟实现，不手搓。

密钥从服务密钥派生而不是另配一个：多一个必配的密钥就多一种"忘了配所以线上起不来"
的故障，而服务密钥本来就已经是部署必填、且要求至少 32 字符。派生用 HKDF 加固定
info，保证这里的密钥与服务密钥的其它用途互不相干。
"""

from __future__ import annotations

import base64

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

__all__ = ["DataSourceSecretBox", "SecretDecryptionError"]

# 改这个值会让所有已存的连接串解不开，等同于强制所有数据源重新填写。
_DERIVATION_INFO = b"knowflow-analytics/data-source-dsn/v1"


class SecretDecryptionError(RuntimeError):
    """密文解不开。

    单独一个类型是为了让上层能把它与"数据库连不上"区分开：前者是密钥换了或数据被
    改了，重试没有意义，得让用户重新填连接信息。
    """


class DataSourceSecretBox:
    """加解密数据源连接串。"""

    def __init__(self, service_secret: str) -> None:
        if len(service_secret) < 32:
            # 与服务密钥自身的下限一致。短密钥派生出的密钥同样短命，不如直接拒绝。
            raise ValueError("service secret must contain at least 32 characters")
        derived = HKDF(
            algorithm=hashes.SHA256(),
            length=32,
            salt=None,
            info=_DERIVATION_INFO,
        ).derive(service_secret.encode("utf-8"))
        self._fernet = Fernet(base64.urlsafe_b64encode(derived))

    def encrypt(self, plaintext: str) -> str:
        if not plaintext.strip():
            raise ValueError("data source connection string is required")
        return self._fernet.encrypt(plaintext.encode("utf-8")).decode("ascii")

    def decrypt(self, ciphertext: str) -> str:
        """解密。**失败一律抛错，绝不返回可疑明文。**

        返回原文兜底会让一条没加密的脏数据被当作连接串直接拿去连库。
        """

        try:
            return self._fernet.decrypt(ciphertext.encode("ascii")).decode("utf-8")
        except (InvalidToken, UnicodeDecodeError, ValueError) as exc:
            raise SecretDecryptionError("data source credentials could not be decrypted") from exc
