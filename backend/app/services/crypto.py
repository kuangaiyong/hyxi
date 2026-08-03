"""数据源凭据的对称加密。

密钥来自 TWEAKERS_SECRET_KEY（.env 已 gitignore）。没配密钥时保存凭据直接报错，
**不静默降级成明文**：论坛账号密码被盗的后果远大于一个可随时轮换的 LLM key，
"先跑起来以后再加密"意味着明文会一直留在 hyxi.db 里没人再想起来。
"""

from cryptography.fernet import Fernet, InvalidToken

from app.config import settings

_KEY_HINT = (
    '未配置 TWEAKERS_SECRET_KEY，无法加密保存凭据。请在项目根 .env 中设置，密钥用这条命令生成：\n'
    '  python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"'
)


def _fernet() -> Fernet:
    if not settings.secret_key:
        raise ValueError(_KEY_HINT)
    try:
        return Fernet(settings.secret_key.encode())
    except (ValueError, TypeError):
        raise ValueError(
            "TWEAKERS_SECRET_KEY 不是合法的 Fernet 密钥（需 32 字节 urlsafe base64）。\n"
            '  python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"'
        )


def encrypt(plaintext: str) -> str:
    return _fernet().encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt(ciphertext: str) -> str:
    try:
        return _fernet().decrypt(ciphertext.encode("ascii")).decode("utf-8")
    except InvalidToken:
        raise ValueError("凭据解密失败：TWEAKERS_SECRET_KEY 与保存时的密钥不一致，请重新录入凭据")


def is_configured() -> bool:
    return bool(settings.secret_key)
