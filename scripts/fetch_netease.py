#!/usr/bin/env python3
"""
网易云每日新碟 - GitHub Actions 全量扫描器

本版本把参考扫描器里的“全量候选池 / 严格校验 / 重试 / 去重 / 缓存 / 完整性诊断”原则，
移植到当前 GitHub Pages + GitHub Actions 架构。

重点修复：
1. 不再因日期边界提前停止分页。
2. 使用固定 offset + limit 推进，避免短页造成跳项。
3. GET / WeAPI 双通道回退。
4. 单页多次重试。
5. 重复页检测，防止 offset 失效死循环。
6. 目标日期 album 全局按 album_id 去重。
7. 可选 album 详情严格校验，检测 album_id / album_name 错配。
8. album 缓存带完整性校验，并对近期/未知日期周期复查。
9. 完整性元数据写入每日日志，明确是否“完整扫描”。
10. JSON 原子写入，避免 Actions 中断留下半文件。
11. 支持深度区域复扫（ALL + ZH/EA/KR/JP）作为额外召回兜底。
"""

import argparse
import base64
import binascii
import json
import os
import random
import sys
import time
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
ALBUM_CACHE_FILE = DATA_DIR / "album_cache.json"

OFFICIAL_BASE = os.getenv("NETEASE_OFFICIAL_BASE", "https://music.163.com").rstrip("/")
USER_AGENT = os.getenv(
    "NETEASE_USER_AGENT",
    "Mozilla/5.0 (Linux; Android 14; iPad) AppleWebKit/537.36 "
    "Chrome/128 Safari/537.36",
)
HEADERS = {
    "User-Agent": USER_AGENT,
    "Referer": "https://music.163.com/",
    "Accept": "application/json,text/plain,*/*",
}

DEFAULT_PAGE_SIZE = max(10, min(int(os.getenv("NETEASE_PAGE_SIZE", "50")), 100))
DEFAULT_MAX_PAGES = max(10, int(os.getenv("NETEASE_MAX_PAGES", "500")))
DEFAULT_DETAIL_WORKERS = max(1, min(int(os.getenv("NETEASE_DETAIL_WORKERS", "6")), 12))
DEFAULT_DETAIL_RETRIES = max(1, int(os.getenv("NETEASE_DETAIL_RETRIES", "3")))
CACHE_RECENT_RECHECK_HOURS = float(
    os.getenv("NETEASE_ALBUM_CACHE_RECENT_RECHECK_HOURS", "12")
)
CACHE_UNKNOWN_RECHECK_HOURS = float(
    os.getenv("NETEASE_ALBUM_CACHE_UNKNOWN_RECHECK_HOURS", "6")
)

# 标准 WeAPI 常量。
MODULUS = (
    "00e0b509f6259df8642dbc35662901477df22677ec152b5ff68ace615bb7b725"
    "152b3ab17a876aea8a5aa76d2e417629ec4ee341f56135fccf695280104e0312"
    "ecbda92557c93870114af6c9d05c4f7f0c3685b7a46bee255932575cce10b424"
    "d813cfe4875d3e82047b97ddef52741d546b8e289dc6935b3ece0462db0a22b8e7"
)
PUBKEY = "010001"
NONCE = "0CoJUm6Qyw8W8jud"
IV = b"0102030405060708"


def build_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=3,
        connect=3,
        read=3,
        backoff_factor=0.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET", "POST"}),
        respect_retry_after_header=True,
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=20, pool_maxsize=20)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update(HEADERS)
    return session


def aes_encrypt(text: str, key: bytes) -> str:
    try:
        from Crypto.Cipher import AES
    except ImportError as exc:
        raise RuntimeError(
            "WeAPI 需要 pycryptodome；requirements.txt 已包含该依赖。"
        ) from exc

    raw = text.encode("utf-8")
    pad = 16 - (len(raw) % 16)
    raw += bytes([pad]) * pad
    return base64.b64encode(
        AES.new(key, AES.MODE_CBC, IV).encrypt(raw)
    ).decode("utf-8")


def rsa_encrypt(sec_key: bytes) -> str:
    value = int(binascii.hexlify(sec_key[::-1]), 16)
    result = pow(value, int(PUBKEY, 16), int(MODULUS, 16))
    return format(result, "x").zfill(256)


def encrypted_request(payload: dict[str, Any]) -> dict[str, str]:
    text = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    secret = os.urandom(16)
    return {
        "params": aes_encrypt(aes_encrypt(text, NONCE.encode("utf-8")), secret),
        "encSecKey": rsa_encrypt(secret),
    }


def parse_json(resp: requests.Response) -> dict[str, Any]:
    resp.raise_for_status()
    try:
        data = resp.json()
    except ValueError as exc:
        snippet = resp.text[:300].replace("\n", " ")
        raise RuntimeError(f"网易云返回的不是 JSON：{snippet}") from exc
    if not isinstance(data, dict):
        raise RuntimeError("网易云返回 JSON 顶层不是对象")
    return data


def normalize_album(album: dict[str, Any]) -> dict[str, Any]:
    artists_raw = album.get("artists") or album.get("artist") or []
    artists = []
    if isinstance(artists_raw, dict):
        artists_raw = [artists_raw]
    for artist in artists_raw if isinstance(artists_raw, list) else []:
        if not isinstance(artist, dict):
            continue
        artists.append(
            {
                "id": artist.get("id"),
                "name": artist.get("name") or "未知艺人",
            }
        )

    album_id = album.get("id")
    publish_ts = album.get("publishTime")
    return {
        "id": album_id,
        "name": album.get("name") or "未命名专辑",
        "artists": artists,
        "artist_names": [x["name"] for x in artists],
        "publish_time": publish_ts,
        "publish_date": ts_to_date(publish_ts),
        "size": album.get("size"),
        "company": album.get("company") or "",
        "pic_url": album.get("picUrl") or album.get("blurPicUrl") or "",
        "sub_type": album.get("subType") or "",
        "type": album.get("type") or "",
        "status": album.get("status"),
        "description": (album.get("description") or "").strip(),
        "url": (
            f"https://music.163.com/#/album?id={album_id}"
            if album_id
            else "https://music.163.com/"
        ),
    }


def ts_to_date(ts_ms: int | None) -> str | None:
    if not ts_ms:
        return None
    try:
        return datetime.fromtimestamp(int(ts_ms) / 1000, TZ).date().isoformat()
    except (OverflowError, OSError, ValueError, TypeError):
        return None


def normalize_name(value: Any) -> str:
    text = str(value or "").strip().casefold()
    return "".join(ch for ch in text if ch.isalnum())


def names_compatible(actual: Any, expected: Iterable[str] | None) -> bool:
    actual_key = normalize_name(actual)
    candidates = {normalize_name(x) for x in (expected or []) if normalize_name(x)}
    return not actual_key or not candidates or actual_key in candidates


def get_total(data: dict[str, Any]) -> int | None:
    for value in (
        data.get("total"),
        data.get("albumCount"),
        (data.get("result") or {}).get("albumCount")
        if isinstance(data.get("result"), dict)
        else None,
        (data.get("result") or {}).get("total")
        if isinstance(data.get("result"), dict)
        else None,
    ):
        try:
            if value is not None:
                return int(value)
        except (TypeError, ValueError):
            pass
    return None


def get_albums(data: dict[str, Any]) -> list[dict[str, Any]]:
    albums = data.get("albums")
    if albums is None and isinstance(data.get("result"), dict):
        albums = data["result"].get("albums")
    if not isinstance(albums, list):
        return []
    return [x for x in albums if isinstance(x, dict)]


def fetch_page_get(
    session: requests.Session, area: str, offset: int, limit: int
) -> dict[str, Any]:
    resp = session.get(
        f"{OFFICIAL_BASE}/api/album/new",
        params={
            "area": area,
            "offset": offset,
            "total": "true",
            "limit": limit,
            "_scan_nonce": f"{int(time.time() * 1000)}-{random.randint(1000, 9999)}",
        },
        timeout=25,
    )
    return parse_json(resp)


def fetch_page_weapi(
    session: requests.Session, area: str, offset: int, limit: int
) -> dict[str, Any]:
    payload = {
        "area": area,
        "offset": offset,
        "total": "true",
        "limit": limit,
        "csrf_token": "",
    }
    resp = session.post(
        f"{OFFICIAL_BASE}/weapi/album/new?csrf_token=",
        data=encrypted_request(payload),
        timeout=25,
    )
    return parse_json(resp)


def fetch_page(
    session: requests.Session,
    area: str,
    offset: int,
    limit: int,
    preferred: str,
) -> tuple[dict[str, Any], str]:
    methods = [preferred, "weapi" if preferred == "get" else "get"]
    errors: list[str] = []

    for method in methods:
        try:
            data = (
                fetch_page_get(session, area, offset, limit)
                if method == "get"
                else fetch_page_weapi(session, area, offset, limit)
            )
            albums = get_albums(data)
            code = data.get("code")
            if code not in (None, 200):
                errors.append(f"{method}: code={code}")
                continue
            if "albums" not in data and not (
                isinstance(data.get("result"), dict) and "albums" in data["result"]
            ):
                errors.append(f"{method}: albums 字段缺失")
                continue
            data["albums"] = albums
            return data, method
        except Exception as exc:
            errors.append(f"{method}: {exc}")

    raise RuntimeError("；".join(errors))


def page_signature(albums: list[dict[str, Any]]) -> tuple[str, ...]:
    ids = [str(x.get("id")) for x in albums if x.get("id") is not None]
    return tuple(ids[:25])


def scan_area(
    session: requests.Session,
    area: str,
    target: date,
    page_size: int,
    max_pages: int,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """
    关键原则：这里只以“API 真正到末尾”为结束条件。
    不再依据“本页日期比目标日期旧”提前停止。
    """
    target_str = target.isoformat()
    collected: dict[str, dict[str, Any]] = {}
    offset = 0
    pages = 0
    reported_total: int | None = None
    preferred = "get"
    methods_used: list[str] = []
    retries = 0
    repeated_pages = 0
    incomplete_reason = ""
    last_sig: tuple[str, ...] | None = None

    while pages < max_pages:
        last_exc: Exception | None = None
        data: dict[str, Any] | None = None
        used_method = preferred

        for attempt in range(3):
            try:
                data, used_method = fetch_page(
                    session, area, offset, page_size, preferred
                )
                break
            except Exception as exc:
                last_exc = exc
                retries += 1
                if attempt < 2:
                    time.sleep(0.7 * (attempt + 1))

        if data is None:
            incomplete_reason = (
                f"offset={offset} 连续失败：{last_exc}"
            )
            break

        preferred = used_method
        if used_method not in methods_used:
            methods_used.append(used_method)

        pages += 1
        albums = get_albums(data)

        if reported_total is None:
            reported_total = get_total(data)

        if not albums:
            # 空页是最可靠的真实末尾信号之一。
            break

        sig = page_signature(albums)
        if sig and sig == last_sig:
            repeated_pages += 1

            # 先切另一种方法，再用较小 limit 做一次修复性探测。
            recovered = False
            for alt_method in (
                "weapi" if used_method == "get" else "get",
            ):
                try:
                    alt_data, alt_used = fetch_page(
                        session, area, offset, page_size, alt_method
                    )
                    alt_albums = get_albums(alt_data)
                    alt_sig = page_signature(alt_albums)
                    if alt_albums and alt_sig and alt_sig != sig:
                        albums = alt_albums
                        preferred = alt_used
                        if alt_used not in methods_used:
                            methods_used.append(alt_used)
                        repeated_pages = 0
                        recovered = True
                        break
                except Exception:
                    continue

            if not recovered:
                incomplete_reason = (
                    f"offset={offset} 返回重复页，offset 可能未生效"
                )
                break

        last_sig = page_signature(albums) or last_sig

        for raw in albums:
            row = normalize_album(raw)
            album_id = row.get("id")
            if album_id is None:
                continue
            if row.get("publish_date") == target_str:
                collected[str(album_id)] = row

        # 固定推进 page_size；不要使用“返回多少就推进多少”，
        # 否则服务端短页可能造成下一页偏移重复/错位。
        next_offset = offset + page_size

        if reported_total is not None and next_offset >= reported_total:
            offset = next_offset
            break

        if len(albums) < page_size and reported_total is None:
            # 没有 total 时，短页通常意味着末尾；再发一次下一页进行确认。
            probe_offset = next_offset
            try:
                probe_data, probe_used = fetch_page(
                    session, area, probe_offset, page_size, preferred
                )
                probe_albums = get_albums(probe_data)
                if not probe_albums:
                    offset = probe_offset
                    break
                # 下一页仍有数据，说明刚才的短页不是安全的停止条件。
            except Exception as exc:
                retries += 1
                incomplete_reason = (
                    f"offset={offset} 短页后尾页确认失败：{exc}"
                )
                offset = probe_offset
                break

        offset = next_offset
        time.sleep(0.18)

    reached_total = reported_total is not None and offset >= reported_total
    complete = not incomplete_reason and (
        reached_total or pages < max_pages
    )

    if pages >= max_pages and not reached_total:
        incomplete_reason = incomplete_reason or (
            f"达到安全上限 max_pages={max_pages}"
        )
        complete = False

    return collected, {
        "area": area,
        "pages_scanned": pages,
        "page_size": page_size,
        "final_offset": offset,
        "reported_total": reported_total,
        "target_count": len(collected),
        "complete_scan": complete,
        "max_pages_reached": pages >= max_pages,
        "methods_used": methods_used,
        "retry_count": retries,
        "repeated_pages": repeated_pages,
        "incomplete_reason": incomplete_reason or None,
    }


def cache_valid(entry: Any, album_id: str) -> bool:
    if not isinstance(entry, dict):
        return False
    actual_id = entry.get("album_id") or entry.get("id")
    if actual_id is not None and str(actual_id) != str(album_id):
        return False
    return bool(
        entry.get("album_name")
        or entry.get("publish_ts")
        or entry.get("date_str")
    )


def load_cache() -> dict[str, Any]:
    if not ALBUM_CACHE_FILE.exists():
        return {}
    try:
        raw = json.loads(ALBUM_CACHE_FILE.read_text("utf-8"))
    except Exception:
        return {}
    if not isinstance(raw, dict):
        return {}
    cleaned = {}
    for key, entry in raw.items():
        if cache_valid(entry, str(key)):
            cleaned[str(key)] = dict(entry)
            cleaned[str(key)]["album_id"] = str(key)
    return cleaned


def save_json_atomic(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(obj, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    tmp.replace(path)


def save_cache(cache: dict[str, Any]) -> None:
    save_json_atomic(ALBUM_CACHE_FILE, cache)


def cache_needs_recheck(entry: dict[str, Any]) -> bool:
    now = datetime.now(TZ).replace(tzinfo=None)
    cached_at = float(entry.get("cached_at") or 0)
    age_hours = (time.time() - cached_at) / 3600 if cached_at else 999999

    publish_ts = entry.get("publish_ts")
    if not publish_ts or entry.get("date_str") in (None, "未知日期"):
        return age_hours >= CACHE_UNKNOWN_RECHECK_HOURS

    published = datetime.fromtimestamp(int(publish_ts) / 1000, TZ).replace(
        tzinfo=None
    )
    return abs((now - published).days) <= 14 and age_hours >= CACHE_RECENT_RECHECK_HOURS


def fetch_album_detail(
    session: requests.Session,
    album_id: str,
    expected_names: Iterable[str] | None,
    cache: dict[str, Any],
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    album_id = str(album_id)
    cached = cache.get(album_id)

    if (
        cached
        and cache_valid(cached, album_id)
        and not cache_needs_recheck(cached)
        and names_compatible(cached.get("album_name"), expected_names)
    ):
        return cached, {"source": "cache", "mismatch": False, "retries": 0}

    errors: list[str] = []
    for attempt in range(DEFAULT_DETAIL_RETRIES):
        for method in ("get", "weapi"):
            try:
                if method == "get":
                    resp = session.get(
                        f"{OFFICIAL_BASE}/api/album",
                        params={
                            "id": album_id,
                            "_detail_nonce": f"{int(time.time()*1000)}-{random.randint(1000,9999)}",
                        },
                        timeout=20,
                    )
                    data = parse_json(resp)
                else:
                    payload = {"id": album_id, "csrf_token": ""}
                    resp = session.post(
                        f"{OFFICIAL_BASE}/weapi/album?csrf_token=",
                        data=encrypted_request(payload),
                        timeout=20,
                    )
                    data = parse_json(resp)

                if data.get("code") not in (None, 200):
                    errors.append(f"{method}: code={data.get('code')}")
                    continue

                album = data.get("album") or {}
                returned_id = album.get("id")
                if returned_id is not None and str(returned_id) != album_id:
                    errors.append(
                        f"{method}: album_id错配 请求={album_id} 返回={returned_id}"
                    )
                    continue

                if not names_compatible(album.get("name"), expected_names):
                    errors.append(
                        f"{method}: album_name疑似错配 请求={album_id} 返回={album.get('name')}"
                    )
                    continue

                row = {
                    "album_id": album_id,
                    "album_name": album.get("name") or "",
                    "date_str": ts_to_date(album.get("publishTime")),
                    "publish_ts": album.get("publishTime"),
                    "artist_ids": [
                        str(x.get("id"))
                        for x in (album.get("artists") or [])
                        if isinstance(x, dict) and x.get("id")
                    ],
                    "artist_names": [
                        x.get("name")
                        for x in (album.get("artists") or [])
                        if isinstance(x, dict) and x.get("name")
                    ],
                    "size": album.get("size"),
                    "pic_url": album.get("picUrl") or "",
                    "cached_at": time.time(),
                }
                cache[album_id] = row
                return row, {
                    "source": method,
                    "mismatch": False,
                    "retries": attempt,
                }
            except Exception as exc:
                errors.append(f"{method}: {exc}")
        if attempt < DEFAULT_DETAIL_RETRIES - 1:
            time.sleep(0.25 + random.random() * 0.25)

    return None, {
        "source": "fallback",
        "mismatch": any("错配" in x for x in errors),
        "retries": DEFAULT_DETAIL_RETRIES,
        "errors": errors[-6:],
    }


def merge_album_rows(
    list_row: dict[str, Any],
    detail: dict[str, Any] | None,
) -> dict[str, Any]:
    if not detail:
        return list_row

    merged = dict(list_row)
    if detail.get("album_name"):
        merged["name"] = detail["album_name"]
    if detail.get("publish_ts"):
        merged["publish_time"] = detail["publish_ts"]
        merged["publish_date"] = detail.get("date_str")
    if detail.get("artist_names"):
        merged["artist_names"] = detail["artist_names"]
    if detail.get("artist_ids"):
        merged["artists"] = [
            {"id": aid, "name": name}
            for aid, name in zip(
                detail.get("artist_ids", []),
                detail.get("artist_names", []),
            )
        ]
    if detail.get("size") is not None:
        merged["size"] = detail["size"]
    if detail.get("pic_url"):
        merged["pic_url"] = detail["pic_url"]
    return merged


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--date",
        help="目标日期 YYYY-MM-DD；默认为北京时间今天",
    )
    parser.add_argument(
        "--rescan-days",
        type=int,
        default=2,
        help="从目标日期往前连续扫描 N 天，默认 2",
    )
    parser.add_argument(
        "--deep-areas",
        action="store_true",
        help="额外扫描 ZH/EA/KR/JP，与 ALL 结果按 album_id 合并",
    )
    parser.add_argument(
        "--page-size",
        type=int,
        default=DEFAULT_PAGE_SIZE,
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=DEFAULT_MAX_PAGES,
    )
    return parser.parse_args()


def update_date(
    session: requests.Session,
    target: date,
    args: argparse.Namespace,
) -> dict[str, Any]:
    cache = load_cache()

    areas = ["ALL"]
    if args.deep_areas:
        areas.extend(["ZH", "EA", "KR", "JP"])

    per_area = []
    merged: dict[str, dict[str, Any]] = {}

    for area in areas:
        rows, meta = scan_area(
            session=session,
            area=area,
            target=target,
            page_size=max(10, min(int(args.page_size), 100)),
            max_pages=max(10, int(args.max_pages)),
        )
        per_area.append(meta)

        for album_id, row in rows.items():
            existing = merged.get(album_id)
            if existing is None:
                merged[album_id] = row
            else:
                # 优先更丰富的记录。
                if len(row.get("artist_names", [])) > len(
                    existing.get("artist_names", [])
                ):
                    merged[album_id] = row

    albums = list(merged.values())

    # 详情验证只作用于目标日期候选池，不会为了全目录逐张请求。
    detail_verified = 0
    detail_fallback = 0
    detail_mismatch = 0
    detail_cache_hits = 0
    detail_errors = 0

    def verify_one(row: dict[str, Any]) -> tuple[str, dict[str, Any], dict[str, Any]]:
        local_session = build_session()
        detail, info = fetch_album_detail(
            local_session,
            str(row["id"]),
            [row.get("name", "")],
            cache,
        )
        return str(row["id"]), row, {"detail": detail, "info": info}

    with ThreadPoolExecutor(max_workers=DEFAULT_DETAIL_WORKERS) as pool:
        futures = [pool.submit(verify_one, row) for row in albums]
        for fut in as_completed(futures):
            try:
                album_id, original, result = fut.result()
                detail = result["detail"]
                info = result["info"]
                if info.get("source") == "cache":
                    detail_cache_hits += 1
                if info.get("mismatch"):
                    detail_mismatch += 1
                if detail:
                    detail_verified += 1
                    merged[album_id] = merge_album_rows(original, detail)
                else:
                    detail_fallback += 1
                    detail_errors += 1
            except Exception:
                detail_fallback += 1
                detail_errors += 1

    albums = list(merged.values())
    albums = [
        x for x in albums
        if x.get("publish_date") == target.isoformat()
    ]
    albums.sort(
        key=lambda x: (
            x.get("publish_time") or 0,
            str(x.get("name") or ""),
        ),
        reverse=True,
    )

    now = datetime.now(TZ).isoformat()
    complete = all(bool(x.get("complete_scan")) for x in per_area)

    payload = {
        "date": target.isoformat(),
        "generated_at": now,
        "count": len(albums),
        "scan": {
            "complete_scan": complete,
            "areas": per_area,
            "areas_scanned": areas,
            "unique_album_count": len(albums),
            "detail_verified": detail_verified,
            "detail_fallback": detail_fallback,
            "detail_mismatch": detail_mismatch,
            "detail_cache_hits": detail_cache_hits,
            "detail_errors": detail_errors,
            "page_size": args.page_size,
            "max_pages": args.max_pages,
        },
        "albums": albums,
    }

    save_json_atomic(
        DATA_DIR / f"{target.isoformat()}.json",
        payload,
    )
    save_cache(cache)

    print(
        f"{target.isoformat()}: {len(albums)} albums; "
        f"areas={','.join(areas)}; "
        f"complete={complete}; "
        f"detail={detail_verified}/{len(albums)}; "
        f"cache_hits={detail_cache_hits}; "
        f"mismatch={detail_mismatch}"
    )
    return payload


def update_index(results: list[dict[str, Any]]) -> None:
    index_file = DATA_DIR / "index.json"
    try:
        index = json.loads(index_file.read_text("utf-8")) if index_file.exists() else {}
    except Exception:
        index = {}

    dates_by_key = {}
    for item in index.get("dates", []):
        if isinstance(item, dict) and item.get("date"):
            dates_by_key[str(item["date"])] = item

    for payload in results:
        dates_by_key[payload["date"]] = {
            "date": payload["date"],
            "count": payload["count"],
            "generated_at": payload["generated_at"],
            "complete_scan": payload.get("scan", {}).get("complete_scan", False),
        }

    dates = sorted(
        dates_by_key.values(),
        key=lambda x: str(x["date"]),
        reverse=True,
    )
    now = datetime.now(TZ).isoformat()
    save_json_atomic(
        index_file,
        {
            "updated_at": now,
            "latest_date": dates[0]["date"] if dates else None,
            "dates": dates,
        },
    )


def main() -> int:
    args = parse_args()
    start = (
        datetime.strptime(args.date, "%Y-%m-%d").date()
        if args.date
        else datetime.now(TZ).date()
    )
    days = max(1, int(args.rescan_days))

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    session = build_session()
    results = []

    for i in range(days):
        target = start - timedelta(days=i)
        try:
            results.append(update_date(session, target, args))
        except Exception as exc:
            print(
                f"抓取 {target.isoformat()} 失败：{exc}",
                file=sys.stderr,
            )
            return 1

    update_index(results)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
