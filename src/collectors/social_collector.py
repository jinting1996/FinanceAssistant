"""
X (Twitter) 舆论采集器 - 集成情感分析

用法:
    from src.collectors.social_collector import SocialSentimentCollector
    collector = SocialSentimentCollector(x_config, ai_client)
    items = await collector.analyze(["AAPL", "TSLA"], count=20)
"""

import json
import logging
import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


# ============================================================
# 数据模型
# ============================================================

@dataclass
class SocialItem:
    """社交媒体帖子数据"""
    source: str                          # "twitter"
    external_id: str                     # 推文 ID
    username: str                        # 作者用户名
    display_name: str                    # 作者显示名
    content: str                         # 推文内容
    publish_time: Optional[datetime]     # 发布时间
    symbols: list[str] = field(default_factory=list)  # 关联股票代码
    metrics: dict[str, int] = field(default_factory=dict)  # {likes, retweets, replies, views}
    url: str = ""                        # 推文链接
    sentiment: str = ""                  # 情感标签: positive / negative / neutral
    sentiment_score: float = 0.0         # 情感分数: 0(极度负面) ~ 1(极度正面)
    sentiment_reasoning: str = ""        # 情感分析理由

    @property
    def engagement(self) -> int:
        """总互动量"""
        m = self.metrics
        return m.get("likes", 0) + m.get("retweets", 0) + m.get("replies", 0)


@dataclass
class SentimentSummary:
    """情感汇总"""
    symbol: str
    total_posts: int
    positive_count: int = 0
    negative_count: int = 0
    neutral_count: int = 0
    avg_score: float = 0.0
    top_posts: list[SocialItem] = field(default_factory=list)
    analyzed_at: Optional[datetime] = None

    @property
    def positive_ratio(self) -> float:
        if self.total_posts == 0:
            return 0.0
        return self.positive_count / self.total_posts

    @property
    def negative_ratio(self) -> float:
        if self.total_posts == 0:
            return 0.0
        return self.negative_count / self.total_posts

    @property
    def sentiment_label(self) -> str:
        """整体情感倾向"""
        if self.total_posts == 0:
            return "no_data"
        if self.positive_ratio > 0.5:
            return "bullish"
        if self.negative_ratio > 0.5:
            return "bearish"
        if self.avg_score > 0.55:
            return "slightly_bullish"
        if self.avg_score < 0.45:
            return "slightly_bearish"
        return "neutral"


# ============================================================
# 抽象基类
# ============================================================

class BaseSocialCollector(ABC):
    """社交媒体采集器基类"""
    source: str = ""

    @abstractmethod
    async def fetch_posts(
        self,
        symbols: list[str],
        since: Optional[datetime] = None,
        count: int = 20,
    ) -> list[SocialItem]:
        """采集帖子"""
        ...


# ============================================================
# X (Twitter) 采集器 —— 基于 twikit
# ============================================================

class XTwitterCollector(BaseSocialCollector):
    """
    X (Twitter) 舆论采集器

    使用 twikit 库抓取 X 前端 API，无需官方 API Key。
    需要 X 账号登录（建议使用小号）。

    环境变量:
        X_USERNAME: X 账号用户名
        X_EMAIL: X 账号邮箱
        X_PASSWORD: X 账号密码
    """

    source = "twitter"

    # 速率限制（每 15 分钟）
    SEARCH_RATE_LIMIT = 50
    # 搜索间隔（秒）
    SEARCH_COOLDOWN = 3.0

    def __init__(
        self,
        username: str = "",
        email: str = "",
        password: str = "",
        cookie_dir: str = "/app/data",
        language: str = "en-US",
    ):
        self.username = username
        self.email = email
        self.password = password
        self.cookie_path = Path(cookie_dir) / "twitter_cookies.json"
        self.language = language
        self._client = None
        self._search_count = 0
        self._search_window_start = time.time()

    @property
    def is_configured(self) -> bool:
        return bool(self.username and self.email and self.password)

    def _get_client(self):
        """延迟初始化 twikit Client"""
        if self._client is None:
            try:
                from twikit import Client
                self._client = Client(self.language)
            except ImportError:
                raise ImportError(
                    "twikit 未安装。请运行: pip install twikit\n"
                    "或在 Dockerfile 中添加 twikit 依赖。"
                )
        return self._client

    async def _ensure_login(self) -> bool:
        """确保已登录，优先使用 cookie 恢复会话"""
        if not self.is_configured:
            logger.warning("X 账号未配置，跳过登录")
            return False

        client = self._get_client()

        # 尝试从 cookie 文件恢复
        if self.cookie_path.exists():
            try:
                client.load_cookies(str(self.cookie_path))
                logger.info("已从 cookie 文件恢复 X 会话")
                return True
            except Exception as e:
                logger.warning(f"Cookie 加载失败: {e}，将重新登录")

        # 重新登录
        try:
            await client.login(
                auth_info_1=self.username,
                auth_info_2=self.email,
                password=self.password,
            )
            # 保存 cookie
            client.save_cookies(str(self.cookie_path))
            logger.info("X 登录成功，cookie 已保存")
            return True
        except Exception as e:
            logger.error(f"X 登录失败: {e}")
            # 尝试用 set_cookies 代替（兼容不同版本）
            try:
                await client.login(
                    auth_info_1=self.username,
                    auth_info_2=self.email,
                    password=self.password,
                )
                cookies = client.get_cookies()
                self.cookie_path.parent.mkdir(parents=True, exist_ok=True)
                self.cookie_path.write_text(json.dumps(cookies))
                logger.info("X 登录成功 (备用方式)")
                return True
            except Exception as e2:
                logger.error(f"X 登录彻底失败: {e2}")
                return False

    def _check_rate_limit(self):
        """检查并等待速率限制"""
        now = time.time()
        # 重置 15 分钟窗口
        if now - self._search_window_start > 900:
            self._search_count = 0
            self._search_window_start = now

        if self._search_count >= self.SEARCH_RATE_LIMIT:
            wait = 900 - (now - self._search_window_start) + 5
            logger.warning(f"X 搜索速率限制达到，等待 {wait:.0f} 秒...")
            return wait
        return 0

    def _build_search_query(self, symbol: str) -> str:
        """构建 Cashtag 搜索查询"""
        # 去掉市场后缀（如 600519.SH -> 600519）
        clean_symbol = symbol.split(".")[0].upper()
        return f"${clean_symbol}"

    async def fetch_posts(
        self,
        symbols: list[str],
        since: Optional[datetime] = None,
        count: int = 20,
    ) -> list[SocialItem]:
        """
        按股票代码搜索 X 上的讨论

        Args:
            symbols: 股票代码列表，如 ["AAPL", "TSLA", "600519"]
            since: 最早时间过滤
            count: 每只股票最多获取的推文数

        Returns:
            合并去重后的帖子列表
        """
        if not self.is_configured:
            logger.warning("X 账号未配置，无法采集")
            return []

        logged_in = await self._ensure_login()
        if not logged_in:
            return []

        client = self._get_client()
        all_items: list[SocialItem] = []
        seen_ids: set[str] = set()

        for symbol in symbols:
            query = self._build_search_query(symbol)
            logger.info(f"搜索 X: {query}")

            # 速率限制
            wait = self._check_rate_limit()
            if wait > 0:
                import asyncio
                await asyncio.sleep(wait)

            try:
                tweets = await client.search_tweet(query, "Latest")
                self._search_count += 1

                count_added = 0
                for tweet in tweets[:count]:
                    if tweet.id in seen_ids:
                        continue
                    seen_ids.add(tweet.id)

                    # 解析时间
                    pub_time = None
                    try:
                        if hasattr(tweet, "created_at") and tweet.created_at:
                            pub_time = datetime.fromisoformat(
                                str(tweet.created_at).replace("Z", "+00:00")
                            )
                    except Exception:
                        pass

                    # 时间过滤
                    if since and pub_time and pub_time < since:
                        continue

                    # 指标
                    metrics = {}
                    for field in ["favorite_count", "retweet_count", "reply_count", "view_count"]:
                        try:
                            val = getattr(tweet, field, 0)
                            if val:
                                metrics[field.replace("_count", "s").replace("favorites", "likes")] = int(val)
                        except Exception:
                            pass

                    # 用户名
                    username = ""
                    display_name = ""
                    try:
                        if hasattr(tweet, "user"):
                            username = getattr(tweet.user, "screen_name", "")
                            display_name = getattr(tweet.user, "name", "")
                    except Exception:
                        pass

                    item = SocialItem(
                        source="twitter",
                        external_id=str(tweet.id),
                        username=username,
                        display_name=display_name,
                        content=getattr(tweet, "text", ""),
                        publish_time=pub_time,
                        symbols=[symbol],
                        metrics=metrics,
                        url=f"https://x.com/{username}/status/{tweet.id}" if username else "",
                    )
                    all_items.append(item)
                    count_added += 1

                logger.info(f"  {symbol}: 获取 {count_added} 条推文")

                # 搜索间隔
                import asyncio
                await asyncio.sleep(self.SEARCH_COOLDOWN)

            except Exception as e:
                logger.error(f"搜索 {symbol} 失败: {e}")
                continue

        # 按发布时间倒序排列
        all_items.sort(key=lambda x: x.publish_time or datetime.min, reverse=True)
        logger.info(f"X 采集完成: 共 {len(all_items)} 条推文，涉及 {len(symbols)} 只股票")
        return all_items


# ============================================================
# 情感分析采集器（聚合器）
# ============================================================

class SocialSentimentCollector:
    """
    社交媒体情感分析器

    组合采集器 + AI 情感分析，生成结构化情感数据。
    可以作为 Agent 的数据源使用。

    用法:
        collector = SocialSentimentCollector(x_config, ai_client)
        summary = await collector.analyze_symbol("AAPL")
        summaries = await collector.analyze(["AAPL", "TSLA"])
    """

    SENTIMENT_PROMPT = """你是一个专业的金融舆情分析师。请分析以下关于股票 {symbol} 的社交媒体帖子，判断其情感倾向。

分类标准:
- positive: 看涨/乐观情绪，包含「买入」「看好」「突破」「利好」「增长」等
- negative: 看跌/悲观情绪，包含「卖出」「看空」「暴跌」「利空」「风险」等
- neutral: 中性/客观陈述，没有明显的方向性偏向

返回一个 JSON 对象（只返回 JSON，不要任何其他文字）:
{{"sentiment": "positive|negative|neutral", "score": 0.0~1.0, "reasoning": "一句话理由"}}

score 含义: 0.0=极度负面, 0.5=中性, 1.0=极度正面

帖子内容:
{content}"""

    BATCH_SENTIMENT_PROMPT = """你是一个专业的金融舆情分析师。请分析以下多条关于股票 {symbol} 的社交媒体帖子，对每条帖子判断情感倾向。

分类标准:
- positive: 看涨/乐观情绪
- negative: 看跌/悲观情绪
- neutral: 中性/客观陈述

返回一个 JSON 数组，每个元素对应一条帖子:
[{{"id": "帖子编号", "sentiment": "positive|negative|neutral", "score": 0.0~1.0, "reasoning": "一句话理由"}}, ...]

score 含义: 0.0=极度负面, 0.5=中性, 1.0=极度正面

帖子列表:
{posts_text}"""

    def __init__(self, collector: BaseSocialCollector, ai_client=None):
        """
        Args:
            collector: 社交媒体采集器实例
            ai_client: AIClient 实例（可选，不传则不做情感分析）
        """
        self.collector = collector
        self.ai_client = ai_client

    @property
    def sentiment_enabled(self) -> bool:
        return self.ai_client is not None

    async def analyze_symbol(
        self,
        symbol: str,
        count: int = 20,
        since: Optional[datetime] = None,
        enable_sentiment: bool = True,
    ) -> SentimentSummary:
        """分析单只股票的社交舆论"""
        items = await self.collector.fetch_posts([symbol], since=since, count=count)

        if not items:
            return SentimentSummary(symbol=symbol, total_posts=0)

        # 情感分析
        if enable_sentiment and self.sentiment_enabled:
            await self._analyze_sentiment(items, symbol)

        return self._build_summary(symbol, items)

    async def analyze(
        self,
        symbols: list[str],
        count: int = 20,
        since: Optional[datetime] = None,
        enable_sentiment: bool = True,
    ) -> list[SentimentSummary]:
        """分析多只股票的社交舆论"""
        items = await self.collector.fetch_posts(symbols, since=since, count=count)

        if not items:
            return [SentimentSummary(symbol=s, total_posts=0) for s in symbols]

        # 情感分析
        if enable_sentiment and self.sentiment_enabled:
            # 按股票分组分析
            for symbol in symbols:
                symbol_items = [i for i in items if symbol in i.symbols]
                if symbol_items:
                    await self._analyze_sentiment_batch(symbol_items, symbol)

        # 按股票汇总
        summaries = []
        for symbol in symbols:
            symbol_items = [i for i in items if symbol in i.symbols]
            summaries.append(self._build_summary(symbol, symbol_items))

        return summaries

    async def _analyze_sentiment(self, items: list[SocialItem], symbol: str):
        """逐条分析情感（适用于少量推文）"""
        for item in items:
            if item.sentiment:  # 已分析过
                continue
            try:
                prompt = self.SENTIMENT_PROMPT.format(
                    symbol=symbol, content=item.content[:500]
                )
                result = await self.ai_client.chat(
                    system_prompt="你是一个金融舆情分析专家，只返回 JSON。",
                    user_content=prompt,
                    temperature=0.1,
                )
                # 解析 JSON
                data = self._parse_json(result)
                if data:
                    item.sentiment = data.get("sentiment", "neutral")
                    item.sentiment_score = float(data.get("score", 0.5))
                    item.sentiment_reasoning = data.get("reasoning", "")
            except Exception as e:
                logger.warning(f"情感分析失败 ({item.external_id[:8]}...): {e}")
                item.sentiment = "neutral"
                item.sentiment_score = 0.5

    async def _analyze_sentiment_batch(self, items: list[SocialItem], symbol: str, batch_size: int = 10):
        """批量分析情感（节省 API 调用）"""
        for i in range(0, len(items), batch_size):
            batch = items[i:i + batch_size]
            unanalyzed = [it for it in batch if not it.sentiment]
            if not unanalyzed:
                continue

            # 构建批量帖子文本
            posts_text = ""
            for j, item in enumerate(unanalyzed):
                posts_text += f"[{j}] {item.content[:200]}\n\n"

            try:
                prompt = self.BATCH_SENTIMENT_PROMPT.format(
                    symbol=symbol, posts_text=posts_text
                )
                result = await self.ai_client.chat(
                    system_prompt="你是一个金融舆情分析专家，只返回 JSON 数组。",
                    user_content=prompt,
                    temperature=0.1,
                )
                data_list = self._parse_json(result)
                if isinstance(data_list, list):
                    for j, data in enumerate(data_list):
                        if j < len(unanalyzed):
                            unanalyzed[j].sentiment = data.get("sentiment", "neutral")
                            unanalyzed[j].sentiment_score = float(data.get("score", 0.5))
                            unanalyzed[j].sentiment_reasoning = data.get("reasoning", "")
            except Exception as e:
                logger.warning(f"批量情感分析失败 ({symbol}): {e}")
                for item in unanalyzed:
                    item.sentiment = "neutral"
                    item.sentiment_score = 0.5

    def _parse_json(self, text: str) -> Any:
        """从 AI 回复中提取 JSON"""
        if not text:
            return None
        # 尝试提取 JSON 块
        text = text.strip()
        # 去掉 markdown 代码块
        if text.startswith("```"):
            lines = text.split("\n")
            text = "\n".join(lines[1:-1]) if len(lines) > 2 else text
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            # 尝试用正则提取
            match = re.search(r'\[.*\]|\{.*\}', text, re.DOTALL)
            if match:
                try:
                    return json.loads(match.group())
                except json.JSONDecodeError:
                    pass
        return None

    def _build_summary(self, symbol: str, items: list[SocialItem]) -> SentimentSummary:
        """构建情感汇总"""
        if not items:
            return SentimentSummary(symbol=symbol, total_posts=0)

        positive = [i for i in items if i.sentiment == "positive"]
        negative = [i for i in items if i.sentiment == "negative"]
        neutral = [i for i in items if i.sentiment == "neutral"]

        scores = [i.sentiment_score for i in items if i.sentiment]

        # 按互动量排序取 top 5
        top_posts = sorted(items, key=lambda x: x.engagement, reverse=True)[:5]

        return SentimentSummary(
            symbol=symbol,
            total_posts=len(items),
            positive_count=len(positive),
            negative_count=len(negative),
            neutral_count=len(neutral),
            avg_score=sum(scores) / len(scores) if scores else 0.5,
            top_posts=top_posts,
            analyzed_at=datetime.now(),
        )


# ============================================================
# 格式化输出（供 Agent 使用）
# ============================================================

def format_sentiment_for_agent(summaries: list[SentimentSummary]) -> str:
    """将情感汇总格式化为 Agent 可读的文本"""
    if not summaries:
        return "（无社交媒体数据）"

    lines = ["## 📊 X/Twitter 舆论情感分析\n"]
    for s in summaries:
        if s.total_posts == 0:
            lines.append(f"### {s.symbol}: 无数据\n")
            continue

        emoji = {"bullish": "🟢", "slightly_bullish": "🟡", "bearish": "🔴",
                 "slightly_bearish": "🟠", "neutral": "⚪"}.get(s.sentiment_label, "⚪")

        lines.append(f"### {emoji} {s.symbol}: {s.sentiment_label}")
        lines.append(f"- 帖子数: {s.total_posts}")
        lines.append(f"- 正面: {s.positive_count} ({s.positive_ratio:.0%})")
        lines.append(f"- 负面: {s.negative_count} ({s.negative_ratio:.0%})")
        lines.append(f"- 中性: {s.neutral_count}")
        if s.total_posts > 0:
            lines.append(f"- 情感均分: {s.avg_score:.2f}")

        if s.top_posts:
            lines.append(f"\n**热门帖子:**")
            for post in s.top_posts[:3]:
                sentiment_icon = {"positive": "🟢", "negative": "🔴", "neutral": "⚪"}.get(
                    post.sentiment, "⚪"
                )
                content = post.content[:100].replace("\n", " ")
                engage = post.engagement
                lines.append(
                    f"- {sentiment_icon} @{post.username}: _{content}_ "
                    f"(互动: {engage})"
                )
        lines.append("")

    return "\n".join(lines)