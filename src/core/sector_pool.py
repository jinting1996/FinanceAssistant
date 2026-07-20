"""板块池:分类骨架与种子数据。

分类参考"生产资料"投资体系:金融/资源/能源转化/通道/被消费的制造/产业链主题,
行业板块(申万一级,腾讯 hy 榜)做骨架,概念板块(gn 榜)做细分补充。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from src.web.models import WatchedBoard

logger = logging.getLogger(__name__)

# (key, 中文名),顺序即前端展示顺序
SECTOR_CATEGORIES: tuple[tuple[str, str], ...] = (
    ("finance", "金融"),
    ("resource", "资源"),
    ("energy", "能源转化"),
    ("channel", "通道"),
    ("consumer", "被消费的制造"),
    ("theme", "产业链主题"),
    ("other", "其他"),
)

SECTOR_CATEGORY_LABELS: dict[str, str] = dict(SECTOR_CATEGORIES)


@dataclass(frozen=True)
class SectorSeed:
    category: str
    name: str
    scope: str = "industry"  # industry=腾讯行业榜(申万一级) / concept=概念榜
    aliases: tuple[str, ...] = field(default_factory=tuple)
    tags: tuple[str, ...] = field(default_factory=tuple)


SECTOR_POOL_SEED: tuple[SectorSeed, ...] = (
    # 金融
    SectorSeed("finance", "银行"),
    SectorSeed("finance", "非银金融", aliases=("保险", "证券")),
    # 资源
    SectorSeed("resource", "有色金属", tags=("资源安全",)),
    SectorSeed("resource", "煤炭", tags=("能源安全",)),
    SectorSeed("resource", "钢铁"),
    SectorSeed("resource", "基础化工"),
    SectorSeed("resource", "黄金概念", scope="concept", aliases=("黄金",)),
    SectorSeed("resource", "铜概念", scope="concept", aliases=("铜",)),
    SectorSeed("resource", "铝概念", scope="concept", aliases=("铝",)),
    SectorSeed("resource", "钨概念", scope="concept", aliases=("钨",), tags=("资源安全",)),
    SectorSeed("resource", "小金属概念", scope="concept", aliases=("小金属",), tags=("资源安全",)),
    SectorSeed("resource", "稀土永磁", scope="concept", aliases=("稀土",), tags=("资源安全",)),
    SectorSeed("resource", "锂矿", scope="concept"),
    SectorSeed("resource", "化肥", scope="concept", tags=("粮食安全",)),
    # 能源转化
    SectorSeed("energy", "公用事业", aliases=("电力",), tags=("能源安全",)),
    SectorSeed("energy", "石油石化", tags=("能源安全",)),
    SectorSeed("energy", "核电概念", scope="concept", aliases=("核电",), tags=("能源安全",)),
    SectorSeed("energy", "绿色电力", scope="concept", aliases=("绿电",)),
    # 通道
    SectorSeed("channel", "通信", tags=("网络安全",)),
    SectorSeed("channel", "交通运输"),
    SectorSeed("channel", "光通信", scope="concept"),
    SectorSeed("channel", "共封装光模块(CPO）", scope="concept", aliases=("CPO",)),
    SectorSeed("channel", "快递物流", scope="concept"),
    SectorSeed("channel", "特高压", scope="concept", aliases=("电网",), tags=("能源安全",)),
    # 被消费的制造
    SectorSeed("consumer", "汽车"),
    SectorSeed("consumer", "家用电器"),
    SectorSeed("consumer", "食品饮料", tags=("粮食安全",)),
    SectorSeed("consumer", "医药生物", tags=("生物安全",)),
    SectorSeed("consumer", "农林牧渔", tags=("粮食安全",)),
    SectorSeed("consumer", "纺织服饰"),
    SectorSeed("consumer", "轻工制造"),
    SectorSeed("consumer", "商贸零售"),
    SectorSeed("consumer", "美容护理"),
    SectorSeed("consumer", "社会服务"),
    SectorSeed("consumer", "白酒概念", scope="concept", aliases=("白酒",)),
    SectorSeed("consumer", "创新药", scope="concept", tags=("生物安全",)),
    # 产业链主题
    SectorSeed("theme", "电子"),
    SectorSeed("theme", "计算机", tags=("数据安全",)),
    SectorSeed("theme", "传媒"),
    SectorSeed("theme", "国防军工", tags=("军事安全",)),
    SectorSeed("theme", "电力设备"),
    SectorSeed("theme", "机械设备"),
    SectorSeed("theme", "人工智能", scope="concept", tags=("数据安全",)),
    SectorSeed("theme", "人形机器人", scope="concept", aliases=("机器人概念",)),
    SectorSeed("theme", "半导体产业", scope="concept", aliases=("半导体",)),
    SectorSeed("theme", "存储器", scope="concept", aliases=("存储",)),
    SectorSeed("theme", "商业航天", scope="concept", aliases=("航天装备概念",), tags=("太空安全",)),
    SectorSeed("theme", "低空经济", scope="concept", tags=("低空安全",)),
    # 其他
    SectorSeed("other", "房地产"),
    SectorSeed("other", "建筑材料"),
    SectorSeed("other", "建筑装饰"),
    SectorSeed("other", "环保"),
)


def _match_board(seed: SectorSeed, boards: list[dict]) -> dict | None:
    """在拉到的板块名单里解析种子对应的板块(优先精确名,再别名包含)。"""
    pool = [b for b in boards if (b.get("scope") or "industry") == seed.scope]
    for b in pool:
        if b.get("name") == seed.name:
            return b
    candidates = (seed.name, *seed.aliases)
    for alias in candidates:
        for b in pool:
            name = str(b.get("name") or "")
            if alias and (alias in name or name in alias):
                return b
    return None


async def fetch_all_boards(proxy: str | None = None) -> list[dict]:
    """腾讯行业榜全量 + 概念榜分页,统一 [{code,name,scope,change_pct,turnover}]。"""
    from src.collectors.tencent_board_collector import TencentBoardCollector

    collector = TencentBoardCollector(timeout_s=12.0, proxy=proxy or None, retries=1)
    boards: list[dict] = []
    rows = await collector.fetch_hot_boards(scope="industry", limit=100)
    boards.extend(rows)
    for offset in (0, 100, 200, 300, 400):
        rows = await collector.fetch_hot_boards(scope="concept", limit=100, offset=offset)
        boards.extend(rows)
        if len(rows) < 100:
            break
    seen: set[str] = set()
    out: list[dict] = []
    for b in boards:
        code = str(b.get("code") or "")
        if not code or code in seen:
            continue
        seen.add(code)
        out.append(b)
    return out


def seed_sector_pool(db: Session, boards: list[dict]) -> dict:
    """按种子清单播种板块池。幂等:已存在的行只补空的 category/scope/tags。"""
    created = 0
    updated = 0
    unresolved: list[str] = []
    for seed in SECTOR_POOL_SEED:
        board = _match_board(seed, boards)
        if not board:
            unresolved.append(seed.name)
            continue
        code = str(board.get("code") or "")
        row = (
            db.query(WatchedBoard)
            .filter(WatchedBoard.market == "CN", WatchedBoard.board_code == code)
            .first()
        )
        if row is None:
            row = WatchedBoard(
                market="CN",
                board_code=code,
                board_name=str(board.get("name") or seed.name),
                category=seed.category,
                tier="pool",
                scope=seed.scope,
                tags=list(seed.tags) or None,
                sort_order=0,
                enabled=True,
            )
            db.add(row)
            created += 1
        else:
            changed = False
            if not (row.category or ""):
                row.category = seed.category
                changed = True
            if (row.scope or "industry") != seed.scope:
                row.scope = seed.scope
                changed = True
            if not row.tags and seed.tags:
                row.tags = list(seed.tags)
                changed = True
            if changed:
                updated += 1
    db.commit()
    if unresolved:
        logger.warning("板块池播种未解析到板块: %s", "、".join(unresolved))
    return {
        "created": created,
        "updated": updated,
        "unresolved": unresolved,
        "total_seed": len(SECTOR_POOL_SEED),
    }
