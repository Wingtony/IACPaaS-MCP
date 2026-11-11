"""User-authorized account storage, encrypted for the current Windows user."""
import ctypes
import json
import os
from ctypes import wintypes
from pathlib import Path

from .paths import DATA
ACCOUNT_FILE = DATA / "account.dpapi"
KEY_FILE = DATA / "secrets.key"


class Blob(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]


def protect(data, decrypt=False):
    if os.name != "nt":
        from cryptography.fernet import Fernet, InvalidToken
        import stat
        KEY_FILE.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not KEY_FILE.exists():
            if decrypt:
                raise ValueError("Нет локального ключа хранилища")
            try:
                fd = os.open(KEY_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "wb") as handle: handle.write(Fernet.generate_key())
            except FileExistsError:
                pass
        info = KEY_FILE.lstat()
        if stat.S_ISLNK(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o077 or info.st_uid != os.getuid():
            raise ValueError("Ключ должен принадлежать текущему пользователю и иметь права 0600")
        try:
            cipher = Fernet(KEY_FILE.read_bytes())
            return cipher.decrypt(data) if decrypt else cipher.encrypt(data)
        except (InvalidToken, ValueError):
            raise ValueError("Не удалось прочитать локальное зашифрованное хранилище") from None
    library = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    buffer = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    target = Blob()
    if decrypt:
        function = library.CryptUnprotectData
        function.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                             ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    else:
        function = library.CryptProtectData
        function.argtypes = [ctypes.POINTER(Blob), wintypes.LPCWSTR, ctypes.c_void_p, ctypes.c_void_p,
                             ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    function.restype = wintypes.BOOL
    # CRYPTPROTECT_UI_FORBIDDEN; DPAPI binds the file to this Windows account.
    if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
        raise ValueError("Windows не удалось обработать зашифрованные данные аккаунта")
    try:
        return ctypes.string_at(target.data, target.size)
    finally:
        kernel.LocalFree.argtypes = [ctypes.c_void_p]
        kernel.LocalFree.restype = ctypes.c_void_p
        kernel.LocalFree(target.data)


def load_account():
    if not ACCOUNT_FILE.exists():
        return {}
    try:
        return json.loads(protect(ACCOUNT_FILE.read_bytes(), decrypt=True).decode("utf-8"))
    except (ValueError, OSError):
        raise ValueError("Не удалось открыть сохранённый аккаунт. Войдите заново под текущим пользователем ОС.") from None


def save_account(username, password, token):
    payload = json.dumps({"username":username,"password":password,"token":token},ensure_ascii=False).encode()
    encrypted = protect(payload)
    ACCOUNT_FILE.parent.mkdir(parents=True, exist_ok=True)
    temp = ACCOUNT_FILE.with_suffix(".tmp")
    temp.write_bytes(encrypted)
    if os.name != "nt": temp.chmod(0o600)
    temp.replace(ACCOUNT_FILE)


def account_status():
    account = load_account()
    return {"username":account.get("username", ""), "password_saved":bool(account.get("password")),
            "connected":bool(account.get("token"))}
