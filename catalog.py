from dataclasses import dataclass
from html.parser import HTMLParser
import os
import re
from pathlib import Path
from tempfile import NamedTemporaryFile
from threading import Lock
from time import sleep
import unicodedata
from urllib.parse import parse_qs, unquote, urlsplit
from xml.etree import ElementTree

import requests


API_URL = "https://api.kcisa.kr/openapi/service/rest/meta2018/getKSCD0820181"
GOOGLE_BOOKS_URL = "https://www.googleapis.com/books/v1/volumes"
YES24_BOOK_URL = "https://apis.yes24.com/v1/goods/itemDetail"
CATALOG_DOWNLOAD_LOCK = Lock()

# Short editorial notes keyed by the exact edition in the library catalog.
EDITION_NOTES = {
    "9788952784933": (
        "할리우드 감독이 나무 집을 영화로 만들러 오고, 기발한 장치가 가득한 집에서 엉뚱한 소동이 벌어진다.",
        "https://www.yes24.com/product/goods/35244690",
        "YES24 책 소개 요약",
    ),
    "9788943312381": (
        "개구리가 가시연잎을 배 삼아 연못에서 바다로 향하고, 거센 물결을 넘으며 낯선 세상을 만난다.",
        "https://www.yes24.com/product/goods/80921650",
        "YES24 출판사 리뷰 요약",
    ),
    "9788952795090": (
        "앤디와 테리가 말장난 연필을 구하려다 기발한 장치와 뜻밖의 소동을 마주하는 엉뚱한 모험.",
        "https://www.yes24.com/product/goods/67507594",
        "YES24 웹페이지 줄거리",
    ),
    "9788952781819": (
        "기발한 발명품이 가득한 나무 집에서 앤디와 테리가 출판사 사장의 행방을 쫓는 모험.",
        "https://www.yes24.com/product/goods/24452840",
        "YES24 책 소개",
    ),
    "9791168341890": (
        "24절기를 따라 일상의 작은 기쁨을 발견하고 계절이 달라지는 풍경을 관찰하는 에세이.",
        "https://www.yes24.com/product/goods/126120766",
        "YES24 책 소개",
    ),
}


class CatalogError(Exception):
    pass


def xml_root(payload):
    if re.search(rb"<!\s*(DOCTYPE|ENTITY)\b", payload.replace(b"\x00", b""), re.I):
        raise ValueError("XML declarations with entities are unsupported")
    return ElementTree.fromstring(payload)


def fetch_page(session, service_key, rows, page, on_retry):
    for attempt in range(1, 5):
        delay = 2 ** attempt
        try:
            response = session.get(
                API_URL,
                params={"serviceKey": service_key, "numOfRows": rows, "pageNo": page},
                timeout=(10, 45),
                allow_redirects=False,
            )
        except requests.exceptions.SSLError:
            raise CatalogError("API 서버의 HTTPS 연결을 확인할 수 없어요.") from None
        except (requests.ConnectionError, requests.Timeout, requests.exceptions.ChunkedEncodingError):
            problem = "응답이 지연되거나 연결이 끊겼어요."
        except requests.RequestException:
            raise CatalogError(f"{page}쪽 요청을 완료하지 못했어요.") from None
        else:
            if response.status_code == 200:
                return response.content
            problem = f"HTTP {response.status_code} 오류가 발생했어요."
            if response.status_code not in {408, 429, 500, 502, 503, 504}:
                raise CatalogError(f"{page}쪽 요청에서 {problem}")
            retry_after = response.headers.get("Retry-After", "")
            if retry_after.isascii() and retry_after.isdigit() and len(retry_after) <= 5:
                delay = min(60, max(delay, int(retry_after)))
        if attempt < 4:
            on_retry(page, attempt, delay)
            sleep(delay)
    raise CatalogError(f"{page}쪽을 네 번 요청했지만 {problem}")


def clear_checkpoint(checkpoint):
    for path in checkpoint.glob("page-*.xml"):
        if re.fullmatch(r"page-\d{4}\.xml", path.name):
            path.unlink(missing_ok=True)


def pull_catalog(service_key, full, on_page, *, checkpoint=None, on_retry=lambda *_: None):
    """Fetch a preview or every KCISA page; return XML only after success."""
    combined = ElementTree.Element("items")
    seen_pages = set()
    expected_total = None
    rows = 100 if full else 10
    key = unquote(service_key.strip())
    if not full:
        checkpoint = None
    with requests.Session() as session:
        for page in range(1, 2001):
            saved_page = checkpoint / f"page-{page:04d}.xml" if checkpoint is not None else None
            from_disk = saved_page is not None and saved_page.is_file()
            payload = saved_page.read_bytes() if from_disk else fetch_page(session, key, rows, page, on_retry)
            try:
                root = xml_root(payload)
            except (ElementTree.ParseError, LookupError, ValueError):
                raise CatalogError(f"{page}쪽 응답을 XML로 읽을 수 없어요. 저장된 페이지라면 처음부터 다시 받기를 선택해 주세요.") from None
            result = root.findtext("./header/resultCode", "").strip()
            if result != "0000":
                safe_code = result if re.fullmatch(r"[A-Z0-9]{1,12}", result) else "확인 불가"
                raise CatalogError(f"{page}쪽 API 응답 코드: {safe_code}. 서비스키와 승인 상태를 확인해 주세요.")

            total = root.findtext("./body/totalCount", "").strip()
            if total.isascii() and total.isdigit() and len(total) <= 9:
                current_total = int(total)
                if expected_total is not None and current_total != expected_total:
                    raise CatalogError("서버의 전체 도서 수가 바뀌었어요. 처음부터 다시 받기를 선택해 주세요.")
                expected_total = current_total

            entries = root.findall("./body/items/item")
            if not entries:
                if len(combined) and (expected_total is None or len(combined) >= expected_total):
                    break
                raise CatalogError(f"{page}쪽 응답에 도서가 없어요.")
            page_signature = tuple(
                item.findtext("localId") or item.findtext("url") or item.findtext("title")
                for item in entries
            )
            if page_signature in seen_pages:
                raise CatalogError("API에서 같은 목록을 반복해서 보내고 있어요. 다시 시도해 주세요.")
            seen_pages.add(page_signature)
            combined.extend(entries)
            if saved_page is not None and not from_disk:
                saved_root = ElementTree.Element("response")
                ElementTree.SubElement(ElementTree.SubElement(saved_root, "header"), "resultCode").text = result
                body = ElementTree.SubElement(saved_root, "body")
                if expected_total is not None:
                    ElementTree.SubElement(body, "totalCount").text = str(expected_total)
                ElementTree.SubElement(body, "items").extend(entries)
                save_catalog(saved_page, ElementTree.tostring(saved_root, encoding="utf-8", xml_declaration=True))
            on_page(page, len(combined), expected_total)
            if not full or (expected_total is not None and len(combined) >= expected_total):
                break
        else:
            raise CatalogError("페이지 한도에 도달했어요. 데이터 제공기관의 조회 범위를 확인해 주세요.")

    return ElementTree.tostring(combined, encoding="utf-8", xml_declaration=True)


def save_catalog(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=path.parent, suffix=".tmp", delete=False) as temp:
        temporary_path = Path(temp.name)
        try:
            temp.write(payload)
            temp.close()
            temporary_path.replace(path)
        finally:
            temporary_path.unlink(missing_ok=True)


def yes24_key_path():
    local_app_data = os.getenv("LOCALAPPDATA")
    if os.name == "nt" and local_app_data:
        return Path(local_app_data) / "OlympicWalkCatalog" / "yes24-api-key.bin"
    return None


def windows_protect_key(payload, *, decrypt=False):
    import ctypes
    from ctypes import wintypes

    class DataBlob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]

    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    function = crypt32.CryptUnprotectData if decrypt else crypt32.CryptProtectData
    function.argtypes = [
        ctypes.POINTER(DataBlob),
        ctypes.POINTER(wintypes.LPWSTR) if decrypt else wintypes.LPCWSTR,
        ctypes.POINTER(DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(DataBlob),
    ]
    function.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p

    input_buffer = (ctypes.c_ubyte * len(payload)).from_buffer_copy(payload)
    original = DataBlob(len(payload), input_buffer)
    result = DataBlob()
    # Default DPAPI scope is the current Windows user. Suppress any system UI.
    if not function(ctypes.byref(original), None, None, None, None, 1, ctypes.byref(result)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(result.data, result.size)
    finally:
        kernel32.LocalFree(result.data)


def load_yes24_key(path):
    if path is None:
        return ""
    try:
        encrypted = path.read_bytes()
    except FileNotFoundError:
        return ""
    return windows_protect_key(encrypted, decrypt=True).decode("utf-8")


def save_yes24_key(path, key):
    save_catalog(path, windows_protect_key(key.encode("utf-8")))


# KCISA fields and aliases used by older catalog exports.
FIELDS = {
    "title": ("title", "bookNm", "bookName", "bookTitle", "trgetNm", "도서명", "서명"),
    "author": ("author", "writer", "creator", "authorNm", "autNm", "저자", "작가"),
    "description": ("description", "bookDesc", "bookDescription", "bookIntrcn", "summary", "cn", "도서설명", "도서소개"),
    "category": ("subjectCategory", "category", "classification", "classNm", "genre", "분류", "도서분류"),
    "keywords": ("subjectKeyword", "keywords", "주제어"),
    "subject": ("subject", "도서주제"),
    "publisher": ("publisher", "publisherNm", "publshrNm", "pubNm", "출판사"),
    "url": ("url", "link", "homepageUrl", "reference", "detailUrl", "홈페이지URL"),
    "isbn": ("isbn", "isbn13", "isbnNo"),
}

PUBLICATION = re.compile(r"(?P<publisher>[^.!?;,]{0,80}),\s*(?P<year>(?:18|19|20)\d{2})?\.?")


@dataclass(frozen=True)
class Book:
    title: str
    author: str = ""
    description: str = ""
    category: str = ""
    publisher: str = ""
    url: str = ""
    isbn: str = ""
    keywords: str = ""
    subject: str = ""
    publication_year: str = ""


def normalized(text):
    return re.sub(r"[\W_]+", "", unicodedata.normalize("NFKC", text).casefold())


KOREAN_DIGITS = str.maketrans("영공일이삼사오육칠팔구", "00123456789")
SMALL_UNITS = {"십": 10, "백": 100, "천": 1000}
LARGE_UNITS = {"만": 10_000, "억": 100_000_000, "조": 1_000_000_000_000}
NUMBER_WITH_COUNTER = re.compile(
    r"(?<![가-힣0-9])(?P<prefix>제\s*)?"
    r"(?P<number>[0-9영공일이삼사오육칠팔구십백천만억조]{1,24})\s*"
    r"(?P<counter>세기|층|권|화|편|장)"
)


def korean_number(text):
    digits = text.translate(KOREAN_DIGITS)
    total = section = 0
    pending = ""
    previous_small = 10_000
    previous_large = float("inf")
    for character in digits:
        if character.isdigit():
            pending += character
        elif character in SMALL_UNITS:
            unit = SMALL_UNITS[character]
            if unit >= previous_small:
                return None
            section += (int(pending) if pending else 1) * unit
            pending = ""
            previous_small = unit
        else:
            unit = LARGE_UNITS[character]
            if unit >= previous_large:
                return None
            section += int(pending) if pending else 0
            total += (section or 1) * unit
            section = 0
            pending = ""
            previous_small = 10_000
            previous_large = unit
    return total + section + (int(pending) if pending else 0)


def number_search_text(text):
    text = unicodedata.normalize("NFKC", text)

    def replace_counter(match):
        value = korean_number(match["number"])
        if value is None:
            return match[0]
        return f"{match['prefix'] or ''}{value}{match['counter']}"

    return NUMBER_WITH_COUNTER.sub(replace_counter, text)


def search_books(books, query):
    key = normalized(query)
    matches = [book for book in books if key in normalized(book.title) or key in normalized(book.author)]
    if matches:
        return matches, ""
    if not NUMBER_WITH_COUNTER.search(unicodedata.normalize("NFKC", query)):
        return [], ""
    alternate = number_search_text(query)
    alternate_key = normalized(alternate)
    return [book for book in books if alternate_key in normalized(number_search_text(book.title))], alternate


FIELD_ALIASES = {
    name: tuple(normalized(alias) for alias in aliases)
    for name, aliases in FIELDS.items()
}


class TextFromMarkup(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []

    def handle_data(self, text):
        self.parts.append(text.replace("\u200b", ""))


def plain_text(text):
    parser = TextFromMarkup()
    parser.feed(text)
    parser.close()
    return " ".join(" ".join(parser.parts).split())


def read_catalog(files):
    """Read one or more XML response pages and merge duplicate records."""
    books = {}
    bad_files = 0
    for payload in files:
        try:
            root = xml_root(payload)
        except (ElementTree.ParseError, LookupError, ValueError):
            bad_files += 1
            continue

        found = False
        for record in root.iter():
            values = {
                normalized(child.tag.rsplit("}", 1)[-1]): "".join(child.itertext()).strip()
                for child in record
            }
            fields = {
                name: next(
                    (values[key] for key in aliases if values.get(key)),
                    "",
                )
                for name, aliases in FIELD_ALIASES.items()
            }
            # A title by itself can be a channel/header, not a book record.
            if not fields["title"] or not any(value for name, value in fields.items() if name != "title"):
                continue
            for name in ("title", "author", "description", "category", "publisher"):
                fields[name] = plain_text(fields[name])
            if not fields["title"]:
                continue
            if not fields["author"] or fields["author"] in {"누리집", "홈페이지", "한국체육산업개발", "한국체육산업개발주식회사"}:
                author = re.search(r"(?:개인저자|저자)\s*[:：]\s*([^|\r\n]+)", values.get("rights", ""))
                fields["author"] = plain_text(author.group(1)) if author else ""
            for name in ("keywords", "subject"):
                fields[name] = plain_text(fields[name])
            publication = PUBLICATION.fullmatch(fields["description"])
            if publication:
                fields["publisher"] = fields["publisher"] or publication["publisher"].strip()
                fields["publication_year"] = publication["year"] or ""
                fields["description"] = ""
            found = True
            book = Book(**fields)
            identity = (
                ("isbn", normalized(book.isbn))
                if normalized(book.isbn)
                else ("metadata", normalized(book.title), normalized(book.author),
                      normalized(book.publisher), book.url)
            )
            previous = books.get(identity)
            if previous is None or len(book.description) > len(previous.description):
                books[identity] = book
        if not found:
            bad_files += 1
    return list(books.values()), bad_files


def official_book_url(value):
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme == "https"
            and parsed.hostname in {"ksponco.or.kr", "www.ksponco.or.kr"}
            and parsed.path.startswith("/lib/")
            and not parsed.username
            and not parsed.password
        ):
            return value
    except ValueError:
        pass
    return ""


def book_isbn(book):
    candidates = book.isbn
    url = official_book_url(book.url)
    if url:
        candidates += " " + " ".join(parse_qs(urlsplit(url).query).get("isbn", []))
    for candidate in re.findall(r"(?<!\d)97[89]\d{10}(?!\d)", candidates.replace("-", "")):
        if sum(int(digit) * (1 if i % 2 == 0 else 3) for i, digit in enumerate(candidate)) % 10 == 0:
            return candidate
    return ""


def google_book_intro(isbn, api_key):
    try:
        response = requests.get(
            GOOGLE_BOOKS_URL,
            params={"q": f"isbn:{isbn}", "maxResults": 5, "key": api_key},
            timeout=(3, 6),
            allow_redirects=False,
        )
    except requests.RequestException:
        raise CatalogError("Google Books에 연결할 수 없어요.") from None
    if response.status_code != 200:
        raise CatalogError(f"Google Books 조회에 실패했어요 (HTTP {response.status_code}).")
    try:
        body = response.json()
    except ValueError:
        raise CatalogError("Google Books 응답을 읽을 수 없어요.") from None
    for item in body.get("items", []):
        info = item.get("volumeInfo", {})
        exact_match = any(
            identifier.get("type") == "ISBN_13" and identifier.get("identifier") == isbn
            for identifier in info.get("industryIdentifiers", [])
        )
        if exact_match:
            description = plain_text(info.get("description") or "")
            if description:
                return description
    return ""


def yes24_items(search_type, query, api_key):
    for attempt in range(2):
        try:
            response = requests.get(
                YES24_BOOK_URL,
                params={"searchType": search_type, "query": query, "detail": "Y"},
                headers={"X-Api-Key": api_key.strip()},
                timeout=(4, 8),
                allow_redirects=False,
            )
        except requests.exceptions.SSLError:
            raise CatalogError("YES24와 보안 연결을 맺지 못했어요.") from None
        except (requests.Timeout, requests.ConnectionError):
            if attempt:
                raise CatalogError("YES24에 두 번 요청했지만 연결하지 못했어요.") from None
        except requests.RequestException:
            raise CatalogError("YES24에 요청을 보낼 수 없어요.") from None
        else:
            if response.status_code not in {500, 502, 503, 504} or attempt:
                break
        sleep(0.5)
    if response.status_code == 401:
        raise CatalogError("YES24가 API 키를 인증하지 못했어요 (HTTP 401).")
    if response.status_code == 403:
        raise CatalogError("YES24가 이 API 요청을 거부했어요 (HTTP 403).")
    if response.status_code == 429:
        raise CatalogError("YES24 API 요청 한도에 도달했어요 (HTTP 429).")
    if response.status_code == 404:
        return []
    if response.status_code != 200:
        raise CatalogError(f"YES24 조회에 실패했어요 (HTTP {response.status_code}).")
    try:
        body = response.json()
    except ValueError:
        raise CatalogError("YES24 응답을 읽을 수 없어요.") from None
    if not isinstance(body, dict) or body.get("success") is not True:
        raise CatalogError("YES24가 조회 결과를 반환하지 않았어요.")
    return (body.get("data") or {}).get("items") or []


def yes24_book_text(isbn, api_key):
    lookups = [("ISBN13", isbn)]
    note = EDITION_NOTES.get(isbn)
    if note:
        lookups.append(("ItemId", note[1].rsplit("/", 1)[-1]))

    matched_link = ""
    for search_type, query in lookups:
        for item in yes24_items(search_type, query, api_key):
            if re.sub(r"[\s-]", "", str(item.get("isbn13", ""))) != isbn:
                continue
            content = item.get("contentDetail") or {}
            sections = [
                (label, plain_text(content.get(field) or ""))
                for field, label in (
                    ("bookSummary", "줄거리"),
                    ("bookIntroduction", "책 소개"),
                    ("tableOfContents", "목차"),
                )
                if content.get(field)
            ]
            link = item.get("link") or ""
            if not re.fullmatch(r"https://(?:www\.)?yes24\.com/product/goods/\d+/?", link, re.I):
                item_id = str(item.get("itemId", ""))
                link = (
                    f"https://www.yes24.com/product/goods/{item_id}"
                    if re.fullmatch(r"[1-9]\d*", item_id)
                    else (note[1] if note else "")
                )
            if link:
                matched_link = link
                if sections:
                    return sections, link
    return [], matched_link


MOODS = {
    "계절과 자연 관찰": ("계절", "제철", "야생화", "꽃", "정원", "식물", "숲", "자연 관찰", "기후", "생태"),
    "물가와 차분한 사색": ("물결", "바다", "물가", "호수", "하천", "고요", "차분", "잔잔", "휴식", "흐른다", "강가"),
    "역사와 옛 풍경": ("고대", "백제", "역사", "전통", "유적", "문화유산", "옛날", "고고학"),
    "사랑과 사람 이야기": ("사랑", "우정", "연인", "함께", "가족", "관계", "편지", "행복하다는"),
    "도전과 활기": ("도전", "모험", "운동", "달리기", "자전거", "여행", "새로운 시작", "스포츠", "성장", "기발", "엉뚱", "말장난"),
    "기억과 돌아봄": ("추억", "기억", "회고", "철학", "성찰", "되돌아", "사색", "일상"),
}

MOOD_ROUTES = {
    "계절과 자연 관찰": "memory-walk",
    "물가와 차분한 사색": "lakeside-walk",
    "역사와 옛 풍경": "fortress-walk",
    "사랑과 사람 이야기": "lovers-walk",
    "도전과 활기": "youth-walk",
    "기억과 돌아봄": "memory-walk",
}

ROUTE_REASONS = {
    "memory-walk": "몽촌토성을 둘러보고 야생화학습장을 지나는 길에서 책의 장면을 떠올려 보세요.",
    "lakeside-walk": "몽촌해자 물가를 걸으며 책에서 마음에 남은 장면을 천천히 떠올려 보세요.",
    "fortress-walk": "몽촌토성 순환로를 걸으며 책 속 이야기를 공원에 남은 옛 풍경과 연결해 보세요.",
    "lovers-walk": "연인의 길을 따라 소마미술관과 평화의광장 주변을 걸으며 책에서 떠오른 사람을 생각해 보세요.",
    "youth-walk": "공원 외곽을 크게 한 바퀴 돌며 책에서 마음에 남은 도전이나 새로운 시작을 떠올려 보세요.",
}


def infer_mood(book, impression=""):
    book_text = (
        (("독자 감상", normalized(impression), 1),)
        if impression else (
            ("책 소개", normalized(book.description), 3),
            ("주제·주제어", normalized(" ".join((book.keywords, book.subject))), 4),
            ("제목", normalized(book.title), 2),
            ("분류", normalized(book.category), 1),
        )
    )
    scores = {}
    for mood, keywords in MOODS.items():
        hits = {}
        for term in keywords:
            key = normalized(term)
            evidence = [(source, weight) for source, field, weight in book_text if key in field]
            if evidence:
                hits[term] = (sum(weight for _, weight in evidence), [source for source, _ in evidence])
        scores[mood] = (
            sum(weight for weight, _ in hits.values()),
            sorted(hits, key=lambda term: hits[term][0], reverse=True)[:4],
            hits,
        )

    best = max(scores, key=lambda mood: scores[mood][0])
    score, terms, evidence = scores[best]
    tied_routes = {MOOD_ROUTES[mood] for mood, (value, _, _) in scores.items() if value == score}
    if score == 0 or len(tied_routes) > 1:
        return None, [], []
    sources = list(dict.fromkeys(source for term in terms for source in evidence[term][1]))
    return best, terms, sources
