from __future__ import annotations

import io
import zipfile
from typing import Any
from xml.etree import ElementTree

import requests


class DartClient:
    BASE_URL = "https://opendart.fss.or.kr/api"

    def __init__(self, api_key: str, *, timeout: float = 10.0) -> None:
        self.api_key = api_key
        self.timeout = timeout

    def companies(self) -> list[dict[str, str | None]]:
        response = requests.get(
            f"{self.BASE_URL}/corpCode.xml",
            params={"crtfc_key": self.api_key},
            timeout=self.timeout,
        )
        response.raise_for_status()
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            xml_data = archive.read(archive.namelist()[0])
        root = ElementTree.fromstring(xml_data)
        return [
            {
                "corp_code": _xml_text(item, "corp_code"),
                "corp_name": _xml_text(item, "corp_name"),
                "corp_eng_name": _xml_text(item, "corp_eng_name") or None,
                "stock_code": _xml_text(item, "stock_code") or None,
                "modify_date": _xml_text(item, "modify_date") or None,
            }
            for item in root.findall("list")
        ]

    def disclosures(
        self,
        *,
        corp_code: str,
        begin_date: str,
        end_date: str,
        page_count: int = 100,
    ) -> list[dict[str, Any]]:
        page_no = 1
        items: list[dict[str, Any]] = []
        while True:
            response = requests.get(
                f"{self.BASE_URL}/list.json",
                params={
                    "crtfc_key": self.api_key,
                    "corp_code": corp_code,
                    "bgn_de": begin_date,
                    "end_de": end_date,
                    "page_no": page_no,
                    "page_count": page_count,
                },
                timeout=self.timeout,
            )
            response.raise_for_status()
            payload = response.json()
            status = str(payload.get("status", ""))
            if status == "013":
                return items
            if status != "000":
                raise RuntimeError(
                    f"OpenDART 오류 {status}: {payload.get('message', '알 수 없음')}"
                )
            items.extend(payload.get("list") or [])
            total_page = int(payload.get("total_page") or 1)
            if page_no >= total_page:
                return items
            page_no += 1


class NaverNewsClient:
    BASE_URL = "https://openapi.naver.com/v1/search/news.json"

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        *,
        timeout: float = 10.0,
    ) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.timeout = timeout

    def search(
        self,
        query: str,
        *,
        display: int = 20,
        start: int = 1,
        sort: str = "date",
    ) -> list[dict[str, Any]]:
        response = requests.get(
            self.BASE_URL,
            headers={
                "X-Naver-Client-Id": self.client_id,
                "X-Naver-Client-Secret": self.client_secret,
            },
            params={
                "query": query,
                "display": display,
                "start": start,
                "sort": sort,
            },
            timeout=self.timeout,
        )
        response.raise_for_status()
        return list(response.json().get("items") or [])


def _xml_text(node: ElementTree.Element, name: str) -> str:
    return (node.findtext(name) or "").strip()
