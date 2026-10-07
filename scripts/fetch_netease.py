#!/usr/bin/env python3
"""
网易云每日新碟 · 全量增强扫描器

基于用户提供的 Album Candidate Pool 参考扫描器，针对 GitHub Actions + GitHub Pages
架构重新实现，重点解决：

- 现在只扫描到 1~10 张：改为登录态 + ALL/ZH/EA/KR/JP 多源分页。
- 短页提前停止：只有真正到达 total / 空页确认 / 明确末尾才停止。
- offset 不生效：重复页检测 + GET/POST 回退。
- V.A. / Various Artists 不齐：加入“全部关注艺人 -> recent50 + 新专20 -> album 候选池 -> owned 验证”的参考算法，
  并与全局新碟结果按 album_id 合并。
- 首次扫描异常快：完整模式会真正执行艺人列表、recent、new-album probe、album detail、owned verify 等阶段；
  进度与统计会打印到 Actions 日志。
- album 详情错配：严格校验返回 album_id / album_name。
- 缓存污染：album cache 带 ID 校验，近期日期周期复查。
- 多次运行重复：最终按 album_id 去重，JSON 原子写入。

安全：网易云 MUSIC_U 只能通过 GitHub Actions Secret NETEASE_MUSIC_U 注入，绝不写入仓库文件。
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

TZ = timezone(timedelta(hours=8))
ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
CACHE_FILE = DATA_DIR / "album_cache.json"

API_BASE = os.getenv("NETEASE_API_BASE", "http://127.0.0.1:3000").rstrip("/")
PAGE_SIZE = max(10, min(int(os.getenv("NETEASE_PAGE_SIZE", "30")), 100))
MAX_PAGES = max(10, int(os.getenv("NETEASE_MAX_PAGES", "500")))
RECENT_LIMIT = max(10, min(int(os.getenv("NETEASE_RECENT_LIMIT", "50")), 200))
NEW_ALBUM_PROBE_LIMIT = max(5, min(int(os.getenv("NETEASE_NEW_ALBUM_PROBE_LIMIT", "20")), 100))
OWNED_LIMIT = max(10, min(int(os.getenv("NETEASE_OWNED_VERIFY_LIMIT", "50")), 200))
THREADS = max(1, min(int(os.getenv("NETEASE_SCAN_THREADS", "4")), 8))
API_TIMEOUT = float(os.getenv("NETEASE_API_TIMEOUT", "20"))
API_RETRIES = max(1, int(os.getenv("NETEASE_API_RETRIES", "3")))
CACHE_RECENT_HOURS = float(os.getenv("NETEASE_CACHE_RECENT_HOURS", "12"))
CACHE_UNKNOWN_HOURS = float(os.getenv("NETEASE_CACHE_UNKNOWN_HOURS", "6"))

AREAS = ["ALL", "ZH", "EA", "KR", "JP"]
BAD_ARTISTS = {"various artists", "v.a.", "v.a", "华语群星"}


def build_session() -> requests.Session:
    s = requests.Session()
    retry = Retry(
        total=2,
        connect=2,
        read=2,
        backoff_factor=0.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET", "POST"}),
        respect_retry_after_header=True,
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=20, pool_maxsize=20)
    s.mount("http://", adapter)
    s.mount("https://", adapter)
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/128 Safari/537.36",
        "Accept": "application/json,text/plain,*/*",
    })
    return s


def normalize_cookie(raw: str) -> str:
    raw = (raw or "").strip()
    if not raw:
        return ""
    if "MUSIC_U=" in raw:
        import re
        m = re.search(r"MUSIC_U=([^;\s]+)", raw)
        return f"MUSIC_U={m.group(1)};" if m else raw
    return f"MUSIC_U={raw};"


def ts_to_date(ts_ms: Any) -> str | None:
    try:
        if not ts_ms:
            return None
        return datetime.fromtimestamp(int(ts_ms) / 1000, TZ).date().isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def parse_dt(ts_ms: Any) -> datetime | None:
    try:
        if not ts_ms:
            return None
        return datetime.fromtimestamp(int(ts_ms) / 1000, TZ).replace(tzinfo=None)
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def normalize_name(v: Any) -> str:
    return "".join(ch for ch in str(v or "").strip().casefold() if ch.isalnum())


def names_compatible(actual: Any, expected: Iterable[Any]) -> bool:
    a = normalize_name(actual)
    e = {normalize_name(x) for x in expected if normalize_name(x)}
    return not a or not e or a in e


def get_count(data: dict[str, Any]) -> int | None:
    for key in ("count", "total", "size", "albumCount"):
        v = data.get(key)
        try:
            if v is not None and int(v) >= 0:
                return int(v)
        except (TypeError, ValueError):
            pass
    if isinstance(data.get("result"), dict):
        for key in ("count", "total", "size", "albumCount"):
            v = data["result"].get(key)
            try:
                if v is not None and int(v) >= 0:
                    return int(v)
            except (TypeError, ValueError):
                pass
    return None


def safe_get_list(data: dict[str, Any], *keys: str) -> list[Any]:
    for key in keys:
        if isinstance(data.get(key), list):
            return data[key]
    if isinstance(data.get("result"), dict):
        for key in keys:
            if isinstance(data["result"].get(key), list):
                return data["result"][key]
    return []


def api_call(
    session: requests.Session,
    path: str,
    params: dict[str, Any],
    *,
    preferred: str = "GET",
    exact: bool = False,
) -> dict[str, Any]:
    """Local NeteaseCloudMusicApi wrapper. GET first; POST fallback on method/response errors."""
    methods = [preferred.upper()]
    other = "POST" if methods[0] == "GET" else "GET"
    if not exact:
        methods.append(other)

    last: Exception | None = None
    for attempt in range(API_RETRIES):
        for method in methods:
            try:
                start = time.time()
                if method == "POST":
                    r = session.post(API_BASE + path, data=params, timeout=API_TIMEOUT)
                else:
                    r = session.get(API_BASE + path, params=params, timeout=API_TIMEOUT)
                if r.status_code == 405 and not exact:
                    last = RuntimeError(f"{path} GET/POST 405")
                    continue
                r.raise_for_status()
                data = r.json()
                if not isinstance(data, dict):
                    raise RuntimeError(f"{path}: 返回不是 JSON 对象")
                code = data.get("code")
                if code not in (None, 200):
                    # 460 是风控；交给重试层。
                    raise RuntimeError(f"{path}: code={code}")
                return data
            except Exception as exc:
                last = exc
        if attempt + 1 < API_RETRIES:
            time.sleep(0.7 * (attempt + 1) + random.random() * 0.4)
    raise RuntimeError(f"{path} 请求失败：{last}")


def normalize_album(album: dict[str, Any]) -> dict[str, Any]:
    artists_raw = album.get("artists") or album.get("artist") or []
    if isinstance(artists_raw, dict):
        artists_raw = [artists_raw]
    artists = []
    for a in artists_raw if isinstance(artists_raw, list) else []:
        if isinstance(a, dict):
            artists.append({"id": a.get("id"), "name": a.get("name") or "未知艺人"})
    aid = album.get("id")
    pts = album.get("publishTime") or album.get("publish_time")
    return {
        "id": aid,
        "name": album.get("name") or "未命名专辑",
        "artists": artists,
        "artist_names": [a["name"] for a in artists],
        "publish_time": pts,
        "publish_date": ts_to_date(pts),
        "size": album.get("size"),
        "company": album.get("company") or "",
        "pic_url": album.get("picUrl") or album.get("blurPicUrl") or "",
        "sub_type": album.get("subType") or "",
        "type": album.get("type") or "",
        "status": album.get("status"),
        "description": (album.get("description") or "").strip(),
        "url": f"https://music.163.com/#/album?id={aid}" if aid else "https://music.163.com/",
    }


def page_sig(rows: list[Any]) -> tuple[str, ...]:
    ids = []
    for x in rows[:25]:
        if isinstance(x, dict) and x.get("id") is not None:
            ids.append(str(x["id"]))
    return tuple(ids)



def scan_top_album_month(
    session: requests.Session,
    cookie: str,
    area: str,
    target: date,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """补充扫描 /top/album 当月“新碟上架”分页，专门提高 V.A./Various Artists 召回。"""
    collected: dict[str, dict[str, Any]] = {}
    offset = 0
    pages = 0
    limit = PAGE_SIZE
    max_pages = MAX_PAGES
    repeated = 0
    last_sig: tuple[str, ...] | None = None
    incomplete_reason = ""
    reported_total = None

    while pages < max_pages:
        try:
            data = api_call(session, "/top/album", {
                "area": area,
                "type": "new",
                "year": target.year,
                "month": target.month,
                "limit": limit,
                "offset": offset,
                "cookie": cookie,
            }, preferred="GET")
        except Exception as exc:
            incomplete_reason = f"offset={offset} 请求失败：{exc}"
            break

        pages += 1
        rows = safe_get_list(data, "monthData", "albums", "data")
        if reported_total is None:
            reported_total = get_count(data)

        if not rows:
            break
        sig = page_sig(rows)
        if sig and sig == last_sig:
            repeated += 1
            incomplete_reason = f"offset={offset} 返回重复页，分页可能未生效"
            break
        last_sig = sig or last_sig

        for raw in rows:
            if not isinstance(raw, dict):
                continue
            row = normalize_album(raw)
            if row.get("id") is not None and row.get("publish_date") == target.isoformat():
                collected[str(row["id"])] = row

        next_offset = offset + limit
        if reported_total is not None and next_offset >= reported_total:
            offset = next_offset
            break
        if len(rows) < limit:
            # 与 /album/new 一样：短页不能在已知 total 尚未达到时提前结束。
            if reported_total is None:
                try:
                    probe = api_call(session, "/top/album", {
                        "area": area,
                        "type": "new",
                        "year": target.year,
                        "month": target.month,
                        "limit": limit,
                        "offset": next_offset,
                        "cookie": cookie,
                    }, preferred="GET", exact=True)
                    if not safe_get_list(probe, "monthData", "albums", "data"):
                        offset = next_offset
                        break
                except Exception as exc:
                    incomplete_reason = f"短页后尾页确认失败：{exc}"
                    offset = next_offset
                    break
        offset = next_offset
        time.sleep(0.12)

    reached_total = reported_total is not None and offset >= reported_total
    complete = not incomplete_reason and (reached_total or pages < max_pages)
    if pages >= max_pages and not reached_total:
        incomplete_reason = incomplete_reason or f"达到 max_pages={max_pages}"
        complete = False
    return collected, {
        "source": "top_album",
        "area": area,
        "year": target.year,
        "month": target.month,
        "pages_scanned": pages,
        "page_size": limit,
        "reported_total": reported_total,
        "final_offset": offset,
        "target_count": len(collected),
        "repeated_pages": repeated,
        "complete_scan": complete,
        "incomplete_reason": incomplete_reason or None,
    }

def scan_album_new_area(
    session: requests.Session,
    cookie: str,
    area: str,
    target: date,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """全量分页 /album/new；绝不因“短页”在 total 未达到时提前结束。"""
    target_str = target.isoformat()
    collected: dict[str, dict[str, Any]] = {}
    offset = 0
    pages = 0
    reported_total = None
    repeated = 0
    last_sig: tuple[str, ...] | None = None
    method = "GET"
    incomplete_reason = ""
    short_pages = 0

    while pages < MAX_PAGES:
        try:
            data = api_call(session, "/album/new", {
                "area": area,
                "limit": PAGE_SIZE,
                "offset": offset,
                "total": "true",
                "cookie": cookie,
            }, preferred=method)
        except Exception as exc:
            incomplete_reason = f"offset={offset} 请求失败：{exc}"
            break

        pages += 1
        rows = safe_get_list(data, "albums")
        if reported_total is None:
            reported_total = get_count(data)

        if not rows:
            break

        sig = page_sig(rows)
        if sig and sig == last_sig:
            repeated += 1
            # 明确探测另一协议，避免 offset 被服务端忽略。
            try:
                alt = "POST" if method == "GET" else "GET"
                alt_data = api_call(session, "/album/new", {
                    "area": area,
                    "limit": PAGE_SIZE,
                    "offset": offset,
                    "total": "true",
                    "cookie": cookie,
                }, preferred=alt, exact=True)
                alt_rows = safe_get_list(alt_data, "albums")
                if page_sig(alt_rows) and page_sig(alt_rows) != sig:
                    rows = alt_rows
                    method = alt
                    if reported_total is None:
                        reported_total = get_count(alt_data)
                    repeated = 0
                else:
                    incomplete_reason = f"offset={offset} 返回重复页，offset 可能未生效"
                    break
            except Exception as exc:
                incomplete_reason = f"offset={offset} 返回重复页且协议切换失败：{exc}"
                break
        last_sig = page_sig(rows) or last_sig

        for raw in rows:
            if not isinstance(raw, dict):
                continue
            row = normalize_album(raw)
            if row.get("id") is not None and row.get("publish_date") == target_str:
                collected[str(row["id"])] = row

        # 关键：永远按 page_size 前进，不按返回长度前进。
        next_offset = offset + PAGE_SIZE
        if reported_total is not None and next_offset >= reported_total:
            offset = next_offset
            break

        if len(rows) < PAGE_SIZE:
            short_pages += 1
            if reported_total is None:
                # 没 total 时，短页先探下一页确认真正末尾。
                try:
                    probe = api_call(session, "/album/new", {
                        "area": area,
                        "limit": PAGE_SIZE,
                        "offset": next_offset,
                        "total": "true",
                        "cookie": cookie,
                    }, preferred=method, exact=True)
                    probe_rows = safe_get_list(probe, "albums")
                    if not probe_rows:
                        offset = next_offset
                        break
                    if reported_total is None:
                        reported_total = get_count(probe)
                except Exception as exc:
                    incomplete_reason = f"短页后尾页确认失败 offset={next_offset}：{exc}"
                    offset = next_offset
                    break
            # 有 total 但尚未达到 total：继续，不能停。

        offset = next_offset
        time.sleep(0.12)

    reached_total = reported_total is not None and offset >= reported_total
    complete = not incomplete_reason and (reached_total or pages < MAX_PAGES)
    if pages >= MAX_PAGES and not reached_total:
        incomplete_reason = incomplete_reason or f"达到 max_pages={MAX_PAGES}"
        complete = False

    return collected, {
        "source": "album_new",
        "area": area,
        "pages_scanned": pages,
        "page_size": PAGE_SIZE,
        "reported_total": reported_total,
        "final_offset": offset,
        "target_count": len(collected),
        "short_pages": short_pages,
        "repeated_pages": repeated,
        "complete_scan": complete,
        "incomplete_reason": incomplete_reason or None,
        "method": method,
    }


def artist_cache_key(cookie: str) -> str:
    import hashlib
    return hashlib.sha256(cookie.encode("utf-8")).hexdigest()[:16]


def artist_cache_file(cookie: str) -> Path:
    return DATA_DIR / f"artist_cache_{artist_cache_key(cookie)}.json"


def load_artist_cache(cookie: str) -> tuple[list[dict[str, str]], dict[str, Any]] | None:
    p = artist_cache_file(cookie)
    if not p.exists():
        return None
    try:
        obj = json.loads(p.read_text("utf-8"))
        arr = obj.get("artists")
        if not isinstance(arr, list):
            return None
        clean = []
        seen = set()
        for a in arr:
            if isinstance(a, dict) and a.get("id") and a.get("name"):
                sid = str(a["id"])
                if sid not in seen:
                    clean.append({"id": sid, "name": str(a["name"])})
                    seen.add(sid)
        meta = obj.get("meta") if isinstance(obj.get("meta"), dict) else {}
        return clean, meta
    except Exception:
        return None


def save_artist_cache(cookie: str, artists: list[dict[str, str]], meta: dict[str, Any]) -> None:
    save_json_atomic(artist_cache_file(cookie), {"saved_at": time.time(), "count": len(artists), "meta": meta, "artists": artists})


def fetch_followed_artists(session: requests.Session, cookie: str) -> tuple[list[dict[str, str]], dict[str, Any]]:
    cached = load_artist_cache(cookie)
    cache_ttl = float(os.getenv("NETEASE_ARTIST_CACHE_TTL_HOURS", "24"))
    refresh = os.getenv("NETEASE_REFRESH_ARTIST_CACHE", "0") == "1"
    if cached and not refresh:
        artists, meta = cached
        try:
            age = (time.time() - float(meta.get("saved_at", 0))) / 3600
        except Exception:
            age = 999999
        if age <= cache_ttl and meta.get("complete_confirmed") is True:
            print(f"[artist] 使用缓存：{len(artists)} 位艺人，年龄 {age:.1f}h")
            return artists, {**meta, "source": "cache"}

    plans = [500, 200, 100]
    methods = ["GET", "POST"]
    best: list[dict[str, str]] = []
    best_meta: dict[str, Any] = {}

    for limit in plans:
        for method in methods:
            artists: list[dict[str, str]] = []
            seen = set()
            seen_pages = set()
            offset = 0
            page_no = 0
            server_count = None
            end_reason = "unknown"
            had_repeat = False
            error = None
            print(f"[artist] 分页计划 {method} limit={limit}")

            while page_no < 100:
                try:
                    data = api_call(session, "/artist/sublist", {
                        "limit": limit,
                        "offset": offset,
                        "cookie": cookie,
                    }, preferred=method, exact=True)
                except Exception as exc:
                    error = str(exc)
                    end_reason = f"api_error:{exc}"
                    break

                if server_count is None:
                    server_count = get_count(data)
                page = safe_get_list(data, "data", "artists")
                if not page:
                    end_reason = f"empty_page_offset_{offset}"
                    break

                sig = page_sig(page)
                if sig and sig in seen_pages:
                    had_repeat = True
                    end_reason = f"repeat_page_offset_{offset}"
                    break
                if sig:
                    seen_pages.add(sig)

                before = len(artists)
                for a in page:
                    if isinstance(a, dict) and a.get("id") and a.get("name"):
                        sid = str(a["id"])
                        if sid not in seen:
                            artists.append({"id": sid, "name": str(a["name"])})
                            seen.add(sid)
                gained = len(artists) - before
                print(f"[artist] {method} limit={limit} offset={offset}: 返回{len(page)} 新增{gained} 累计{len(artists)} / server≈{server_count}")

                if gained == 0:
                    had_repeat = True
                    end_reason = f"no_new_ids_offset_{offset}"
                    break
                if server_count is not None and len(artists) >= server_count:
                    end_reason = "server_count_reached"
                    break
                if data.get("hasMore") is False or data.get("more") is False:
                    end_reason = "server_has_more_false"
                    break

                # server_count 已明确大于当前数量时，即使当前页短，也继续按 limit 探测。
                if len(page) < limit and server_count is None:
                    next_offset = offset + limit
                    try:
                        probe = api_call(session, "/artist/sublist", {
                            "limit": limit, "offset": next_offset, "cookie": cookie
                        }, preferred=method, exact=True)
                        probe_page = safe_get_list(probe, "data", "artists")
                        if not probe_page:
                            end_reason = "short_page_confirmed_end"
                            break
                    except Exception as exc:
                        error = str(exc)
                        end_reason = f"short_page_probe_error:{exc}"
                        break

                offset += limit
                page_no += 1
                time.sleep(0.12)

            complete_confirmed = bool(
                (server_count is not None and len(artists) >= server_count)
                or (end_reason in ("short_page_confirmed_end", "server_has_more_false") and not (server_count and len(artists) < server_count))
            )
            meta = {
                "method": method,
                "limit": limit,
                "count": len(artists),
                "server_count": server_count,
                "end_reason": end_reason,
                "complete_confirmed": complete_confirmed,
                "had_repeat": had_repeat,
                "error": error,
                "saved_at": time.time(),
            }
            if len(artists) > len(best):
                best, best_meta = artists, meta
            if complete_confirmed:
                save_artist_cache(cookie, artists, meta)
                return artists, {**meta, "source": "live"}

    if best:
        print(f"[artist] 警告：没有计划确认完整，采用最多的一组 {len(best)} 位；meta={best_meta}")
        save_artist_cache(cookie, best, best_meta)
        return best, {**best_meta, "source": "best-effort"}
    raise RuntimeError(f"无法读取关注艺人列表：{best_meta}")


def fetch_recent_for_artist(
    session: requests.Session,
    artist: dict[str, str],
    cookie: str,
) -> list[dict[str, Any]]:
    data = api_call(session, "/artist/songs", {
        "id": artist["id"], "limit": RECENT_LIMIT, "offset": 0,
        "order": "time", "cookie": cookie,
    }, preferred="GET")
    songs = safe_get_list(data, "songs")
    rels = []
    for s in songs:
        if not isinstance(s, dict) or not s.get("id"):
            continue
        al = s.get("al") or {}
        aid = al.get("id")
        if not aid:
            continue
        rels.append({
            "artist_id": artist["id"],
            "artist_name": artist["name"],
            "song_id": str(s["id"]),
            "song_name": s.get("name") or "",
            "album_id": str(aid),
            "album_name": al.get("name") or "",
            "song_publish_ts": s.get("publishTime") or s.get("publish_time") or 0,
            "song_artist_ids": [str(x.get("id")) for x in (s.get("ar") or []) if isinstance(x, dict) and x.get("id")],
            "song_artist_names": [x.get("name") for x in (s.get("ar") or []) if isinstance(x, dict) and x.get("name")],
            "link": f"https://music.163.com/#/song?id={s['id']}",
        })
    return rels


def fetch_new_albums_for_artist(
    session: requests.Session,
    artist: dict[str, str],
    cookie: str,
    start_dt: datetime,
    end_dt: datetime,
) -> list[dict[str, Any]]:
    data = api_call(session, "/artist/album", {
        "id": artist["id"], "limit": NEW_ALBUM_PROBE_LIMIT, "offset": 0,
        "cookie": cookie,
    }, preferred="GET")
    albums = safe_get_list(data, "hotAlbums")
    found = []
    for a in albums:
        if not isinstance(a, dict) or not a.get("id"):
            continue
        dt = parse_dt(a.get("publishTime"))
        if dt and start_dt <= dt <= end_dt:
            found.append({
                "artist_id": artist["id"],
                "artist_name": artist["name"],
                "album_id": str(a["id"]),
                "album_name": a.get("name") or "",
                "publish_ts": a.get("publishTime"),
            })
    return found


def load_album_cache() -> dict[str, dict[str, Any]]:
    if not CACHE_FILE.exists():
        return {}
    try:
        raw = json.loads(CACHE_FILE.read_text("utf-8"))
    except Exception:
        return {}
    if not isinstance(raw, dict):
        return {}
    out = {}
    for k, v in raw.items():
        if not isinstance(v, dict):
            continue
        actual = v.get("album_id") or v.get("id")
        if actual is not None and str(actual) != str(k):
            continue
        if not (v.get("album_name") or v.get("publish_ts") or v.get("date_str")):
            continue
        v = dict(v)
        v["album_id"] = str(k)
        out[str(k)] = v
    return out


def album_cache_needs_recheck(entry: dict[str, Any]) -> bool:
    cached_at = float(entry.get("cached_at") or 0)
    age_h = (time.time() - cached_at) / 3600 if cached_at else 999999
    if not entry.get("publish_ts") or entry.get("date_str") in (None, "未知日期"):
        return age_h >= CACHE_UNKNOWN_HOURS
    dt = parse_dt(entry.get("publish_ts"))
    if not dt:
        return age_h >= CACHE_UNKNOWN_HOURS
    now = datetime.now(TZ).replace(tzinfo=None)
    return abs((now - dt).days) <= 14 and age_h >= CACHE_RECENT_HOURS


def fetch_album_detail(
    session: requests.Session,
    album_id: str,
    cookie: str,
    expected_names: Iterable[str],
    cache: dict[str, dict[str, Any]],
    need_songs: bool = False,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    aid = str(album_id)
    cached = cache.get(aid)
    if (
        cached
        and not album_cache_needs_recheck(cached)
        and names_compatible(cached.get("album_name"), expected_names)
        and (not need_songs or cached.get("songs"))
    ):
        return cached, {"source": "cache", "mismatch": False}

    errors = []
    for attempt in range(3):
        for method in ("GET", "POST"):
            try:
                params = {"id": aid, "cookie": cookie}
                data = api_call(session, "/album", params, preferred=method, exact=True)
                album = data.get("album") or {}
                returned_id = album.get("id")
                if returned_id is not None and str(returned_id) != aid:
                    errors.append(f"{method}: album_id错配 请求={aid} 返回={returned_id}")
                    continue
                if not names_compatible(album.get("name"), expected_names):
                    errors.append(f"{method}: album_name错配 请求={aid} 返回={album.get('name')}")
                    continue
                artists = album.get("artists") or []
                row = {
                    "album_id": aid,
                    "album_name": album.get("name") or "",
                    "date_str": ts_to_date(album.get("publishTime")),
                    "publish_ts": album.get("publishTime"),
                    "artist_ids": [str(x.get("id")) for x in artists if isinstance(x, dict) and x.get("id")],
                    "artist_names": [x.get("name") for x in artists if isinstance(x, dict) and x.get("name")],
                    "size": album.get("size"),
                    "pic_url": album.get("picUrl") or album.get("blurPicUrl") or "",
                    "company": album.get("company") or "",
                    "sub_type": album.get("subType") or "",
                    "type": album.get("type") or "",
                    "cached_at": time.time(),
                }
                if need_songs:
                    compact = []
                    for s in data.get("songs") or []:
                        if not isinstance(s, dict) or not s.get("id"):
                            continue
                        compact.append({
                            "id": str(s["id"]),
                            "name": s.get("name") or "",
                        })
                    row["songs"] = compact
                old = cache.get(aid)
                if old and old.get("songs") and not row.get("songs"):
                    row["songs"] = old["songs"]
                cache[aid] = row
                return row, {"source": method.lower(), "mismatch": False}
            except Exception as exc:
                errors.append(f"{method}: {exc}")
        time.sleep(0.2 + random.random() * 0.3)
    return None, {"source": "error", "mismatch": any("错配" in x for x in errors), "errors": errors[-6:]}


def fetch_owned_ids(
    session: requests.Session,
    artist_id: str,
    cookie: str,
) -> set[str]:
    data = api_call(session, "/artist/album", {
        "id": artist_id,
        "limit": OWNED_LIMIT,
        "offset": 0,
        "cookie": cookie,
    }, preferred="GET")
    out = set()
    for a in data.get("hotAlbums") or []:
        if isinstance(a, dict) and a.get("id"):
            out.add(str(a["id"]))
    return out


def save_json_atomic(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)



def scan_va_search_rescue(
    session: requests.Session,
    cookie: str,
    target: date,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """针对 V.A. / Various Artists 的关键词搜索兜底；这是召回增强，不宣称数学意义上的全量。"""
    terms = ["Various Artists", "V.A.", "华语群星"]
    candidates: dict[str, dict[str, Any]] = {}
    queries_ok = 0
    errors = []

    for term in terms:
        offset = 0
        seen_pages = set()
        for _ in range(10):
            try:
                data = api_call(session, "/search", {
                    "keywords": term,
                    "type": 10,
                    "limit": 100,
                    "offset": offset,
                    "cookie": cookie,
                }, preferred="GET")
                result = data.get("result") or {}
                albums = result.get("albums") if isinstance(result, dict) else []
                if not isinstance(albums, list) or not albums:
                    break
                sig = page_sig(albums)
                if sig in seen_pages:
                    break
                seen_pages.add(sig)
                queries_ok += 1
                for raw in albums:
                    if not isinstance(raw, dict):
                        continue
                    row = normalize_album(raw)
                    if row.get("id") is not None:
                        # 搜索结果的日期可能不全，先收集 album id，由后面的详情严格确认。
                        candidates[str(row["id"])] = row
                if len(albums) < 100:
                    break
                offset += 100
            except Exception as exc:
                errors.append(f"{term}: {exc}")
                break

    # 严格详情校验搜索候选，避免搜索结果里的日期/标题脏数据。
    cache = load_album_cache()
    verified = {}
    for aid, row in list(candidates.items()):
        try:
            local = build_session()
            detail, _ = fetch_album_detail(local, aid, cookie, [row.get("name", "")], cache, need_songs=False)
            if detail and detail.get("date_str") == target.isoformat():
                merged = dict(row)
                merged.update({
                    "name": detail.get("album_name") or row.get("name"),
                    "artists": [{"id": x, "name": n} for x, n in zip(detail.get("artist_ids", []), detail.get("artist_names", []))],
                    "artist_names": detail.get("artist_names") or row.get("artist_names", []),
                    "publish_time": detail.get("publish_ts") or row.get("publish_time"),
                    "publish_date": detail.get("date_str"),
                    "size": detail.get("size"),
                    "company": detail.get("company") or row.get("company", ""),
                    "pic_url": detail.get("pic_url") or row.get("pic_url", ""),
                    "sub_type": detail.get("sub_type") or row.get("sub_type", ""),
                    "type": detail.get("type") or row.get("type", ""),
                })
                verified[aid] = merged
        except Exception as exc:
            errors.append(f"detail {aid}: {exc}")
    save_json_atomic(CACHE_FILE, cache)
    return verified, {
        "source": "va_search_rescue",
        "terms": terms,
        "queries_ok": queries_ok,
        "search_candidates": len(candidates),
        "verified_target_count": len(verified),
        "errors": errors[-10:],
    }

def run_reference_candidate_pool(
    session: requests.Session,
    cookie: str,
    target: date,
    new_start_dt: datetime,
    new_end_dt: datetime,
    external_start_dt: datetime,
    external_end_dt: datetime,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    print("========== 参考算法：Album Candidate Pool ==========")
    artists, artist_meta = fetch_followed_artists(session, cookie)
    print(f"[full] 关注艺人：{len(artists)} 位；recent={RECENT_LIMIT}；new_album_probe={NEW_ALBUM_PROBE_LIMIT}")

    relations: list[dict[str, Any]] = []
    new_candidates: list[dict[str, Any]] = []
    artist_failures = 0

    def worker(artist: dict[str, str]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str | None]:
        local = build_session()
        try:
            rels = fetch_recent_for_artist(local, artist, cookie)
            news = fetch_new_albums_for_artist(local, artist, cookie, new_start_dt, new_end_dt)
            return rels, news, None
        except Exception as exc:
            return [], [], str(exc)

    with ThreadPoolExecutor(max_workers=THREADS) as pool:
        futures = {pool.submit(worker, a): a for a in artists}
        done = 0
        for fut in as_completed(futures):
            done += 1
            rels, news, err = fut.result()
            if err:
                artist_failures += 1
            relations.extend(rels)
            new_candidates.extend(news)
            if done % 25 == 0 or done == len(artists):
                print(f"[full][recent+new] {done}/{len(artists)} 艺人；关系={len(relations)}；新专候选={len(new_candidates)}；失败={artist_failures}")

    by_album: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in relations:
        by_album[str(r["album_id"])].append(r)
    by_new: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for c in new_candidates:
        by_new[str(c["album_id"])].append(c)
    album_ids = list(dict.fromkeys(list(by_album.keys()) + list(by_new.keys())))
    print(f"[full] 候选池构建完成：relations={len(relations)}；unique_album={len(album_ids)}")

    cache = load_album_cache()
    details: dict[str, dict[str, Any]] = {}
    detail_errors = 0
    detail_mismatch = 0

    def detail_worker(aid: str) -> tuple[str, dict[str, Any] | None, dict[str, Any]]:
        local = build_session()
        names = [r.get("album_name") for r in by_album.get(aid, [])]
        names.extend(c.get("album_name") for c in by_new.get(aid, []))
        return (aid, *fetch_album_detail(local, aid, cookie, names, cache, need_songs=(aid in by_new)))

    with ThreadPoolExecutor(max_workers=THREADS) as pool:
        futures = [pool.submit(detail_worker, aid) for aid in album_ids]
        for i, fut in enumerate(as_completed(futures), 1):
            aid, detail, info = fut.result()
            if detail:
                details[aid] = detail
            else:
                detail_errors += 1
            if info.get("mismatch"):
                detail_mismatch += 1
            if i % 50 == 0 or i == len(album_ids):
                print(f"[full][album-detail] {i}/{len(album_ids)}；成功={len(details)}；错误={detail_errors}；错配={detail_mismatch}")

    save_json_atomic(CACHE_FILE, cache)

    external_ids: set[str] = set()
    new_ids: set[str] = set()
    date_hit_details: dict[str, dict[str, Any]] = {}
    for aid, detail in details.items():
        dt = parse_dt(detail.get("publish_ts"))
        if not dt:
            continue
        if external_start_dt <= dt <= external_end_dt:
            external_ids.add(aid)
        if new_start_dt <= dt <= new_end_dt:
            new_ids.add(aid)
        if (external_start_dt <= dt <= external_end_dt) or (new_start_dt <= dt <= new_end_dt):
            date_hit_details[aid] = detail

    verify_artist_ids = {}
    for aid in date_hit_details:
        for r in by_album.get(aid, []):
            verify_artist_ids.setdefault(r["artist_id"], r["artist_name"])
        for c in by_new.get(aid, []):
            verify_artist_ids.setdefault(c["artist_id"], c["artist_name"])

    owned_map: dict[str, set[str]] = {}
    owned_failures = 0
    verify_items = list(verify_artist_ids.items())
    print(f"[full] 日期命中 album={len(date_hit_details)}；需 Owned 验证艺人={len(verify_items)}")

    def owned_worker(item: tuple[str, str]) -> tuple[str, set[str] | None, str | None]:
        artist_id, _ = item
        try:
            local = build_session()
            return artist_id, fetch_owned_ids(local, artist_id, cookie), None
        except Exception as exc:
            return artist_id, None, str(exc)

    with ThreadPoolExecutor(max_workers=THREADS) as pool:
        futures = [pool.submit(owned_worker, x) for x in verify_items]
        for i, fut in enumerate(as_completed(futures), 1):
            aid, owned, err = fut.result()
            if owned is not None:
                owned_map[aid] = owned
            else:
                owned_failures += 1
            if i % 25 == 0 or i == len(verify_items):
                print(f"[full][owned] {i}/{len(verify_items)}；失败={owned_failures}")

    extra_albums: dict[str, dict[str, Any]] = {}
    external_hits = 0
    new_hits = 0
    for aid, detail in date_hit_details.items():
        candidate_rels = by_album.get(aid, [])
        candidate_new = by_new.get(aid, [])
        is_new = aid in new_ids
        is_external = False
        if aid in external_ids:
            for r in candidate_rels:
                owned = owned_map.get(r["artist_id"])
                if owned is not None and aid not in owned:
                    is_external = True
                    break
            # If an album was found only through the new-album probe, it is still a new release,
            # not an external compilation assertion.
        if is_external or is_new:
            if is_external:
                external_hits += 1
            if is_new:
                new_hits += 1
            extra_albums[aid] = detail

    meta = {
        "artists": len(artists),
        "artist_meta": artist_meta,
        "relations": len(relations),
        "new_album_candidates": len(new_candidates),
        "unique_albums": len(album_ids),
        "detail_success": len(details),
        "detail_errors": detail_errors,
        "detail_mismatch": detail_mismatch,
        "date_hit_albums": len(date_hit_details),
        "verify_artists": len(verify_artist_ids),
        "owned_failures": owned_failures,
        "external_hits": external_hits,
        "new_release_hits": new_hits,
        "complete_candidate_pool": bool(artists) and bool(artist_meta.get("complete_confirmed")) and not artist_failures,
        "artist_failures": artist_failures,
    }
    print(f"[full] Candidate Pool 完成：external_hits={external_hits} new_release_hits={new_hits}")
    return extra_albums, meta


def update_date(session: requests.Session, cookie: str, target: date, args: argparse.Namespace) -> dict[str, Any]:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    merged: dict[str, dict[str, Any]] = {}
    global_meta = []

    # 全局源 1：全部新碟，多区域联合召回。
    for area in AREAS:
        rows, meta = scan_album_new_area(session, cookie, area, target)
        global_meta.append(meta)
        for aid, row in rows.items():
            old = merged.get(aid)
            if old is None or len(row.get("artist_names", [])) > len(old.get("artist_names", [])):
                merged[aid] = row
        print(f"[global/album_new] area={area} target={target} 命中={len(rows)} pages={meta['pages_scanned']} total={meta['reported_total']} complete={meta['complete_scan']}")

    # 全局源 2：当月“新碟上架”，专门做第二套召回，覆盖 ALL 接口没有返回的边界数据。
    for area in AREAS:
        rows, meta = scan_top_album_month(session, cookie, area, target)
        global_meta.append(meta)
        for aid, row in rows.items():
            old = merged.get(aid)
            if old is None or len(row.get("artist_names", [])) > len(old.get("artist_names", [])):
                merged[aid] = row
        print(f"[global/top_album] area={area} target={target} 命中={len(rows)} pages={meta['pages_scanned']} total={meta['reported_total']} complete={meta['complete_scan']}")

    va_meta = None
    va_extra = {}
    if args.va_rescue:
        va_extra, va_meta = scan_va_search_rescue(session, cookie, target)
        for aid, row in va_extra.items():
            old = merged.get(aid)
            if old is None:
                merged[aid] = row
            else:
                merged[aid] = {**old, **row}
        print(f"[global/VA-rescue] 命中={len(va_extra)}")

    ref_meta = None
    ref_extra = {}
    if args.full:
        end_external = datetime.combine(target, datetime.max.time())
        start_external = end_external - timedelta(days=args.external_days - 1)
        new_start = datetime.combine(target - timedelta(days=args.new_past_days), datetime.min.time())
        new_end = datetime.combine(target + timedelta(days=args.new_future_days), datetime.max.time())
        ref_extra, ref_meta = run_reference_candidate_pool(
            session,
            cookie,
            target,
            new_start,
            new_end,
            start_external,
            end_external,
        )
        for aid, detail in ref_extra.items():
            # reference Candidate Pool 返回的是 album detail 结构；转换成站点统一的 album JSON。
            row = {
                "id": int(aid) if str(aid).isdigit() else aid,
                "name": detail.get("album_name") or "未命名专辑",
                "artists": [{"id": x, "name": n} for x, n in zip(detail.get("artist_ids", []), detail.get("artist_names", []))],
                "artist_names": detail.get("artist_names", []),
                "publish_time": detail.get("publish_ts"),
                "publish_date": detail.get("date_str"),
                "size": detail.get("size"),
                "company": detail.get("company", ""),
                "pic_url": detail.get("pic_url", ""),
                "sub_type": detail.get("sub_type", ""),
                "type": detail.get("type", ""),
                "status": None,
                "description": "",
                "url": f"https://music.163.com/#/album?id={aid}",
            }
            old = merged.get(aid)
            merged[aid] = row if old is None else {**old, **row}

    # 最终严格只保留目标日期。reference 新 release 可能来自昨天~未来窗口，因此这里只保留当天数据，
    # 未来/昨天会在相应日期的另一轮 rescan 中进入对应文件。
    albums = [row for row in merged.values() if row.get("publish_date") == target.isoformat()]
    albums.sort(key=lambda x: (x.get("publish_time") or 0, str(x.get("name") or "")), reverse=True)

    complete_global = all(bool(m.get("complete_scan")) for m in global_meta)
    complete_full = bool(ref_meta.get("complete_candidate_pool")) if ref_meta else None
    now = datetime.now(TZ).isoformat()
    payload = {
        "date": target.isoformat(),
        "generated_at": now,
        "count": len(albums),
        "scan": {
            "mode": "full" if args.full else "global-authenticated",
            "complete_scan": complete_global and (True if complete_full is None else complete_full),
            "global": global_meta,
            "va_rescue": va_meta,
            "reference_candidate_pool": ref_meta,
            "unique_album_count": len(albums),
        },
        "albums": albums,
    }
    save_json_atomic(DATA_DIR / f"{target.isoformat()}.json", payload)
    print(f"[RESULT] {target.isoformat()}：{len(albums)} albums；complete={payload['scan']['complete_scan']}；mode={payload['scan']['mode']}")
    return payload


def load_index() -> dict[str, Any]:
    p = DATA_DIR / "index.json"
    if not p.exists():
        return {"updated_at": None, "latest_date": None, "dates": []}
    try:
        obj = json.loads(p.read_text("utf-8"))
        return obj if isinstance(obj, dict) else {"dates": []}
    except Exception:
        return {"dates": []}


def update_index(payloads: list[dict[str, Any]]) -> None:
    index = load_index()
    dates = {}
    for d in index.get("dates", []):
        if isinstance(d, dict) and d.get("date"):
            dates[str(d["date"])] = d
    for p in payloads:
        dates[p["date"]] = {
            "date": p["date"],
            "count": p["count"],
            "generated_at": p["generated_at"],
            "complete_scan": p["scan"].get("complete_scan", False),
        }
    rows = sorted(dates.values(), key=lambda x: str(x["date"]), reverse=True)
    save_json_atomic(DATA_DIR / "index.json", {
        "updated_at": datetime.now(TZ).isoformat(),
        "latest_date": rows[0]["date"] if rows else None,
        "dates": rows,
    })


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--date", help="YYYY-MM-DD；默认北京时间今天")
    p.add_argument("--rescan-days", type=int, default=2)
    p.add_argument("--full", action="store_true", help="启用参考算法 Candidate Pool 全量扫描")
    p.add_argument("--va-rescue", action="store_true", help="启用 V.A./Various Artists 关键词召回兜底")
    p.add_argument("--external-days", type=int, default=30)
    p.add_argument("--new-past-days", type=int, default=1)
    p.add_argument("--new-future-days", type=int, default=3)
    return p.parse_args()


def main() -> int:
    args = parse_args()
    cookie = normalize_cookie(os.getenv("NETEASE_MUSIC_U", ""))
    if not cookie:
        print("ERROR: 缺少 NETEASE_MUSIC_U。全量扫描现在要求通过 GitHub Actions Secret 注入网易云 MUSIC_U。", file=sys.stderr)
        return 2

    try:
        start = datetime.strptime(args.date, "%Y-%m-%d").date() if args.date else datetime.now(TZ).date()
    except ValueError:
        print("ERROR: --date 必须是 YYYY-MM-DD", file=sys.stderr)
        return 2

    days = max(1, int(args.rescan_days))
    session = build_session()
    payloads = []

    print("============================================================")
    print("网易云每日新碟 · 全量增强扫描")
    print(f"mode={'FULL Candidate Pool' if args.full else 'GLOBAL AUTHENTICATED'}")
    print(f"date={start.isoformat()} rescan_days={days} threads={THREADS}")
    print("============================================================")

    for i in range(days):
        target = start - timedelta(days=i)
        try:
            payloads.append(update_date(session, cookie, target, args))
        except Exception as exc:
            print(f"ERROR: 抓取 {target.isoformat()} 失败：{exc}", file=sys.stderr)
            return 1

    update_index(payloads)
    print("扫描全部结束。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
