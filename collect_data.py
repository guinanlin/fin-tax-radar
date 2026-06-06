#!/usr/bin/env python3
"""
tax-radar1 -- Self-contained data collector for GitHub Actions.
Scrapes fiscal/tax hot topics from Chinese social-media platforms
and writes JSON files consumed by the GitHub Pages frontend.

Platforms: Weibo, Zhihu, Bilibili (both hot-lists and keyword search).
No API keys required -- uses only public endpoints.

Categories: daily, weekly, monthly, crs, odi, overseas_asset, overseas_company
"""

import asyncio
import hashlib
import json
import logging
import os
import random
import re
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import quote

import httpx
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("collect")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
BJT = timezone(timedelta(hours=8))
NOW = datetime.now(BJT)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# ---------------------------------------------------------------------------
# Keyword definitions per category
# ---------------------------------------------------------------------------
TAX_KEYWORDS = [
    "增值税", "企业所得税", "个人所得税", "个税", "消费税", "房产税",
    "土地增值税", "印花税", "关税", "车辆购置税", "资源税", "城建税",
    "契税", "环境保护税", "烟叶税", "船舶吨税", "耕地占用税",
    "税收", "税务", "纳税", "退税", "免税", "减税", "避税", "节税",
    "税负", "税率", "税基", "税制", "税改", "税法", "税筹", "税务筹划",
    "发票", "数电发票", "专票", "普票", "进项", "销项",
    "留抵退税", "加计扣除", "即征即退", "先征后退",
    "汇算清缴", "纳税申报", "税务登记", "税收优惠",
    "小规模纳税人", "一般纳税人", "核定征收", "查账征收",
    "财务", "会计", "审计", "财报", "报表", "资产负债", "利润表",
    "现金流", "会计准则", "财务核算", "成本核算", "做账", "记账",
    "应收账款", "应付账款", "固定资产", "折旧", "摊销",
    "CPA", "注册会计师", "税务师", "CMA", "中级会计", "初级会计",
    "财政", "地方财政", "财政收入", "财政支出", "国债", "地方债",
    "转移支付", "预算", "财政部",
    "税务总局", "国家税务", "税务局", "海关总署",
    "报税", "开票", "抵扣", "税前扣除", "专项附加扣除",
    "出口退税", "跨境电商税", "电商税", "直播税",
    "股权转让税", "分红税", "工资税", "年终奖税",
    "社保", "公积金", "五险一金",
]

TAX_PHRASES = [
    "税收政策", "财税新规", "财税改革", "税务合规",
    "企业报税", "个税申报", "增值税发票", "所得税汇算",
    "减税降费", "税收征管", "税务稽查", "税务检查",
    "营商环境", "税收营商", "涉税风险", "税务风险",
]

CRS_KEYWORDS = [
    "CRS申报", "共同申报准则", "税务信息交换", "海外账户申报",
    "AEOI", "金融账户涉税", "CRS合规", "涉税信息交换",
    "非居民金融账户", "尽职调查", "自动交换",
]

ODI_KEYWORDS = [
    "ODI备案", "境外投资备案", "对外直接投资", "境外投资管理",
    "37号文", "返程投资", "海外投资架构", "境外投资审批",
    "对外投资合规", "发改委境外投资",
]

OVERSEAS_ASSET_KEYWORDS = [
    "海外资产申报", "境外资产申报", "海外房产税", "全球征税",
    "海外信托", "境外所得申报", "海外资产配置税务",
    "境外收入纳税", "海外资产合规", "个人境外所得",
]

OVERSEAS_COMPANY_KEYWORDS = [
    "海外公司注册", "离岸公司注册", "香港公司注册", "新加坡公司注册",
    "BVI公司注册", "经济实质法", "离岸架构搭建",
    "开曼公司", "海外公司税务", "注册离岸公司",
]

# Search keywords per category (subset used for active search queries)
CATEGORY_CONFIG = {
    "daily": {
        "keywords": TAX_KEYWORDS,
        "search_terms": ["税收政策", "个人所得税", "增值税", "减税降费", "税务筹划"],
        "filter_keywords": TAX_KEYWORDS,
        "filter_phrases": TAX_PHRASES,
    },
    "weekly": {
        "keywords": TAX_KEYWORDS,
        "search_terms": ["税收政策", "财税改革", "纳税申报"],
        "filter_keywords": TAX_KEYWORDS,
        "filter_phrases": TAX_PHRASES,
    },
    "monthly": {
        "keywords": TAX_KEYWORDS,
        "search_terms": ["税收", "财税新规", "税务"],
        "filter_keywords": TAX_KEYWORDS,
        "filter_phrases": TAX_PHRASES,
    },
    "crs": {
        "keywords": CRS_KEYWORDS,
        "search_terms": ["CRS申报 税务", "共同申报准则", "海外账户涉税申报"],
        "filter_keywords": CRS_KEYWORDS,
        "filter_phrases": [],
    },
    "odi": {
        "keywords": ODI_KEYWORDS,
        "search_terms": ["ODI备案 境外投资", "境外投资备案流程", "海外投资架构 税务"],
        "filter_keywords": ODI_KEYWORDS,
        "filter_phrases": [],
    },
    "overseas_asset": {
        "keywords": OVERSEAS_ASSET_KEYWORDS,
        "search_terms": ["海外资产申报 税务", "境外资产 全球征税", "海外信托 税务"],
        "filter_keywords": OVERSEAS_ASSET_KEYWORDS,
        "filter_phrases": [],
    },
    "overseas_company": {
        "keywords": OVERSEAS_COMPANY_KEYWORDS,
        "search_terms": ["离岸公司注册 税务", "香港公司注册 税务", "海外公司注册"],
        "filter_keywords": OVERSEAS_COMPANY_KEYWORDS,
        "filter_phrases": [],
    },
}

# Platform CSS classes
PLATFORM_WEIBO = "platform-weibo"
PLATFORM_ZHIHU = "platform-zhihu"
PLATFORM_BILIBILI = "platform-bilibili"

# Max content age (days) and sort strategy per category
CATEGORY_MAX_AGE_DAYS: dict[str, int] = {
    "daily": 3,
    "weekly": 14,
    "monthly": 45,
    "crs": 30,
    "odi": 30,
    "overseas_asset": 30,
    "overseas_company": 30,
}
CATEGORY_SORT_MODE: dict[str, str] = {
    "daily": "recency",
    "weekly": "blended",
    "monthly": "blended",
    "crs": "blended",
    "odi": "blended",
    "overseas_asset": "blended",
    "overseas_company": "blended",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def is_tax_related(text: str, keywords: list[str], phrases: list[str],
                    threshold: int = 1) -> bool:
    """Return True if *text* contains enough matching keywords/phrases."""
    if not text:
        return False
    t = text.lower()
    for phrase in phrases:
        if phrase.lower() in t:
            return True
    hits = 0
    for kw in keywords:
        if kw.lower() in t:
            hits += 1
            if hits >= threshold:
                return True
    return False


def extract_tags(text: str, keywords: list[str], limit: int = 3) -> list[str]:
    """Pull matching keywords from text as tags."""
    tags: list[str] = []
    for kw in keywords:
        if kw in text and len(kw) >= 2:
            tags.append(kw)
        if len(tags) >= limit:
            break
    return tags


def format_discussions(n: int) -> str:
    if n >= 10000:
        return f"{n / 10000:.1f}万"
    return str(n)


def make_id(source: str, title: str) -> str:
    return hashlib.md5(f"{source}:{title}".encode()).hexdigest()[:12]


def make_detail(title: str, summary: str, points: list[str] | None = None) -> str:
    """Build a short HTML detail block."""
    html = f"<p>{summary}</p>"
    if points:
        html += "<h4>要点</h4><ul>" + "".join(f"<li>{p}</li>" for p in points) + "</ul>"
    return html


def _to_bjt(ts: int | float | None) -> datetime | None:
    """Convert unix timestamp to timezone-aware Beijing datetime."""
    if not ts:
        return None
    try:
        return datetime.fromtimestamp(int(ts), BJT)
    except (ValueError, TypeError, OSError):
        return None


def parse_weibo_created_at(raw: str | None) -> datetime | None:
    """Parse Weibo ``created_at`` like ``Sun Mar 30 12:00:00 +0800 2026``."""
    if not raw:
        return None
    try:
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(raw)
        return dt.astimezone(BJT)
    except (ValueError, TypeError):
        return None


def parse_publish_time(*candidates: int | float | str | datetime | None) -> datetime | None:
    """Return the first valid publish datetime from mixed platform fields."""
    for value in candidates:
        if value is None:
            continue
        if isinstance(value, datetime):
            return value.astimezone(BJT) if value.tzinfo else value.replace(tzinfo=BJT)
        if isinstance(value, (int, float)):
            dt = _to_bjt(value)
            if dt:
                return dt
        if isinstance(value, str):
            dt = parse_weibo_created_at(value)
            if dt:
                return dt
    return None


def content_age_days(published_at: datetime | None) -> float | None:
    if published_at is None:
        return None
    return (NOW - published_at).total_seconds() / 86400


def topic_dict(
    title: str, summary: str, source: str, platform_class: str,
    author: str, url: str, heat: int, discussions, tags: list[str],
    is_hot: bool, detail: str | None = None,
    published_at: datetime | None = None,
    *,
    assume_now_if_missing: bool = False,
) -> dict:
    """Build a topic record. Search results must pass real ``published_at``."""
    published_ts: int | None = None
    if published_at is not None:
        published = published_at.astimezone(BJT)
        published_ts = int(published.timestamp())
    elif assume_now_if_missing:
        published = NOW
        published_ts = int(NOW.timestamp())
    else:
        published = NOW

    payload = {
        "id": make_id(source, title),
        "title": title,
        "summary": summary,
        "source": source,
        "platform_class": platform_class,
        "author": author,
        "url": url,
        "time": published.strftime("%Y-%m-%d %H:%M"),
        "heat": int(heat),
        "discussions": discussions if isinstance(discussions, str) else format_discussions(discussions),
        "tags": tags,
        "isHot": is_hot,
        "detail": detail or make_detail(title, summary),
    }
    if published_ts is not None:
        payload["published_at"] = published.isoformat()
        payload["_published_ts"] = published_ts
    return payload


def _sort_topics(items: list[dict], mode: str) -> None:
    """Sort topics in-place by recency and/or heat."""
    if mode == "recency":
        items.sort(key=lambda x: (x.get("_published_ts", 0), x["heat"]), reverse=True)
        return

    def blended_key(item: dict) -> tuple[float, int, int]:
        ts = item.get("_published_ts", 0)
        age_days = content_age_days(_to_bjt(ts)) if ts else 999.0
        recency_boost = max(0.0, 1.0 - age_days / 30.0)
        score = item["heat"] * (0.55 + 0.45 * recency_boost)
        return (score, ts, item["heat"])

    items.sort(key=blended_key, reverse=True)


def _finalize_topics(items: list[dict], category_name: str) -> list[dict]:
    """Filter by age, sort, mark hot, and strip internal fields."""
    max_age = CATEGORY_MAX_AGE_DAYS.get(category_name, 30)
    sort_mode = CATEGORY_SORT_MODE.get(category_name, "blended")

    filtered: list[dict] = []
    dropped_old = 0
    dropped_unknown = 0
    for item in items:
        ts = item.get("_published_ts")
        if not ts:
            dropped_unknown += 1
            continue
        age = content_age_days(_to_bjt(ts))
        if age is None or age > max_age:
            dropped_old += 1
            continue
        filtered.append(item)

    if dropped_old or dropped_unknown:
        logger.info(
            "[%s] filtered out %d old and %d undated items (max_age=%dd)",
            category_name, dropped_old, dropped_unknown, max_age,
        )

    _sort_topics(filtered, sort_mode)

    for i, item in enumerate(filtered):
        item["isHot"] = i < 3
        item.pop("_published_ts", None)

    return filtered


def make_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=30.0,
        headers={
            "User-Agent": USER_AGENT,
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Accept": "application/json, text/plain, */*",
            "Connection": "keep-alive",
        },
        follow_redirects=True,
    )


def _weibo_headers() -> dict[str, str]:
    return {
        "User-Agent": USER_AGENT,
        "Referer": "https://m.weibo.cn/",
        "X-Requested-With": "XMLHttpRequest",
        "Accept": "application/json, text/plain, */*",
    }


def _zhihu_headers(referer: str = "https://www.zhihu.com/") -> dict[str, str]:
    return {
        "User-Agent": USER_AGENT,
        "Referer": referer,
        "Accept": "application/json, text/plain, */*",
        "x-requested-with": "fetch",
    }


async def _safe_json(resp: httpx.Response, label: str) -> dict | list | None:
    try:
        return resp.json()
    except Exception:
        ctype = resp.headers.get("content-type", "")
        logger.warning(
            "%s returned non-JSON payload (HTTP %s, %s, %d bytes)",
            label, resp.status_code, ctype, len(resp.content),
        )
        return None


async def bootstrap_weibo(client: httpx.AsyncClient) -> None:
    """Acquire guest cookies required by m.weibo.cn APIs."""
    headers = _weibo_headers()
    try:
        await client.get("https://m.weibo.cn/", headers=headers)
        fp = json.dumps({
            "os": "1",
            "browser": "chrome",
            "fonts": "undefined",
            "screenInfo": "1920*1080*24",
            "plugins": "",
        })
        resp = await client.post(
            "https://passport.weibo.com/visitor/genvisitor2",
            data={"cb": "gen_callback", "fp": fp},
            headers={
                "User-Agent": USER_AGENT,
                "Referer": "https://passport.weibo.com/visitor/visitor",
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        match = re.search(r"gen_callback\((\{.*\})\)", resp.text)
        if not match:
            logger.warning("Weibo guest bootstrap: genvisitor2 parse failed")
            return
        payload = json.loads(match.group(1)).get("data", {})
        if not payload.get("tid"):
            logger.warning("Weibo guest bootstrap: missing tid")
            return
        await client.get(
            "https://passport.weibo.com/visitor/visitor",
            params={
                "a": "incarnate",
                "t": payload["tid"],
                "w": 2,
                "cb": "cross_domain",
                "from": "weibo",
                "c": payload.get("new_tid", payload["tid"]),
                "_rand": str(random.random()),
            },
            headers=headers,
        )
        await client.get("https://m.weibo.cn/api/config", headers=headers)
    except Exception as e:
        logger.warning("Weibo guest bootstrap failed: %s", e)


async def bootstrap_zhihu(client: httpx.AsyncClient) -> None:
    """Warm Zhihu cookies before API/HTML requests."""
    try:
        await client.get("https://www.zhihu.com/", headers=_zhihu_headers())
    except Exception as e:
        logger.warning("Zhihu bootstrap failed: %s", e)


def _extract_zhihu_initial_data(html: str) -> dict | None:
    match = re.search(
        r'<script id="js-initialData" type="text/json">(.*?)</script>',
        html,
        re.S,
    )
    if not match:
        return None
    try:
        return json.loads(match.group(1))
    except json.JSONDecodeError:
        return None


def _zhihu_question_url(link: str | None, fallback_id: str | int | None = None) -> str:
    if link:
        if link.startswith("http"):
            return link
        return f"https://www.zhihu.com{link}"
    if fallback_id:
        return f"https://www.zhihu.com/question/{fallback_id}"
    return "https://www.zhihu.com/hot"


def _append_weibo_mblog(
    results: list[dict], mblog: dict, keywords: list[str], phrases: list[str],
) -> None:
    text_raw = mblog.get("text", "")
    text_plain = re.sub(r"<[^>]+>", "", text_raw)
    title = text_plain[:80].strip()
    if not title:
        return
    if not is_tax_related(text_plain, keywords, phrases):
        return
    reposts = mblog.get("reposts_count", 0)
    comments = mblog.get("comments_count", 0)
    attitudes = mblog.get("attitudes_count", 0)
    heat = int(attitudes * 2 + comments * 5 + reposts * 3)
    user_info = mblog.get("user", {}) or {}
    mid = mblog.get("mid", "") or mblog.get("id", "")
    published_at = parse_publish_time(mblog.get("created_at"))
    results.append(topic_dict(
        title=title,
        summary=text_plain[:200].strip(),
        source="微博",
        platform_class=PLATFORM_WEIBO,
        author=user_info.get("screen_name", ""),
        url=f"https://m.weibo.cn/detail/{mid}",
        heat=heat,
        discussions=format_discussions(comments),
        tags=extract_tags(text_plain, keywords),
        is_hot=attitudes > 10000,
        published_at=published_at,
    ))


async def random_delay(lo: float = 0.5, hi: float = 2.0) -> None:
    """Sleep a random duration to avoid rate limiting."""
    await asyncio.sleep(random.uniform(lo, hi))


# ---------------------------------------------------------------------------
# Weibo collectors
# ---------------------------------------------------------------------------
# PLACEHOLDER: weibo_hotlist
async def collect_weibo_hotlist(
    keywords: list[str], phrases: list[str], client: httpx.AsyncClient | None = None,
) -> list[dict]:
    """Weibo hot-search: filter for matching keywords."""
    results: list[dict] = []
    owns_client = client is None
    try:
        if owns_client:
            client = make_client()
            await bootstrap_weibo(client)

        # Primary: mobile hot band
        resp = await client.get(
            "https://m.weibo.cn/api/container/getIndex",
            params={"containerid": "102803"},
            headers=_weibo_headers(),
        )
        data = await _safe_json(resp, "Weibo hotlist(mobile)")
        if isinstance(data, dict):
            for card in data.get("data", {}).get("cards", []):
                for cg in card.get("card_group", []) or [card]:
                    desc = cg.get("desc", "")
                    note = cg.get("note", "")
                    title = desc or note
                    if not title:
                        continue
                    combined = f"{title} {note}"
                    if not is_tax_related(combined, keywords, phrases):
                        continue
                    scheme = cg.get("scheme", "")
                    raw_heat = int(re.sub(r"[^\d]", "", note or "0") or "0")
                    results.append(topic_dict(
                        title=title,
                        summary=note or title,
                        source="微博",
                        platform_class=PLATFORM_WEIBO,
                        author="微博热搜",
                        url=scheme or f"https://s.weibo.com/weibo?q={quote(title)}",
                        heat=max(raw_heat, 1000),
                        discussions=format_discussions(max(raw_heat, 1000)),
                        tags=extract_tags(combined, keywords),
                        is_hot=True,
                        assume_now_if_missing=True,
                    ))

        # Fallback: desktop hot search API
        if not results:
            resp = await client.get(
                "https://weibo.com/ajax/side/hotSearch",
                headers={"User-Agent": USER_AGENT, "Referer": "https://weibo.com/"},
            )
            data = await _safe_json(resp, "Weibo hotlist(desktop)")
            if isinstance(data, dict):
                for item in data.get("data", {}).get("realtime", []):
                    word = item.get("word", "")
                    note = item.get("note", "")
                    raw_heat = item.get("raw_hot", 0) or item.get("num", 0)
                    combined = word + " " + note
                    if not is_tax_related(combined, keywords, phrases):
                        continue
                    results.append(topic_dict(
                        title=word,
                        summary=note or word,
                        source="微博",
                        platform_class=PLATFORM_WEIBO,
                        author="微博热搜",
                        url=f"https://s.weibo.com/weibo?q=%23{quote(word)}%23",
                        heat=raw_heat,
                        discussions=format_discussions(raw_heat),
                        tags=extract_tags(combined, keywords),
                        is_hot=raw_heat > 500000,
                        assume_now_if_missing=True,
                    ))
    except Exception as e:
        logger.warning("Weibo hotlist collection failed: %s", e)
    finally:
        if owns_client and client is not None:
            await client.aclose()
    return results

# PLACEHOLDER: weibo_search
async def collect_weibo_search(
    search_terms: list[str], keywords: list[str], phrases: list[str],
    client: httpx.AsyncClient | None = None,
) -> list[dict]:
    """Search Weibo for specific keywords."""
    results: list[dict] = []
    owns_client = client is None
    try:
        if owns_client:
            client = make_client()
            await bootstrap_weibo(client)

        for term in search_terms:
            await random_delay(1.0, 3.0)
            try:
                resp = await client.get(
                    "https://m.weibo.cn/api/container/getIndex",
                    params={"containerid": f"100103type=1&q={term}"},
                    headers=_weibo_headers(),
                )
                if resp.status_code != 200:
                    logger.warning("Weibo search '%s' returned HTTP %s", term, resp.status_code)
                    continue
                data = await _safe_json(resp, f"Weibo search '{term}'")
                if not isinstance(data, dict):
                    continue
                if data.get("ok") not in (1, "1"):
                    logger.warning(
                        "Weibo search '%s' API rejected request (ok=%s, msg=%s)",
                        term, data.get("ok"), data.get("msg") or "unknown",
                    )
                    continue
                cards = data.get("data", {}).get("cards", [])
                for card in cards:
                    card_group = card.get("card_group", []) or [card]
                    for cg in card_group:
                        mblog = cg.get("mblog")
                        if mblog:
                            _append_weibo_mblog(results, mblog, keywords, phrases)
            except Exception as e:
                logger.warning("Weibo search '%s' failed: %s", term, e)
    except Exception as e:
        logger.warning("Weibo search collection failed: %s", e)
    finally:
        if owns_client and client is not None:
            await client.aclose()
    return results


# ---------------------------------------------------------------------------
# Zhihu collectors
# ---------------------------------------------------------------------------
def _append_zhihu_search_object(
    results: list[dict], obj: dict, term: str, keywords: list[str],
) -> None:
    title = re.sub(r"<[^>]+>", "", obj.get("title", "") or "")
    excerpt = re.sub(
        r"<[^>]+>", "",
        obj.get("excerpt", "") or obj.get("description", "") or "",
    )
    if not title:
        return
    author_info = obj.get("author", {}) or {}
    author_name = author_info.get("name", "")
    question = obj.get("question", {}) or {}
    qid = question.get("id", "") or obj.get("id", "")
    answer_count = question.get("answer_count", 0) or 0
    follower_count = question.get("follower_count", 0) or 0
    obj_type = obj.get("type", "")
    if obj_type == "answer":
        result_url = (
            f"https://www.zhihu.com/question/{question.get('id', '')}"
            f"/answer/{obj.get('id', '')}"
        )
    elif obj_type == "article":
        result_url = f"https://zhuanlan.zhihu.com/p/{obj.get('id', '')}"
    else:
        result_url = f"https://www.zhihu.com/question/{qid}"
    heat = int(follower_count * 0.5 + answer_count * 10)
    published_at = parse_publish_time(
        obj.get("updated_time"),
        obj.get("created_time"),
        question.get("updated_time"),
        question.get("created_time"),
    )
    results.append(topic_dict(
        title=title,
        summary=excerpt[:200] if excerpt else title,
        source="知乎",
        platform_class=PLATFORM_ZHIHU,
        author=author_name,
        url=result_url or f"https://www.zhihu.com/search?type=content&q={quote(term)}",
        heat=heat,
        discussions=format_discussions(answer_count),
        tags=extract_tags(title + " " + excerpt, keywords),
        is_hot=follower_count > 5000,
        published_at=published_at,
    ))


def _zhihu_hotlist_from_html(html: str, keywords: list[str], phrases: list[str]) -> list[dict]:
    data = _extract_zhihu_initial_data(html)
    if not data:
        return []
    results: list[dict] = []
    hot_list = data.get("initialState", {}).get("topstory", {}).get("hotList", [])
    for item in hot_list:
        target = item.get("target", {})
        title = (target.get("titleArea") or {}).get("text") or target.get("title", "")
        excerpt = (target.get("excerptArea") or {}).get("text") or target.get("excerpt", "")
        combined = f"{title} {excerpt}"
        if not title or not is_tax_related(combined, keywords, phrases):
            continue
        detail_text = item.get("detailText", "") or item.get("detail_text", "0")
        heat_num = int(re.sub(r"[^\d]", "", detail_text) or "0")
        link = (target.get("link") or {}).get("url")
        qid_match = re.search(r"/question/(\d+)", link or "")
        qid = qid_match.group(1) if qid_match else target.get("id", "")
        results.append(topic_dict(
            title=title,
            summary=excerpt[:200] if excerpt else title,
            source="知乎",
            platform_class=PLATFORM_ZHIHU,
            author="知乎热榜",
            url=_zhihu_question_url(link, qid),
            heat=max(heat_num, 1000),
            discussions=format_discussions(max(heat_num, 1000)),
            tags=extract_tags(combined, keywords),
            is_hot=heat_num > 1000000,
            assume_now_if_missing=True,
        ))
    return results


def _zhihu_search_from_html(
    html: str, term: str, keywords: list[str], phrases: list[str],
) -> list[dict]:
    data = _extract_zhihu_initial_data(html)
    if not data:
        return []
    state = data.get("initialState", {})
    entities = state.get("entities", {})
    results: list[dict] = []
    search_entities = (
        state.get("search", {}).get("entities", {}).get("content", [])
        or state.get("search", {}).get("data", [])
    )
    for entry in search_entities:
        if isinstance(entry, str):
            obj = entities.get("questions", {}).get(entry) or entities.get("answers", {}).get(entry)
            if not obj:
                continue
            combined = f"{obj.get('title', '')} {obj.get('excerpt', '')}"
            if not is_tax_related(combined, keywords, phrases):
                continue
            _append_zhihu_search_object(results, obj, term, keywords)
            continue
        obj = entry.get("object", entry)
        if not isinstance(obj, dict):
            continue
        combined = f"{obj.get('title', '')} {obj.get('excerpt', '')} {obj.get('description', '')}"
        if not is_tax_related(combined, keywords, phrases):
            continue
        _append_zhihu_search_object(results, obj, term, keywords)
    return results


# PLACEHOLDER: zhihu_hotlist
async def collect_zhihu_hotlist(
    keywords: list[str], phrases: list[str], client: httpx.AsyncClient | None = None,
) -> list[dict]:
    """Zhihu hot-list: filter for matching keywords."""
    results: list[dict] = []
    owns_client = client is None
    try:
        if owns_client:
            client = make_client()
            await bootstrap_zhihu(client)

        resp = await client.get(
            "https://www.zhihu.com/api/v3/feed/topstory/hot-lists/total",
            params={"limit": 50},
            headers=_zhihu_headers("https://www.zhihu.com/hot"),
        )
        data = await _safe_json(resp, "Zhihu hotlist(api)")
        if isinstance(data, dict):
            for item in data.get("data", []):
                target = item.get("target", {})
                title = target.get("title", "")
                excerpt = target.get("excerpt", "")
                combined = title + " " + excerpt
                if not is_tax_related(combined, keywords, phrases):
                    continue
                detail_text = item.get("detail_text", "0")
                heat_num = int(re.sub(r"[^\d]", "", detail_text) or "0")
                answer_count = target.get("answer_count", 0)
                qid = target.get("id", "")
                results.append(topic_dict(
                    title=title,
                    summary=excerpt[:200] if excerpt else title,
                    source="知乎",
                    platform_class=PLATFORM_ZHIHU,
                    author=target.get("author", {}).get("name", ""),
                    url=f"https://www.zhihu.com/question/{qid}",
                    heat=heat_num,
                    discussions=format_discussions(answer_count),
                    tags=extract_tags(combined, keywords),
                    is_hot=heat_num > 1000000,
                    assume_now_if_missing=True,
                ))

        if not results:
            html_resp = await client.get(
                "https://www.zhihu.com/hot",
                headers=_zhihu_headers("https://www.zhihu.com/hot"),
            )
            if html_resp.status_code == 200:
                results = _zhihu_hotlist_from_html(html_resp.text, keywords, phrases)
                if results:
                    logger.info("Zhihu hotlist recovered via HTML fallback (%d items)", len(results))
            else:
                logger.warning("Zhihu hotlist HTML fallback returned HTTP %s", html_resp.status_code)
    except Exception as e:
        logger.warning("Zhihu hotlist collection failed: %s", e)
    finally:
        if owns_client and client is not None:
            await client.aclose()
    return results

# PLACEHOLDER: zhihu_search
async def collect_zhihu_search(
    search_terms: list[str], keywords: list[str], phrases: list[str],
    client: httpx.AsyncClient | None = None,
) -> list[dict]:
    """Search Zhihu for specific keywords."""
    results: list[dict] = []
    owns_client = client is None
    try:
        if owns_client:
            client = make_client()
            await bootstrap_zhihu(client)

        for term in search_terms:
            await random_delay(1.0, 3.0)
            try:
                resp = await client.get(
                    "https://www.zhihu.com/api/v4/search_v3",
                    params={
                        "t": "general",
                        "q": term,
                        "correction": 1,
                        "offset": 0,
                        "limit": 20,
                        "filter_fields": "",
                        "lc_idx": 0,
                        "show_all_topics": 0,
                        "search_source": "Filter",
                    },
                    headers=_zhihu_headers(
                        f"https://www.zhihu.com/search?type=content&q={quote(term)}"
                    ),
                )
                term_results: list[dict] = []
                if resp.status_code == 200:
                    data = await _safe_json(resp, f"Zhihu search '{term}'")
                    if isinstance(data, dict):
                        for item in data.get("data", []):
                            if item.get("type") != "search_result":
                                continue
                            obj = item.get("object", {})
                            combined = (
                                f"{obj.get('title', '')} {obj.get('excerpt', '')}"
                                f" {obj.get('description', '')}"
                            )
                            if not is_tax_related(combined, keywords, phrases):
                                continue
                            _append_zhihu_search_object(term_results, obj, term, keywords)
                if term_results:
                    results.extend(term_results)
                    continue

                html_resp = await client.get(
                    f"https://www.zhihu.com/search?type=content&q={quote(term)}",
                    headers=_zhihu_headers(
                        f"https://www.zhihu.com/search?type=content&q={quote(term)}"
                    ),
                )
                if html_resp.status_code == 200:
                    html_items = _zhihu_search_from_html(html_resp.text, term, keywords, phrases)
                    if html_items:
                        results.extend(html_items)
                        logger.info("Zhihu search '%s' recovered via HTML fallback (%d items)", term, len(html_items))
                    else:
                        logger.warning("Zhihu search '%s' API/HTML both empty (HTTP %s)", term, resp.status_code)
                else:
                    logger.warning("Zhihu search '%s' returned HTTP %s", term, resp.status_code)
            except Exception as e:
                logger.warning("Zhihu search '%s' failed: %s", term, e)
    except Exception as e:
        logger.warning("Zhihu search collection failed: %s", e)
    finally:
        if owns_client and client is not None:
            await client.aclose()
    return results


# ---------------------------------------------------------------------------
# Bilibili collectors
# ---------------------------------------------------------------------------
# PLACEHOLDER: bilibili_ranking
async def collect_bilibili_ranking(keywords: list[str], phrases: list[str]) -> list[dict]:
    """Bilibili ranking (all categories): filter for matching keywords."""
    url = "https://api.bilibili.com/x/web-interface/ranking/v2"
    results: list[dict] = []
    try:
        async with make_client() as client:
            resp = await client.get(url)
            if resp.status_code != 200:
                logger.warning("Bilibili ranking returned HTTP %s", resp.status_code)
                return results
            data = resp.json()
            for item in data.get("data", {}).get("list", []):
                title = item.get("title", "")
                desc = item.get("desc", "")
                combined = title + " " + desc
                if not is_tax_related(combined, keywords, phrases):
                    continue
                stat = item.get("stat", {})
                view = stat.get("view", 0)
                danmaku = stat.get("danmaku", 0)
                like = stat.get("like", 0)
                reply = stat.get("reply", 0)
                heat = int(view * 0.1 + like * 2 + reply * 5 + danmaku * 1)
                bvid = item.get("bvid", "")
                published_at = parse_publish_time(item.get("pubdate"))
                results.append(topic_dict(
                    title=title,
                    summary=desc[:200] if desc else title,
                    source="B站",
                    platform_class=PLATFORM_BILIBILI,
                    author=item.get("owner", {}).get("name", ""),
                    url=f"https://www.bilibili.com/video/{bvid}" if bvid else "",
                    heat=heat,
                    discussions=format_discussions(danmaku + reply),
                    tags=extract_tags(combined, keywords),
                    is_hot=view > 500000,
                    published_at=published_at,
                ))
    except Exception as e:
        logger.warning("Bilibili ranking collection failed: %s", e)
    return results

# PLACEHOLDER: bilibili_search
async def collect_bilibili_search(search_terms: list[str], keywords: list[str],
                                   phrases: list[str]) -> list[dict]:
    """Search Bilibili for specific keywords."""
    results: list[dict] = []
    try:
        async with make_client() as client:
            for term in search_terms:
                await random_delay(1.0, 3.0)
                url = "https://api.bilibili.com/x/web-interface/search/all/v2"
                try:
                    resp = await client.get(
                        url,
                        params={"keyword": term, "order": "pubdate", "page": 1},
                    )
                    if resp.status_code != 200:
                        logger.warning("Bilibili search '%s' returned HTTP %s", term, resp.status_code)
                        continue
                    data = resp.json()
                    result_list = data.get("data", {}).get("result", [])
                    for group in result_list:
                        # Each group has a "result_type" and "data" list
                        if group.get("result_type") != "video":
                            continue
                        for item in group.get("data", []):
                            title = item.get("title", "")
                            # Strip HTML highlight tags from search results
                            title = re.sub(r"<[^>]+>", "", title)
                            desc = item.get("description", "")
                            if not title:
                                continue
                            view = item.get("play", 0) or 0
                            danmaku = item.get("danmaku", 0) or 0
                            like = item.get("like", 0) or 0
                            review = item.get("review", 0) or 0
                            # 'play' may be a string like "1.2万"
                            if isinstance(view, str):
                                view = _parse_chinese_num(view)
                            heat = int(view * 0.1 + like * 2 + review * 5 + danmaku * 1)
                            bvid = item.get("bvid", "")
                            arcurl = item.get("arcurl", "")
                            published_at = parse_publish_time(
                                item.get("pubdate"),
                                item.get("senddate"),
                            )
                            results.append(topic_dict(
                                title=title,
                                summary=desc[:200] if desc else title,
                                source="B站",
                                platform_class=PLATFORM_BILIBILI,
                                author=item.get("author", ""),
                                url=arcurl or (f"https://www.bilibili.com/video/{bvid}" if bvid else ""),
                                heat=heat,
                                discussions=format_discussions(danmaku + review),
                                tags=extract_tags(title + " " + desc, keywords),
                                is_hot=view > 100000,
                                published_at=published_at,
                            ))
                except Exception as e:
                    logger.warning("Bilibili search '%s' failed: %s", term, e)
    except Exception as e:
        logger.warning("Bilibili search collection failed: %s", e)
    return results


def _parse_chinese_num(s: str) -> int:
    """Parse strings like '1.2万' into integers."""
    s = s.strip()
    if not s:
        return 0
    try:
        if "万" in s:
            return int(float(s.replace("万", "")) * 10000)
        if "亿" in s:
            return int(float(s.replace("亿", "")) * 100000000)
        return int(float(re.sub(r"[^\d.]", "", s)))
    except (ValueError, TypeError):
        return 0


# ---------------------------------------------------------------------------
# Category collector
# ---------------------------------------------------------------------------
# PLACEHOLDER: collect_category
async def collect_category(category_name: str) -> list[dict]:
    """Collect topics for a single category from all platforms."""
    config = CATEGORY_CONFIG[category_name]
    kw = config["filter_keywords"]
    phrases = config["filter_phrases"]
    search_terms = config["search_terms"]

    logger.info("Collecting category '%s' with %d search terms...", category_name, len(search_terms))

    weibo_client = make_client()
    zhihu_client = make_client()
    await bootstrap_weibo(weibo_client)
    await bootstrap_zhihu(zhihu_client)

    try:
        search_tasks = [
            collect_weibo_search(search_terms, kw, phrases, client=weibo_client),
            collect_zhihu_search(search_terms, kw, phrases, client=zhihu_client),
            collect_bilibili_search(search_terms, kw, phrases),
        ]

        hotlist_tasks: list = []
        if category_name in ("daily", "weekly", "monthly"):
            hotlist_tasks = [
                collect_weibo_hotlist(kw, phrases, client=weibo_client),
                collect_zhihu_hotlist(kw, phrases, client=zhihu_client),
                collect_bilibili_ranking(kw, phrases),
            ]

        all_tasks = search_tasks + hotlist_tasks
        task_labels = [
            "Weibo-search", "Zhihu-search", "Bilibili-search",
        ] + (["Weibo-hotlist", "Zhihu-hotlist", "Bilibili-ranking"] if hotlist_tasks else [])

        results_raw = await asyncio.gather(*all_tasks, return_exceptions=True)

        all_items: list[dict] = []
        for label, result in zip(task_labels, results_raw):
            if isinstance(result, Exception):
                logger.error("[%s] %s raised: %s", category_name, label, result)
                continue
            logger.info("[%s] %s returned %d items", category_name, label, len(result))
            all_items.extend(result)
    finally:
        await weibo_client.aclose()
        await zhihu_client.aclose()

    # Deduplicate by title (normalized), keep the newer duplicate
    seen: dict[str, dict] = {}
    for item in all_items:
        norm_title = item["title"].strip().lower()
        prev = seen.get(norm_title)
        if prev is None:
            seen[norm_title] = item
            continue
        prev_ts = prev.get("_published_ts", 0)
        new_ts = item.get("_published_ts", 0)
        if new_ts > prev_ts:
            seen[norm_title] = item

    unique = list(seen.values())
    unique = _finalize_topics(unique, category_name)

    source_counts: dict[str, int] = {}
    for item in unique:
        source_counts[item["source"]] = source_counts.get(item["source"], 0) + 1
    logger.info(
        "[%s] %d unique topics after dedup/filter, by source: %s",
        category_name, len(unique), source_counts or "none",
    )
    if unique and len(source_counts) == 1 and "B站" in source_counts:
        logger.warning(
            "[%s] only Bilibili data collected; Weibo/Zhihu may be blocked by anti-bot "
            "(common on GitHub Actions overseas runners or datacenter IPs)",
            category_name,
        )
    return unique


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------
# PLACEHOLDER: collect_all
async def collect_all() -> dict[str, list[dict]]:
    """Collect all categories. Returns {category_name: [topics]}."""
    results: dict[str, list[dict]] = {}
    # Collect categories sequentially to be gentle on rate limits.
    # daily/weekly/monthly share the same sources so collect once and reuse.
    daily_topics = await collect_category("daily")
    results["daily"] = daily_topics

    # Weekly and monthly reuse daily data (same keywords, different file paths)
    # but we re-collect with their own (smaller) search term sets for variety.
    for cat in ("weekly", "monthly"):
        await random_delay(2.0, 4.0)
        results[cat] = await collect_category(cat)

    # Specialty categories
    for cat in ("crs", "odi", "overseas_asset", "overseas_company"):
        await random_delay(2.0, 4.0)
        results[cat] = await collect_category(cat)

    return results


# ---------------------------------------------------------------------------
# File I/O
# ---------------------------------------------------------------------------
# PLACEHOLDER: save_json
def save_all(results: dict[str, list[dict]]) -> None:
    """Write JSON files for every category, always creating directories."""
    base = Path(os.getenv("DATA_DIR", "data"))
    now = NOW

    date_str = now.strftime("%Y-%m-%d")
    iso_cal = now.isocalendar()
    week_str = f"{iso_cal[0]}-W{iso_cal[1]:02d}"
    month_str = now.strftime("%Y-%m")

    # Map category -> (subdirectory, filename)
    file_map: dict[str, tuple[str, str]] = {
        "daily": ("daily", f"{date_str}.json"),
        "weekly": ("weekly", f"{week_str}.json"),
        "monthly": ("monthly", f"{month_str}.json"),
        "crs": ("crs", "latest.json"),
        "odi": ("odi", "latest.json"),
        "overseas_asset": ("overseas_asset", "latest.json"),
        "overseas_company": ("overseas_company", "latest.json"),
    }

    # Always create all directories, even if no topics
    for subdir, _ in file_map.values():
        (base / subdir).mkdir(parents=True, exist_ok=True)

    for category, (subdir, filename) in file_map.items():
        topics = results.get(category, [])
        payload = {
            "updated_at": now.isoformat(),
            "count": len(topics),
            "topics": topics,
        }
        path = base / subdir / filename
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        logger.info("Wrote %s  (%d topics)", path, len(topics))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
async def main():
    logger.info("=== tax-radar collector started at %s ===", NOW.strftime("%Y-%m-%d %H:%M %Z"))
    results = await collect_all()
    save_all(results)
    total = sum(len(v) for v in results.values())
    logger.info("Collection complete: %d total topics across %d categories.", total, len(results))


if __name__ == "__main__":
    asyncio.run(main())
