import json
import logging
from dataclasses import dataclass
from typing import Any
from urllib import parse, request
from urllib.error import URLError

from config.settings import TelegramSettings, load_telegram_settings


LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class TelegramNotifier:
    settings: TelegramSettings

    @classmethod
    def from_env(cls) -> "TelegramNotifier":
        return cls(load_telegram_settings())

    @property
    def configured(self) -> bool:
        return bool(
            self.settings.enabled
            and self.settings.bot_token
            and self.settings.chat_id
        )

    def send(self, text: str) -> bool:
        if not self.configured:
            return False
        payload = parse.urlencode(
            {
                "chat_id": self.settings.chat_id,
                "text": text,
                "disable_web_page_preview": "true",
            }
        ).encode("utf-8")
        url = (
            "https://api.telegram.org/bot"
            f"{self.settings.bot_token}/sendMessage"
        )
        try:
            req = request.Request(url, data=payload, method="POST")
            with request.urlopen(req, timeout=self.settings.request_timeout) as response:
                raw = response.read().decode("utf-8")
        except (OSError, URLError) as exc:
            LOGGER.warning("텔레그램 알림 전송 실패: %s", exc)
            return False
        try:
            body: dict[str, Any] = json.loads(raw)
        except json.JSONDecodeError:
            LOGGER.warning("텔레그램 알림 응답 파싱 실패")
            return False
        if not body.get("ok"):
            LOGGER.warning("텔레그램 알림 API 오류: %s", body.get("description"))
            return False
        return True
