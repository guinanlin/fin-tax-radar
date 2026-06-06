#!/usr/bin/env python3
"""
tax-radar1 -- Self-contained data collector for GitHub Actions.
Collects fiscal/tax information from official sites + Bilibili and writes JSON
files consumed by the GitHub Pages frontend.

Primary sites:
- 国家税务总局
- 财政部
- 12366 纳税服务
- 税屋
- 中国会计视野
- 中国税务网
- 巨潮资讯网
- 国家法律法规数据库
- Bilibili (kept as requested)

Categories: policy, tax, finance, macro
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
from urllib.parse import quote, urlparse

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
# Category/source definitions
# ---------------------------------------------------------------------------
BASE_KEYWORDS = [
    "税收", "税务", "财税", "财政", "会计", "纳税", "发票",
    "增值税", "所得税", "税收政策", "税法", "财务",
]
BASE_PHRASES = [
    "税收政策", "税务动态", "财税改革", "政策法规",
    "财务实务", "宏观财经",
]

OFFICIAL_SOURCES = [
    {"name": "国家税务总局", "domain": "chinatax.gov.cn"},
    {"name": "财政部", "domain": "mof.gov.cn"},
    {"name": "12366纳税服务", "domain": "12366.chinatax.gov.cn"},
    {"name": "税屋", "domain": "shui5.cn"},
    {"name": "中国会计视野", "domain": "esnai.com"},
    {"name": "中国税务网", "domain": "ctaxnews.com.cn"},
    {"name": "巨潮资讯网", "domain": "cninfo.com.cn"},
    {"name": "国家法律法规数据库", "domain": "flk.npc.gov.cn"},
]
SOURCE_NAME_BY_DOMAIN = {s["domain"]: s["name"] for s in OFFICIAL_SOURCES}

# Search keywords per category
CATEGORY_CONFIG = {
    "policy": {
        "search_terms": ["财税政策法规", "税收优惠政策", "会计准则更新", "税法修订"],
        "filter_keywords": BASE_KEYWORDS + ["政策", "法规", "法律", "修订", "公告", "通知", "办法"],
        "filter_phrases": BASE_PHRASES + ["政策法规", "税法修订"],
        "site_domains": ["chinatax.gov.cn", "mof.gov.cn", "flk.npc.gov.cn", "shui5.cn"],
    },
    "tax": {
        "search_terms": ["税务新闻动态", "纳税申报新规", "税务稽查案例", "增值税改革"],
        "filter_keywords": BASE_KEYWORDS + ["稽查", "征管", "申报", "退税", "办税", "征收"],
        "filter_phrases": BASE_PHRASES + ["税务动态", "纳税服务"],
        "site_domains": ["chinatax.gov.cn", "12366.chinatax.gov.cn", "ctaxnews.com.cn"],
    },
    "finance": {
        "search_terms": ["财务会计实务", "企业财税筹划", "发票管理新规", "企业税务合规"],
        "filter_keywords": BASE_KEYWORDS + ["实务", "合规", "会计准则", "做账", "申报", "票据"],
        "filter_phrases": BASE_PHRASES + ["财务实务", "企业税务合规"],
        "site_domains": ["shui5.cn", "esnai.com", "ctaxnews.com.cn"],
    },
    "macro": {
        "search_terms": ["宏观经济政策", "财政数据发布", "GDP经济增长", "财政收支数据"],
        "filter_keywords": BASE_KEYWORDS + ["宏观", "经济", "GDP", "财政收支", "预算", "国债"],
        "filter_phrases": BASE_PHRASES + ["宏观财经", "财政数据"],
        "site_domains": ["mof.gov.cn", "cninfo.com.cn", "chinatax.gov.cn"],
    },
}

# Platform CSS classes
PLATFORM_OFFICIAL = "platform-official"
PLATFORM_BILIBILI = "platform-bilibili"

# Max content age (days) and sort strategy per category
CATEGORY_MAX_AGE_DAYS: dict[str, int] = {
    "policy": 3650,
    "tax": 3650,
    "finance": 3650,
    "macro": 3650,
}
CATEGORY_SORT_MODE: dict[str, str] = {
    "policy": "recency",
    "tax": "blended",
    "finance": "blended",
    "macro": "blended",
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


def _rebalance_sources(items: list[dict], category_name: str) -> list[dict]:
    """Avoid one source (typically B站) overwhelming official sources."""
    official_items = [x for x in items if x.get("source") != "B站"]
    bilibili_items = [x for x in items if x.get("source") == "B站"]
    if not official_items:
        # If no official hits, keep original order.
        return items

    bili_cap = max(10, min(30, len(official_items) * 2))
    mixed = official_items + bilibili_items[:bili_cap]
    _sort_topics(mixed, CATEGORY_SORT_MODE.get(category_name, "blended"))
    return mixed


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
# Official-site collectors (Bing RSS based)
# ---------------------------------------------------------------------------
def _infer_source_name(link: str) -> str:
    host = (urlparse(link).hostname or "").lower()
    for domain, name in SOURCE_NAME_BY_DOMAIN.items():
        if host == domain or host.endswith("." + domain):
            return name
    return host or "未知来源"


def _is_allowed_domain(link: str, allowed_domains: list[str]) -> bool:
    host = (urlparse(link).hostname or "").lower()
    if not host:
        return False
    for domain in allowed_domains:
        d = domain.lower()
        if host == d or host.endswith("." + d):
            return True
    return False


def parse_rfc822_time(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(raw)
        return dt.astimezone(BJT)
    except (ValueError, TypeError):
        return None


def _bing_rss_url(query: str, domain: str) -> str:
    q = f"site:{domain} {query}"
    return f"https://www.bing.com/search?q={quote(q)}&format=rss&setlang=zh-cn"


async def collect_official_rss(
    category_name: str, search_terms: list[str], keywords: list[str], phrases: list[str],
    site_domains: list[str],
) -> list[dict]:
    """Collect official news via Bing RSS site-restricted search."""
    results: list[dict] = []
    try:
        async with make_client() as client:
            for domain in site_domains:
                for term in search_terms:
                    await random_delay(0.2, 0.8)
                    url = _bing_rss_url(term, domain)
                    try:
                        resp = await client.get(
                            url,
                            headers={"User-Agent": USER_AGENT, "Referer": "https://www.bing.com/"},
                        )
                        if resp.status_code != 200:
                            logger.warning(
                                "Official RSS '%s' site '%s' returned HTTP %s",
                                term, domain, resp.status_code,
                            )
                            continue
                        soup = BeautifulSoup(resp.text, "xml")
                        for item in soup.find_all("item"):
                            title = (item.title.text if item.title else "").strip()
                            desc = (item.description.text if item.description else "").strip()
                            link = (item.link.text if item.link else "").strip()
                            if not title or not link:
                                continue
                            if not _is_allowed_domain(link, site_domains):
                                continue
                            combined = f"{title} {desc}"
                            if not is_tax_related(combined, keywords, phrases):
                                continue
                            pub_dt = parse_rfc822_time(item.pubDate.text if item.pubDate else None)
                            source_name = _infer_source_name(link)
                            heat = max(100, int(5000 - min(content_age_days(pub_dt) or 365, 365) * 10))
                            results.append(topic_dict(
                                title=title,
                                summary=desc[:220] if desc else title,
                                source=source_name,
                                platform_class=PLATFORM_OFFICIAL,
                                author=source_name,
                                url=link,
                                heat=heat,
                                discussions=format_discussions(max(10, heat // 20)),
                                tags=extract_tags(combined, keywords),
                                is_hot=False,
                                published_at=pub_dt,
                                assume_now_if_missing=True,
                            ))
                    except Exception as e:
                        logger.warning(
                            "Official RSS '%s' site '%s' failed: %s",
                            term, domain, e,
                        )
    except Exception as e:
        logger.warning("[%s] official RSS collection failed: %s", category_name, e)
    return results


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
    site_domains = config["site_domains"]

    logger.info(
        "Collecting category '%s' with %d terms, %d official sites...",
        category_name, len(search_terms), len(site_domains),
    )

    tasks = [
        collect_official_rss(category_name, search_terms, kw, phrases, site_domains),
        collect_bilibili_search(search_terms, kw, phrases),
        collect_bilibili_ranking(kw, phrases),
    ]
    labels = ["Official-RSS", "Bilibili-search", "Bilibili-ranking"]
    results_raw = await asyncio.gather(*tasks, return_exceptions=True)

    all_items: list[dict] = []
    for label, result in zip(labels, results_raw):
        if isinstance(result, Exception):
            logger.error("[%s] %s raised: %s", category_name, label, result)
            continue
        logger.info("[%s] %s returned %d items", category_name, label, len(result))
        all_items.extend(result)

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
    unique = _rebalance_sources(unique, category_name)
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
            "[%s] only Bilibili data collected; official-site RSS queries may be blocked "
            "or returned empty for current keywords/IP",
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
    for cat in ("policy", "tax", "finance", "macro"):
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

    # Map category -> (subdirectory, filename)
    file_map: dict[str, tuple[str, str]] = {
        "policy": ("policy", "latest.json"),
        "tax": ("tax", "latest.json"),
        "finance": ("finance", "latest.json"),
        "macro": ("macro", "latest.json"),
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
