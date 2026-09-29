import json

import requests

from config.settings import load_settings


def request_access_token(app_key: str, secret_key: str, use_mock: bool = False) -> dict:
    host = "https://mockapi.kiwoom.com" if use_mock else "https://api.kiwoom.com"
    endpoint = "/oauth2/token"
    url = host + endpoint

    headers = {
        "Content-Type": "application/json;charset=UTF-8",
    }
    payload = {
        "grant_type": "client_credentials",
        "appkey": app_key,
        "secretkey": secret_key,
    }

    response = requests.post(url, headers=headers, json=payload)
    response.raise_for_status()
    return response.json()


def main() -> None:
    settings = load_settings()
    print("환경변수에서 API 자격증명을 불러왔습니다.")

    try:
        token_data = request_access_token(
            settings.app_key,
            settings.secret_key,
            use_mock=settings.use_mock,
        )
        print("액세스 토큰 발급 성공")
        print(f"만료 시각: {token_data.get('expires_dt')}")
    except requests.HTTPError as exc:
        print("토큰 발급에 실패했습니다.")
        if exc.response is not None:
            try:
                print(json.dumps(exc.response.json(), indent=4, ensure_ascii=False))
            except ValueError:
                print(exc.response.text)
        else:
            print(str(exc))


if __name__ == "__main__":
    main()
