from config.settings import load_settings
from kiwoom_rest_client import KiwoomRestClient


def main() -> None:
    settings = load_settings()

    client = KiwoomRestClient(
        app_key=settings.app_key,
        secret_key=settings.secret_key,
        use_mock=settings.use_mock,
        token_cache=settings.token_cache_path,
        timeout=settings.request_timeout,
    )
    token_data = client.request_access_token()
    print("== Token ==")
    print(f"발급 성공, 만료 시각: {token_data.get('expires_dt')}")

    print("== Account Number ==")
    acct = client.get_account_number()
    print(acct)

    sample_stock = "A005930"
    print(f"== Hoga ({sample_stock}) ==")
    hoga = client.get_hoga(sample_stock)
    print(hoga)

    print(f"== Daily Prices ({sample_stock}) ==")
    daily = client.get_daily_prices(sample_stock)
    print(daily)

    print(f"== Intraday Prices ({sample_stock}) ==")
    intraday = client.get_intraday_prices(sample_stock)
    print(intraday)

    print("== Deposit Details ==")
    deposit = client.get_deposit_details(qry_tp="0")
    print(deposit)

    print("== Balance Details ==")
    balance = client.get_balance_details()
    print(balance)


if __name__ == "__main__":
    main()
