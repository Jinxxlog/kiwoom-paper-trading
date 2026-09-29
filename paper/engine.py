import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP
from typing import Optional

from config.settings import PaperTradingSettings, RiskSettings
from data.watchlist import normalize_stock_code
from db.database import Database


class PaperTradingError(RuntimeError):
    pass


@dataclass(frozen=True)
class PaperFillResult:
    order_id: int
    account_name: str
    stock_code: str
    side: str
    quantity: int
    market_price: int
    fill_price: int
    gross_amount: int
    commission: int
    tax: int
    slippage_amount: int
    realized_pnl: int
    cash_balance: int


def _floor_money(value: Decimal) -> int:
    return int(value.to_integral_value(rounding=ROUND_FLOOR))


def _fill_price(market_price: int, side: str, slippage_rate: Decimal) -> int:
    multiplier = Decimal("1") + slippage_rate
    rounding = ROUND_CEILING
    if side == "sell":
        multiplier = Decimal("1") - slippage_rate
        rounding = ROUND_FLOOR
    return int((Decimal(market_price) * multiplier).to_integral_value(rounding=rounding))


def _parse_decimal(value: object) -> Optional[Decimal]:
    if value is None:
        return None
    text = str(value).strip().replace(",", "").replace("%", "")
    if not text:
        return None
    try:
        return Decimal(text)
    except Exception:
        return None


class PaperTradingEngine:
    def __init__(
        self,
        database: Database,
        settings: PaperTradingSettings,
        risk_settings: Optional[RiskSettings] = None,
        max_price_age_seconds: Optional[int] = None,
    ) -> None:
        for name, value in (
            ("commission_rate", settings.commission_rate),
            ("sell_tax_rate", settings.sell_tax_rate),
            ("slippage_rate", settings.slippage_rate),
        ):
            if value < 0 or value >= 1:
                raise ValueError(f"{name}는 0 이상 1 미만이어야 합니다.")
        self.database = database
        self.settings = settings
        self.risk_settings = risk_settings
        if max_price_age_seconds is not None and max_price_age_seconds < 1:
            raise ValueError("최대 가격 경과시간은 1초 이상이어야 합니다.")
        self.max_price_age_seconds = max_price_age_seconds
        if risk_settings is not None:
            for name, value in (
                ("stop_loss_rate", risk_settings.stop_loss_rate),
                ("take_profit_rate", risk_settings.take_profit_rate),
                ("trailing_activation_rate", risk_settings.trailing_activation_rate),
                ("trailing_drawdown_rate", risk_settings.trailing_drawdown_rate),
                ("daily_max_loss_rate", risk_settings.daily_max_loss_rate),
                ("max_entry_gain_rate", risk_settings.max_entry_gain_rate),
            ):
                if value < 0 or value >= 1:
                    raise ValueError(f"{name}는 0 이상 1 미만이어야 합니다.")
            if risk_settings.max_consecutive_losses < 0:
                raise ValueError("max_consecutive_losses는 0 이상이어야 합니다.")
            if risk_settings.max_daily_stop_losses < 0:
                raise ValueError("max_daily_stop_losses는 0 이상이어야 합니다.")
            if risk_settings.stop_loss_cooldown_minutes < 0:
                raise ValueError("stop_loss_cooldown_minutes는 0 이상이어야 합니다.")
            if risk_settings.max_positions < 1:
                raise ValueError("max_positions는 1 이상이어야 합니다.")
            if (
                risk_settings.max_position_rate <= 0
                or risk_settings.max_position_rate > 1
            ):
                raise ValueError("max_position_rate는 0 초과 1 이하여야 합니다.")

    def create_account(
        self,
        name: str = "default",
        initial_cash: Optional[int] = None,
    ) -> int:
        normalized_name = name.strip()
        if not normalized_name:
            raise ValueError("계좌 이름은 비어 있을 수 없습니다.")
        cash = self.settings.initial_cash if initial_cash is None else initial_cash
        if cash < 0:
            raise ValueError("초기 자금은 0원 이상이어야 합니다.")
        timestamp = datetime.now().isoformat(timespec="seconds")
        with self.database.connect() as connection:
            try:
                cursor = connection.execute(
                    """
                    INSERT INTO paper_accounts(
                        name, initial_cash, cash_balance, realized_pnl,
                        created_at, updated_at
                    )
                    VALUES (?, ?, ?, 0, ?, ?)
                    """,
                    (normalized_name, cash, cash, timestamp, timestamp),
                )
            except Exception as exc:
                if "UNIQUE constraint failed" in str(exc):
                    raise PaperTradingError(
                        f"이미 존재하는 모의계좌입니다: {normalized_name}"
                    ) from exc
                raise
            return int(cursor.lastrowid)

    def place_market_order(
        self,
        account_name: str,
        stock_code: str,
        side: str,
        quantity: int,
        *,
        reason: Optional[str] = None,
        client_order_id: Optional[str] = None,
        risk_override: bool = False,
        reentry_approval_lock_id: Optional[int] = None,
    ) -> PaperFillResult:
        normalized_code = normalize_stock_code(stock_code)
        normalized_side = side.strip().lower()
        if normalized_side not in {"buy", "sell"}:
            raise ValueError("주문 방향은 buy 또는 sell이어야 합니다.")
        if quantity <= 0:
            raise ValueError("주문 수량은 1주 이상이어야 합니다.")
        if client_order_id is not None and not client_order_id.strip():
            raise ValueError("client_order_id는 비어 있을 수 없습니다.")

        price = self.get_trade_price(normalized_code)
        market_price = int(price["price"])
        if market_price <= 0:
            raise PaperTradingError(f"유효하지 않은 시장가격입니다: {market_price}")

        fill_price = _fill_price(
            market_price,
            normalized_side,
            self.settings.slippage_rate,
        )
        if fill_price <= 0:
            raise PaperTradingError(f"슬리피지 적용 체결가가 유효하지 않습니다: {fill_price}")
        gross_amount = fill_price * quantity
        commission = _floor_money(
            Decimal(gross_amount) * self.settings.commission_rate
        )
        if self.settings.commission_rate > 0:
            commission = max(1, commission)
        tax = (
            _floor_money(Decimal(gross_amount) * self.settings.sell_tax_rate)
            if normalized_side == "sell"
            else 0
        )
        slippage_amount = abs(fill_price - market_price) * quantity
        timestamp = datetime.now().isoformat(timespec="seconds")
        normalized_client_order_id = (
            client_order_id.strip() if client_order_id is not None else None
        )

        normalized_account_name = account_name.strip()
        if not normalized_account_name:
            raise ValueError("계좌 이름은 비어 있을 수 없습니다.")

        with self.database.connect() as connection:
            account = connection.execute(
                """
                SELECT id, name, initial_cash, cash_balance, realized_pnl
                FROM paper_accounts
                WHERE name = ?
                """,
                (normalized_account_name,),
            ).fetchone()
            if account is None:
                raise PaperTradingError(
                    f"모의계좌를 찾을 수 없습니다: {normalized_account_name}"
                )

            if normalized_client_order_id is not None:
                duplicate = connection.execute(
                    """
                    SELECT id FROM paper_orders
                    WHERE account_id = ? AND client_order_id = ?
                    """,
                    (account["id"], normalized_client_order_id),
                ).fetchone()
                if duplicate is not None:
                    raise PaperTradingError(
                        f"중복 client_order_id입니다: {normalized_client_order_id}"
                    )

            position = connection.execute(
                """
                SELECT quantity, cost_basis
                FROM paper_positions
                WHERE account_id = ? AND stock_code = ?
                """,
                (account["id"], normalized_code),
            ).fetchone()
            rejection_reason = self._rejection_reason(
                normalized_side,
                quantity,
                gross_amount,
                commission,
                account["cash_balance"],
                position["quantity"] if position is not None else 0,
            )
            if rejection_reason is None and normalized_side == "buy":
                rejection_reason = self._risk_entry_rejection_reason(
                    connection,
                    account["id"],
                    account["initial_cash"],
                    normalized_code,
                    market_price,
                    gross_amount + commission,
                    position is not None,
                    timestamp,
                    str(price["price_time"])[:10],
                    risk_override=risk_override,
                    reentry_approval_lock_id=reentry_approval_lock_id,
                )
            if rejection_reason is not None:
                self._insert_order(
                    connection,
                    account_id=account["id"],
                    client_order_id=normalized_client_order_id,
                    stock_code=normalized_code,
                    side=normalized_side,
                    quantity=quantity,
                    status="rejected",
                    reason=reason,
                    rejection_reason=rejection_reason,
                    timestamp=timestamp,
                )
                connection.commit()
                raise PaperTradingError(rejection_reason)

            order_id = self._insert_order(
                connection,
                account_id=account["id"],
                client_order_id=normalized_client_order_id,
                stock_code=normalized_code,
                side=normalized_side,
                quantity=quantity,
                status="filled",
                reason=reason,
                rejection_reason=None,
                timestamp=timestamp,
            )

            realized_pnl = 0
            if normalized_side == "buy":
                total_cost = gross_amount + commission
                cash_balance = account["cash_balance"] - total_cost
                if position is None:
                    connection.execute(
                        """
                        INSERT INTO paper_positions(
                            account_id, stock_code, quantity, cost_basis, updated_at
                        )
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            account["id"],
                            normalized_code,
                            quantity,
                            total_cost,
                            timestamp,
                        ),
                    )
                else:
                    connection.execute(
                        """
                        UPDATE paper_positions
                        SET quantity = quantity + ?,
                            cost_basis = cost_basis + ?,
                            updated_at = ?
                        WHERE account_id = ? AND stock_code = ?
                        """,
                        (
                            quantity,
                            total_cost,
                            timestamp,
                            account["id"],
                            normalized_code,
                        ),
                    )
                self._upsert_risk_profile(
                    connection,
                    account_id=account["id"],
                    stock_code=normalized_code,
                    market_price=market_price,
                    timestamp=timestamp,
                )
            else:
                position_quantity = int(position["quantity"])
                position_cost = int(position["cost_basis"])
                if quantity == position_quantity:
                    allocated_cost = position_cost
                else:
                    allocated_cost = int(
                        (
                            Decimal(position_cost)
                            * Decimal(quantity)
                            / Decimal(position_quantity)
                        ).to_integral_value(rounding=ROUND_HALF_UP)
                    )
                net_proceeds = gross_amount - commission - tax
                realized_pnl = net_proceeds - allocated_cost
                cash_balance = account["cash_balance"] + net_proceeds
                remaining_quantity = position_quantity - quantity
                if remaining_quantity == 0:
                    connection.execute(
                        """
                        DELETE FROM paper_positions
                        WHERE account_id = ? AND stock_code = ?
                        """,
                        (account["id"], normalized_code),
                    )
                    connection.execute(
                        """
                        DELETE FROM paper_risk_profiles
                        WHERE account_id = ? AND stock_code = ?
                        """,
                        (account["id"], normalized_code),
                    )
                else:
                    connection.execute(
                        """
                        UPDATE paper_positions
                        SET quantity = ?,
                            cost_basis = ?,
                            updated_at = ?
                        WHERE account_id = ? AND stock_code = ?
                        """,
                        (
                            remaining_quantity,
                            position_cost - allocated_cost,
                            timestamp,
                            account["id"],
                            normalized_code,
                        ),
                    )

            connection.execute(
                """
                UPDATE paper_accounts
                SET cash_balance = ?,
                    realized_pnl = realized_pnl + ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (cash_balance, realized_pnl, timestamp, account["id"]),
            )
            connection.execute(
                """
                INSERT INTO paper_fills(
                    order_id, stock_code, side, quantity, market_price,
                    fill_price, gross_amount, commission, tax,
                    slippage_amount, realized_pnl, price_time, filled_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    order_id,
                    normalized_code,
                    normalized_side,
                    quantity,
                    market_price,
                    fill_price,
                    gross_amount,
                    commission,
                    tax,
                    slippage_amount,
                    realized_pnl,
                    price["price_time"],
                    timestamp,
                ),
            )

        return PaperFillResult(
            order_id=order_id,
            account_name=normalized_account_name,
            stock_code=normalized_code,
            side=normalized_side,
            quantity=quantity,
            market_price=market_price,
            fill_price=fill_price,
            gross_amount=gross_amount,
            commission=commission,
            tax=tax,
            slippage_amount=slippage_amount,
            realized_pnl=realized_pnl,
            cash_balance=cash_balance,
        )

    def get_trade_price(
        self,
        stock_code: str,
        *,
        now: Optional[datetime] = None,
    ) -> object:
        price = self.database.get_latest_price(stock_code)
        if price is None:
            raise PaperTradingError(f"저장된 가격이 없습니다: {stock_code}")
        self.ensure_fresh_price(price["price_time"], stock_code, now=now)
        return price

    def ensure_fresh_price(
        self,
        price_time: str,
        stock_code: str,
        *,
        now: Optional[datetime] = None,
    ) -> None:
        if self.max_price_age_seconds is None:
            return
        try:
            collected_at = datetime.fromisoformat(str(price_time))
        except ValueError as exc:
            raise PaperTradingError(
                f"가격 시각을 해석할 수 없습니다: {stock_code} {price_time}"
            ) from exc
        current = now or datetime.now()
        if current.tzinfo is not None and collected_at.tzinfo is None:
            current = current.replace(tzinfo=None)
        elif current.tzinfo is None and collected_at.tzinfo is not None:
            collected_at = collected_at.replace(tzinfo=None)
        elif current.tzinfo is not None and collected_at.tzinfo is not None:
            current = current.astimezone(collected_at.tzinfo)
        age_seconds = max(
            0,
            int((current - collected_at).total_seconds()),
        )
        if age_seconds > self.max_price_age_seconds:
            age = str(timedelta(seconds=age_seconds))
            raise PaperTradingError(
                f"오래된 가격으로 거래할 수 없습니다: {stock_code} "
                f"가격시각={price_time}, 경과={age}, "
                f"허용={self.max_price_age_seconds}초"
            )

    def _risk_entry_rejection_reason(
        self,
        connection: object,
        account_id: int,
        initial_cash: int,
        stock_code: str,
        market_price: int,
        order_cost: int,
        has_position: bool,
        timestamp: str,
        price_day: str,
        *,
        risk_override: bool = False,
        reentry_approval_lock_id: Optional[int] = None,
    ) -> Optional[str]:
        pending_reentry_lock = connection.execute(
            """
            SELECT id, reason, setup_version
            FROM reentry_locks
            WHERE stock_code = ?
              AND status = 'active'
              AND datetime(created_at) <= datetime(?)
            ORDER BY created_at DESC, id DESC
            LIMIT 1
            """,
            (stock_code, timestamp),
        ).fetchone()
        if (
            pending_reentry_lock is not None
            and int(pending_reentry_lock["id"])
            != int(reentry_approval_lock_id or 0)
        ):
            return (
                "재진입 엔진 승인이 필요합니다: "
                f"잠금={pending_reentry_lock['id']}, "
                f"버전={pending_reentry_lock['setup_version']}, "
                f"사유={pending_reentry_lock['reason']}"
            )
        if self.risk_settings is None:
            return None
        day = timestamp[:10]
        daily_realized = connection.execute(
            """
            SELECT COALESCE(SUM(f.realized_pnl), 0)
            FROM paper_fills f
            JOIN paper_orders o ON o.id = f.order_id
            WHERE o.account_id = ?
              AND f.side = 'sell'
              AND substr(f.filled_at, 1, 10) = ?
            """,
            (account_id, day),
        ).fetchone()[0]
        loss_limit = int(
            Decimal(initial_cash) * self.risk_settings.daily_max_loss_rate
        )
        if daily_realized <= -loss_limit and not risk_override:
            return (
                f"일일 최대 손실 제한에 도달했습니다: "
                f"실현손익={daily_realized:,}원, 제한=-{loss_limit:,}원"
            )

        if self.risk_settings.max_daily_stop_losses > 0:
            stop_loss_count = connection.execute(
                """
                SELECT COUNT(*)
                FROM paper_risk_events
                WHERE account_id = ?
                  AND event_type = 'stop_loss'
                  AND status = 'executed'
                  AND substr(occurred_at, 1, 10) = ?
                """,
                (account_id, day),
            ).fetchone()[0]
            if (
                stop_loss_count >= self.risk_settings.max_daily_stop_losses
                and not risk_override
            ):
                return (
                    "일중 손절 횟수 한도에 도달했습니다: "
                    f"손절={stop_loss_count}회, "
                    f"제한={self.risk_settings.max_daily_stop_losses}회"
                )

        if (
            not has_position
            and self.risk_settings.stop_loss_cooldown_minutes > 0
            and not risk_override
        ):
            recent_stop = connection.execute(
                """
                SELECT occurred_at
                FROM paper_risk_events
                WHERE account_id = ?
                  AND stock_code = ?
                  AND event_type = 'stop_loss'
                  AND status = 'executed'
                ORDER BY occurred_at DESC, id DESC
                LIMIT 1
                """,
                (account_id, stock_code),
            ).fetchone()
            if recent_stop is not None:
                last_stop = datetime.fromisoformat(recent_stop["occurred_at"])
                current = datetime.fromisoformat(timestamp)
                if current.tzinfo is not None and last_stop.tzinfo is None:
                    current = current.replace(tzinfo=None)
                elif current.tzinfo is None and last_stop.tzinfo is not None:
                    last_stop = last_stop.replace(tzinfo=None)
                elif current.tzinfo is not None and last_stop.tzinfo is not None:
                    current = current.astimezone(last_stop.tzinfo)
                elapsed_minutes = (current - last_stop).total_seconds() / 60
                if elapsed_minutes < self.risk_settings.stop_loss_cooldown_minutes:
                    remaining = int(
                        Decimal(
                            str(
                                self.risk_settings.stop_loss_cooldown_minutes
                                - elapsed_minutes
                            )
                        ).to_integral_value(rounding=ROUND_CEILING)
                    )
                    return (
                        "손절 후 재진입 대기 중입니다: "
                        f"남은시간={remaining}분, "
                        f"대기={self.risk_settings.stop_loss_cooldown_minutes}분"
                    )

        if self.risk_settings.max_consecutive_losses > 0 and not risk_override:
            recent = connection.execute(
                """
                SELECT f.realized_pnl
                FROM paper_fills f
                JOIN paper_orders o ON o.id = f.order_id
                WHERE o.account_id = ?
                  AND f.side = 'sell'
                  AND substr(f.filled_at, 1, 10) = ?
                ORDER BY f.id DESC
                LIMIT ?
                """,
                (account_id, day, self.risk_settings.max_consecutive_losses),
            ).fetchall()
            if (
                len(recent) >= self.risk_settings.max_consecutive_losses
                and all(row["realized_pnl"] < 0 for row in recent)
            ):
                return (
                    f"연속 손실 {self.risk_settings.max_consecutive_losses}회로 "
                    "추가 매수를 중단합니다."
                )

        positions = connection.execute(
            """
            SELECT stock_code, quantity, cost_basis
            FROM paper_positions
            WHERE account_id = ?
            """,
            (account_id,),
        ).fetchall()
        if not has_position and len(positions) >= self.risk_settings.max_positions:
            return (
                f"동시 보유 종목 제한에 도달했습니다: "
                f"보유={len(positions)}개, 제한={self.risk_settings.max_positions}개"
            )

        total_equity = connection.execute(
            "SELECT cash_balance FROM paper_accounts WHERE id = ?",
            (account_id,),
        ).fetchone()[0]
        current_position_value = 0
        for position in positions:
            latest = self._latest_price_with_connection(
                connection,
                position["stock_code"],
            )
            position_value = (
                latest * position["quantity"]
                if latest is not None
                else position["cost_basis"]
            )
            total_equity += position_value
            if position["stock_code"] == stock_code:
                current_position_value = position_value
        position_limit = int(
            Decimal(total_equity) * self.risk_settings.max_position_rate
        )
        if current_position_value + order_cost > position_limit:
            return (
                f"종목당 투자 한도를 초과합니다: "
                f"주문 후={current_position_value + order_cost:,}원, "
                f"한도={position_limit:,}원"
            )

        intraday_gain_rate = self._latest_intraday_gain_rate(
            connection,
            stock_code,
            market_price,
        )
        if intraday_gain_rate is not None:
            if (
                self.risk_settings.max_entry_gain_rate > Decimal("0")
                and intraday_gain_rate > self.risk_settings.max_entry_gain_rate
            ):
                return (
                    f"급등주 추격 제한을 초과했습니다: "
                    f"상승률={intraday_gain_rate:.2%}, "
                    f"제한={self.risk_settings.max_entry_gain_rate:.2%}"
                )
            return None

        previous_close = connection.execute(
            """
            SELECT close_price
            FROM candles
            WHERE stock_code = ?
              AND candle_type = 'daily'
              AND candle_time < ?
              AND close_price IS NOT NULL
            ORDER BY candle_time DESC
            LIMIT 1
            """,
            (stock_code, price_day),
        ).fetchone()
        if previous_close is not None and previous_close["close_price"] > 0:
            gain_rate = (
                Decimal(market_price) / Decimal(previous_close["close_price"])
            ) - Decimal("1")
            if (
                self.risk_settings.max_entry_gain_rate > Decimal("0")
                and gain_rate > self.risk_settings.max_entry_gain_rate
            ):
                return (
                    f"급등주 추격 제한을 초과했습니다: "
                    f"상승률={gain_rate:.2%}, "
                    f"제한={self.risk_settings.max_entry_gain_rate:.2%}"
                )
        return None

    @staticmethod
    def _latest_intraday_gain_rate(
        connection: object,
        stock_code: str,
        market_price: int,
    ) -> Optional[Decimal]:
        row = connection.execute(
            """
            SELECT a.payload_json
            FROM price_snapshots AS p
            JOIN api_snapshots AS a ON a.id = p.raw_snapshot_id
            WHERE p.stock_code = ?
              AND p.current_price IS NOT NULL
            ORDER BY p.collected_at DESC, p.id DESC
            LIMIT 1
            """,
            (stock_code,),
        ).fetchone()
        if row is None:
            return None
        try:
            payload = json.loads(row["payload_json"])
        except (TypeError, ValueError):
            return None
        if not isinstance(payload, dict):
            return None

        for key in ("flu_rt", "change_rate", "fluctuation_rate"):
            rate = _parse_decimal(payload.get(key))
            if rate is not None:
                return rate / Decimal("100")

        previous_diff = _parse_decimal(payload.get("pre"))
        if previous_diff is None:
            return None
        previous_close = Decimal(market_price) - previous_diff
        if previous_close <= 0:
            return None
        return (Decimal(market_price) / previous_close) - Decimal("1")

    @staticmethod
    def _latest_price_with_connection(
        connection: object,
        stock_code: str,
    ) -> Optional[int]:
        row = connection.execute(
            """
            SELECT current_price AS price
            FROM price_snapshots
            WHERE stock_code = ? AND current_price IS NOT NULL
            ORDER BY collected_at DESC, id DESC
            LIMIT 1
            """,
            (stock_code,),
        ).fetchone()
        if row is not None:
            return int(row["price"])
        row = connection.execute(
            """
            SELECT close_price AS price
            FROM candles
            WHERE stock_code = ? AND close_price IS NOT NULL
            ORDER BY candle_time DESC, id DESC
            LIMIT 1
            """,
            (stock_code,),
        ).fetchone()
        return int(row["price"]) if row is not None else None

    def _upsert_risk_profile(
        self,
        connection: object,
        *,
        account_id: int,
        stock_code: str,
        market_price: int,
        timestamp: str,
    ) -> None:
        if self.risk_settings is None:
            return
        connection.execute(
            """
            INSERT INTO paper_risk_profiles(
                account_id, stock_code, stop_loss_rate, take_profit_rate,
                trailing_activation_rate, trailing_drawdown_rate,
                highest_price, trailing_active, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
            ON CONFLICT(account_id, stock_code) DO UPDATE SET
                highest_price = MAX(highest_price, excluded.highest_price),
                updated_at = excluded.updated_at
            """,
            (
                account_id,
                stock_code,
                float(self.risk_settings.stop_loss_rate),
                float(self.risk_settings.take_profit_rate),
                float(self.risk_settings.trailing_activation_rate),
                float(self.risk_settings.trailing_drawdown_rate),
                market_price,
                timestamp,
                timestamp,
            ),
        )

    @staticmethod
    def _rejection_reason(
        side: str,
        quantity: int,
        gross_amount: int,
        commission: int,
        cash_balance: int,
        position_quantity: int,
    ) -> Optional[str]:
        if side == "buy" and gross_amount + commission > cash_balance:
            return (
                f"주문 가능 현금이 부족합니다: 필요={gross_amount + commission:,}원, "
                f"보유={cash_balance:,}원"
            )
        if side == "sell" and quantity > position_quantity:
            return (
                f"보유 수량이 부족합니다: 주문={quantity:,}주, "
                f"보유={position_quantity:,}주"
            )
        return None

    @staticmethod
    def _insert_order(
        connection: object,
        *,
        account_id: int,
        client_order_id: Optional[str],
        stock_code: str,
        side: str,
        quantity: int,
        status: str,
        reason: Optional[str],
        rejection_reason: Optional[str],
        timestamp: str,
    ) -> int:
        cursor = connection.execute(
            """
            INSERT INTO paper_orders(
                account_id, client_order_id, stock_code, side, order_type,
                quantity, status, reason, rejection_reason,
                requested_at, completed_at
            )
            VALUES (?, ?, ?, ?, 'market', ?, ?, ?, ?, ?, ?)
            """,
            (
                account_id,
                client_order_id,
                stock_code,
                side,
                quantity,
                status,
                reason,
                rejection_reason,
                timestamp,
                timestamp,
            ),
        )
        return int(cursor.lastrowid)

    def account_summary(self, account_name: str) -> dict:
        with self.database.connect() as connection:
            account = connection.execute(
                "SELECT * FROM paper_accounts WHERE name = ?",
                (account_name,),
            ).fetchone()
            if account is None:
                raise PaperTradingError(f"모의계좌를 찾을 수 없습니다: {account_name}")
            positions = connection.execute(
                """
                SELECT stock_code, quantity, cost_basis, updated_at
                FROM paper_positions
                WHERE account_id = ?
                ORDER BY stock_code
                """,
                (account["id"],),
            ).fetchall()

        position_data = []
        market_value = 0
        unrealized_pnl = 0
        for position in positions:
            price = self.database.get_latest_price(position["stock_code"])
            current_price = int(price["price"]) if price is not None else None
            value = (
                current_price * position["quantity"]
                if current_price is not None
                else None
            )
            pnl = value - position["cost_basis"] if value is not None else None
            if value is not None:
                market_value += value
                unrealized_pnl += pnl
            position_data.append(
                {
                    "stock_code": position["stock_code"],
                    "quantity": position["quantity"],
                    "cost_basis": position["cost_basis"],
                    "average_price": (
                        position["cost_basis"] / position["quantity"]
                    ),
                    "current_price": current_price,
                    "market_value": value,
                    "unrealized_pnl": pnl,
                }
            )
        return {
            "name": account["name"],
            "initial_cash": account["initial_cash"],
            "cash_balance": account["cash_balance"],
            "realized_pnl": account["realized_pnl"],
            "market_value": market_value,
            "unrealized_pnl": unrealized_pnl,
            "total_equity": account["cash_balance"] + market_value,
            "positions": position_data,
        }

    def list_orders(self, account_name: str, limit: int = 20) -> list[dict]:
        if limit < 1:
            raise ValueError("조회 건수는 1 이상이어야 합니다.")
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT o.id, o.stock_code, o.side, o.quantity, o.status,
                       o.reason, o.rejection_reason, o.requested_at,
                       f.fill_price, f.commission, f.tax, f.realized_pnl
                FROM paper_orders o
                JOIN paper_accounts a ON a.id = o.account_id
                LEFT JOIN paper_fills f ON f.order_id = o.id
                WHERE a.name = ?
                ORDER BY o.id DESC
                LIMIT ?
                """,
                (account_name, limit),
            ).fetchall()
            return [dict(row) for row in rows]

    def list_fills(self, account_name: str, limit: int = 20) -> list[dict]:
        if limit < 1:
            raise ValueError("조회 건수는 1 이상이어야 합니다.")
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT f.id, f.order_id, f.stock_code, f.side, f.quantity,
                       f.market_price, f.fill_price, f.gross_amount,
                       f.commission, f.tax, f.slippage_amount,
                       f.realized_pnl, f.price_time, f.filled_at
                FROM paper_fills f
                JOIN paper_orders o ON o.id = f.order_id
                JOIN paper_accounts a ON a.id = o.account_id
                WHERE a.name = ?
                ORDER BY f.id DESC
                LIMIT ?
                """,
                (account_name, limit),
            ).fetchall()
            return [dict(row) for row in rows]
