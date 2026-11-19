from dataclasses import dataclass
from difflib import get_close_matches
from html.parser import HTMLParser
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
CATALOG_DOWNLOAD_LOCK = Lock()

# Short editorial notes keyed by the exact edition in the library catalog.
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


def build_search_index(books):
    return [
        (book, normalized(book.title), normalized(book.author), normalized(number_search_text(book.title)))
        for book in books
    ]


def search_books(books, query, index=None):
    key = normalized(query)
    if not key:
        return books, ""
    index = build_search_index(books) if index is None else index
    matches = [book for book, title, author, _ in index if key in title or key in author]
    if matches:
        return matches, ""
    number_query = NUMBER_WITH_COUNTER.search(unicodedata.normalize("NFKC", query))
    if number_query:
        query = number_search_text(query)
        key = normalized(query)
        matches = [book for book, _, _, numbered_title in index if key in numbered_title]
        if matches:
            return matches, "숫자 표기도 함께 검색했어요."
    terms = [term for token in query.split() if (term := normalized(token))]
    if len(terms) > 1:
        matches = [
            book for book, title, author, numbered_title in index
            if all(term in (numbered_title if number_query else title) or term in author for term in terms)
        ]
        if matches:
            return matches, ""
    key = normalized(query)
    # Keep suggestions for longer titles separate from literal catalog matches.
    if len(key) >= 5 and not number_query:
        titles = {title for _, title, _, _ in index if abs(len(title) - len(key)) <= 3}
        close = get_close_matches(key, sorted(titles), n=5, cutoff=0.82)
        if close:
            rank = {title: index for index, title in enumerate(close)}
            suggestions = sorted((entry for entry in index if entry[1] in rank), key=lambda entry: rank[entry[1]])
            return [book for book, _, _, _ in suggestions], "제목이 비슷한 책이에요. 찾던 책이 맞는지 확인해 주세요."
    return [], ""


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


ROUTE_KEYWORDS = {
    "lakeside-walk": tuple(term.replace("_", " ") for term in """
        바다 물결 물가 호수 연못 하천 강가 호숫가 물안개 수면
        파도 해안 해변 바닷가 포구 갯벌 섬 물빛 윤슬 잔물결
        물소리 빗소리 노을 석양 새벽 달빛 별빛 별밤 수평선 지평선
        고요 평온 차분 잔잔 적막 침묵 고즈넉 여유 휴식 휴양
        명상 사유 고독 외로움 위로 치유 안식 쉼 안도 마음챙김
        숨고르기 심호흡 느림 천천히 나지막 정적 고요함 정갈함 포근함 느긋함
        물풀 수련 가시연잎 갈대 수초 물새 오리 백조 잉어 고래
        조개 모래밭 자갈밭 강물 샘물 냇물 시냇물 빗물 개울 폭포
        호반 강변 둔치 수변 계곡 물줄기 조수 밀물 썰물 풍랑
        소나기 이슬비 안개비 잠수 부표 돛단배 나룻배 뱃길 잔향 여운
    """.split()),
    "fortress-walk": tuple(term.replace("_", " ") for term in """
        역사 고대 백제 전통 유적 문화유산 옛날 고고학 선사 유물
        토성 산성 성곽 성벽 성문 고분 왕릉 무덤 사적 유산
        발굴 답사 기행 고증 복원 보존 기록물 사료 문헌 연대기
        조선 고려 고구려 신라 삼국 가야 통일신라 대한제국 일제강점기 중세
        근대 현대사 고대사 한국사 세계사 왕국 제국 왕조 국왕 왕실
        궁궐 왕궁 궁성 도읍 한성 도성 왕위 왕권 왕세자 조정
        전쟁 전투 방어 군영 보루 봉수대 요새 성터 옛터 유적지
        토기 청동기 석기 철기 움집 움집터 고인돌 도자기 기와 비석
        비문 금석문 옛_지도 족보 사당 서원 향교 한옥 종묘 전각
        전승 설화 민속 풍속 관습 의례 제례 역사관 몽촌토성 시대상
    """.split()),
    "memory-walk": tuple(term.replace("_", " ") for term in """
        추억 기억 회고 철학 성찰 되돌아 사색 일상 유년 어린_시절
        옛친구 향수 고향 귀향 재회 회상 자서전 회고록 일기 수기
        기록 그리움 잊혀짐 아련함 지난여름 세월 시간 흘러간 첫_기억 오래전
        계절 제철 야생화 꽃 정원 식물 숲 자연_관찰 기후 생태
        봄 여름 가을 겨울 단풍 낙엽 새싹 꽃잎 꽃망울 풀잎
        나무 나이테 풀꽃 들꽃 민들레 제비꽃 진달래 개나리 국화 코스모스
        곤충 나비 벌레 꽃가루 향기 흙냄새 숲길 오솔길 나무_그늘 나들이
        문학기행 문학여행 소설 동화 시집 수필 에세이 그림책 시인 작가
        책장 책갈피 낭독 독서 이야기책 장면 문장 문학 글쓰기 글귀
        발자취 지난날 오늘 옛사진 앨범 추억담 회한 자책 후회 회복
    """.split()),
    "lovers-walk": tuple(term.replace("_", " ") for term in """
        사랑 우정 연인 가족 관계 편지 공동체 마을목회 이웃 연애
        설렘 첫사랑 짝사랑 고백 청혼 약혼 결혼 신혼 부부 배우자
        데이트 만남 이별 헤어짐 애정 애틋함 다정함 달콤함 애틋 다정
        손길 손잡기 포옹 입맞춤 눈맞춤 미소 웃음 장난 다독임 보살핌
        돌봄 배려 공감 존중 신뢰 동행 동반자 벗 친구 우애
        형제 자매 남매 부모 조부모 할머니 할아버지 손주 자녀 아이들
        가정 우리집 친척 식구 가족사진 나눔 봉사 협동 연대 상생
        마을_공동체 주민 지역사회 마을_사람 대화 소통 화해 용서 이해 감사
        축하 응원 격려 마음 따뜻함 행복 기념일 축제 선물 꽃다발
        예술 미술 조각 전시 미술관 예술가 그림_전시 공연 노래 합창
    """.split()),
    "youth-walk": tuple(term.replace("_", " ") for term in """
        도전 모험 운동 달리기 자전거 여행 새출발 스포츠 성장 기발
        엉뚱 말장난 활동 활기 생동감 에너지 체력 열정 용기 도약
        도전정신 개척 탐험 원정 탐사 미지의_세계 탐험가 미션 임무 발견
        수수께끼 추리 모험담 상상력 판타지 신비 마법 비행 우주 탈출
        위기 극복 해결책 발명 실험 발명품 발상 창의력 호기심 학습
        경기 대회 경쟁 승부 승리 패배 우승 메달 선수 코치
        기록_경신 달리기_대회 단거리 장거리 마라톤 조깅 걷기_경주 릴레이 완주 페이스
        질주 속도 출발선 결승선 등산 트레킹 하이킹 야영 캠핑 사이클
        축구 야구 농구 배구 테니스 수영 체조 태권도 훈련 연습
        진로 꿈 목표 노력 끈기 인내 성취 자립 전환 가능성
    """.split()),
}


ADDITIONAL_KEYWORDS = {
    "lakeside-walk": """
        강변길 호수공원 생태호수 저수지 댐 수로 운하 도랑 웅덩이 개울가
        연못가 강기슭 호숫물 바닷물 샛강 상류 하류 지류 강어귀 하구
        강둑 제방 방파제 선착장 나루터 다리밑 물굽이 소용돌이 강바람 바닷바람
        산들바람 미풍 솔바람 청량함 선선함 서늘함 물방울 빗방울 이슬방울 무지개
        여명 동틀녘 황혼 저녁놀 달무리 은하수 별똥별 초승달 보름달 
        물비늘 반짝임 반영 수묵화 풍경화 부레옥잠 연꽃 잎새 수생식물 수변식물
        수달 물총새 왜가리 백로 청둥오리 흰뺨검둥오리 잠자리 물방개 송사리 피라미
        조약돌 몽돌 해송 연잎 고둥 소라 해초 산호 해양생물 바다_거북 조개껍데기
        고요하다 차분함 평안 평정 평정심 안정 정화 위안 위무 휴식처
        고요히 잔잔함 느긋하다 차분히 쉼터 숨결 호흡 휴식시간 멍하니 느린걸음
    """,
    "fortress-walk": """
        왕조사 향토사 지역사 근현대사 사학 역사학 역사교육 역사기행 고도 고도유적
        순례지 사적지 고분군 선사유적 폐사지 절터 옛도읍 구도심 고건축 기와집
        기단 대들보 처마 단청 궁궐길 고택 사찰 문화재 보물 국보
        벽화 고분벽화 삼족오 고지도 목판 팔만대장경 비문해독 석상 석등 불상
        청자 백자 분청사기 질그릇 주먹도끼 빗살무늬토기 민무늬토기 돌도끼 철검 청동거울
        박물관 유물관 전시유물 문화재청 자료관 기념관 발굴단 지층 유구 유물발굴
        고려청자 조선백자 왕릉군 봉분 능묘 무덤방 제단 기념비 돌담 성가퀴
        삼국시대 고려시대 조선시대 청동기시대 신석기 구석기 한성백제 위례 고조선 신라왕경
        백제문화 왕조실록 세조 세종대왕 왕족 정승 궁중 궁녀 사대부 무관
        민란 항쟁 독립운동 의병 의병장 항일 개화기 식민지 해방기 통치
    """,
    "memory-walk": """
        회상록 추모 고향집 옛집 생가 옛이야기 옛_교실 옛시절 유년기 소년시절
        어린날 학창시절 어릴_적 풋내기 세월의_흔적 책갈피_속 기억_속 꿈결 사무침 그립다
        그리움의기록 추적 회한록 추억여행 인생기록 옛_풍경 옛엽서 흑백_사진 오래된_사진 졸업앨범
        오래된_일기 유고 유년풍경 성장소설 시대소설 산문집 시문학 동시 연작소설 장편소설
        단편소설 시선집 희곡 문학작품 문예 문예집 문학관 문학상 독후감 감상문
        책읽기 도서관 서재 책방 서점 고전_읽기 문예비평 문장수집 필사 소설집
        봄날 여름날 가을날 겨울날 첫눈 서리 계절감 꽃구경 꽃밭 숲속
        꽃씨 씨앗 잔디밭 들판 초원 수풀 초록빛 들녘 산책로 녹음
        봄꽃 매화 목련 벚꽃 산수유 철쭉 수국 접시꽃 해바라기 목화
        억새 억새밭 억새꽃 단풍나무 은행잎 솔잎 잎사귀 꽃송이 잔디 생태공원
    """,
    "lovers-walk": """
        연애담 애정담 사랑_이야기 연애편지 러브레터 러브스토리 연애소설 로맨스 혼인 웨딩
        연애중 첫데이트 밀회 절절함 순애 사랑앓이 실연 감정교류 마음씀씀 다정다감
        결혼식 하객 혼례 혼수 신랑 신부 신혼부부 신혼여행 재혼 부부애
        장미꽃 꽃반지 커플링 반지 커플 연분 인연 약속 친밀감 친근감
        친구사이 우정담 단짝 친구들 첫만남 애착 우애심 화목 기쁨 함께함
        모녀 모자 부녀 부자관계 며느리 사위 시부모 양가 조카 손녀
        아빠 엄마 어머니 아버지 육아 자매애 아기 아들 딸 가정생활
        동네사람 이웃사촌 주민모임 동네_잔치 마을잔치 마을살이 공동육아 생활_협동조합 협동조합 봉사_활동
        관계회복 우정회복 가족화해 상호존중 정서적_지지 마음나눔 정서교류 감정표현 마음표현 생일잔치
        미술전시 조각공원 예술작품 그림감상 예술감상 공동창작 극장 연극 무대 관객
    """,
    "youth-walk": """
        도전기 도전장 탐험담 모험기 탐사대 원정대 탐험대 수색 작전 여정
        배낭여행 오지탐험 체험 야외활동 체험학습 과학실험 로봇제작 발명대회 창작경연 참가자
        지구력 기록_도전 기량 역량 담력 기백 투지 악착같이 과감함 진취적
        열의 패기 의욕 결단 결심 용맹 용맹함 자신감 자기극복 재도약
        재기 재도전 돌파구 새로운_시작 새_도전 도전자 개척자 개척정신 시행착오 목표_달성
        러닝 달리다 산악등반 자전거길 자전거여행 라이딩 자전거도로 사이클링 러너 수영장
        농구장 운동장 육상경기 경기장 체육관 리그 토너먼트 챔피언 결승전 승부차기
        역전 반격 접전 쟁탈전 국가대표 대표팀 단체전 개인전 동메달 금메달
        발명가 과학자 sf 과학소설 모험소설 비밀기지 우주선 달탐사 망원경 우주인
        창업 독립심 자기계발 포부 진취성 모험심 도전의식 실행력 성공담 시련
    """,
}

KEYWORD_PHRASE_GROUPS = {
    "lakeside-walk": """
        한적한 조용한 평화로운 고즈넉한 고요한 | 물가 호숫가 강가 연못 수변
        푸른 짙푸른 드넓은 끝없는 잔잔한 | 바다 호수 강물 수면 물결
        거친 거센 높은 낮은 넘실대는 | 파도 너울 물결 물살 풍랑
        물가의 호숫가의 강가의 해변의 연못가의 | 풍경 산책 쉼터 저녁 아침
        바다의 강의 호수의 물의 자연의 | 소리 숨결 리듬 빛 반짝임
        부드러운 서늘한 따스한 촉촉한 선선한 | 바람 햇살 공기 안개 새벽
        새벽의 한낮의 저녁의 해질녘의 밤의 | 호수 바다 물가 수면 풍경
        조용히 고요히 천천히 느긋하게 가만히 | 걷다 앉다 쉬다 바라보다 머물다
        잔잔한 차분한 평온한 고요한 차가운 | 마음 밤 저녁 아침 시간
        마음의 내면의 일상의 영혼의 몸의 | 평화 균형 휴식 여유 안식
        짧은 긴 느린 혼자만의 고요한 | 산책 휴식 여행 명상 여정
        따뜻한 잔잔한 조용한 뜻밖의 깊은 | 위로 여운 여유 안도 쉼
    """,
    "fortress-walk": """
        고대의 중세의 근대의 조선의 백제의 | 유적 왕궁 문화 역사 도읍
        고대 중세 근대 백제 조선 | 건축 유물 문헌 유산 기록
        남겨진 발견된 출토된 발굴된 복원된 | 유물 토기 비석 유적 유구
        오래된 옛 전통 한옥 조선시대 | 가옥 건물 마을 건축 담장
        고대의 중세의 백제의 신라의 고구려의 | 전쟁 왕국 제도 통치 외교
        왕실의 궁중의 귀족의 왕족의 조정의 | 의례 문화 생활 기록 역사
        역사의 전통의 유적의 선조들의 옛사람들의 | 흔적 기억 이야기 지혜 생활
        역사 유적 문화재 성곽 고분 | 답사 탐방 탐구 연구 조사
        고고학 역사학 고문서 금석문 유물 | 분석 연구 해설 자료 조사
        전통 민속 향토 지역 고대 | 문화 신앙 설화 공예 예술
        잊힌 사라진 되찾은 보존된 재발견된 | 왕국 유적 문화유산 옛터 역사
        옛날의 삼국의 고려의 조선의 근대의 | 풍속 생활상 기록 시대 민속
    """,
    "memory-walk": """
        어린_날의 유년의 학창시절의 먼_날의 오래전의 | 기억 추억 일기 풍경 이야기
        그리운 정겨운 아련한 오래된 희미한 | 기억 추억 고향 풍경 목소리
        지난 흘러간 돌아오지_않는 지나온 보낸 | 시간 세월 나날 계절 시절
        잊지_못할 잊힌 남겨진 사라진 되찾은 | 추억 일기 편지 사진 기억
        사소한 소중한 평범한 특별한 익숙한 | 하루 일상 순간 장면 이야기
        봄의 여름의 가을의 겨울의 사계절의 | 풍경 정원 산책 기억 이야기
        봄날의 여름날의 가을날의 겨울날의 지난여름의 | 햇살 바람 추억 풍경 정원
        숲의 정원의 들판의 나무의 꽃밭의 | 변화 생명 향기 풍경 계절
        초록빛 작은 커다란 울창한 고요한 | 숲 정원 들판 산책길 풀밭
        문학 독서 낭독 글쓰기 시 | 모임 수업 교실 산책 기록
        짧은 긴 오래된 새로운 마음에_남는 | 문장 소설 산문 동화 이야기
        마음_속의 기억_속의 서재_속의 책_속의 삶_속의 | 장면 여운 기억 문장 이야기
    """,
    "lovers-walk": """
        다정한 따뜻한 자상한 친절한 사려_깊은 | 손길 눈빛 마음 말투 배려
        뜻밖의 소중한 특별한 새로운 설레는 | 만남 인연 데이트 동행 사랑
        가족의 부모의 어머니의 아버지의 아이의 | 사랑 마음 손길 기억 응원
        새삼_느끼는 풋풋한 서툰 아름다운 애틋한 | 사랑 연애 고백 첫사랑 만남
        친구의 자매의 연인의 이웃의 동료의 | 위로 응원 편지 배려 격려
        깊은 오래된 돈독한 변함없는 끈끈한 | 우정 정 애정 신뢰 인연
        함께한 함께하는 둘만의 다정했던 웃음_가득한 | 시간 순간 추억 여행 하루
        어려운 소원한 서먹한 복잡한 어색한 | 관계 대화 만남 화해 소통
        이웃과 가족과 친구와 연인과 동료와 | 함께 소통 화해 동행 협력
        지역 교육 이웃 돌봄 생활 | 공동체 모임 축제 활동 문화
        가족관계 부부관계 부모_자녀 친구_사이 이웃관계 | 이야기 갈등 회복 소통 애정
        작은 정다운 고마운 포근한 다사로운 | 나눔 인사 선물 손길 위로
    """,
    "youth-walk": """
        새로운 대담한 과감한 거침없는 낯선 | 도전 모험 여정 시도 여행
        처음_하는 반가운 어려운 흥미로운 재미있는 | 도전 실험 체험 훈련 시도
        멈추지_않는 끝없는 계속되는 반복되는 끊임없는 | 도전 연습 훈련 노력 시도
        극한의 한계에 미지로 우주로 정상으로 | 도전 비행 여행 여정 출발
        두려움을 역경을 난관을 위기를 실패를 | 극복 이기다 딛고 넘어서 넘다
        육상 체력 자전거 스포츠 경기 | 훈련 대회 체험 연습 도전
        전국 국제 지역 학교 아마추어 | 대회 경기 리그 선수권 토너먼트
        모험의 도전의 탐험의 여행의 성장의 | 시작 여정 세계 이야기 기록
        상상의 환상의 새로운 낯선 광활한 | 세계 공간 우주 대륙 도시
        창의적인 기발한 엉뚱한 참신한 독창적인 | 아이디어 발상 발명 실험 시도
        성공을 꿈을 목표를 기록을 가능성을 | 향해 좇아 이루다 쫓다 찾아
        씩씩한 당찬 활기찬 용감한 패기_있는 | 도전 탐험 모험 성장 출발
    """,
}

EXTRA_KEYWORD_PHRASE_GROUPS = {
    "lakeside-walk": """
        밀려오는 밀려드는 밀려나가는 부서지는 잦아드는 | 파도 너울 물결 포말 파랑
        수평선의 저편의 맞은편의 먼바다의 먼_곳의 | 불빛 윤곽 노을 배 선박
        물에_비친 수면에_비친 물가에_비친 호수에_비친 연못에_비친 | 달 별 달빛 구름 나무 산 그림자 하늘 불빛 햇살
        물가를_따라 호숫가를_따라 해안을_따라 강변을_따라 둔치를_따라 | 걷다 걷는 걸으며 걷기 거닐다 거니는 거닐며 산책하다 산책하는 돌아오다
        수면_아래 물속에 물속의 물_아래 물밑의 | 물풀 수초 모래 돌 조약돌 바닥 물고기 잉어 작은_생물 풍경
        물소리에 파도_소리에 빗소리에 바람_소리에 물새_소리에 | 귀를_기울이다 귀_기울이다 귀를_기울이며 잠이_들다 잠드는 묻히다 잠기다 익숙해지다 위로받다 기대다
        새벽_물가의 늦은_밤의 한밤중의 저녁_강가의 이른_아침의 | 고요 정적 침묵 적막 바람 공기 안개 물소리 물결 수면
        흐르는_물처럼 잔잔한_물결처럼 밀려오는_파도처럼 저무는_햇살처럼 서늘한_바람처럼 | 천천히 조용히 느리게 잔잔히 고요히 내려앉다 지나가다 사라지다 번지다 흔들리다
        수면을_스치는 물결을_스치는 물가를_지나는 물_위를_지나는 호수를_가로지르는 | 바람 바람결 햇살 그림자 불빛 새 물새 오리 배 나룻배
        차분히 조용히 가만히 홀로 느긋하게 | 숨을_고르다 마음을_가라앉히다 눈을_감다 생각을_정리하다 물결을_바라보다 바람을_느끼다 햇살을_받다 수평선을_보다 물가에_앉다 명상에_잠기다
        강가에 호숫가에 바닷가에 연못가에 잔잔한_물가에 | 걸터앉다 자리_잡다 발을_담그다 한동안_머물다 오래_앉다 앉아_있다 멈춰_서다 잠시_쉬다 돗자리를_펴다 시선을_두다
        한강의 강변의 하천의 개울의 계곡의 | 자갈 물굽이 바람 물새 둔치 물소리 물길 풀숲 물안개 물빛
        맑게_흐르는 투명하게_흐르는 굽이쳐_흐르는 유유히_흐르는 느리게_흐르는 | 시냇물 하천 계곡물 강물 개울 샘물 물줄기 물 흐름 물길
    """,
    "fortress-walk": """
        한성_백제의 웅진_백제의 사비_백제의 삼국_시대의 통일_신라의 | 왕궁 왕릉 도읍 성벽 유물 불교 기록 의례 건축 문화
        흙으로_쌓은 돌로_쌓은 오랫동안_남은 발굴로_드러난 복원_중인 | 성곽 토성 성벽 성문 성터 담장 석축 옛_길 기단 유구
        도굴된 무너진 훼손된 일제강점기에_조사된 다시_복원된 | 고분 왕릉 무덤 유적 건축물 석탑 성벽 비석 사찰 성문
        금속으로_만든 흙으로_빚은 출토된 그대로_보존된 전시실에_놓인 | 그릇 토기 장신구 유물 제기 생활용품 모형 공예품 무기 도구
        탐방객이_찾는 학예사가_설명하는 해설사가_안내하는 고고학자가_조사한 발굴팀이_확인한 | 유적지 고분군 토성 성벽 문화재 사찰터 유적 왕릉 유물 전시관
        문화재의 고고학의 지역사의 생활사의 풍속사의 | 보존 전승 기록 연구 교육 해설 조사 재현 전시 발자취
        왕실에서 궁궐에서 도읍에서 왕국에서 옛_마을에서 | 사용된 전해진 살았던 지켰던 발굴된 기록된 이어진 발견된 만들어진 드러난
        조선_전기의 조선_후기의 고려_후기의 개화기의 일제강점기의 | 의복 신분 제도 교육 직업 생활사 연대 기록 문헌 생활상
        탁본으로 유물로 기록으로 실록으로 고문서로 | 전해지는 확인하는 남겨진 입증된 풀어낸 조사한 복원한 알려진 읽는 살펴본
        발굴_현장의 답사_현장의 유적_조사의 문화재_수리의 보존_처리의 | 기록 일지 보고서 사진 지도 도면 단서 작업 과정 자료
        옛_도읍의 폐허가_된 사람의_손길이_남은 세월을_견딘 오래_묻혀_있던 | 창고 다리 터 기와 돌담 석축 마을 성터 토담 골목
    """,
    "memory-walk": """
        떠오르는 되살아나는 사라져가는 흐릿해지는 문득_생각나는 | 어린_시절 옛_친구 고향_풍경 기억 한때 일상 사진 냄새 목소리 장면
        손때_묻은 빛바랜 색이_바랜 오래_접어둔 서랍_속의 | 가족사진 사진첩 편지 엽서 공책 일기장 수첩 지도 낙서 교과서
        잃어버린 잊고_지낸 다시_꺼낸 가끔_돌이켜보는 그때의 | 유년기 약속 꿈 이야기 한_장면 첫날 오후 풍경 일기 노랫소리
        동네_골목의 고향_마을의 옛_학교의 할머니_댁의 어린_시절의 | 놀이터 뒷마당 문방구 운동장 정류장 가로수 옛집 들판 논밭 버스
        갓_피어난 활짝_핀 하나둘_피는 새로_돋은 막_자라난 | 새잎 새순 풀잎 새싹 들꽃 야생화 꽃잎 꽃봉오리 식물 봄꽃
        늦봄의 초여름의 늦가을의 초겨울의 한겨울의 | 숲길 그늘 햇빛 향기 빛깔 꽃밭 바람 햇살 정원 날씨
        나무_사이의 좁은_숲길의 숲속의 숲_가장자리의 집_앞_마당의 | 그늘 나뭇잎 나뭇가지 꽃밭 흙길 풀 내음 풀꽃 장면 계절
        계절이_바뀌는 봄이_오는 여름이_가는 가을이_깊어지는 겨울이_오는 | 하루 날 길목 산책길 시간 무렵 풍경 동네 숲 마을
        낙엽이_떨어지는 풀벌레가_우는 눈이_쌓이는 서리가_내리는 비가_그친 | 아침 저녁 들판 시골 정원 공터 골목 운동장 숲길 오솔길
        다시_방문한 오랜만에_돌아간 옛_생각이_나는 시간을_거슬러_찾은 오래_비워둔 | 학교 마을 놀이터 고향 교실 책방 도서관 기찻길 텃밭 집
        조용히_읽는 혼자_다시_읽는 책장에_남겨둔 어릴_때_즐겨_읽던 다시_펼쳐_보는 | 동화집 시집 그림책 수필집 독서_일기 여행기 옛이야기 기록장 회고록 소설책
    """,
    "lovers-walk": """
        오랜_친구에게 가족에게 오랜_연인에게 함께_살아온_이에게 이웃에게 | 안부를_묻다 소식을_전하다 용기를_주다 손을_내밀다 마음을_전하다 선물하다 편지를_쓰다 고마움을_전하다 사과하다 따뜻하게_인사하다
        망설인 조심스러운 마음에_담아둔 오랫동안_미뤄온 용기를_낸 | 고백 사과 부탁 인사 대화 화해 약속 안부 편지 표현
        서로를_향한 서로의 오랫동안_쌓은 힘들_때_건네는 조건_없는 | 믿음 애정 응원 신뢰 지지 배려 존중 관심 마음 위로
        사랑을_나누는 서로를_보듬는 아픔을_나누는 다정히_맞이하는 함께_자라는 | 관계 사람 이웃 친구 가족 동반자 사이 부부 커플 공동체
        사랑하는_사람과 오래된_친구와 소중한_이와 다정한_가족과 마음_맞는_이와 | 함께_걷다 길을_나서다 나란히_앉다 추억을_만들다 저녁을_먹다 마음을_나누다 얘기를_나누다 웃음을_나누다 손을_잡다 길을_걷다
        재회한 멀어졌던 서먹해진 뜻밖에_다시_만난 다시_가까워진 | 친구 벗 연인 사람 동네_친구 이웃 가족 선후배 사이 관계
        그를_향한 그녀를_향한 부모를_향한 아이를_향한 친구를_향한 | 마음 용서 다짐 애정 그리움 미안함 고마움 바람 응원 믿음
        주고받은 조용히_건넨 먼저_내민 다정하게_보낸 말없이_내어준 | 미소 손 마음 인사 편지 문자 선물 위로 표정 눈길
        함께_보낸 마음을_모은 오랫동안_기다린 같이_준비한 서로를_보살핀 | 주말 하루 밤 한때 여름날 시간 저녁 순간 생일 여행
        갈등_뒤의 다툰_뒤의 오해가_풀린 마음을_열게_된 차츰_쌓이는 | 믿음 이해 신뢰 관계 애정 화해 공감 존중 연대 우정
        정을_나누는 서로_기대는 힘을_보태는 곁을_내주는 마음을_잇는 | 친구들 이웃들 동네 공동체 모임 동행 가족 관계 사람들 연대
    """,
    "youth-walk": """
        스스로_세운 팀이_세운 처음_세운 새로_세운 오래전부터_세운 | 목표 계획 기록 기준 전략 원칙 꿈 로드맵 일정 방향
        끝까지_밀어붙인 끝내_이뤄낸 다시_시작한 포기하지_않은 겁없이_뛰어든 | 도전 훈련 연습 모험 탐험 시도 여정 경주 실험 경기
        기록을_깨기_위한 한계를_넘기_위한 더_높이_오르기_위한 새로운_길을_찾기_위한 세계에_도전하기_위한 | 연습 훈련 전술 도전 실험 전략 연구 준비 계획 노력
        온_힘을_다한 전력을_다한 쉬지_않고_달린 숨차게_뛰어오른 거침없이_나아간 | 경주 질주 여정 등반 행군 경기 레이스 도전 달리기 구간
        바람을_가르는 거리를_누비는 들판을_달리는 경기장을_채운 트랙을_도는 | 선수 주자 러너 아이들 자전거 팀 달리기 행렬 함성 응원
        성공할_때까지 한계를_넘을_때까지 목표에_닿을_때까지 결승선을_향해 다시_한_번 | 달리다 도전하다 나아가다 연습하다 훈련하다 버티다 뛰다 시도하다 페달을_밟다 오르다
        처음_발을_디딘 힘겹게_올라선 아무도_가보지_않은 모험가가_찾은 탐험대가_오른 | 정상 고지 능선 고개 산길 오지 빙하 미지의_땅 고원 계곡
        실패를_딛고 고비를_넘기고 좌절을_딛고 역경을_딛고 한계에_맞서 | 다시_뛰다 다시_일어서다 계속_나아가다 도전하다 완주하다 기록을_세우다 목표를_이루다 나아가다 끝까지_버티다 역전하다
        하나씩_만들어_본 직접_만들어_본 과감히_시험한 계속_개선한 새롭게_조합한 | 발명품 기계 장치 모형 로봇 도구 실험 설계 기술 작품
        가슴_뛰는 박진감_넘치는 숨가쁜 손에_땀을_쥐는 한치도_물러설_수_없는 | 승부 대결 경기 경쟁 추격 질주 역전 경주 레이스 모험
        가보지_않은 처음_마주한 새로_발견한 미지에_놓인 탐험하고_싶은 | 세상 세계 행성 나라 밀림 동굴 정글 미로 해협 산맥
    """,
}


ROUTE_DETAIL_PHRASE_GROUPS = {
    "lakeside-walk": """
        고요한 잔잔한 한적한 적막한 고즈넉한 조용한 평화로운 차분한 안개_낀 인적_드문 | 호숫가 연못가 저수지 호반 둔치 둑길 방파제 물가 강변 수변_공원
        은빛 푸른빛 금빛 붉은빛 분홍빛 옅은빛 노을빛 달빛이_밴 햇빛을_받은 구름을_담은 | 수면 호면 잔물결 여울 물굽이 포말 물빛 물보라 강물 윤슬
        고개를_들어 걸음을_멈춰 멀리서 가까이서 물가에_서서 한동안_가만히 눈을_들어 저_멀리 벤치에_앉아 호숫가에서 | 윤슬을_바라보다 물빛을_살피다 물결을_보다 파도를_바라보다 달의_반영을_보다 물새를_관찰하다 황혼을_바라보다 물안개를_바라보다 물길을_눈에_담다 수평선을_바라보다
        물결에 파도에 잔물결에 수면_위에 호수_위에 연못_위에 강물_위에 물빛에 여울에 물안개에 | 흔들리는_달빛 퍼지는_노을 번지는_별빛 비치는_하늘 떠다니는_잎사귀 길게_드리운_그림자 일렁이는_불빛 비친_갈대 반짝이는_햇살 겹쳐_보이는_구름
        한낮에 새벽녘에 이른_저녁에 해질_무렵에 밤이_깊을_때 황혼에 동틀_때 해가_기울_때 별이_뜰_때 노을이_질_때 겨울_새벽에 | 호숫가를_거닐다 물가를_따라_걷다 호반을_돌다 연못_둘레를_걷다 저수지_둑을_걷다 강변을_따라_걷다 수변을_거닐다 강가에서_쉬다 물안개를_보다 호숫가에_앉다
        가늘게_번지는 동그랗게_퍼지는 잦아들기_시작한 느리게_일렁이는 은은하게_반짝이는 불빛에_흔들리는 바람에_이리저리_일렁이는 살짝_일어나는 얕게_이는 조용히_흐르는 | 물결 잔물결 물빛 수면 호면 강물 연못물 시냇물 여울 물살
        굽이굽이_흐르는 깊은_골짜기를_지나는 들판을_가르는 바다로_이어지는 산자락을_도는 마을_곁을_흐르는 얕은_곳을_흐르는 돌_사이를_타고_흐르는 느릿하게_흐르는 모래밭을_지나는 | 냇물 강줄기 실개천 물줄기 계류 지류 하천 개천 시내 시냇물
        갈대_사이에서 수초_사이에서 백사장_끝에서 수변_데크에서 연못_둘레에서 호수_가에서 갯벌_가에서 강변_벤치에서 둑길_옆에서 얕은_연안에서 | 바람_소리를_듣다 물_소리를_듣다 물새를_기다리다 갈대가_흔들리는_소리를_듣다 물비늘을_보다 수면을_살피다 물길을_따라_걷다 잔물결을_보다 강바람을_맞다 저녁_풍경을_바라보다
        바람에_눕는 천천히_흔들리는 무리지어_자라는 물결에_따라_흔들리는 물_너머로_보이는 물가에_모인 짙푸르게_펼쳐진 흐릿하게_보이는 물에_그림자를_드리운 물결에_몸을_맡긴 | 갈대 부들 억새 수련 연잎 마름 수초 개구리밥 버드나무 물풀
        눈을_감고 잠시_멈춰 가만히_앉아 바람을_맞으며 수면을_바라보며 물소리를_들으며 호흡을_고르며 차분히_숨을_쉬며 강변에_앉아 달빛_아래서 | 마음을_진정시키다 심호흡하다 한숨을_내쉬다 숨을_고르다 조용히_쉬다 바라보며_쉬다 가만히_머무르다 사색에_잠기다 고요를_느끼다 여유를_되찾다
        노을을_품은 햇살이_쏟아지는 안개가_겹겹이_낀 물안개가_피는 물새가_머무는 파문이_번지는 눈이_날리는 비구름이_스치는 바람이_잠잠한 별빛이_보이는 | 호반 호수 저수지 연못 큰_강 조용한_수변 고요한_물가 얕은_내 물가_쉼터 산책_둔치
        바닷바람에 강바람에 파도_곁에서 호숫가_바람에 선선한_밤공기에 물비린_공기에 새벽_이슬에 해풍에 습한_안개에 잔잔한_물가_바람에 | 속도를_늦추다 잠시_눈을_감다 가만히_앉다 고요히_걷다 천천히_거닐다 잠깐_쉬다 제자리에_서다 발걸음을_멈추다 느릿하게_걷다 벤치에_기대다
    """,
    "fortress-walk": """
        선사_시대의 철기_시대의 삼한의 삼국_초기의 남북국_시대의 후삼국의 고려_전기의 조선_중기의 대한제국의 근대_초기의 | 유물 유적 제도 정치사 의례 생활사 전통 무덤 거주지 문물
        백제_한성기의 백제_웅진기의 백제_사비기의 고구려군의 신라군의 조선_수군의 조선_관군의 옛_주민들의 지방_관아의 왕조의 | 방어_시설 성벽 보루 진지 성터 토성 방벽 망루 성문 요새
        성_안쪽의 성_바깥의 성문_근처의 성벽_아래의 왕궁_뒤편의 고분군_가장자리의 옛_도읍_중심의 유적지_동쪽의 성곽_서쪽의 토성_둘레의 | 옛길 석축 해자 배수로 돌담 봉수대 건물터 초석 발굴터 지층
        층마다_다른 흙에_묻혀_있던 지표에서_발견된 오랜_세월_남은 조사를_거쳐_확인된 보존_처리를_마친 발굴팀이_정리한 연구자가_분류한 시대별로_전시한 박물관에서_복원한 | 토기_조각 기와_조각 석기 철기 청동기 토제_그릇 철제_무기 석재 생활_도구 장식품
        나무로_만든 돌을_깎아_만든 검게_그을린 물레로_빚은 섬세하게_새긴 손으로_빚은 가마에서_구운 흙으로_형태를_잡은 복원해_전시한 조각을_맞춘 | 토기 토우 옹기 그릇 기와 접시 의례용_기물 조각품 문양 타일
        유적_조사에서 시굴_조사에서 발굴_현장에서 무덤방에서 성곽_안에서 성문터에서 사찰터에서 옛_주거지에서 지층_분석에서 왕궁터에서 | 유물이_나오다 흔적이_나오다 단서가_확인되다 축조_방식이_드러나다 토기가_나오다 유구가_드러나다 문화층이_드러나다 토층을_확인하다 건물터를_찾다 돌무지를_확인하다
        해설사와 함께_걷는 도보로_둘러보는 지도를_펼쳐_보는 답사_모임에서_가는 박물관_수업에서_찾은 옛길을_따라_가는 현장에서_배우는 궁궐_뒤편을_지나는 안내판을_읽으며_걷는 | 유적_답사 성곽_답사 왕릉_탐방 성문_답사 역사_탐방 토성_답사 문화재_답사 한옥_탐방 고분군_답사 옛_도읍_탐방
        역사학자가 고고학자가 지역사_연구자가 발굴단이 박물관이 국립박물관이 학예연구사가 해설사가 문화재_연구진이 복원팀이 | 유물을_조사하다 유구를_살피다 유적을_기록하다 토층을_읽다 문헌을_검토하다 보존_상태를_점검하다 전시를_준비하다 축조_방식을_연구하다 연대를_추정하다 옛_지도를_비교하다
        왕조가_교체되는 왕국이_성장하던 도읍을_옮기던 왕실을_중심으로_한 교류가_활발하던 외세가_침입한 교역이_늘어난 새_왕이_즉위한 오랜_왕국이_저문 개혁이_진행된 | 시대 시기 무렵 역사 정치_질서 국제_관계 교역로 교역지 수도_정비 문화_이동
        오래된_비석에 낡은_기록에 옛_지도에 지방지에 관찬_기록에 읍지에 탁본에 왕조실록에 발굴_기록에 유적_안내판에 | 새겨진_이름 남은_연대 적힌_지명 기록된_지명 전해_오는_설명 남은_흔적 기록된_사실 남겨진_지도 적힌_내역 정리된_기록
        옛_장인의 조상들의 초기_정착민의 선조들의 성_안_주민의 고려_주민의 옛_왕실의 세대를_이은 전해_내려온 지역에_뿌리내린 | 기술 장례 풍속 의복 문화 도구 공예 장식 의례 생활_방식
        오랜_세월을_거친 중수된 지금까지_남아_있는 옛_지도와_비교한 기록으로_복원한 흔적이_남아_있는 옛_사람이_오가던 탐방길에서_만나는 발굴로_찾은 학술_조사로_확인한 | 고분군 한옥 종묘 궁터 옛길 왕릉 성곽 전통_가옥 성문터 성벽
    """,
    "memory-walk": """
        어려서_살던 처음_이사한 놀러_가곤_했던 자주_지나던 오래_머물던 오랜만에_찾은 꿈에도_그리던 예전에_살던 어린_날_누볐던 아직도_기억나는 | 동네 골목 옛집 고향 시골집 마당 학교 등굣길 골목길 기찻길
        어머니가_간직한 할머니가_남긴 아버지가_찍은 옛_친구가_보낸 어릴_적_만든 학창_시절에_쓰던 졸업할_때_받은 다락에서_찾은 오래_벗겨지지_않은_표지의 가족과_함께_본 | 사진첩 편지 졸업_앨범 사진 엽서 수첩 일기장 낡은_공책 추억_상자 여행_사진
        어릴_적에 유년_시절에 학창_시절에 초등학생_때에 옛_동네에서 가족_여행_때에 오래전_여름에 방학_때에 설날마다 추석마다 | 듣던_노래 놀던_골목 잊지_못한_냄새 맡았던_향기 느꼈던_설렘 부르던_노래 보던_풍경 자주_걷던_길 경험한_일 다시_떠오른_기억
        잊혀졌다가_떠오른 향기에_이끌려_되살아난 오래_묻혀_있던 마음_깊이_숨겨_둔 낯익은_풍경에서_되살아난 다시_생각난 말없이_간직했던 문득_되살아난 오래도록_기억한 세월이_지나_떠오른 | 옛날 유년의_추억 지난날 학교_생활 방학의_기억 옛_일 고향의_추억 어린_시절 옛_날들 어린_날의_풍경
        봄부터_가을까지 해마다_되돌아오는 달마다_달라지는 계절마다_색이_바뀌는 아침마다_바라보는 창밖에서_지켜본 다시_피어나는 저물어_가는 새로_시작된 해마다_기다려지는 | 계절 나무의_변화 햇살 올해의_봄 들꽃 풍경 초록빛 정원 풀꽃 숲의_색
        바람에_떨어지는 서서히_물드는 서리가_앉은 햇살이_스미는 안개가_걷히는 비_온_뒤에_반짝이는 해마다_푸르게_돋는 오래_자리를_지키는 계절을_알려주는 사각사각_밟히는 | 나뭇잎 낙엽 나뭇가지 풀잎 나무 초록빛_잎 작은_풀꽃 들꽃 꽃잎 나무_그늘
        숲길을_걸으며 그늘이_드리운_길을_걸으며 마당을_바라보며 들판을_지나며 꽃밭을_지나며 자연을_살피며 오솔길을_따라_걷다 정원에_들러 산책길에_나서며 텃밭을_돌보며 | 계절을_느끼다 나무를_살피다 낙엽을_줍다 새싹을_발견하다 들꽃을_보다 풀내음을_맡다 꽃향기를_맡다 새잎을_보다 새순을_보다 사계절을_기억하다
        가을_아침의 봄_저녁의 여름_한낮의 겨울_해질녘의 장마가_끝난_뒤의 눈이_녹기_전의 볕이_뜨거울_때의 아침_햇살_속의 저녁_노을_아래의 찬바람이_불던_날의 | 동네_풍경 골목과_마당 들판 정원 산책길 학교_운동장 나무_그늘 통학로 숲길 공원_풍경
        노랗게_물든 붉게_물든 새하얗게_핀 보랏빛을_띤 연분홍으로_물든 초록빛이_짙어진 가을빛으로_바뀐 손바닥만한 이슬에_젖은 볕을_받은 | 잎사귀 나뭇잎 풀잎 꽃잎 산책길_옆_풀 들꽃 정원_꽃잎 옛집_마당의_꽃 풀숲 노란_꽃
        느닷없이_떠오른 돌아갈_수_없어_그리운 다시_보고_싶은 사진으로_돌아보는 자주_꿈에서_보는 문득_생각이_나는 새삼_정겨운 다시_찾고_싶은 이따금_떠올리는 이제는_사라진 | 교실 지름길 고향집 시골길 통학로 앞마당 골목길 놀이터 오래된_책방 버스_정류장
        일기에_적어_둔 사진_뒷면에_적힌 서랍에_간직한 쪽지에_남은 오래전_상자에_넣어_둔 한참_뒤에_다시_본 손으로_써_둔 기억_저편에_남은 함께_찍어_둔 한때_소중했던 | 기록 사진 일기 엽서 편지 가족사진 메모 과거의_기록 여행_수첩 오래된_기념품
        이른_봄에 늦은_봄에 초여름에 한여름에 초가을에 깊은_가을에 초겨울에 한겨울에 해마다 계절이_바뀔_때마다 | 마당을_둘러보다 정원을_거닐다 숲길을_걷다 풍경을_기록하다 꽃을_관찰하다 풀숲을_살피다 동네를_돌다 계절의_변화를_느끼다 그때를_떠올리다 옛_풍경을_떠올리다
    """,
    "lovers-walk": """
        깊어지는 멀어졌다_가까워진 다시_이어진 조심스레_이어진 서서히_쌓이는 오래_지켜_온 다시_회복된 흔들리다_되찾은 천천히_회복되는 서툴게_쌓아온 | 관계 신뢰 우정 애정 유대 친밀감 연대 교감 믿음 의리
        소중한_사람에게 연인에게 배우자에게 자녀에게 부모님에게 동네_친구에게 옆집_이웃에게 오랜_벗에게 함께한_사람에게 떠나보낸_이에게 | 고맙다고_말하다 사랑을_전하다 안부를_전하다 편지를_건네다 위로를_건네다 용서를_구하다 진심을_전하다 걱정을_덜어_주다 다정하게_대답하다 응원을_보내다
        서로에게 상대에게 친구에게 소중한_이에게 이웃에게 동행에게 오래된_연인에게 곁에_있는_이에게 가족들에게 연인에게 | 마음을_나누다 안부를_묻다 신뢰를_쌓다 믿음을_주다 다가서다 속마음을_전하다 비밀을_털어놓다 애정을_표현하다 힘이_되어_주다 곁을_내어주다
        관계를_회복하려 서로를_아끼려고 함께_버티려고 멀어진_마음을_잇기_위해 상대를_이해하려 진심을_전하려 화해하고_싶어서 먼저_다가가려고 마음을_나누려고 아픔을_달래_주려고 | 다정하게_말하다 조심스레_다가가다 먼저_손_내밀다 간절하게_부탁하다 용기_내다 마음을_열다 다정히_건네다 이야기를_들어_주다 곁에_있다 걱정해_주다
        말없이_곁을_지킨 함께_울어_준 꼭_안아_준 손을_잡아_준 웃으며_맞아_준 오래_기다려_준 마음을_헤아려_준 한편이_되어_준 힘을_내라고_격려한 안부를_물어_준 | 가족 친구 연인 동반자 벗 이웃 부모 자매 형제 부부
        사려_깊게 조심스럽게 따뜻하게 다정하게 정성껏 진심으로 담담하게 한_번_더 오래_기다린_끝에 조용히 | 상대를_위로하다 용서를_청하다 사랑을_고백하다 우정을_되새기다 마음을_전하다 서로를_끌어안다 다시_손잡다 따뜻한_손을_내밀다 웃어_주다 상대의_말을_듣다
        함께_이겨_낸 함께_나눈 같이_버틴 함께_건너온 서로를_지지한 마음을_모아_견딘 손_놓지_않고_버틴 서로를_보듬은 곁을_지키며_보낸 나란히_걸어온 | 세월 나날 날들 어려움 시련 고비 긴_밤 하루 여정 지난날
        오해를_풀_수_있었던 서로를_알아_가는 여기까지_함께_온 고마움을_깨닫는 용기를_내_다가간 다시_함께하게_된 마음이_열린 서로를_이해하게_된 아픔을_나눌_수_있었던 서로의_편이_되기로_한 | 날 저녁 아침 산책 편지 인사 만남 대화 약속 마음의_변화
        소소하게_준비한 정성_들여_쓴 마음을_담아_보낸 따뜻한_손으로_건넨 친절을_담은 손수_만든 진심을_담은 사랑을_실은 서로에게_준비한 조용히_내민 | 선물 손편지 꽃다발 기념품 카드 정표 안부 작은_선물 감사_편지 손길
        수줍은 설레는 서로에게_익숙해지는 서로_오래_바라본 소중하게_기억하는 서로를_안심시킨 눈을_맞춘 조용히_손을_잡은 애틋하고_따뜻한 다시_만나_반가운 | 첫_인사 데이트 만남 재회 고백 약속 미소 포옹 눈인사 동행
        내어_준 지켜_준 살펴_준 기꺼이_보태_준 힘을_빌려_준 안심시켜_준 따뜻하게_맞아_준 친절하게_건네_준 돌봐_준 사정을_살핀 | 이웃 친구 가족 지인 자매 부부 연인 동료 벗 형제
        친구와_함께 연인과_함께 배우자와_함께 아이와_함께 가족과_함께 부모님과_함께 이웃과_함께 오래된_벗과_함께 마음을_열고 함께_손을_잡고 | 산책하다 길을_걷다 추억을_쌓다 맞이하다 즐거움을_나누다 약속을_지키다 서로를_기억하다 다정히_걷다 감사를_나누다 관계를_가꾸다
    """,
    "youth-walk": """
        한계를_시험하는 새로운_기록에_도전하는 성장을_향한 새로운_방식에_도전하는 전력을_다하는 끈질기게_이어_가는 망설임_없이_뛰어드는 전혀_새로운 익숙한_경계를_넘는 끝을_보려는 | 경기 탐험 모험 등반 도전 겨루기 합숙_훈련 레이스 프로젝트 연구_실험
        산악인들이 탐험가들이 달리기_선수들이 청소년_팀이 모험가들이 초보_러너들이 자전거_동호인들이 젊은_선수들이 도전자들이 참가자들이 | 도전에_나서다 한계를_넘다 코스를_완주하다 목표를_향해_달리다 결승선에_닿다 정상에_오르다 출발선에_서다 훈련을_시작하다 다시_뛰다 새로운_기록을_세우다
        고비를_넘은 끈질기게_이어온 조바심을_이겨낸 실패_끝에_이어가는 제자리에서_다시_나선 물러서지_않은 꾸준히_반복한 오랫동안_준비한 망설임을_이겨낸 이따금_쉬어_가며_이어온 | 연습 훈련 도전 준비 탐험 실험 시도 경기 레이스 등반
        출발_신호에 신호탄이_울릴_때 카운트다운이_끝나자 결승선을_바라보며 트랙을_따라 시합_초반에 코스를_따라 시합이_시작되자 관중이_응원하자 목표_지점을_향해 | 뛰어_나가다 달려_나가다 빠르게_달리다 속도를_올리다 전력_질주하다 앞으로_내닫다 힘차게_뛰다 가속하다 페달을_밟다 출발하다
        전국_규모의 국제_규모의 지역_대표 학교_대표 청소년_대표 꿈나무들의 아마추어_팀의 동호인_팀의 새로운_팀의 첫_출전_팀의 | 마라톤_대회 육상_대회 축구_경기 자전거_경주 수영_대회 등반_대회 농구_경기 체력_훈련 야구_경기 계주_경기
        정상을_향한 마음이_뛰는 미지의_땅으로_향하는 길_밖으로_나가는 새_대륙으로_떠나는 험한_길로_향하는 더_넓은_세상으로_향하는 지도를_따라_가는 산맥을_넘는 사막을_가로지르는 | 여정 탐사 모험 출발 탐험 여행 등반 원정 탐방 종주
        발을_헛디딘_뒤 고비에서_흔들린_뒤 기록을_놓친_뒤 훈련에_실패한_뒤 다시_숨을_고른_뒤 넘어졌다_일어난_뒤 패배한_뒤 난관을_겪은_뒤 시합을_마친_뒤 뒤처진_뒤 | 다시_일어서다 재도전하다 기록에_도전하다 훈련을_재개하다 계속_달리다 다시_시작하다 도전을_이어가다 버티다 한계를_뛰어넘다 한발_더_내딛다
        새해에 방학_동안에 매일_새벽에 해가_뜨기_전에 경기_전날에 긴_연습_끝에 주말마다 팀원과_함께 코치와_함께 아침_일찍 | 근력_운동을_하다 몸을_풀다 달리기_연습을_하다 코스를_달리다 인터벌_훈련을_하다 지구력을_기르다 자전거를_타다 기록을_재다 체력_훈련을_하다 팀_훈련을_하다
        작은_부품을_조립해 설계도를_바꾸어 처음으로_직접 실패한_시제품을_고쳐 물리_원리를_적용해 기존_방식을_바꿔 친구들과_힘을_합쳐 아이디어를_실험해 새_재료를_써 서로_다른_구상을_합쳐 | 시제품을_만들다 장치를_시험하다 로봇을_만들다 모형을_제작하다 기계를_조립하다 발명품을_만들다 도구를_개발하다 새_방식을_검증하다 발상을_구체화하다 구조를_개선하다
        경쟁자가_바짝_쫓는 마지막까지_팽팽한 예상치_못하게_뒤집힌 출발부터_빠른 종료_직전까지_포기_없는 선두가_바뀐 앞서다_뒤집힌 처음_판세를_뒤집은 역전이_필요한 결승점까지_긴장되는 | 경기 승부 대결 레이스 추격 경쟁 도전 질주 경주 시합
        청춘기의 진로를_고민하는 꿈을_키우는 첫걸음을_내딛는 어제보다_나아지려는 용기를_내_시작하는 목표를_새로_찾는 새로운_분야를_개척하는 한계를_넘으려는 스스로_길을_찾는 | 여정 첫_도전 출발 도전 실험 훈련 성장 탐험 도약 모험
        첫_기록을_세운 개인_기록을_갱신한 마침내_완주한 훈련을_끝마친 도전의_벽을_넘은 응원에_힘을_얻은 목표를_이룬 멀리_달려_온 두려움을_이겨낸 새로운_경기를_치른 | 선수 도전자 팀 달리기_모임 러너 참가자 선수단 친구들 경기_팀 탐험대
    """,
}


GENERIC_REVIEW_WORDS = frozenset("""
    장면 주인공 인물 등장인물 줄거리 스토리 내용 작품 책 저자 작가 독자 감상문
    독후감 느낀점 감상 소감 느낌 인상 생각 문장 구절 이야기 독서 리뷰 책소개
    감동 교훈 메시지 표현 부분 묘사 설정 전개 결말 제목 서평
""".split())
GENERIC_STANDALONE_WORDS = frozenset("사람 마음 시간 하루 순간 일상 인생 삶 세상 세월 우리 모습 변화 계기 경험".split())
REVIEW_PARTICLE = re.compile(r"(?:에서부터|에게서|으로는|으로도|이라도|에서는|에서|으로|에게|이다|이며|이고|이라는|들의|들|은|는|이|가|을|를|의|에|도|만|와|과|로)$")
ROUTE_KEYWORD_LIMIT = 2000


def is_generic_review_term(term):
    words = term.split()
    if len(words) == 1 and words[0] in GENERIC_STANDALONE_WORDS:
        return True
    if "".join(words) in GENERIC_REVIEW_WORDS:
        return True
    return any(
        word in GENERIC_REVIEW_WORDS or REVIEW_PARTICLE.sub("", word) in GENERIC_REVIEW_WORDS
        for word in words
    )


def expand_route_keywords(route, base_terms):
    terms = list(base_terms)
    terms.extend(term.replace("_", " ") for term in ADDITIONAL_KEYWORDS[route].split())
    groups = (
        KEYWORD_PHRASE_GROUPS[route].strip(),
        EXTRA_KEYWORD_PHRASE_GROUPS[route].strip(),
        ROUTE_DETAIL_PHRASE_GROUPS[route].strip(),
    )
    for line in "\n".join(groups).splitlines():
        modifiers, topics = line.split("|")
        for modifier in modifiers.split():
            for topic in topics.split():
                terms.append(f"{modifier} {topic}".replace("_", " "))
    unique_terms = {}
    for term in terms:
        # Matching permits optional spacing between words.
        if is_generic_review_term(term):
            continue
        key = "".join(unicodedata.normalize("NFKC", term).casefold().split())
        unique_terms.setdefault(key, term)
        if len(unique_terms) == ROUTE_KEYWORD_LIMIT:
            break
    return tuple(unique_terms.values())


ROUTE_KEYWORDS = {route: expand_route_keywords(route, terms) for route, terms in ROUTE_KEYWORDS.items()}



MOOD_ROUTES = {
    "계절과 자연 관찰": "memory-walk",
    "물가와 차분한 사색": "lakeside-walk",
    "역사와 옛 풍경": "fortress-walk",
    "사랑과 사람 이야기": "lovers-walk",
    "도전과 활기": "youth-walk",
    "기억과 돌아봄": "memory-walk",
    "문학과 이야기": "memory-walk",
}

ROUTE_REASONS = {
    "memory-walk": "몽촌토성을 둘러보고 야생화학습장을 지나는 길에서 책의 장면을 떠올려 보세요.",
    "lakeside-walk": "몽촌해자 물가를 걸으며 책에서 마음에 남은 장면을 천천히 떠올려 보세요.",
    "fortress-walk": "몽촌토성 순환로를 걸으며 책 속 이야기를 공원에 남은 옛 풍경과 연결해 보세요.",
    "lovers-walk": "연인의 길을 따라 소마미술관과 평화의광장 주변을 걸으며 책에서 떠오른 사람을 생각해 보세요.",
    "youth-walk": "공원 외곽을 크게 한 바퀴 돌며 책에서 마음에 남은 도전이나 새로운 시작을 떠올려 보세요.",
}



# Match complete words, allowing common Korean particles and inflections.
KOREAN_SUFFIX = (
    r"(?:에서부터|에게서|으로는|에서는|으로도|까지만|이라고|이라는|으로|에서|에게|한테|께서|"
    r"까지|부터|처럼|마다|조차|마저|보다|이나|이며|이라|하고|하다|했던|했다|"
    r"하기|하여|하게|합니다|한다|한|할|해|했다가|적인|적|들이|들을|들의|"
    r"들|은|는|이|가|을|를|의|에|도|만|와|과|로)?"
)
KEYWORD_PATTERNS = {
    route: [
        (
            term,
            term.split()[0][:2].casefold(),
            re.compile(r"\s*".join(re.escape(word) for word in term.casefold().split())
                       + KOREAN_SUFFIX + r"(?!\S)"),
        )
        for term in terms
    ]
    for route, terms in ROUTE_KEYWORDS.items()
}


def match_route_keywords(documents, route_ids):
    text_by_source = []
    for source, text in documents:
        if not text:
            continue
        text = re.sub(r"[\W_]+", " ", unicodedata.normalize("NFKC", text).casefold())
        prefixes = {}
        for token in re.finditer(r"\S+", text):
            word = token[0]
            prefixes.setdefault(word[0], []).append(token.start())
            if len(word) > 1:
                prefixes.setdefault(word[:2], []).append(token.start())
        text_by_source.append((source, text, prefixes))
    matches = {}
    for route in route_ids:
        route_hits = {}
        for term, prefix, pattern in KEYWORD_PATTERNS.get(route, []):
            sources = tuple(dict.fromkeys(
                source for source, text, prefixes in text_by_source
                if any(pattern.match(text, start) for start in prefixes.get(prefix, ()))
            ))
            if sources:
                route_hits[term] = sources
        matches[route] = route_hits
    return matches

