#!/usr/bin/env python3
import argparse
import base64
import binascii
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests

TZ = timezone(timedelta(hours=8))
ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"

OFFICIAL_BASE = "https://music.163.com"
USER_AGENT = "Mozilla/5.0 (Linux; Android 14; iPad) AppleWebKit/537.36 Chrome/128 Safari/537.36"
HEADERS = {
    "User-Agent": USER_AGENT,
    "Referer": "https://music.163.com/",
    "Accept": "application/json,text/plain,*/*",
}

# Standard NetEase WeAPI constants used by long-standing open-source clients.
MODULUS = (
    "00e0b509f6259df8642dbc35662901477df22677ec152b5ff68ace615bb7b725"
    "152b3ab17a876aea8a5aa76d2e417629ec4ee341f56135fccf695280104e0312"
    "ecbda92557c93870114af6c9d05c4f7f0c3685b7a46bee255932575cce10b424"
    "d813cfe4875d3e82047b97ddef52741d546b8e289dc6935b3ece0462db0a22b8e7"
)
PUBKEY = "010001"
NONCE = "0CoJUm6Qyw8W8jud"
IV = b"0102030405060708"


def aes_encrypt(text: str, key: bytes) -> str:
    try:
        from Crypto.Cipher import AES
    except ImportError as exc:
        raise RuntimeError("WeAPI 回退模式需要 pycryptodome；GitHub Actions 会自动安装 requirements.txt。") from exc
    raw = text.encode("utf-8")
    pad = 16 - (len(raw) % 16)
    raw += bytes([pad]) * pad
    cipher = AES.new(key, AES.MODE_CBC, IV)
    return base64.b64encode(cipher.encrypt(raw)).decode("utf-8")


def rsa_encrypt(sec_key: bytes) -> str:
    reversed_key = sec_key[::-1]
    value = int(binascii.hexlify(reversed_key), 16)
    result = pow(value, int(PUBKEY, 16), int(MODULUS, 16))
    return format(result, "x").zfill(256)


def encrypted_request(payload: dict[str, Any]) -> dict[str, str]:
    text = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    secret = os.urandom(16)
    return {
        "params": aes_encrypt(aes_encrypt(text, NONCE.encode("utf-8")), secret),
        "encSecKey": rsa_encrypt(secret),
    }


def parse_json_response(resp: requests.Response) -> dict[str, Any]:
    resp.raise_for_status()
    try:
        return resp.json()
    except ValueError as exc:
        snippet = resp.text[:300].replace("\n", " ")
        raise RuntimeError(f"网易云返回的不是 JSON：{snippet}") from exc


def fetch_page_get(session: requests.Session, offset: int, limit: int) -> dict[str, Any]:
    url = f"{OFFICIAL_BASE}/api/album/new"
    params = {"area": "ALL", "offset": offset, "total": "true", "limit": limit}
    resp = session.get(url, params=params, timeout=25)
    return parse_json_response(resp)


def fetch_page_weapi(session: requests.Session, offset: int, limit: int) -> dict[str, Any]:
    url = f"{OFFICIAL_BASE}/weapi/album/new?csrf_token="
    payload = {
        "area": "ALL",
        "offset": offset,
        "total": "true",
        "limit": limit,
        "csrf_token": "",
    }
    resp = session.post(url, data=encrypted_request(payload), timeout=25)
    return parse_json_response(resp)


def fetch_page(session: requests.Session, offset: int, limit: int, use_weapi: bool) -> tuple[dict[str, Any], bool]:
    errors = []
    methods = [fetch_page_weapi, fetch_page_get] if use_weapi else [fetch_page_get, fetch_page_weapi]
    for method in methods:
        try:
            data = method(session, offset, limit)
            if isinstance(data, dict) and ("albums" in data or "result" in data):
                albums = data.get("albums") or data.get("result", {}).get("albums") or []
                if isinstance(albums, list):
                    data["albums"] = albums
                    return data, method is fetch_page_weapi
            errors.append(f"{method.__name__}: unexpected response shape")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{method.__name__}: {exc}")
    raise RuntimeError("；".join(errors))


def ts_to_date(ts_ms: int | None) -> str | None:
    if not ts_ms:
        return None
    try:
        return datetime.fromtimestamp(ts_ms / 1000, TZ).date().isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def normalize_artist(artist: dict[str, Any]) -> dict[str, Any]:
    return {"id": artist.get("id"), "name": artist.get("name") or "未知艺人"}


def normalize_album(album: dict[str, Any]) -> dict[str, Any]:
    artists = [normalize_artist(a) for a in (album.get("artists") or album.get("artist") or []) if isinstance(a, dict)]
    pic = album.get("picUrl") or album.get("blurPicUrl") or ""
    album_id = album.get("id")
    publish_ts = album.get("publishTime")
    name = album.get("name") or "未命名专辑"
    return {
        "id": album_id,
        "name": name,
        "artists": artists,
        "artist_names": [a["name"] for a in artists],
        "publish_time": publish_ts,
        "publish_date": ts_to_date(publish_ts),
        "size": album.get("size"),
        "company": album.get("company") or "",
        "pic_url": pic,
        "sub_type": album.get("subType") or "",
        "type": album.get("type") or "",
        "status": album.get("status"),
        "description": (album.get("description") or "").strip(),
        "url": f"https://music.163.com/#/album?id={album_id}" if album_id else "https://music.163.com/",
    }


def fetch_all_for_date(session: requests.Session, target: datetime.date, page_size: int = 50, max_pages: int = 250) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    all_rows: dict[int, dict[str, Any]] = {}
    offset = 0
    pages = 0
    total_reported = None
    weapi_mode = False
    consecutive_older_pages = 0

    while pages < max_pages:
        data, used_weapi = fetch_page(session, offset, page_size, use_weapi=weapi_mode)
        weapi_mode = used_weapi
        pages += 1
        albums = data.get("albums") or []
        if not isinstance(albums, list):
            raise RuntimeError("albums 字段格式异常")
        if total_reported is None:
            total_reported = data.get("total") or data.get("result", {}).get("albumCount")

        if not albums:
            break

        page_normalized = []
        has_target = False
        has_newer = False
        has_older = False
        for raw in albums:
            if not isinstance(raw, dict):
                continue
            row = normalize_album(raw)
            pid = row.get("id")
            if pid is None:
                continue
            d = row.get("publish_date")
            if d == target.isoformat():
                has_target = True
                all_rows[int(pid)] = row
            elif d and d > target.isoformat():
                has_newer = True
                all_rows[int(pid)] = row
            elif d and d < target.isoformat():
                has_older = True
            page_normalized.append(row)

        # The endpoint is expected to be descending by publish time. Once a page is fully older
        # than the target, a second such page is a safety signal to stop; this still tolerates
        # minor ordering glitches around the date boundary.
        if has_older and not has_target and not has_newer:
            consecutive_older_pages += 1
        else:
            consecutive_older_pages = 0
        if consecutive_older_pages >= 2:
            break

        # When the API reports a total, avoid requesting past the end.
        offset += page_size
        if total_reported is not None:
            try:
                if offset >= int(total_reported):
                    break
            except (TypeError, ValueError):
                pass
        if len(albums) < page_size:
            break
        time.sleep(0.18)

    rows = [r for r in all_rows.values() if r.get("publish_date") == target.isoformat()]
    rows.sort(key=lambda x: (x.get("publish_time") or 0, x.get("name") or ""), reverse=True)
    reached_reported_total = False
    if total_reported is not None:
        try:
            reached_reported_total = offset >= int(total_reported)
        except (TypeError, ValueError):
            reached_reported_total = False
    meta = {
        "pages_scanned": pages,
        "page_size": page_size,
        "reported_total": total_reported,
        "max_pages_reached": pages >= max_pages,
        "complete_scan": consecutive_older_pages >= 2 or reached_reported_total,
        "api_mode": "weapi" if weapi_mode else "get",
    }
    return rows, meta


def load_index() -> dict[str, Any]:
    path = DATA_DIR / "index.json"
    if not path.exists():
        return {"updated_at": None, "latest_date": None, "dates": []}
    try:
        return json.loads(path.read_text("utf-8"))
    except json.JSONDecodeError:
        return {"updated_at": None, "latest_date": None, "dates": []}


def write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def update_date(target: datetime.date) -> None:
    session = requests.Session()
    session.headers.update(HEADERS)
    rows, meta = fetch_all_for_date(session, target)

    now = datetime.now(TZ).isoformat()
    payload = {
        "date": target.isoformat(),
        "generated_at": now,
        "count": len(rows),
        "scan": meta,
        "albums": rows,
    }
    out = DATA_DIR / f"{target.isoformat()}.json"
    write_json(out, payload)

    index = load_index()
    by_date = {d.get("date"): d for d in index.get("dates", []) if isinstance(d, dict) and d.get("date")}
    by_date[target.isoformat()] = {
        "date": target.isoformat(),
        "count": len(rows),
        "generated_at": now,
    }
    dates = sorted(by_date.values(), key=lambda x: x["date"], reverse=True)
    index = {
        "updated_at": now,
        "latest_date": dates[0]["date"] if dates else None,
        "dates": dates,
    }
    write_json(DATA_DIR / "index.json", index)
    print(f"{target.isoformat()}: {len(rows)} albums; pages={meta['pages_scanned']}; mode={meta['api_mode']}")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--date", help="抓取日期 YYYY-MM-DD；默认今天（Asia/Shanghai）")
    p.add_argument("--rescan-days", type=int, default=2, help="同时抓取指定日期及其前 N-1 天；默认 2")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    if args.date:
        start = datetime.strptime(args.date, "%Y-%m-%d").date()
    else:
        start = datetime.now(TZ).date()
    days = max(1, args.rescan_days)
    for i in range(days):
        target = start - timedelta(days=i)
        try:
            update_date(target)
        except Exception as exc:  # noqa: BLE001
            print(f"抓取 {target.isoformat()} 失败：{exc}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
