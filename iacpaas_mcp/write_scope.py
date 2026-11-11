"""Default-deny server boundary for writes, independent of model instructions."""
import json
import os
import re
import unicodedata


def segments(path):
    if not isinstance(path, str) or len(path) > 1000:
        raise ValueError("Нужен путь внутри Мой Фонд")
    path = unicodedata.normalize("NFKC", path).strip().removesuffix("$;").replace("\\", "/")
    parts = [p.strip() for p in path.split("/") if p.strip()]
    keys = [p.casefold().replace("ё", "е") for p in parts]
    if "мой фонд" in keys:
        parts = parts[keys.index("мой фонд") + 1:]
    if any(p in {".", ".."} or re.search(r"[\x00-\x1f%]", p) for p in parts):
        raise ValueError("Недопустимый сегмент пути")
    return parts


def canonical(path):
    return "/".join(segments(path))


def key(path):
    return canonical(path).casefold().replace("ё", "е")


class WriteScope:
    def __init__(self, protected=(), roots=None):
        self.protected = {key(p) for p in protected}
        self.protected.add("загрузки")
        self.roots = [key(p) for p in (roots if roots is not None else json.loads(os.getenv("IACPAAS_WRITE_ROOTS", '[]')))]
        if any(not root or root == "загрузки" or root.startswith("загрузки/") for root in self.roots):
            raise ValueError("Область записи должна быть собственной папкой вне Загрузок")

    def check(self, path, creating_folder=False):
        target = key(path)
        if any(target == p or target.startswith(p + "/") or p.startswith(target + "/") for p in self.protected):
            raise ValueError("Защищённый ресурс или папка: изменение запрещено сервером")
        if not any(target == root or target.startswith(root + "/") for root in self.roots):
            raise ValueError("Путь вне IACPAAS_WRITE_ROOTS: разрешены только собственные папки")
        if not creating_folder and target in self.roots:
            raise ValueError("Корневую папку области записи нельзя заменить или удалить")
        return canonical(path)
