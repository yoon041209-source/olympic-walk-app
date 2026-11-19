import json
import os
import sys
from contextlib import closing
from html import escape
from pathlib import Path

# QGIS에 포함된 Windows Python에서 SQLite DLL을 찾을 수 있게 합니다.
qgis_folder = Path(sys.base_prefix).parent.parent
if sys.platform == "win32" and qgis_folder.name.startswith("QGIS "):
    qgis_dlls = os.add_dll_directory(str(qgis_folder / "bin"))

import sqlite3

import pydeck as pdk
import streamlit as st

from catalog import Book, MOODS, MOOD_ROUTES, ROUTE_REASONS, infer_mood, normalized, official_book_url, read_catalog
from catalog import CATALOG_DOWNLOAD_LOCK, CatalogError, clear_checkpoint, pull_catalog, save_catalog
from catalog import EDITION_NOTES, book_isbn, google_book_intro, yes24_book_text
from catalog import load_yes24_key, save_yes24_key, yes24_key_path
from catalog import search_books


TITLE = "책 한 권으로 시작하는 올림픽공원 산책"
DATASET_URL = "https://www.data.go.kr/data/15008517/openapi.do"
BOOK_MODE = "책으로 추천받기"
ROUTE_MODE = "전체 코스 둘러보기"


def korean_html(content):
    st.html(f'<section lang="ko" translate="no" class="notranslate">{content}</section>')


def update_catalog(snapshot_path):
    checkpoint = snapshot_path.parent / ".ksponco-pages"
    with st.expander("공공데이터 API에서 직접 가져오기"):
        if any(checkpoint.glob("page-*.xml")):
            st.caption("이전에 받은 페이지가 있어요. 전체 장서를 선택하면 이어서 가져와요.")
        with st.form("catalog_api", clear_on_submit=True):
            key = st.text_input("서비스키", type="password")
            scope = st.radio("가져올 범위", ("테스트 10권", "전체 장서"))
            restart = st.checkbox("전체 장서를 처음부터 다시 받기")
            requested = st.form_submit_button("도서 가져오기")
        if not requested:
            return
        if not key.strip():
            st.warning("발급받은 서비스키를 입력해 주세요.")
            return

        full = scope == "전체 장서"
        status = st.empty()

        def on_page(page, count, total):
            count_text = f"{count:,} / {total:,}건" if full and total else f"{count:,}건"
            status.info(f"{page}쪽 조회 완료 · {count_text}")

        def on_retry(page, attempt, delay):
            status.info(f"{page}쪽 응답을 받지 못했어요. {delay}초 후 재시도 {attempt}/3.")

        if not CATALOG_DOWNLOAD_LOCK.acquire(blocking=False):
            status.warning("다른 창에서 도서를 가져오고 있어요.")
            return
        try:
            if full and restart:
                clear_checkpoint(checkpoint)
            payload = pull_catalog(key, full, on_page, checkpoint=checkpoint, on_retry=on_retry)
            records, invalid = read_catalog([payload])
            if not records or invalid:
                raise CatalogError("응답에서 도서 정보를 읽을 수 없어요.")
            if full:
                save_catalog(snapshot_path, payload)
                st.session_state.pop("catalog_api_preview", None)
            else:
                st.session_state["catalog_api_preview"] = payload
        except CatalogError as error:
            resume = " 전체 장서를 다시 누르면 저장한 페이지부터 이어서 받아요." if full and any(checkpoint.glob("page-*.xml")) else ""
            status.error(f"{error}{resume}")
        except OSError:
            status.error("받아온 장서를 catalog 폴더에 저장할 수 없어요.")
        else:
            status.success(f"{scope} 조회 완료 · 검색 가능한 도서 {len(records):,}권")
            if full:
                try:
                    clear_checkpoint(checkpoint)
                except OSError:
                    st.warning("임시 페이지를 지울 수 없어요. 다음 갱신 때 처음부터 다시 받기를 선택해 주세요.")
        finally:
            CATALOG_DOWNLOAD_LOCK.release()


def automatic_book_mood(book, google_key, yes24_key, lookup_status):
    isbn = book_isbn(book)
    cache = st.session_state.setdefault("book_intro_recommendations", {})
    if not yes24_key:
        lookup_status.caption("YES24 API 미조회 · API 키가 설정되지 않았어요.")
    elif not isbn:
        lookup_status.caption("YES24 API 미조회 · 장서 정보에서 유효한 ISBN을 확인할 수 없어요.")
    else:
        cache_key = ("yes24-detail", isbn)
        if cache_key not in cache:
            try:
                with st.spinner("YES24에서 같은 ISBN의 책을 찾고 있어요."):
                    sections, link, matched = yes24_book_text(isbn, yes24_key)
            except CatalogError as error:
                lookup_status.caption(f"YES24 API 조회 오류 · {error}")
            else:
                recommendation = None
                for label, value in sections:
                    mood, terms, _ = infer_mood(Book(title="", description=value))
                    if mood:
                        recommendation = (mood, terms, [label], link, "YES24 도서 정보")
                        break
                cache[cache_key] = (recommendation, link, matched, bool(sections))
        if cache_key in cache:
            recommendation, link, matched, has_text = cache[cache_key]
            if recommendation:
                lookup_status.caption("YES24 API 조회 성공 · 받은 내용을 추천에 사용했어요.")
                return recommendation
            if matched:
                message = (
                    "소개 자료를 받았지만 추천 기준에 맞는 단서가 없어요."
                    if has_text else "같은 ISBN의 책을 찾았지만 소개 자료가 비어 있어요."
                )
                lookup_status.caption(f"YES24 API 조회 성공 · {message}")
                if link and isbn not in EDITION_NOTES:
                    st.markdown(f"[YES24 상품 페이지 보기]({link})")
            else:
                lookup_status.caption("YES24 API 응답 수신 · 같은 ISBN의 도서가 반환되지 않았어요.")
    note = EDITION_NOTES.get(isbn)
    if note:
        mood, terms, _ = infer_mood(Book(title="", description=note[0]))
        if mood:
            return mood, terms, [note[2]], note[1], note[2]
    mood, terms, sources = infer_mood(book)
    if mood:
        return mood, terms, sources, "", "지샘터 도서정보"
    if isbn and google_key:
        cache_key = ("google", isbn)
        if cache_key not in cache:
            try:
                with st.spinner("같은 ISBN의 책 소개를 찾고 있어요."):
                    description = google_book_intro(isbn, google_key)
            except CatalogError as error:
                st.caption(str(error))
            else:
                cache[cache_key] = infer_mood(Book(title="", description=description))
        if cache_key in cache:
            mood, terms, sources = cache[cache_key]
            if mood:
                return mood, terms, sources, "", "Google Books"
    return None, [], [], "", ""


def book_option_label(item):
    if isinstance(item, str):
        return item
    return " · ".join(part for part in (item.title, item.author, item.publisher, item.isbn) if part)


def show_book_recommendation(folder, demo_books, routes):
    xml_folder = folder / "catalog"
    snapshot = xml_folder / "ksponco-api.xml"
    update_catalog(snapshot)
    paths = sorted(path for path in xml_folder.glob("*.xml") if path.is_file())
    try:
        files = [path.read_bytes() for path in paths]
    except OSError:
        st.error("catalog 폴더의 XML 파일을 읽을 수 없어요.")
        files = []

    with st.expander("공공데이터 XML 가져오기"):
        st.markdown(
            "올림픽공원 도서정보 API의 응답 여러 페이지를 XML 파일로 올리거나 "
            "앱 폴더 안의 catalog 폴더에 저장하세요."
        )
        st.markdown(f"[공공데이터포털 · 도서정보 15008517]({DATASET_URL})")
        uploads = st.file_uploader("도서 목록 XML", type="xml", accept_multiple_files=True)
    files += [upload.getvalue() for upload in uploads]
    preview = st.session_state.get("catalog_api_preview")
    if preview is not None:
        if st.button("테스트 목록 닫기"):
            st.session_state.pop("catalog_api_preview")
            st.rerun()
        files = [preview]
    books, invalid_files = cached_catalog(tuple(files))

    if invalid_files:
        st.warning(
            f"XML {invalid_files}개에서 책을 읽지 못했어요. "
            "API 오류 응답이 들어 있는지, 도서 항목의 태그 이름이 맞는지 확인해 주세요."
        )

    source_is_xml = bool(books)
    if not source_is_xml:
        books = demo_books
        st.info("현재는 기존 10권으로 체험 중이에요. 전체 검색은 API 응답 XML을 불러온 범위에서 제공돼요.")
    else:
        st.caption(f"불러온 도서 {len(books):,}권 · 출처: 올림픽공원 도서정보 API (15008517)")

    with st.container(border=True, key="walk-design-search"):
        pending_search = st.session_state.pop("pending_catalog_search", None)
        if pending_search is not None:
            new_text, previous_choice_key = pending_search
            st.session_state["catalog_search_input"] = new_text
            st.session_state.pop(previous_choice_key, None)
        with st.form("catalog_search", border=False):
            search_column, button_column = st.columns([5, 1], vertical_alignment="bottom")
            with search_column:
                search_text = st.text_input(
                    "전체 장서에서 제목 또는 저자 검색",
                    placeholder="예: 백사층 나무집 또는 104층 나무집",
                    key="catalog_search_input",
                )
            with button_column:
                st.form_submit_button("검색")
        query = normalized(search_text)
        matches, search_message = search_books(books, search_text)
        from_catalog = bool(matches)
        if from_catalog:
            if search_message:
                st.caption(search_message)
            page_size = 40
            total_pages = (len(matches) + page_size - 1) // page_size
            page = 1
            if total_pages > 1:
                page = st.number_input("검색 결과 페이지", 1, total_pages, step=1, key=f"page-{query}")
            start = (page - 1) * page_size
            visible_books = matches[start:start + page_size]
            choice_key = f"catalog_book_choice-{query}-{page}"
            book = st.selectbox(
                "검색 결과에서 책을 골라 주세요",
                visible_books,
                format_func=book_option_label,
                accept_new_options=True,
                help="목록에 없는 제목을 입력하고 Enter를 누르면 전체 장서를 검색해요.",
                key=choice_key,
            )
            if isinstance(book, str):
                st.session_state["pending_catalog_search"] = (book, choice_key)
                st.rerun()
            if book is None:
                st.stop()
        else:
            st.caption("장서에 일치하는 책이 없어 입력한 문구로 산책을 추천해요.")
            book = Book(title=search_text.strip())

    with st.container(border=True, key="walk-design-book"):
        details = [f"<h2>{escape(book.title)}</h2>"]
        bibliography = " · ".join(
            part for part in (book.author, book.publisher, book.publication_year, book.category) if part
        )
        if bibliography:
            details.append(f"<p>{escape(bibliography)}</p>")
        if book.description:
            details.append(f"<p>{escape(book.description[:450])}{'…' if len(book.description) > 450 else ''}</p>")
        if book.keywords:
            details.append(f"<p>주제어 · {escape(book.keywords)}</p>")
        url = official_book_url(book.url)
        if url:
            details.append(f'<p><a href="{escape(url, quote=True)}" target="_blank" rel="noopener noreferrer">지샘터 도서정보 보기</a></p>')
        korean_html("".join(details))

    server_yes24_key = os.getenv("YES24_BOOK_API_KEY", "").strip()
    if not server_yes24_key:
        try:
            server_yes24_key = str(st.secrets.get("YES24_BOOK_API_KEY", "")).strip()
        except st.errors.StreamlitSecretNotFoundError:
            pass
    with st.expander("책 소개 자동 조회 설정"):
        st.markdown(
            "YES24 키를 연결하면 책을 선택할 때 ISBN으로 줄거리와 책 소개를 찾아요. "
            "[YES24 Developers](https://developers.yes24.com/)"
        )
        key_path = yes24_key_path()
        saved_yes24_key = ""
        try:
            saved_yes24_key = load_yes24_key(key_path)
        except (OSError, UnicodeError):
            st.warning("저장된 YES24 키를 읽지 못했어요. 키를 다시 입력하면 교체할 수 있어요.")

        if saved_yes24_key:
            st.caption("YES24 키가 이 Windows 계정에 저장되어 있어요.")
        if server_yes24_key:
            st.caption("서버에 YES24 키가 설정되어 있어요.")
        with st.form("book_intro_settings", clear_on_submit=True):
            yes24_key = st.text_input(
                "YES24 API 키", type="password", placeholder="처음 연결하거나 키를 교체할 때 입력"
            )
            google_key = st.text_input(
                "Google Books API 키 (선택)", type="password", value=os.getenv("GOOGLE_BOOKS_API_KEY", "")
            )
            apply_settings = st.form_submit_button("책 소개 검색 연결")
        if apply_settings:
            new_key = yes24_key.strip()
            if new_key:
                if key_path is None:
                    st.session_state["yes24_book_api_key"] = new_key
                    st.caption("이번 세션에 YES24 키를 적용했어요.")
                else:
                    try:
                        save_yes24_key(key_path, new_key)
                    except OSError:
                        st.session_state["yes24_book_api_key"] = new_key
                        st.warning("YES24 키를 저장하지 못했어요. 이번 세션에는 적용했어요.")
                    else:
                        st.session_state.pop("yes24_book_api_key", None)
                        saved_yes24_key = new_key
                        st.success("YES24 키를 저장했어요. 앱을 다시 실행해도 자동으로 연결해요.")
            st.session_state["google_books_api_key"] = google_key.strip()
            st.session_state.pop("book_intro_recommendations", None)
        if key_path is not None and key_path.is_file() and st.button("저장한 YES24 키 삭제"):
            try:
                key_path.unlink(missing_ok=True)
            except OSError:
                st.warning("저장된 YES24 키를 삭제하지 못했어요.")
            else:
                st.session_state.pop("yes24_book_api_key", None)
                st.session_state.pop("book_intro_recommendations", None)
                st.rerun()
        lookup_details = st.container()
    yes24_key = (
        st.session_state.get("yes24_book_api_key") or saved_yes24_key or server_yes24_key
    )
    google_key = st.session_state.get(
        "google_books_api_key",
        os.getenv("GOOGLE_BOOKS_API_KEY", ""),
    )

    with st.expander("내 감상이나 선호 반영하기"):
        impression = st.text_input(
            "책에서 기억에 남은 장면이나 느낌 (선택)",
            placeholder="예: 바다를 바라보는 고요한 장면",
            key=f"impression-{book.title}-{book.author}-{book.publisher}-{book.publication_year}",
        ).strip()
        available_moods = [mood for mood in MOODS if MOOD_ROUTES[mood] in routes]
        selected_mood = st.selectbox("산책 분위기", ["책 정보에서 자동 추천", *available_moods])

    evidence_url = evidence_name = ""
    lookup_status = st.empty()
    suggested_mood, terms, sources = infer_mood(book, impression) if impression else (None, [], [])
    if selected_mood not in MOOD_ROUTES and not suggested_mood:
        if from_catalog:
            with lookup_details:
                suggested_mood, terms, sources, evidence_url, evidence_name = automatic_book_mood(
                    book, google_key, yes24_key, lookup_status
                )
        else:
            suggested_mood, terms, _ = infer_mood(book)
            sources = ["입력한 문구"] if suggested_mood else []
    if selected_mood in MOOD_ROUTES:
        feature_id = MOOD_ROUTES[selected_mood]
        reason = f"선택한 분위기 · {selected_mood}. {ROUTE_REASONS[feature_id]}"
    elif suggested_mood and MOOD_ROUTES[suggested_mood] in routes:
        feature_id = MOOD_ROUTES[suggested_mood]
        evidence = "·".join(sources)
        reason = f"{evidence}에서 찾은 단서 · {', '.join(terms)}. {ROUTE_REASONS[feature_id]}"
    else:
        feature_id = min(routes, key=lambda key: routes[key][1])
        reason = "가볍게 시작하는 산책 · 가장 짧은 코스를 제안해요. 원하는 분위기를 선택해 코스를 바꿀 수도 있어요."

    if evidence_url:
        st.markdown(f"추천에 참고한 자료 · [{evidence_name}]({evidence_url})")
    elif evidence_name:
        st.caption(f"추천에 참고한 자료 · {evidence_name}")
    name, distance = routes[feature_id]
    return name, distance, feature_id, reason


@st.cache_data(show_spinner=False)
def cached_catalog(files):
    return read_catalog(files)


def show_waypoint_photos(point):
    name = point["name"]
    st.markdown(f"**{name}**")
    photo_folder_name = "몽촌토성" if "몽촌토성" in name else name
    photo_folder = Path(__file__).resolve().parent / "park_photos" / photo_folder_name
    if photo_folder.is_dir():
        photos = sorted(
            (path for path in photo_folder.iterdir()
             if path.is_file() and path.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}),
            key=lambda path: (path.name.casefold(), path.name),
        )
        if photos:
            st.image([str(path) for path in photos], width=320)
            return
    st.caption("등록된 사진이 없어요.")


def show_map(name, feature_id, coordinates, waypoints):
    longitudes, latitudes = zip(*coordinates)
    view = pdk.ViewState(
        longitude=(min(longitudes) + max(longitudes)) / 2,
        latitude=(min(latitudes) + max(latitudes)) / 2,
        zoom=14,
    )
    route_layer = pdk.Layer(
        "PathLayer",
        id=feature_id,
        data=[{"name": name, "path": coordinates}],
        get_path="path",
        get_color=[18, 111, 77],
        width_units=pdk.types.String("pixels"),
        get_width=5,
        width_max_pixels=7,
        cap_rounded=True,
        joint_rounded=True,
    )
    layers = [route_layer]
    points = {}
    for waypoint in waypoints:
        position = waypoint.get("position")
        if position is None and waypoint["role"] == "start":
            position = coordinates[0]
        if position is not None:
            # A place visited more than once shares one marker.
            points.setdefault(waypoint["name"], {
                "name": waypoint["name"],
                "position": position,
                "label": str(waypoint["order"]),
                "color": [191, 64, 35] if waypoint["role"] == "start" else [19, 91, 179],
            })
    marker_layer_id = f"{feature_id}-waypoints"
    if points:
        st.caption("주황색은 출발점이에요. 번호가 있는 지점을 누르면 지도 아래에서 사진을 볼 수 있어요.")
        layers.extend([
            pdk.Layer(
                "ScatterplotLayer",
                id=marker_layer_id,
                data=list(points.values()),
                get_position="position",
                get_fill_color="color",
                radius_units=pdk.types.String("pixels"),
                get_radius=12,
                stroked=True,
                get_line_color=[255, 255, 255],
                line_width_min_pixels=2,
                pickable=True,
            ),
            pdk.Layer(
                "TextLayer",
                id=f"{feature_id}-waypoint-labels",
                data=list(points.values()),
                get_position="position",
                get_text="label",
                get_size=12,
                get_color=[255, 255, 255],
                font_weight=700,
                get_text_anchor=pdk.types.String("middle"),
                get_alignment_baseline=pdk.types.String("center"),
            ),
        ])

    event = st.pydeck_chart(
        pdk.Deck(
            layers=layers,
            initial_view_state=view,
            map_style="light",
            tooltip={"text": "{name}"},
        ),
        height=460,
        key=f"map-{feature_id}",
        on_select="rerun",
        selection_mode="single-object",
    )
    selected = event.selection.objects.get(marker_layer_id, [])
    if selected and (point := points.get(selected[0].get("name"))):
        show_waypoint_photos(point)


st.set_page_config(page_title=TITLE)
st.html("""
<style>
:root, .stApp { color-scheme: only light; }
.stApp [data-testid="stWidgetLabel"],
.stApp [data-testid="stWidgetLabel"] p,
.stApp [data-testid="stRadio"] [role="radiogroup"] p {
    color: #162d49;
}
.stApp [data-baseweb="input"] input::placeholder,
.stApp [data-baseweb="select"] input::placeholder,
.stApp [data-baseweb="textarea"] textarea::placeholder {
    color: #50637b;
    opacity: 1;
}
</style>
""")
st.html('<style>\n.stApp, [data-testid="stHeader"] { background: #eceff3; color: #162d49; }\n[data-testid="stMainBlockContainer"] { max-width: 1000px; }\n.stApp h1 { background: #174a86; color: white; border-radius: 16px; padding: 1.35rem 1.6rem; }\n.stApp h2, .stApp h3 { color: #173d68; }\n.stApp div[class*="st-key-walk-design-"] {\n    background: #f6faff; border: 1px solid #b8cce5; border-radius: 16px;\n    box-shadow: 0 3px 12px #1533580a;\n}\n.stApp div.st-key-walk-design-result { background: #e8f2ff; border-color: #94b7e2; }\n.stApp [data-testid="stExpander"] > details { background: #f0f6ff; border-color: #b8cce5; }\n.st-key-walk-design-search button { background: #135bb3; color: white; border-color: #135bb3; }\n.st-key-walk-design-search button:hover { background: #0d448d; color: white; }\n.stApp a { color: #124c94; }\n@media (max-width: 640px) {\n    [data-testid="stMainBlockContainer"] { padding-left: .85rem; padding-right: .85rem; }\n    .stApp h1 { font-size: 1.65rem; padding: 1rem; }\n}\n</style>')
st.html('<style>\n.st-key-walk-design-search [data-testid="stNumberInputStepDown"],\n.st-key-walk-design-search [data-testid="stNumberInputStepUp"],\n.st-key-walk-design-search [data-testid="stNumberInputStepDown"] *,\n.st-key-walk-design-search [data-testid="stNumberInputStepUp"] * {\n    color: #fff !important;\n}\n.st-key-walk-design-search [data-testid="stTooltipIcon"] button,\n.st-key-walk-design-search [data-testid="stTooltipIcon"] [data-testid="stTooltipHoverTarget"] {\n    background: #f6faff !important;\n    border-color: transparent !important;\n    color: #fff !important;\n}\n.st-key-walk-design-search [data-testid="stTooltipIcon"] svg.icon,\n.st-key-walk-design-search [data-testid="stTooltipIcon"] svg.icon [stroke]:not([stroke="none"]) {\n    stroke: #fff !important;\n}\n.st-key-walk-design-search [data-testid="stTooltipIcon"] svg.icon {\n    filter: drop-shadow(0 0 1px #4e75a5) drop-shadow(0 1px 1px #4e75a5);\n}\n.st-key-walk-design-search [data-testid="stTooltipIcon"] button:focus-visible {\n    outline: 2px solid #4475aa;\n    outline-offset: 2px;\n}\n</style>')
korean_html(f"<h1>{TITLE}</h1>")

folder = Path(__file__).resolve().parent
db_path = folder / "walks.db"
geojson_path = folder / "olympic_park_walks.geojson"

if not db_path.is_file() or not geojson_path.is_file():
    st.error("app.py와 같은 폴더에 walks.db와 olympic_park_walks.geojson을 넣어 주세요.")
    st.stop()

try:
    with closing(sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True)) as db:
        routes = {
            feature_id: (name, distance)
            for feature_id, name, distance in db.execute(
                "SELECT geojson_feature_id, name, official_distance_m FROM Route ORDER BY route_id"
            )
        }
        demo_books = [
            Book(title, author or "")
            for title, author in db.execute("SELECT title, author FROM Book ORDER BY book_id")
        ]
except sqlite3.Error:
    st.error("walks.db에서 책과 코스 정보를 읽을 수 없어요.")
    st.stop()

if not routes:
    st.info("등록된 코스가 아직 없어요.")
    st.stop()

with st.container(border=True, key="walk-design-mode"):
    mode = st.radio("이용 방식", [BOOK_MODE, ROUTE_MODE], horizontal=True)
if mode == BOOK_MODE:
    name, distance, feature_id, reason = show_book_recommendation(folder, demo_books, routes)
else:
    feature_id = st.selectbox("살펴볼 산책 코스를 골라 주세요", list(routes), format_func=lambda key: routes[key][0])
    name, distance = routes[feature_id]
    reason = None

try:
    geojson = json.loads(geojson_path.read_text(encoding="utf-8-sig"))
except (OSError, ValueError):
    st.error("olympic_park_walks.geojson의 지도 데이터를 읽을 수 없어요.")
    st.stop()

feature = next((item for item in geojson["features"] if item["id"] == feature_id), None)
if feature is None or not feature.get("geometry"):
    st.error("선택한 코스의 지도 좌표를 찾을 수 없어요.")
    st.stop()

geometry = feature["geometry"]
coordinates = geometry.get("coordinates", [])
if geometry.get("type") != "LineString" or len(coordinates) < 2:
    st.error("선택한 코스에 지도 선을 그릴 수 있는 좌표가 없어요.")
    st.stop()

waypoints = sorted(
    feature["properties"].get("waypoints", []),
    key=lambda point: point["order"],
)
start_name = next((point["name"] for point in waypoints if point["role"] == "start"), None)
stops = [point for point in waypoints if point["role"] in {"start", "via", "featured_stop", "finish"}]
description = next(
    (point["name"] for point in waypoints if point["role"] == "route_description"),
    None,
)

with st.container(border=True, key="walk-design-result"):
    summary = [
        f"<h2>{escape(name)}</h2>",
        f"<p>공식 거리 <strong>{distance:,}m</strong></p>",
    ]
    if reason:
        summary.append(f"<p>{escape(reason)}</p>")
    if start_name:
        summary.append(f"<p><strong>출발점</strong> {escape(start_name)}</p>")
    korean_html("".join(summary))

with st.container(border=True, key="walk-design-map"):
    show_map(name, feature_id, coordinates, waypoints)
    korean_html("<small>산책로와 지점 표시는 안내용 근사 위치입니다.</small>")

    if stops:
        stop_names = [
            f"<strong>{escape(point['name'])}</strong>"
            if point["role"] == "featured_stop"
            else escape(point["name"])
            for point in stops
        ]
        korean_html(f"<h3>코스 순서</h3><p>{' → '.join(stop_names)}</p>")
    elif description:
        korean_html(f"<p><strong>코스 안내</strong> {escape(description)}</p>")
