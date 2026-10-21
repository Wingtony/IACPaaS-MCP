import hashlib
import json
import secrets
import time


class WritePolicy:
    """One-time capability bound to exact arguments, expires after 5 minutes.

    This does not prove human consent. The host MUST obtain that independently.
    """
    def __init__(self, enabled=False, clock=time.monotonic):
        self.enabled = enabled
        self.clock = clock
        self.pending = {}

    @staticmethod
    def digest(operation, args):
        return hashlib.sha256(json.dumps([operation, args], sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    def issue(self, operation, args):
        if not self.enabled:
            raise ValueError("Запись отключена: IACPAAS_ALLOW_WRITE=false")
        now = self.clock()
        self.pending = {key: value for key, value in self.pending.items() if value[1] > now}
        if len(self.pending) >= 100:
            raise ValueError("Слишком много ожидающих подтверждений")
        token = secrets.token_urlsafe(32)
        self.pending[token] = (self.digest(operation, args), now + 300)
        return token

    def consume(self, operation, args, token):
        if not self.enabled:
            raise ValueError("Запись отключена")
        stored = self.pending.pop(token, None)
        if not stored or stored[1] <= self.clock() or stored[0] != self.digest(operation, args):
            raise ValueError("Подтверждение недействительно, использовано или истекло; запросите предпросмотр")
