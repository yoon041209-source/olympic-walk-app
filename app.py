import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from html import escape
from pathlib import Path
from time import monotonic
from urllib.parse import urlencode

# QGIS에 포함된 Windows Python에서 SQLite DLL을 찾을 수 있게 합니다.
qgis_folder = Path(sys.base_prefix).parent.parent
if sys.platform == "win32" and qgis_folder.name.startswith("QGIS "):
    qgis_dlls = os.add_dll_directory(str(qgis_folder / "bin"))

import sqlite3

import pydeck as pdk
import streamlit as st

from catalog import Book, MOOD_ROUTES, ROUTE_REASONS, match_route_keywords, normalized, official_book_url, read_catalog
from catalog import CATALOG_DOWNLOAD_LOCK, CatalogError, clear_checkpoint, pull_catalog, save_catalog
from catalog import book_isbn, google_book_intro
from catalog import number_search_text
from catalog import build_search_index, search_books
from semantic_routes import MIN_MARGIN, SemanticUnavailable, collect_sentences, load_semantic_model


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


YES24_SITE = "https://www.yes24.com"


def yes24_search_url(query):
    return f"{YES24_SITE}/Product/Search?{urlencode({'domain': 'ALL', 'query': query})}"


def yes24_title_query(book):
    author = book.author.split(" 외", 1)[0].strip()
    return " ".join((number_search_text(book.title), author)).strip()


@st.cache_resource(show_spinner=False)
def book_lookup_executor():
    return ThreadPoolExecutor(max_workers=4, thread_name_prefix="book-lookup")


def cancel_book_lookup():
    pending = st.session_state.pop("pending_book_lookup", None)
    if pending:
        pending[1].cancel()


@st.fragment(run_every=0.75)
def refresh_when_books_arrive(future):
    if future.done():
        st.rerun()


def collect_book_documents(book, google_key, lookup_status, external_lookup):
    isbn = book_isbn(book)
    documents = [("지샘터 책 소개", book.description)]
    source_links = {}
    if not external_lookup or not google_key or not isbn:
        cancel_book_lookup()
        return documents, source_links

    cache = st.session_state.setdefault("book_intro_recommendations", {})
    cache_key = ("google-books", isbn, google_key)
    expires_at, result = cache.get(cache_key, (0, None))
    pending = st.session_state.get("pending_book_lookup")
    if pending and pending[0] != cache_key:
        cancel_book_lookup()
        pending = None

    if expires_at <= monotonic():
        if pending is None:
            future = book_lookup_executor().submit(google_book_intro, isbn, google_key)
            st.session_state["pending_book_lookup"] = (cache_key, future)
        else:
            future = pending[1]
        if not future.done():
            lookup_status.caption("Google Books에서 책 소개를 확인 중이에요. 먼저 현재 자료로 추천을 보여드려요.")
            refresh_when_books_arrive(future)
            return documents, source_links
        st.session_state.pop("pending_book_lookup", None)
        try:
            result = (future.result(), "")
        except CatalogError as error:
            result = ("", str(error))
        except Exception:
            result = ("", "Google Books 응답을 처리하지 못했어요.")
        ttl = 30 if result[1] else 3600
        cache[cache_key] = (monotonic() + ttl, result)

    description, error = result
    if description:
        documents.append(("Google Books 책 소개", description))
        lookup_status.caption("Google Books에서 책 소개를 받았어요.")
    elif error:
        lookup_status.caption(error)
    return documents, source_links


def book_option_label(item):
    if isinstance(item, str):
        return item
    return " · ".join(part for part in (item.title, item.author, item.publisher, item.isbn) if part)


@st.cache_resource(show_spinner=False)
def semantic_model():
    return load_semantic_model()


def recommend_by_sentence(documents, routes, source_links, *, metadata=False):
    sentences = collect_sentences(documents, minimum_chars=4 if metadata else 6)
    if not sentences:
        return None
    if st.session_state.get("semantic_retry_at", 0) > monotonic():
        if not metadata:
            st.caption("문장 모델을 불러오지 못해 키워드와 장서 정보를 확인해요.")
        return None

    cache_key = (sentences, tuple(routes))
    previous = st.session_state.get("semantic_ranking")
    if previous and previous[0] == cache_key:
        ranking = previous[1]
    else:
        try:
            with st.spinner("책의 문장과 산책 코스의 분위기를 비교하고 있어요."):
                ranking = semantic_model().rank(sentences, routes)
        except SemanticUnavailable:
            st.session_state["semantic_retry_at"] = monotonic() + 60
            st.caption("문장 모델을 불러오지 못해 키워드와 장서 정보를 확인해요.")
            return None
        st.session_state["semantic_ranking"] = (cache_key, ranking)

    winner = ranking[0]
    margin = MIN_MARGIN * 1.5 if metadata else MIN_MARGIN
    if len(ranking) > 1 and winner.score - ranking[1].score < margin:
        if not metadata:
            st.caption("문장 점수가 비슷해 키워드도 비교해서 추천해요.")
        return None

    with st.expander("코스별 문장 점수와 추천 근거"):
        st.caption("문장마다 다섯 코스의 평균을 기준으로 계산한 상대 유사도예요.")
        for result in ranking:
            st.write(f"{routes[result.route][0]} · {result.score:+.3f}")
        for sentence in winner.evidence:
            st.caption(sentence.source)
            st.text(sentence.text)
            if sentence.source in source_links:
                st.markdown(f"[자료 보기]({source_links[sentence.source]})")
    basis = "제목·주제 기반 참고 추천" if metadata else "문장 의미 기준"
    return winner.route, f"{basis} · 상대 유사도 {winner.score:+.3f}. {ROUTE_REASONS[winner.route]}"


def show_book_recommendation(folder, demo_books, routes):
    xml_folder = folder / "catalog"
    snapshot = xml_folder / "ksponco-api.xml"
    update_catalog(snapshot)
    with st.expander("공공데이터 XML 가져오기"):
        st.markdown(
            "올림픽공원 도서정보 API의 응답 여러 페이지를 XML 파일로 올리거나 "
            "앱 폴더 안의 catalog 폴더에 저장하세요."
        )
        st.markdown(f"[공공데이터포털 · 도서정보 15008517]({DATASET_URL})")
        uploads = st.file_uploader("도서 목록 XML", type="xml", accept_multiple_files=True)
    files = tuple(upload.getvalue() for upload in uploads)
    preview = st.session_state.get("catalog_api_preview")
    if preview is not None:
        if st.button("테스트 목록 닫기"):
            st.session_state.pop("catalog_api_preview")
            st.rerun()
        books, invalid_files, search_index = cached_catalog((), (preview,))
    else:
        try:
            paths = []
            for path in sorted(xml_folder.glob("*.xml")):
                if path.is_file():
                    info = path.stat()
                    paths.append((str(path), info.st_mtime_ns, info.st_size))
            books, invalid_files, search_index = cached_catalog(tuple(paths), files)
        except OSError:
            st.error("catalog 폴더의 XML 파일을 읽을 수 없어요.")
            books, invalid_files, search_index = cached_catalog((), files)

    if invalid_files:
        st.warning(
            f"XML {invalid_files}개에서 책을 읽지 못했어요. "
            "API 오류 응답이 들어 있는지, 도서 항목의 태그 이름이 맞는지 확인해 주세요."
        )

    source_is_xml = bool(books)
    if not source_is_xml:
        books = demo_books
        search_index = build_search_index(books)
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
        previous = st.session_state.get("last_catalog_search")
        if previous and previous[0] is books and previous[1] == search_text:
            matches, search_message = previous[2]
        else:
            matches, search_message = search_books(books, search_text, search_index)
            st.session_state["last_catalog_search"] = (books, search_text, (matches, search_message))
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

    if from_catalog:
        search_query = book_isbn(book) or yes24_title_query(book)
        st.link_button("YES24에서 직접 검색", yes24_search_url(search_query))

    with st.expander("Google Books 설정 (선택)"):
        external_lookup = st.toggle("Google Books 책 소개 자동 조회", value=True)
        with st.form("book_intro_settings", clear_on_submit=True):
            google_key = st.text_input("Google Books API 키 (선택)", type="password")
            apply_settings = st.form_submit_button("책 소개 검색 연결")
        if apply_settings:
            st.session_state["google_books_api_key"] = google_key.strip()
            st.session_state.pop("book_intro_recommendations", None)
            cancel_book_lookup()
    google_key = st.session_state.get("google_books_api_key") or os.getenv("GOOGLE_BOOKS_API_KEY", "")

    with st.expander("내 감상이나 선호 반영하기"):
        book_key = (book.title, book.author, book.publisher, book.publication_year)
        impression = st.text_area(
            "내 감상문 (선택)",
            placeholder="예: 바다를 바라보는 고요한 장면",
            key=f"impression-{book_key}",
        ).strip()
        extra_text = st.text_area(
            "추가 자료 (선택)",
            placeholder="책 소개, 줄거리, 출판사 리뷰 등을 붙여 넣으세요.",
            key=f"book-evidence-{book_key}",
        ).strip()
        available_moods = [mood for mood, route in MOOD_ROUTES.items() if route in routes]
        selected_mood = st.selectbox("산책 분위기", ["책 정보에서 자동 추천", *available_moods])

    if selected_mood in MOOD_ROUTES:
        cancel_book_lookup()
        feature_id = MOOD_ROUTES[selected_mood]
        reason = f"선택한 분위기 · {selected_mood}. {ROUTE_REASONS[feature_id]}"
    else:
        documents = [("독자 감상문", impression), ("직접 입력한 자료", extra_text)]
        source_links = {}
        if from_catalog:
            lookup_status = st.empty()
            book_documents, source_links = collect_book_documents(
                book, google_key, lookup_status, external_lookup
            )
            documents.extend(book_documents)
        else:
            cancel_book_lookup()
        semantic = recommend_by_sentence(documents, routes, source_links)
        if semantic:
            feature_id, reason = semantic
            name, distance = routes[feature_id]
            return name, distance, feature_id, reason

        matches = match_route_keywords(documents, routes)
        used_metadata = not any(matches.values())
        if used_metadata:
            metadata_documents = [
                ("장서 제목" if from_catalog else "검색어", book.title),
                ("장서 주제", book.subject),
                ("장서 주제어", book.keywords),
            ]
            matches = match_route_keywords([*metadata_documents, ("장서 분류", book.category)], routes)
            if from_catalog and not any(matches.values()):
                semantic = recommend_by_sentence(metadata_documents, routes, {}, metadata=True)
                if semantic:
                    feature_id, reason = semantic
                    name, distance = routes[feature_id]
                    return name, distance, feature_id, reason

        count = max(map(len, matches.values()))
        tied_routes = [route for route, hits in matches.items() if len(hits) == count]
        feature_id = min(tied_routes, key=lambda route: routes[route][1])
        if count:
            hits = matches[feature_id]
            words = ", ".join(list(hits)[:8])
            if count > 8:
                words += f" 외 {count - 8}개"
            basis = "제목·주제 정보" if used_metadata else "책 자료·감상문"
            reason = f"{basis} 기준 · 일치 단어 {count}개 ({words}). {ROUTE_REASONS[feature_id]}"
            if len(tied_routes) > 1:
                reason += " 동점인 코스 중 가장 짧은 길을 골랐어요."
            sources = list(dict.fromkeys(source for found_in in hits.values() for source in found_in))
            for source in sources:
                if source in source_links:
                    st.markdown(f"추천에 사용한 자료 · [{source}]({source_links[source]})")
            with st.expander("코스별 일치 단어와 추천 근거"):
                for route, route_hits in matches.items():
                    words = ", ".join(route_hits)
                    st.write(f"{routes[route][0]} · {len(route_hits)}개" + (f" · {words}" if words else ""))
                st.caption("선택된 코스의 단어별 출처")
                for term, found_in in hits.items():
                    st.write(f"{term} · {', '.join(found_in)}")
        else:
            has_text = bool(collect_sentences(documents))
            detail = (
                "현재 자료만으로는 특정 코스를 고르기 어려워요."
                if has_text else "활용할 책 소개나 감상문이 없고, 제목·주제만으로는 고르기 어려워요."
            )
            reason = f"기본 코스 안내 · {detail} 가장 짧은 코스를 안내해요."

    name, distance = routes[feature_id]
    return name, distance, feature_id, reason


@st.cache_resource(show_spinner=False, max_entries=4)
def cached_catalog(paths, uploads):
    files = [Path(path).read_bytes() for path, _, _ in paths]
    books, invalid_files = read_catalog([*files, *uploads])
    return books, invalid_files, build_search_index(books)


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


@st.fragment
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
