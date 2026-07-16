"""每日交易复盘数据导出 API

生成一段可复制的 Markdown 纯文本，供在 Claude 等对话工具中做复盘分析：
1. 账户概况（总仓位、当日盈亏、本周盈亏近似值）
2. 持仓快照（成本/现价/仓位比例/持有天数/浮动盈亏）
3. 今日成交（来自 trade_records 手动录入的真实成交流水）
4. 个股当日行情（OHLC、涨跌幅、成交量与昨日对比）

hide_amounts=true 时不输出账户绝对金额，仅保留比例口径。
"""
import logging
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from src.web.api.accounts import get_portfolio_summary, _fetch_quotes_for_stocks
from src.web.database import get_db
from src.web.models import Position, Stock, StockKlineCache, TradeRecord

logger = logging.getLogger(__name__)
router = APIRouter()

WEEKDAY_CN = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


# ========== 数据采集 ==========

def _parse_date(date_str: str | None) -> datetime:
    if not date_str:
        return datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    try:
        return datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(400, f"日期格式错误: {date_str}，应为 YYYY-MM-DD")


def _kline_rows_before(
    db: Session, market: str, symbol: str, date_str: str, limit: int = 10
) -> list[StockKlineCache]:
    """取指定日期（不含）之前的最近若干根日K缓存，按日期倒序。"""
    return (
        db.query(StockKlineCache)
        .filter(
            StockKlineCache.market == market,
            StockKlineCache.symbol == symbol,
            StockKlineCache.date < date_str,
        )
        .order_by(StockKlineCache.date.desc())
        .limit(limit)
        .all()
    )


def collect_review_data(
    db: Session, review_date: datetime, account_id: int | None = None
) -> dict:
    """采集复盘所需的全部数据，返回纯 dict（与渲染解耦，便于测试）。"""
    date_str = review_date.strftime("%Y-%m-%d")
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    is_today = review_date.date() == today.date()

    summary = get_portfolio_summary(account_id=account_id, include_quotes=True, db=db)

    # 持仓明细（跨账户拍平）+ position.created_at 用于估算持有天数
    positions: list[dict] = []
    position_ids = []
    for acc in summary["accounts"]:
        for pos in acc["positions"]:
            positions.append({**pos, "account_name": acc["name"]})
            position_ids.append(pos["id"])

    created_map: dict[int, datetime] = {}
    if position_ids:
        for row in db.query(Position).filter(Position.id.in_(position_ids)).all():
            if row.created_at:
                created_map[row.id] = row.created_at

    total_assets = summary["total"]["total_assets"] or 0
    for pos in positions:
        created = created_map.get(pos["id"])
        pos["holding_days"] = (
            max(1, (review_date.date() - created.date()).days + 1) if created else None
        )
        mv_cny = pos.get("market_value_cny") or 0
        pos["position_ratio"] = (mv_cny / total_assets * 100) if total_assets > 0 else None

    # 个股当日行情（腾讯实时行情，含 OHLC / 量）
    symbols = {p["symbol"] for p in positions}
    stocks = (
        db.query(Stock).filter(Stock.symbol.in_(symbols)).all() if symbols else []
    )
    quote_items = _fetch_quotes_for_stocks(stocks) if is_today else {}

    quotes_detail: list[dict] = []
    weekly_pnl_total = 0.0
    weekly_incomplete = False
    monday_str = (review_date - timedelta(days=review_date.weekday())).strftime("%Y-%m-%d")

    for pos in positions:
        symbol, market = pos["symbol"], pos["market"]
        klines = _kline_rows_before(db, market, symbol, date_str, limit=10)
        prev_volume = klines[0].volume if klines else None

        quote = quote_items.get(symbol) or {}
        current = pos.get("current_price")

        volume = quote.get("volume")
        volume_ratio = (
            round(volume / prev_volume, 2)
            if volume and prev_volume and prev_volume > 0
            else None
        )
        quotes_detail.append({
            "symbol": symbol,
            "name": pos["name"],
            "market": market,
            "open": quote.get("open_price"),
            "high": quote.get("high_price"),
            "low": quote.get("low_price"),
            "current": current,
            "change_pct": pos.get("change_pct"),
            "volume": volume,
            "prev_volume": prev_volume,
            "volume_ratio": volume_ratio,
        })

        # 本周盈亏近似：现价对比本周一（不含）之前最近一根日K收盘
        week_base = next((k.close for k in klines if k.date < monday_str), None)
        rate = pos.get("exchange_rate") or 1.0
        if current is not None and week_base and week_base > 0:
            weekly_pnl_total += (current - week_base) * pos["quantity"] * rate
        else:
            weekly_incomplete = True

    # 今日成交（真实流水，手动录入）
    day_start = review_date
    day_end = review_date + timedelta(days=1)
    trade_query = db.query(TradeRecord).filter(
        TradeRecord.traded_at >= day_start, TradeRecord.traded_at < day_end
    )
    if account_id:
        trade_query = trade_query.filter(TradeRecord.account_id == account_id)
    trades = [
        {
            "time": t.traded_at.strftime("%H:%M") if t.traded_at else "",
            "symbol": t.symbol,
            "name": t.name or "",
            "direction": t.direction,
            "price": t.price,
            "quantity": t.quantity,
            "amount": t.amount,
            "note": t.note or "",
        }
        for t in trade_query.order_by(TradeRecord.traded_at.asc()).all()
    ]

    total = dict(summary["total"])
    total["position_ratio"] = (
        round(total["total_market_value"] / total_assets * 100, 2)
        if total_assets > 0
        else None
    )
    prev_assets = total_assets - (total.get("total_daily_pnl") or 0)
    total["daily_pnl_pct"] = (
        round((total.get("total_daily_pnl") or 0) / prev_assets * 100, 2)
        if prev_assets > 0
        else None
    )
    total["weekly_pnl"] = round(weekly_pnl_total, 2)
    week_prev_assets = total_assets - weekly_pnl_total
    total["weekly_pnl_pct"] = (
        round(weekly_pnl_total / week_prev_assets * 100, 2) if week_prev_assets > 0 else None
    )
    total["weekly_incomplete"] = weekly_incomplete

    return {
        "date": date_str,
        "weekday": WEEKDAY_CN[review_date.weekday()],
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "is_today": is_today,
        "total": total,
        "accounts": [
            {k: v for k, v in acc.items() if k != "positions"}
            for acc in summary["accounts"]
        ],
        "positions": positions,
        "trades": trades,
        "quotes_detail": quotes_detail,
    }


# ========== Markdown 渲染 ==========

def _fmt(v, digits: int = 2, dash: str = "—") -> str:
    if v is None:
        return dash
    return f"{v:,.{digits}f}"


def _fmt_signed(v, digits: int = 2, dash: str = "—") -> str:
    if v is None:
        return dash
    return f"{v:+,.{digits}f}"


def _fmt_pct(v, dash: str = "—") -> str:
    if v is None:
        return dash
    return f"{v:+.2f}%"


def _fmt_volume(v, dash: str = "—") -> str:
    if not v:
        return dash
    if v >= 1e8:
        return f"{v / 1e8:.2f}亿"
    if v >= 1e4:
        return f"{v / 1e4:.2f}万"
    return f"{v:,.0f}"


def render_review_markdown(data: dict, hide_amounts: bool = False) -> str:
    """将 collect_review_data 的结果渲染为 Markdown 文本（纯函数）。"""
    lines: list[str] = []
    total = data["total"]

    lines.append(f"# 每日交易复盘数据 | {data['date']} {data['weekday']}")
    lines.append("")
    lines.append(f"> 生成时间：{data['generated_at']} · 来源：PanWatch")
    if not data["is_today"]:
        lines.append("> ⚠️ 非当日导出：持仓与行情为当前实时数据，仅成交记录按所选日期过滤。")
    lines.append("")

    # ---- 账户概况 ----
    lines.append("## 一、账户概况")
    lines.append("")
    if not hide_amounts:
        lines.append(
            f"- 总资产：¥{_fmt(total.get('total_assets'))}"
            f"（市值 ¥{_fmt(total.get('total_market_value'))}"
            f" + 可用资金 ¥{_fmt(total.get('available_funds'))}）"
        )
    lines.append(f"- 总仓位：{_fmt(total.get('position_ratio'))}%（市值 / 总资产）")
    daily = f"- 当日盈亏：{_fmt_pct(total.get('daily_pnl_pct'))}"
    if not hide_amounts:
        daily += f"（¥{_fmt_signed(total.get('total_daily_pnl'))}）"
    lines.append(daily)
    weekly = f"- 本周盈亏（近似）：{_fmt_pct(total.get('weekly_pnl_pct'))}"
    if not hide_amounts:
        weekly += f"（¥{_fmt_signed(total.get('weekly_pnl'))}）"
    weekly += " ※ 按上周最后交易日收盘价回算，忽略本周内仓位变动"
    if total.get("weekly_incomplete"):
        weekly += "；部分个股缺K线数据，结果不完整"
    lines.append(weekly)
    cumulative = f"- 持仓累计浮盈：{_fmt_pct(total.get('total_pnl_pct'))}"
    if not hide_amounts:
        cumulative += f"（¥{_fmt_signed(total.get('total_pnl'))}）"
    lines.append(cumulative)

    accounts = data.get("accounts") or []
    if len(accounts) > 1 and not hide_amounts:
        lines.append("")
        lines.append("分账户：")
        for acc in accounts:
            lines.append(
                f"- {acc['name']}：总资产 ¥{_fmt(acc.get('total_assets'))}，"
                f"当日 ¥{_fmt_signed(acc.get('total_daily_pnl'))}，"
                f"累计 {_fmt_pct(acc.get('total_pnl_pct'))}"
            )
    lines.append("")

    # ---- 持仓快照 ----
    lines.append("## 二、持仓快照")
    lines.append("")
    positions = data.get("positions") or []
    if not positions:
        lines.append("当前无持仓。")
    else:
        if hide_amounts:
            lines.append("| 代码 | 名称 | 市场 | 成本价 | 现价 | 仓位 | 持有天数 | 浮盈 | 今日 |")
            lines.append("|---|---|---|---|---|---|---|---|---|")
        else:
            lines.append("| 代码 | 名称 | 市场 | 成本价 | 现价 | 数量 | 仓位 | 持有天数 | 浮动盈亏 | 浮盈 | 今日 |")
            lines.append("|---|---|---|---|---|---|---|---|---|---|---|")
        for pos in positions:
            days = pos.get("holding_days")
            days_str = f"{days}天" if days else "—"
            common_head = (
                f"| {pos['symbol']} | {pos['name']} | {pos['market']} "
                f"| {_fmt(pos.get('cost_price'), 3)} | {_fmt(pos.get('current_price'), 3)} "
            )
            common_tail = (
                f"| {_fmt(pos.get('position_ratio'), 1)}% | {days_str} "
            )
            if hide_amounts:
                lines.append(
                    common_head + common_tail
                    + f"| {_fmt_pct(pos.get('pnl_pct'))} | {_fmt_pct(pos.get('change_pct'))} |"
                )
            else:
                lines.append(
                    common_head
                    + f"| {pos['quantity']} "
                    + common_tail
                    + f"| {_fmt_signed(pos.get('pnl'))} | {_fmt_pct(pos.get('pnl_pct'))} "
                    + f"| {_fmt_pct(pos.get('change_pct'))} |"
                )
        lines.append("")
        lines.append("※ 持有天数按持仓录入日估算；港/美股盈亏均已折算人民币，价格为原币种。")
    lines.append("")

    # ---- 今日成交 ----
    lines.append("## 三、今日成交")
    lines.append("")
    trades = data.get("trades") or []
    if not trades:
        lines.append("今日无成交记录。")
    else:
        if hide_amounts:
            lines.append("| 时间 | 代码 | 名称 | 方向 | 价格 | 数量 | 备注 |")
            lines.append("|---|---|---|---|---|---|---|")
        else:
            lines.append("| 时间 | 代码 | 名称 | 方向 | 价格 | 数量 | 金额 | 备注 |")
            lines.append("|---|---|---|---|---|---|---|---|")
        for t in trades:
            direction = "买入" if t["direction"] == "buy" else "卖出"
            row = (
                f"| {t['time']} | {t['symbol']} | {t['name']} | {direction} "
                f"| {_fmt(t.get('price'), 3)} | {t['quantity']} "
            )
            if not hide_amounts:
                row += f"| {_fmt(t.get('amount'))} "
            row += f"| {t.get('note') or ''} |"
            lines.append(row)
    lines.append("")

    # ---- 个股当日行情 ----
    lines.append("## 四、个股当日行情")
    lines.append("")
    quotes = data.get("quotes_detail") or []
    has_quote = any(q.get("open") is not None for q in quotes)
    if not quotes:
        lines.append("无持仓个股。")
    elif not data["is_today"] or not has_quote:
        lines.append("非当日或行情不可用，跳过当日 OHLC 明细。")
    else:
        lines.append("| 代码 | 名称 | 开盘 | 最高 | 最低 | 现价 | 涨跌 | 成交量 | 昨日量 | 量比 |")
        lines.append("|---|---|---|---|---|---|---|---|---|---|")
        for q in quotes:
            ratio = f"{q['volume_ratio']:.2f}" if q.get("volume_ratio") else "—"
            lines.append(
                f"| {q['symbol']} | {q['name']} "
                f"| {_fmt(q.get('open'), 3)} | {_fmt(q.get('high'), 3)} | {_fmt(q.get('low'), 3)} "
                f"| {_fmt(q.get('current'), 3)} | {_fmt_pct(q.get('change_pct'))} "
                f"| {_fmt_volume(q.get('volume'))} | {_fmt_volume(q.get('prev_volume'))} | {ratio} |"
            )
        lines.append("")
        lines.append("※ 成交量口径以数据源为准（A股通常为手）；昨日量取自日K缓存。")
    lines.append("")

    return "\n".join(lines)


# ========== Endpoint ==========

@router.get("/daily")
def get_daily_review(
    date: str | None = None,
    account_id: int | None = None,
    hide_amounts: bool = False,
    db: Session = Depends(get_db),
):
    """生成每日复盘 Markdown 文本。"""
    review_date = _parse_date(date)
    data = collect_review_data(db, review_date, account_id)
    markdown = render_review_markdown(data, hide_amounts=hide_amounts)
    return {
        "date": data["date"],
        "hide_amounts": hide_amounts,
        "markdown": markdown,
    }
