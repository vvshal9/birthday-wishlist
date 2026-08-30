#!/usr/bin/env python3
"""Temporary birthday wishlist. Stdlib only. Last updated: 2026-08-30."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import threading
import time
import uuid
from datetime import datetime, timezone
from html import unescape
from html.parser import HTMLParser
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urljoin, urlparse
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent
PUBLIC = ROOT / "public"
DATA_DIR = ROOT / "data"
STORE_PATH = DATA_DIR / "wishlist.json"
CONFIG_PATH = ROOT / "config.json"
LOCAL_CONFIG_PATH = ROOT / "config.local.json"
STORE_VERSION = 1
BACKUP_PATH = DATA_DIR / "wishlist.json.bak"

ITEM_DEFAULTS = {
    "title": "",
    "url": "",
    "price": None,
    "currency": "RUB",
    "imageUrl": "",
    "comment": "",
    "category": "things",
    "status": "available",
    "claimHash": None,
    "createdAt": None,
    "updatedAt": None,
}
PRIVATE_ITEM_KEYS = {"claimHash"}
CORE_ITEM_KEYS = set(ITEM_DEFAULTS) | {"id"} | PRIVATE_ITEM_KEYS
RESERVED_BODY_KEYS = CORE_ITEM_KEYS | {"manageCode", "claimToken"}

LOCK = threading.Lock()
IMAGE_CACHE_LOCK = threading.Lock()
IMAGE_CACHE: dict[str, tuple[bytes, str, float]] = {}

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
}

PRIVATE_HOSTS = {
    "localhost",
    "127.0.0.1",
    "0.0.0.0",
    "::1",
    "metadata.google.internal",
}

IMAGE_TYPES = {
    "image/jpeg",
    "image/jpg",
    "image/png",
    "image/gif",
    "image/webp",
    "image/avif",
}


def load_json_object(path: Path) -> dict[str, Any]:
    try:
        with path.open(encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def listen_port() -> int:
    raw = str(os.environ.get("PORT") or "").strip()
    if raw.isdigit():
        value = int(raw)
        if 1 <= value <= 65535:
            return value
    if os.environ.get("REPL_ID") or os.environ.get("REPLIT_DEV_DOMAIN"):
        return 8080
    return 8765


def load_config() -> dict[str, Any]:
    defaults = {
        "title": "Вишлист",
        "name": "",
        "note": "",
        "manageCode": "",
    }
    defaults.update({k: v for k, v in load_json_object(CONFIG_PATH).items() if v is not None})
    defaults.update({k: v for k, v in load_json_object(LOCAL_CONFIG_PATH).items() if v is not None})
    env_code = str(os.environ.get("MANAGE_CODE") or "").strip()
    if env_code:
        defaults["manageCode"] = env_code
    return defaults


def public_config() -> dict[str, Any]:
    cfg = load_config()
    return {
        "title": cfg.get("title") or "Вишлист",
        "name": cfg.get("name") or "",
        "note": cfg.get("note") or "",
    }


def manage_code() -> str:
    return str(load_config().get("manageCode") or "").strip()


def empty_store() -> dict[str, Any]:
    return {"version": STORE_VERSION, "items": []}


CATEGORIES = ("certificates", "books", "things")


def guess_category(title: str, url: str) -> str:
    text = ("%s %s" % (title or "", url or "")).lower()
    if any(
        token in text
        for token in (
            "сертификат",
            "certificate",
            "gift_certificate",
            "cuva.ru",
            "aerograd",
            "promocards",
            "прыжок",
        )
    ):
        return "certificates"
    if any(token in text for token in ("chitai-gorod", "книга", "litres", "book24", "буквоед")):
        return "books"
    return "things"


def normalize_item(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    item_id = str(raw.get("id") or "").strip()
    if not item_id:
        return None
    original_category = str(raw.get("category") or "").strip()
    item = dict(raw)
    item["id"] = item_id
    for key, default in ITEM_DEFAULTS.items():
        item.setdefault(key, default)
    item["title"] = str(item.get("title") or "")
    item["url"] = str(item.get("url") or "")
    item["imageUrl"] = str(item.get("imageUrl") or "")
    item["comment"] = str(item.get("comment") or "")
    item["currency"] = str(item.get("currency") or "RUB")
    if item.get("status") not in ("available", "reserved"):
        item["status"] = "available"
    if original_category in CATEGORIES:
        item["category"] = original_category
    else:
        item["category"] = guess_category(item["title"], item["url"])
    return item


def extra_fields_from_body(body: dict[str, Any]) -> dict[str, Any]:
    extra: dict[str, Any] = {}
    for key, value in body.items():
        if key in RESERVED_BODY_KEYS:
            continue
        if not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_]{0,30}", str(key)):
            continue
        if isinstance(value, str):
            extra[key] = value.strip()[:400]
        elif isinstance(value, (int, float, bool)) or value is None:
            extra[key] = value
    return extra


def read_json_file(path: Path) -> dict[str, Any] | None:
    try:
        with path.open(encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def read_store(*, persist_defaults: bool = False) -> dict[str, Any]:
    data = read_json_file(STORE_PATH) or read_json_file(BACKUP_PATH) or empty_store()
    raw_items = data.get("items") if isinstance(data.get("items"), list) else []
    items = []
    missing_category = False
    for raw in raw_items:
        if isinstance(raw, dict) and raw.get("category") not in CATEGORIES:
            missing_category = True
        item = normalize_item(raw)
        if item:
            items.append(item)
    try:
        version = int(data.get("version") or 1)
    except (TypeError, ValueError):
        version = 1
    store = {"version": max(version, STORE_VERSION), "items": items}
    if persist_defaults and missing_category and items:
        write_store(store)
    return store


def write_store(data: dict[str, Any]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": int(data.get("version") or STORE_VERSION),
        "items": [normalize_item(item) or item for item in data.get("items") or []],
    }
    tmp = STORE_PATH.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    if STORE_PATH.exists():
        try:
            shutil.copy2(STORE_PATH, BACKUP_PATH)
        except OSError:
            pass
    tmp.replace(STORE_PATH)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def is_safe_http_url(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return False
    host = (parsed.hostname or "").lower()
    if not host or host in PRIVATE_HOSTS or host.endswith(".local"):
        return False
    if host.startswith("127.") or host.startswith("10.") or host.startswith("192.168."):
        return False
    if re.match(r"^172\.(1[6-9]|2\d|3[0-1])\.", host):
        return False
    if host.startswith("169.254.") or host.startswith("::"):
        return False
    return True


class PageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.metas: list[dict[str, str]] = []
        self.title_chunks: list[str] = []
        self.json_ld: list[str] = []
        self.imgs: list[dict[str, str]] = []
        self.links: list[dict[str, str]] = []
        self._in_title = False
        self._in_ld = False
        self._ld_chunks: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        ad = {k.lower(): (v or "") for k, v in attrs}
        if tag == "meta":
            self.metas.append(ad)
        elif tag == "title":
            self._in_title = True
        elif tag == "img":
            self.imgs.append(ad)
        elif tag == "link":
            self.links.append(ad)
        elif tag == "script" and ad.get("type", "").lower() == "application/ld+json":
            self._in_ld = True
            self._ld_chunks = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
        elif tag == "script" and self._in_ld:
            self.json_ld.append("".join(self._ld_chunks))
            self._in_ld = False
            self._ld_chunks = []

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title_chunks.append(data)
        elif self._in_ld:
            self._ld_chunks.append(data)


def meta_value(metas: list[dict[str, str]], *keys: str) -> str | None:
    wanted = {k.lower() for k in keys}
    for meta in metas:
        prop = (
            meta.get("property")
            or meta.get("name")
            or meta.get("itemprop")
            or ""
        ).lower()
        if prop in wanted:
            content = unescape(meta.get("content") or meta.get("value") or "").strip()
            if content:
                return content
    return None


def walk_json(obj: Any, acc: list[dict[str, Any]]) -> None:
    if isinstance(obj, list):
        for item in obj:
            walk_json(item, acc)
        return
    if not isinstance(obj, dict):
        return
    types = obj.get("@type")
    type_list = types if isinstance(types, list) else [types]
    type_names = {str(t).split("/")[-1] for t in type_list if t}
    if type_names & {"Product", "Offer", "AggregateOffer"}:
        acc.append(obj)
    if "@graph" in obj:
        walk_json(obj["@graph"], acc)
    for value in obj.values():
        if isinstance(value, (dict, list)):
            walk_json(value, acc)


def first_image(value: Any) -> str | None:
    if isinstance(value, str) and value.startswith(("http://", "https://", "//")):
        return value
    if isinstance(value, list):
        for item in value:
            found = first_image(item)
            if found:
                return found
    if isinstance(value, dict):
        return first_image(value.get("url") or value.get("contentUrl") or value.get("@id"))
    return None


def json_images(obj: Any, acc: list[str]) -> None:
    if isinstance(obj, list):
        for item in obj:
            json_images(item, acc)
        return
    if not isinstance(obj, dict):
        return
    for key in ("image", "thumbnailUrl", "thumbnail", "contentUrl", "photo"):
        found = first_image(obj.get(key))
        if found:
            acc.append(found)
    if "@graph" in obj:
        json_images(obj["@graph"], acc)
    for value in obj.values():
        if isinstance(value, (dict, list)):
            json_images(value, acc)


def srcset_urls(value: str) -> list[str]:
    urls = []
    for part in value.split(","):
        token = part.strip().split()
        if token:
            urls.append(token[0])
    return urls


def absolute_url(base: str, raw: str) -> str:
    text = unescape(raw or "").strip()
    if not text or text.startswith("data:"):
        return ""
    if text.startswith("//"):
        text = "https:" + text
    return urljoin(base, text)


SKIP_IMAGE_MARKERS = (
    "logo",
    "favicon",
    "sprite",
    "pixel",
    "1x1",
    "/watch/",
    "tracking",
    "counter",
    "badge",
    "icon",
    "avatar",
    "placeholder",
    "spinner",
    "yandex",
    "google-analytics",
    "doubleclick",
    "facebook.com",
    "mc.yandex",
)

IMAGE_FILE_RE = re.compile(
    r'https?://[^\s"\'<>]{8,400}\.(?:jpe?g|png|webp|avif)(?:\?[^\s"\'<>]{0,200})?',
    re.I,
)
SRC_IMAGE_RE = re.compile(
    r'(?:src|data-src|data-original|content)\s*=\s*["\']([^"\']{1,400}\.(?:jpe?g|png|webp|avif)(?:\?[^"\']{0,200})?)["\']',
    re.I,
)


def image_score(url: str) -> int:
    low = url.lower()
    if any(marker in low for marker in SKIP_IMAGE_MARKERS):
        return -100
    if not is_safe_http_url(url):
        return -100
    score = 1
    if re.search(r"\.(?:jpe?g|webp|png|avif)(?:$|\?)", low):
        score += 3
    for bonus in ("storage", "upload", "catalog", "product", "goods", "images", "photo", "banner", "media", "cdn", "basket", "gift"):
        if bonus in low:
            score += 4
    if "/imgs/" in low or "/static/" in low:
        score += 1
    return score


def pick_image(candidates: list[str], base: str) -> str:
    best = ""
    best_score = 0
    seen = set()
    for raw in candidates:
        url = absolute_url(base, raw)
        if not url or url in seen:
            continue
        seen.add(url)
        score = image_score(url)
        if score > best_score:
            best = url
            best_score = score
    return best if best_score > 0 else ""


def parse_price_number(raw: Any) -> float | None:
    if raw is None or raw is False:
        return None
    if isinstance(raw, (int, float)):
        return float(raw) if raw >= 0 else None
    text = unescape(str(raw))
    text = text.replace("\xa0", " ").replace("&nbsp;", " ")
    text = re.sub(r"[^\d,.\s]", "", text).strip()
    if not text:
        return None
    text = re.sub(r"\s+", "", text)
    if text.count(",") == 1 and text.count(".") == 0:
        text = text.replace(",", ".")
    elif text.count(",") > 0 and text.count(".") > 0:
        text = text.replace(",", "")
    try:
        value = float(text)
    except ValueError:
        return None
    return value if value >= 0 else None


def offer_price(node: dict[str, Any]) -> tuple[float | None, str | None]:
    spec = node.get("priceSpecification")
    spec_price = spec.get("price") if isinstance(spec, dict) else None
    price = parse_price_number(node.get("price") or node.get("lowPrice") or spec_price)
    currency = node.get("priceCurrency") or node.get("currency")
    if not currency and isinstance(node.get("priceSpecification"), dict):
        currency = node["priceSpecification"].get("priceCurrency")
    offers = node.get("offers")
    if price is None and isinstance(offers, dict):
        return offer_price(offers)
    if price is None and isinstance(offers, list) and offers:
        return offer_price(offers[0] if isinstance(offers[0], dict) else {})
    return price, str(currency).upper() if currency else None


def decode_html(raw: bytes, content_type: str) -> str:
    match = re.search(r"charset=([\w-]+)", content_type or "", re.I)
    candidates = []
    if match:
        candidates.append(match.group(1))
    peek = raw[:2500].decode("ascii", errors="ignore")
    meta = re.search(r"charset=['\"]?([\w-]+)", peek, re.I)
    if meta:
        candidates.append(meta.group(1))
    candidates.extend(["utf-8", "windows-1251"])
    seen = set()
    for enc in candidates:
        key = enc.lower()
        if key in seen:
            continue
        seen.add(key)
        try:
            return raw.decode(enc)
        except (LookupError, UnicodeDecodeError):
            continue
    return raw.decode("utf-8", errors="replace")


def fetch_url(
    url: str,
    timeout: int = 8,
    max_bytes: int = 2_000_000,
    referer: str | None = None,
    accept: str | None = None,
) -> tuple[bytes, str, str]:
    headers = dict(BROWSER_HEADERS)
    if referer:
        headers["Referer"] = referer
    if accept:
        headers["Accept"] = accept
    req = Request(url, headers=headers)
    with urlopen(req, timeout=timeout) as resp:
        content_type = resp.headers.get("Content-Type", "")
        raw = resp.read(max_bytes + 1)
        if len(raw) > max_bytes:
            raw = raw[:max_bytes]
        final_url = resp.geturl()
        if not is_safe_http_url(final_url):
            raise ValueError("redirected to a blocked host")
        return raw, content_type, final_url


def preview_from_url(url: str) -> dict[str, Any]:
    if not is_safe_http_url(url):
        raise ValueError("Можно только обычные http/https ссылки на магазины")
    raw, content_type, final_url = fetch_url(url)
    html = decode_html(raw, content_type)
    parser = PageParser()
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        pass

    title = meta_value(parser.metas, "og:title", "twitter:title") or "".join(parser.title_chunks).strip()
    price = parse_price_number(
        meta_value(
            parser.metas,
            "product:price:amount",
            "og:price:amount",
            "price",
            "product:price",
        )
    )
    currency = meta_value(parser.metas, "product:price:currency", "og:price:currency")
    candidates: list[str] = []
    for key in ("og:image", "og:image:secure_url", "twitter:image", "twitter:image:src", "image"):
        found = meta_value(parser.metas, key)
        if found:
            candidates.append(found)
    for link in parser.links:
        rel = (link.get("rel") or "").lower()
        if "image" in rel and link.get("href"):
            candidates.append(link["href"])
    for img in parser.imgs:
        for attr in ("src", "data-src", "data-original", "data-lazy", "data-url"):
            if img.get(attr):
                candidates.append(img[attr])
        if img.get("srcset"):
            candidates.extend(srcset_urls(img["srcset"]))
    for block in parser.json_ld:
        cleaned = re.sub(r"^\s*//.*$", "", block, flags=re.M).strip()
        if not cleaned:
            continue
        try:
            data = json.loads(cleaned)
        except json.JSONDecodeError:
            continue
        json_images(data, candidates)
        nodes: list[dict[str, Any]] = []
        walk_json(data, nodes)
        for node in nodes:
            if not title:
                name = node.get("name")
                if isinstance(name, str) and name.strip():
                    title = name.strip()
            node_price, node_currency = offer_price(node)
            if price is None and node_price is not None:
                price = node_price
            if not currency and node_currency:
                currency = node_currency

    candidates.extend(IMAGE_FILE_RE.findall(html))
    candidates.extend(SRC_IMAGE_RE.findall(html))
    image = pick_image(candidates, final_url)

    if title:
        title = re.sub(r"\s+", " ", unescape(title)).strip()
        if len(title) > 180:
            title = title[:177] + "…"
    currency = (currency or "RUB").upper()
    if currency in {"RUR", "₽"}:
        currency = "RUB"

    return {
        "title": title or "",
        "price": price,
        "currency": currency,
        "imageUrl": image,
    }


def clean_comment(raw: Any) -> str:
    text = unescape(str(raw or "")).replace("\r\n", "\n").strip()
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text[:400]


def public_item(item: dict[str, Any]) -> dict[str, Any]:
    item = normalize_item(item) or {}
    return {key: value for key, value in item.items() if key not in PRIVATE_ITEM_KEYS}


def parsed_item_fields(body: dict[str, Any]) -> dict[str, Any]:
    url = str(body.get("url") or "").strip()
    title = re.sub(r"\s+", " ", str(body.get("title") or "")).strip()
    if not title:
        raise ValueError("Нужно название")
    if not is_safe_http_url(url):
        raise ValueError("Нужна обычная ссылка http/https")
    image_url = str(body.get("imageUrl") or "").strip()
    if image_url and not is_safe_http_url(image_url):
        image_url = ""
    currency = str(body.get("currency") or "RUB").upper()
    category = str(body.get("category") or "").strip()
    if category not in CATEGORIES:
        category = guess_category(title, url)
    return {
        "title": title[:180],
        "url": url,
        "price": parse_price_number(body.get("price")),
        "currency": currency,
        "imageUrl": image_url,
        "comment": clean_comment(body.get("comment")),
        "category": category,
    }


def find_item(store: dict[str, Any], item_id: str) -> dict[str, Any] | None:
    for item in store["items"]:
        if item["id"] == item_id:
            return item
    return None


def read_json_body(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    length = int(handler.headers.get("Content-Length") or 0)
    if length > 1_000_000:
        raise ValueError("Слишком большой запрос")
    raw = handler.rfile.read(length) if length else b"{}"
    if not raw:
        return {}
    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Ожидался объект JSON")
    return data


def has_manage_access(handler: BaseHTTPRequestHandler, body: dict[str, Any] | None = None) -> bool:
    expected = manage_code()
    if not expected:
        return True
    provided = handler.headers.get("X-Manage-Code") or ""
    if body and not provided:
        provided = str(body.get("manageCode") or "")
    return provided.strip().casefold() == expected.casefold()


class Handler(BaseHTTPRequestHandler):
    server_version = "Wishlist/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        sys_stderr = __import__("sys").stderr
        sys_stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _send(self, status: int, body: bytes, content_type: str, extra: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if extra:
            for key, value in extra.items():
                self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def send_error_json(self, status: int, message: str) -> None:
        self.send_json(status, {"error": message})

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/api/config":
            self.send_json(200, public_config())
            return
        if path == "/api/items":
            with LOCK:
                store = read_store(persist_defaults=True)
            items = []
            for raw in store["items"]:
                try:
                    items.append(public_item(raw))
                except Exception:
                    continue
            self.send_json(200, {"items": items, "version": store.get("version", 1)})
            return
        if path == "/api/image":
            self.proxy_image(parse_qs(parsed.query).get("url", [""])[0])
            return
        self.serve_static(path)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            body = read_json_body(self)
        except (ValueError, json.JSONDecodeError):
            self.send_error_json(400, "Не получилось прочитать запрос")
            return

        if path == "/api/auth":
            ok = has_manage_access(self, body)
            self.send_json(200, {"ok": ok})
            return
        if path == "/api/preview":
            url = str(body.get("url") or "").strip()
            try:
                self.send_json(200, preview_from_url(url))
            except Exception as exc:
                self.send_error_json(422, "Не удалось прочитать страницу: %s" % exc)
            return
        if path == "/api/items":
            if not has_manage_access(self, body):
                self.send_error_json(403, "Нужен код, чтобы добавлять подарки")
                return
            self.create_item(body)
            return

        match = re.fullmatch(r"/api/items/([^/]+)/(reserve|unreserve)", path)
        if match:
            item_id, action = match.group(1), match.group(2)
            if action == "reserve":
                self.reserve_item(item_id)
            else:
                self.unreserve_item(item_id, body)
            return
        self.send_error_json(404, "Не найдено")

    def do_DELETE(self) -> None:
        parsed = urlparse(self.path)
        match = re.fullmatch(r"/api/items/([^/]+)", parsed.path)
        if not match:
            self.send_error_json(404, "Не найдено")
            return
        try:
            body = read_json_body(self)
        except (ValueError, json.JSONDecodeError):
            body = {}
        if not has_manage_access(self, body):
            self.send_error_json(403, "Нужен код, чтобы удалять карточки")
            return
        item_id = match.group(1)
        with LOCK:
            store = read_store()
            before = len(store["items"])
            store["items"] = [i for i in store["items"] if i["id"] != item_id]
            if len(store["items"]) == before:
                self.send_error_json(404, "Карточка уже удалена")
                return
            write_store(store)
        self.send_json(200, {"ok": True})

    def do_PATCH(self) -> None:
        parsed = urlparse(self.path)
        match = re.fullmatch(r"/api/items/([^/]+)", parsed.path)
        if not match:
            self.send_error_json(404, "Не найдено")
            return
        try:
            body = read_json_body(self)
        except (ValueError, json.JSONDecodeError):
            self.send_error_json(400, "Не получилось прочитать запрос")
            return
        if not has_manage_access(self, body):
            self.send_error_json(403, "Нужен код, чтобы менять карточки")
            return
        try:
            fields = parsed_item_fields(body)
        except ValueError as exc:
            self.send_error_json(400, str(exc))
            return
        item_id = match.group(1)
        with LOCK:
            store = read_store()
            item = find_item(store, item_id)
            if not item:
                self.send_error_json(404, "Карточка не найдена")
                return
            item.update(fields)
            item.update(extra_fields_from_body(body))
            item["updatedAt"] = datetime.now(timezone.utc).isoformat()
            write_store(store)
        self.send_json(200, {"item": public_item(item)})

    def create_item(self, body: dict[str, Any]) -> None:
        try:
            fields = parsed_item_fields(body)
        except ValueError as exc:
            self.send_error_json(400, str(exc))
            return

        item = {
            "id": str(uuid.uuid4()),
            **fields,
            **extra_fields_from_body(body),
            "status": "available",
            "claimHash": None,
            "createdAt": datetime.now(timezone.utc).isoformat(),
            "updatedAt": datetime.now(timezone.utc).isoformat(),
        }
        with LOCK:
            store = read_store()
            store["items"].append(item)
            write_store(store)
        self.send_json(201, {"item": public_item(item)})

    def reserve_item(self, item_id: str) -> None:
        token = str(uuid.uuid4())
        with LOCK:
            store = read_store()
            item = find_item(store, item_id)
            if not item:
                self.send_error_json(404, "Карточка не найдена")
                return
            if item.get("status") == "reserved":
                self.send_error_json(409, "Уже забронировано")
                return
            item["status"] = "reserved"
            item["claimHash"] = hash_token(token)
            write_store(store)
        self.send_json(200, {"item": public_item(item), "claimToken": token})

    def unreserve_item(self, item_id: str, body: dict[str, Any]) -> None:
        token = str(body.get("claimToken") or "").strip()
        with LOCK:
            store = read_store()
            item = find_item(store, item_id)
            if not item:
                self.send_error_json(404, "Карточка не найдена")
                return
            if item.get("status") != "reserved":
                self.send_json(200, {"item": public_item(item)})
                return
            allowed = has_manage_access(self, body)
            if not allowed:
                if not token or item.get("claimHash") != hash_token(token):
                    self.send_error_json(403, "Снять бронь может тот, кто её ставил — с того же телефона или компьютера")
                    return
            item["status"] = "available"
            item["claimHash"] = None
            write_store(store)
        self.send_json(200, {"item": public_item(item)})

    def sniff_image_mime(self, raw: bytes, content_type: str) -> str | None:
        mime = (content_type or "").split(";")[0].strip().lower()
        if mime in IMAGE_TYPES:
            return mime
        if raw[:3] == b"\xff\xd8\xff":
            return "image/jpeg"
        if raw[:8] == b"\x89PNG\r\n\x1a\n":
            return "image/png"
        if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
            return "image/webp"
        if raw[:3] == b"GIF":
            return "image/gif"
        return None

    def load_image(self, url: str) -> tuple[bytes, str]:
        now = time.time()
        with IMAGE_CACHE_LOCK:
            hit = IMAGE_CACHE.get(url)
            if hit and hit[2] > now:
                return hit[0], hit[1]
        parsed_img = urlparse(url)
        origin = "%s://%s/" % (parsed_img.scheme, parsed_img.netloc)
        last_error: Exception | None = None
        for referer in (origin, None):
            try:
                raw, content_type, _final = fetch_url(
                    url,
                    timeout=8,
                    max_bytes=3_000_000,
                    referer=referer,
                    accept="image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
                )
                mime = self.sniff_image_mime(raw, content_type)
                if mime:
                    with IMAGE_CACHE_LOCK:
                        if len(IMAGE_CACHE) >= 80:
                            oldest = min(IMAGE_CACHE.items(), key=lambda item: item[1][2])[0]
                            IMAGE_CACHE.pop(oldest, None)
                        IMAGE_CACHE[url] = (raw, mime, now + 3600)
                    return raw, mime
            except HTTPError as exc:
                last_error = exc
                try:
                    body = exc.read(3_000_000)
                    mime = self.sniff_image_mime(body, exc.headers.get("Content-Type", ""))
                    if mime:
                        with IMAGE_CACHE_LOCK:
                            IMAGE_CACHE[url] = (body, mime, now + 3600)
                        return body, mime
                except Exception:
                    pass
            except (URLError, TimeoutError, ValueError, OSError) as exc:
                last_error = exc
        raise last_error or ValueError("Картинку не скачать")

    def proxy_image(self, url: str) -> None:
        if not is_safe_http_url(url):
            self.send_error_json(400, "Плохая ссылка на картинку")
            return
        try:
            raw, mime = self.load_image(url)
        except Exception:
            self.send_error_json(502, "Картинку не скачать")
            return
        self._send(200, raw, mime, extra={"Cache-Control": "public, max-age=86400"})

    def serve_static(self, path: str) -> None:
        rel = "index.html" if path in ("", "/") else path.lstrip("/")
        candidate = (PUBLIC / rel).resolve()
        if PUBLIC.resolve() not in candidate.parents and candidate != PUBLIC.resolve():
            self.send_error_json(403, "Forbidden")
            return
        if not candidate.is_file():
            fallback = PUBLIC / "index.html"
            if path.startswith("/api/"):
                self.send_error_json(404, "Не найдено")
                return
            if fallback.is_file() and "." not in Path(rel).name:
                candidate = fallback
            else:
                self.send_error_json(404, "Не найдено")
                return
        data = candidate.read_bytes()
        ext = candidate.suffix.lower()
        types = {
            ".html": "text/html; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".js": "text/javascript; charset=utf-8",
            ".svg": "image/svg+xml",
            ".json": "application/json; charset=utf-8",
            ".png": "image/png",
            ".ico": "image/x-icon",
        }
        cache = "no-store" if ext in {".html", ".js"} else "public, max-age=3600"
        self._send(200, data, types.get(ext, "application/octet-stream"), extra={"Cache-Control": cache})


def main() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not STORE_PATH.exists():
        write_store(empty_store())
    port = listen_port()
    cfg = load_config()
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print("Вишлист: http://127.0.0.1:%s" % port, flush=True)
    if cfg.get("manageCode"):
        print("Режим составления списка: код задан (не печатаю его в лог).", flush=True)
    else:
        print("Режим составления списка выключен: задай MANAGE_CODE или config.local.json.", flush=True)
    print("Ctrl+C чтобы остановить", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nОстановлен")
        server.server_close()


if __name__ == "__main__":
    main()
