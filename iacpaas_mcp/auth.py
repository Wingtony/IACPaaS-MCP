"""Interactive sign-in. Password and JWT are never printed or passed to the LLM."""
import getpass
import json
import sys

import httpx
from dotenv import set_key

from .server import PROJECT
from .paths import ENV_FILE
from .credentials import load_account, save_account


def signin(username, password, transport=None):
    # Credentials go only to the official HTTPS endpoint, without redirects.
    with httpx.Client(timeout=60, follow_redirects=False, transport=transport) as client:
        response = client.post("https://iacpaas.dvo.ru/api/signin", json={"username":username,"password":password})
        if response.status_code != 200:
            raise ValueError(f"Вход отклонён: HTTP {response.status_code}. Проверьте логин и пароль.")
        data = response.json()
    token = data.get("accessToken") if isinstance(data, dict) else None
    if not isinstance(token, str) or not token.strip():
        raise ValueError("Ответ не содержит accessToken. Проверьте доступ аккаунта к API.")
    return token


def connect_account(username, password, paths=None, transport=None):
    username = username.strip()
    if not username:
        raise ValueError("Введите логин IACPaaS")
    if not password:
        saved = load_account()
        if saved.get("username") != username or not saved.get("password"):
            raise ValueError("Введите пароль для выбранного аккаунта")
        password = saved["password"]
    token = signin(username, password, transport=transport)
    save_account(username, password, token)
    env_path = ENV_FILE
    env_path.parent.mkdir(parents=True, exist_ok=True)
    if not env_path.exists():
        env_path.write_text((PROJECT / ".env.example").read_text(encoding="utf-8"), encoding="utf-8")
    for key, value in {"IACPAAS_JWT":"", "IACPAAS_API_KEY":"", "IACPAAS_BACKEND":"http", "IACPAAS_ALLOW_WRITE":"false"}.items():
        set_key(str(env_path), key, value)
    if paths is not None:
        set_key(str(env_path), "IACPAAS_KNOWN_IRS", json.dumps(paths, ensure_ascii=False))
    return {"username":username,"connected":True,"password_saved":True}


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    print("IACPaaS: локальный вход. Пароль отправляется только на https://iacpaas.dvo.ru/api/signin")
    username = input("Логин IACPaaS: ").strip()
    password = getpass.getpass("Пароль (символы не отображаются): ")
    paths_input = input("Пути собственных ИР внутри «Мой Фонд» через точку с запятой (пусто — сохранить прежний список): ")
    paths = [path.strip() for path in paths_input.split(";") if path.strip()] if paths_input.strip() else None
    try:
        connect_account(username, password, paths=paths)
    except (ValueError, httpx.HTTPError):
        raise SystemExit("Вход не выполнен. Проверьте данные аккаунта и доступность платформы.") from None
    finally:
        password = None
    print("Вход выполнен. Логин, пароль и JWT сохраняются в локальном зашифрованном хранилище.")
    print("Настройки сохранены. Перезапустите веб-приложение. Запись в реальную базу выключена.")


if __name__ == "__main__":
    main()
