"""Order execution helpers (marketable limits + synthetic stops)."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Callable, Optional

from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, OrderType, TimeInForce
from alpaca.trading.requests import LimitOrderRequest
from dotenv import load_dotenv

logger = logging.getLogger(__name__)


load_dotenv()


@dataclass
class ExecutionConfig:
    buy_slippage_pct: float = 0.002  # 0.2%
    sell_slippage_pct: float = 0.005  # 0.5%
    min_price: float = 0.01


class ExecutionClient:
    """Thin wrapper around Alpaca TradingClient for marketable orders."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        secret_key: Optional[str] = None,
        *,
        paper: Optional[bool] = None,
        cfg: Optional[ExecutionConfig] = None,
        quote_provider: Optional[Callable[[str], Any]] = None,
        dry_run: bool = False,
    ) -> None:
        api_key = api_key or os.getenv("ALPACA_API_KEY")
        secret_key = secret_key or os.getenv("ALPACA_SECRET_KEY")
        if paper is None:
            paper_env = os.getenv("ALPACA_PAPER")
            if paper_env is not None:
                paper = paper_env.lower() in {"1", "true", "yes", "on"}
            else:
                paper = True
        if not dry_run and (not api_key or not secret_key):
            raise ValueError(
                "ExecutionClient requires Alpaca API credentials (or use dry_run)"
            )

        self.dry_run = dry_run
        self.client = (
            None if dry_run else TradingClient(api_key, secret_key, paper=paper)
        )
        self.cfg = cfg or ExecutionConfig()
        self.quote_provider = quote_provider

    def _reference_price(self, symbol: str, side: OrderSide, fallback: float) -> float:
        if not self.quote_provider:
            return fallback
        try:
            quote = self.quote_provider(symbol)
        except Exception:  # pragma: no cover - network errors
            logger.exception("Failed to fetch quote for %s", symbol)
            return fallback

        if side == OrderSide.BUY:
            ask = float(getattr(quote, "ask_price", 0.0) or 0.0)
            if ask > 0:
                return ask
        else:
            bid = float(getattr(quote, "bid_price", 0.0) or 0.0)
            if bid > 0:
                return bid
        return fallback

    def _marketable_limit(self, symbol: str, side: OrderSide, fallback_price: float) -> float:
        price = self._reference_price(symbol, side, fallback_price)
        if side == OrderSide.BUY:
            slip = self.cfg.buy_slippage_pct
            raw = price * (1 + slip)
            raw = max(raw, price + 0.01)
        else:
            slip = self.cfg.sell_slippage_pct
            raw = price * (1 - slip)
            raw = min(raw, price - 0.01)
        raw = max(raw, self.cfg.min_price)
        return round(raw, 2)

    def place_entry(self, symbol: str, qty: int, last_price: float) -> Optional[str]:
        limit_price = self._marketable_limit(symbol, OrderSide.BUY, last_price)
        return self._submit_limit(symbol, qty, OrderSide.BUY, limit_price)

    def place_exit(self, symbol: str, qty: int, last_price: float) -> Optional[str]:
        limit_price = self._marketable_limit(symbol, OrderSide.SELL, last_price)
        return self._submit_limit(symbol, qty, OrderSide.SELL, limit_price)

    def _submit_limit(
        self, symbol: str, qty: int, side: OrderSide, limit_price: float
    ) -> Optional[str]:
        if qty <= 0:
            return None

        if self.dry_run:
            logger.info(
                "[DRY] %s %s qty %s @ %.2f", side.value, symbol, qty, limit_price
            )
            return None

        tif = TimeInForce.IOC if side == OrderSide.SELL else TimeInForce.DAY
        order = LimitOrderRequest(
            symbol=symbol,
            qty=qty,
            side=side,
            type=OrderType.LIMIT,
            time_in_force=tif,
            limit_price=limit_price,
        )
        resp = self.client.submit_order(order)
        logger.info(
            "Submitted %s %s qty %s @ %.2f order_id=%s",
            side.value,
            symbol,
            qty,
            limit_price,
            resp.id,
        )
        return resp.id
