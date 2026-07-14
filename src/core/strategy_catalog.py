"""策略目录与权重读取。"""

from __future__ import annotations

from dataclasses import dataclass

from src.web.database import SessionLocal
from src.web.models import StrategyCatalog, StrategyWeight


@dataclass(frozen=True)
class StrategySpec:
    code: str
    name: str
    description: str
    version: str = "v1"
    enabled: bool = True
    market_scope: str = "ALL"
    risk_level: str = "medium"
    params: dict | None = None
    default_weight: float = 1.0
    run_config: dict | None = None


DEFAULT_STRATEGIES: tuple[StrategySpec, ...] = (
    StrategySpec(
        code="trend_follow",
        name="趋势延续",
        description="顺势跟随，优先均线多头且动量延续",
        risk_level="medium",
        params={"horizon_days": 5},
        default_weight=1.15,
    ),
    StrategySpec(
        code="macd_golden",
        name="MACD金叉",
        description="MACD 金叉确认，偏中短线",
        risk_level="medium",
        params={"horizon_days": 3},
        default_weight=1.10,
    ),
    StrategySpec(
        code="volume_breakout",
        name="放量突破",
        description="放量突破关键位，偏进攻",
        risk_level="high",
        params={"horizon_days": 3},
        default_weight=1.18,
    ),
    StrategySpec(
        code="pullback",
        name="回踩确认",
        description="回踩支撑后二次启动",
        risk_level="low",
        params={"horizon_days": 5},
        default_weight=1.05,
    ),
    StrategySpec(
        code="price_action",
        name="Price Action",
        description="价格行为策略：突破、回踩确认、趋势结构与 ATR 风控",
        risk_level="medium",
        params={
            "breakout_window": 20,
            "pullback_window": 10,
            "volume_ma_window": 5,
            "volume_ratio": 1.5,
            "atr_n": 14,
            "atr_stop_multiple": 2.0,
            "pullback_tolerance": 0.03,
            "reward_risk_ratio": 2.0,
            "max_stop_pct": 0.08,
            "horizon_days": 5,
        },
        default_weight=1.16,
    ),
    StrategySpec(
        code="base_position_vwap_t",
        name="底仓 VWAP 回归做T",
        description="对已有 A 股底仓生成日内做T提醒(正T低吸/倒T高抛双向),不自动交易",
        market_scope="CN",
        risk_level="medium",
        params={
            "min_score": 70,
            "position_ratio": 0.2,
            "max_cycles_per_day": 5,  # 当日最多做T轮数,0=不限
            "cycle_cooldown_minutes": 3,  # 两轮之间冷却分钟,0=不冷却
            "exit_mode": "price",  # price=固定价 / price_or_score=价格或评分任一 / trail=跟踪止盈
            "trail_pct": 0.003,  # trail 模式:自极值回撤/反弹多少触发离场
            "signal_ttl_minutes": 10,
            "min_vwap_deviation_pct": 0.003,
            "min_profit_pct": 0.008,  # 止盈地板;实际取 max(此值, profit_atr_mult×ATR/价)
            "max_stop_pct": 0.015,  # 止损上限地板;实际取 max(此值, stop_atr_mult×ATR/价)
            "profit_atr_mult": 0.5,  # 止盈的 ATR 倍数(自适应振幅)
            "stop_atr_mult": 0.5,  # 止损上限的 ATR 倍数(自适应振幅)
            "direction": "both",  # both=双向 / long=仅正T / short=仅倒T
        },
        default_weight=1.0,
        run_config={"interval_seconds": 10, "requires_holding": True},
    ),
    StrategySpec(
        code="rebound",
        name="超跌反弹",
        description="超跌后的反弹交易",
        risk_level="high",
        params={"horizon_days": 3},
        default_weight=0.95,
    ),
    StrategySpec(
        code="watchlist_agent",
        name="Agent建议",
        description="来自既有 Agent 的综合建议映射",
        risk_level="medium",
        params={"horizon_days": 3},
        default_weight=1.00,
    ),
    StrategySpec(
        code="market_scan",
        name="市场扫描",
        description="市场池扫描策略（热门与活跃）",
        risk_level="medium",
        params={"horizon_days": 3},
        default_weight=1.08,
    ),
    StrategySpec(
        code="breakout_validity",
        name="突破有效性",
        description="上升趋势突破前高有效性(v3.0):60日前高突破+四路径确认,valid_active买入,失效强平",
        risk_level="medium",
        params={
            "lookback": 60,
            "min_high_age": 5,
            "max_observe": 5,
            "breakout_buffer": 1.005,
            "confirm_days": 3,
            "support_ratio": 0.97,
            "vol_up_ratio": 1.3,
            "vol_down_ratio": 0.8,
            "ext_g0": 1.15,
            "horizon_days": 10,
        },
        default_weight=1.10,
    ),
)


def ensure_strategy_catalog() -> None:
    db = SessionLocal()
    try:
        changed = False
        for spec in DEFAULT_STRATEGIES:
            row = (
                db.query(StrategyCatalog)
                .filter(StrategyCatalog.code == spec.code)
                .first()
            )
            if not row:
                db.add(
                    StrategyCatalog(
                        code=spec.code,
                        name=spec.name,
                        description=spec.description,
                        version=spec.version,
                        enabled=bool(spec.enabled),
                        market_scope=spec.market_scope,
                        risk_level=spec.risk_level,
                        params=spec.params or {},
                        default_weight=float(spec.default_weight),
                        strategy_type="builtin",
                        source_ref_type="",
                        source_ref_id=None,
                        run_config=spec.run_config or {},
                        auto_run_enabled=False,
                    )
                )
                changed = True
                continue
            if not getattr(row, "strategy_type", None):
                row.strategy_type = "builtin"
                changed = True
            if row.strategy_type == "builtin":
                if row.source_ref_type:
                    row.source_ref_type = ""
                    changed = True
                if row.source_ref_id is not None:
                    row.source_ref_id = None
                    changed = True
            if row.name != spec.name:
                row.name = spec.name
                changed = True
            if (row.description or "") != (spec.description or ""):
                row.description = spec.description
                changed = True
            if (row.version or "v1") != (spec.version or "v1"):
                row.version = spec.version
                changed = True
            if (row.market_scope or "ALL") != (spec.market_scope or "ALL"):
                row.market_scope = spec.market_scope
                changed = True
            if (row.risk_level or "medium") != (spec.risk_level or "medium"):
                row.risk_level = spec.risk_level
                changed = True
            if float(row.default_weight or 1.0) != float(spec.default_weight):
                row.default_weight = float(spec.default_weight)
                changed = True
            if not row.params:
                row.params = spec.params or {}
                changed = True
            if row.run_config is None:
                row.run_config = spec.run_config or {}
                changed = True
        if changed:
            db.commit()
    finally:
        db.close()


def list_strategy_catalog(enabled_only: bool = True) -> list[dict]:
    ensure_strategy_catalog()
    db = SessionLocal()
    try:
        q = db.query(StrategyCatalog)
        if enabled_only:
            q = q.filter(StrategyCatalog.enabled.is_(True))
        rows = q.order_by(StrategyCatalog.code.asc()).all()
        out = []
        for r in rows:
            out.append(
                {
                    "code": r.code,
                    "name": r.name,
                    "description": r.description or "",
                    "version": r.version or "v1",
                    "enabled": bool(r.enabled),
                    "market_scope": r.market_scope or "ALL",
                    "risk_level": r.risk_level or "medium",
                    "params": r.params or {},
                    "default_weight": float(r.default_weight or 1.0),
                    "strategy_type": r.strategy_type or "builtin",
                    "source_ref_type": r.source_ref_type or "",
                    "source_ref_id": r.source_ref_id,
                    "run_config": r.run_config or {},
                    "auto_run_enabled": bool(r.auto_run_enabled),
                }
            )
        return out
    finally:
        db.close()


def get_strategy_profile_map() -> dict[str, dict]:
    rows = list_strategy_catalog(enabled_only=False)
    return {x["code"]: x for x in rows}


def get_strategy_params(code: str) -> dict:
    ensure_strategy_catalog()
    db = SessionLocal()
    try:
        row = db.query(StrategyCatalog).filter(StrategyCatalog.code == code).first()
        return dict(row.params or {}) if row else {}
    finally:
        db.close()


def update_strategy_params(code: str, partial: dict) -> dict:
    """合并更新某策略的 params(整列重新赋值以触发 JSON 变更检测)。"""
    ensure_strategy_catalog()
    db = SessionLocal()
    try:
        row = db.query(StrategyCatalog).filter(StrategyCatalog.code == code).first()
        if not row:
            raise KeyError("strategy not found")
        merged = {**(row.params or {}), **(partial or {})}
        row.params = merged
        db.commit()
        return dict(merged)
    finally:
        db.close()


def get_effective_weight_map(*, market: str = "ALL", regime: str = "default") -> dict[str, float]:
    ensure_strategy_catalog()
    mkt = (market or "ALL").strip().upper() or "ALL"
    reg = (regime or "default").strip() or "default"
    db = SessionLocal()
    try:
        defaults = {
            s.code: float(s.default_weight or 1.0)
            for s in db.query(StrategyCatalog).all()
        }
        rows = (
            db.query(StrategyWeight)
            .filter(
                StrategyWeight.regime == reg,
                StrategyWeight.market.in_(("ALL", mkt)),
            )
            .all()
        )
        out = dict(defaults)
        for r in rows:
            key = (r.strategy_code or "").strip()
            if not key:
                continue
            # Market-specific weight overrides ALL.
            if (r.market or "ALL").upper() == mkt:
                out[key] = float(r.weight or out.get(key, 1.0))
            elif key not in out:
                out[key] = float(r.weight or 1.0)
        return out
    finally:
        db.close()
