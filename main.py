import html
import json
import re

import pandas as pd
import pydeck as pdk
import requests
import streamlit as st
from openai import OpenAI


# =========================================================
# 1. Streamlit 기본 설정
# =========================================================
st.set_page_config(
    page_title="대한민국 숙박 지도",
    page_icon="🏨",
    layout="wide",
)

st.title("🏨 대한민국 숙박 지도")
st.caption(
    "한국관광공사 TourAPI의 숙박시설 정보와 "
    "Solar AI 숙박 가이드를 함께 이용할 수 있습니다."
)


# =========================================================
# 2. API 주소
# =========================================================
TOUR_BASE_URL = "https://apis.data.go.kr/B551011/KorService2"

GEOJSON_URL = (
    "https://raw.githubusercontent.com/greatsong/modudata/"
    "main/data/boundaries/sigungu_kr.geojson"
)

SOLAR_BASE_URL = "https://api.upstage.ai/v1"
SOLAR_MODEL = "solar-open2"


# =========================================================
# 3. 세션 상태 초기화
# =========================================================
if "selected_stay_id" not in st.session_state:
    st.session_state.selected_stay_id = None

if "search_keyword" not in st.session_state:
    st.session_state.search_keyword = ""

if "chat_messages" not in st.session_state:
    st.session_state.chat_messages = []

if "current_region" not in st.session_state:
    st.session_state.current_region = None


# =========================================================
# 4. 한국관광공사 API 공통 호출 함수
# =========================================================
@st.cache_data(ttl=3600, show_spinner=False)
def call_tour_api(endpoint, service_key, **extra_params):
    """
    한국관광공사 TourAPI를 호출합니다.

    endpoint에는 searchStay2, detailCommon2 같은
    서비스 이름이 들어갑니다.
    """

    params = {
        "serviceKey": service_key,
        "MobileOS": "WEB",
        "MobileApp": "KoreaStayMap",
        "_type": "json",
        "pageNo": 1,
        "numOfRows": 1000,
        **extra_params,
    }

    try:
        response = requests.get(
            f"{TOUR_BASE_URL}/{endpoint}",
            params=params,
            timeout=25,
        )
        response.raise_for_status()

        # 인증키 오류 등은 XML 형태로 반환될 수 있습니다.
        if (
            "OpenAPI_ServiceResponse" in response.text
            or "faultInfo" in response.text
            or "SERVICE_KEY_IS_NOT_REGISTERED_ERROR"
            in response.text
        ):
            raise ValueError(
                "한국관광공사 API 인증 과정에서 문제가 발생했습니다. "
                "TOUR_KEY와 활용 신청 상태를 확인해 주세요."
            )

        data = response.json()
        api_response = data.get("response", {})
        header = api_response.get("header", {})

        result_code = str(
            header.get("resultCode", "")
        ).strip()

        if result_code != "0000":
            result_message = header.get(
                "resultMsg",
                "API 요청이 정상적으로 처리되지 않았습니다.",
            )

            raise ValueError(result_message)

        return api_response.get("body", {})

    except requests.exceptions.Timeout:
        raise ValueError(
            "한국관광공사 서버의 응답이 늦어 "
            "요청 시간이 초과되었습니다."
        )

    except requests.exceptions.ConnectionError:
        raise ValueError(
            "한국관광공사 서버에 연결하지 못했습니다. "
            "인터넷 연결 상태를 확인한 뒤 다시 시도해 주세요."
        )

    except requests.exceptions.RequestException:
        raise ValueError(
            "한국관광공사 데이터를 불러오는 중 문제가 발생했습니다. "
            "잠시 후 다시 시도해 주세요."
        )

    except json.JSONDecodeError:
        raise ValueError(
            "한국관광공사 응답을 읽지 못했습니다. "
            "인증키와 API 설정을 확인해 주세요."
        )

    except ValueError:
        raise

    except Exception:
        raise ValueError(
            "한국관광공사 데이터를 처리하는 중 "
            "예상하지 못한 문제가 발생했습니다."
        )


def get_items(body):
    """
    TourAPI 결과가 한 건이거나 여러 건이어도
    항상 파이썬 목록 형태로 바꿉니다.
    """

    items = body.get("items") or {}

    if not isinstance(items, dict):
        return []

    rows = items.get("item", [])

    if isinstance(rows, dict):
        return [rows]

    if isinstance(rows, list):
        return rows

    return []


# =========================================================
# 5. 시도·시군구 법정동 코드 조회
# =========================================================
@st.cache_data(ttl=86400, show_spinner=False)
def load_regions(service_key, sido_code=""):
    """
    시도 또는 시군구 법정동 코드 목록을 가져옵니다.
    """

    params = {
        "lDongListYn": "N",
    }

    if sido_code:
        params["lDongRegnCd"] = sido_code

    body = call_tour_api(
        "ldongCode2",
        service_key,
        **params,
    )

    result = []

    for row in get_items(body):
        name = str(
            row.get("name", "")
        ).strip()

        code = str(
            row.get("code", "")
        ).strip()

        if name and code:
            result.append(
                {
                    "name": name,
                    "code": code,
                }
            )

    return sorted(
        result,
        key=lambda item: item["name"],
    )


# =========================================================
# 6. 숙박시설 목록 조회
# =========================================================
@st.cache_data(ttl=1800, show_spinner=False)
def load_stays(service_key, sido_code, sigungu_code):
    """
    선택한 시군구의 숙박시설 목록을 가져옵니다.
    """

    body = call_tour_api(
        "searchStay2",
        service_key,
        arrange="A",
        lDongRegnCd=sido_code,
        lDongSignguCd=sigungu_code,
    )

    result = []

    for number, row in enumerate(get_items(body)):
        title = str(
            row.get("title", "")
        ).strip()

        if not title:
            continue

        try:
            longitude = float(row.get("mapx"))
            latitude = float(row.get("mapy"))

        except (TypeError, ValueError):
            longitude = None
            latitude = None

        content_id = str(
            row.get("contentid", "")
        ).strip()

        content_type_id = str(
            row.get("contenttypeid", "32")
        ).strip()

        # 콘텐츠 ID가 없는 경우를 대비한 임시 식별자입니다.
        stay_id = content_id

        if not stay_id:
            stay_id = (
                f"{title}|"
                f"{row.get('addr1', '')}|"
                f"{longitude}|"
                f"{latitude}|"
                f"{number}"
            )

        result.append(
            {
                "stay_id": stay_id,
                "contentid": content_id,
                "contenttypeid": content_type_id or "32",
                "숙박시설": title,
                "주소": str(
                    row.get("addr1", "")
                ).strip(),
                "상세주소": str(
                    row.get("addr2", "")
                ).strip(),
                "전화번호": str(
                    row.get("tel", "")
                ).strip(),
                "대표이미지": str(
                    row.get("firstimage", "")
                ).strip(),
                "경도": longitude,
                "위도": latitude,
            }
        )

    return sorted(
        result,
        key=lambda item: item["숙박시설"],
    )


# =========================================================
# 7. 개별 숙박시설 상세정보 조회
# =========================================================
@st.cache_data(ttl=3600, show_spinner=False)
def load_stay_common_detail(service_key, content_id):
    """
    숙박시설의 공통 상세정보를 가져옵니다.

    개요, 홈페이지, 주소, 좌표 등의 정보가 포함될 수 있습니다.
    """

    if not content_id:
        return {}

    body = call_tour_api(
        "detailCommon2",
        service_key,
        contentId=content_id,
        defaultYN="Y",
        firstImageYN="Y",
        areacodeYN="Y",
        catcodeYN="Y",
        addrinfoYN="Y",
        mapinfoYN="Y",
        overviewYN="Y",
    )

    items = get_items(body)

    if not items:
        return {}

    return items[0]


@st.cache_data(ttl=3600, show_spinner=False)
def load_stay_intro_detail(
    service_key,
    content_id,
    content_type_id="32",
):
    """
    숙박시설의 소개 상세정보를 가져옵니다.

    입실 시간, 퇴실 시간, 객실 수, 주차,
    예약 안내 등이 포함될 수 있습니다.
    """

    if not content_id:
        return {}

    body = call_tour_api(
        "detailIntro2",
        service_key,
        contentId=content_id,
        contentTypeId=content_type_id or "32",
    )

    items = get_items(body)

    if not items:
        return {}

    return items[0]


# =========================================================
# 8. HTML 태그 제거 함수
# =========================================================
def clean_text(value):
    """
    TourAPI 데이터에 포함된 HTML 태그와
    불필요한 공백을 정리합니다.
    """

    if value is None:
        return ""

    text = str(value)

    text = re.sub(
        r"<br\s*/?>",
        "\n",
        text,
        flags=re.IGNORECASE,
    )

    text = re.sub(
        r"<[^>]+>",
        "",
        text,
    )

    text = html.unescape(text)

    text = re.sub(
        r"[ \t]+",
        " ",
        text,
    )

    text = re.sub(
        r"\n{3,}",
        "\n\n",
        text,
    )

    return text.strip()


# =========================================================
# 9. GeoJSON 불러오기
# =========================================================
@st.cache_data(ttl=86400, show_spinner=False)
def load_geojson():
    """
    GitHub에서 전국 시군구 경계 GeoJSON을 불러옵니다.
    """

    try:
        response = requests.get(
            GEOJSON_URL,
            timeout=30,
        )
        response.raise_for_status()

        data = response.json()

        if not isinstance(data, dict):
            raise ValueError

        if "features" not in data:
            raise ValueError

        return data

    except requests.exceptions.Timeout:
        raise ValueError(
            "행정구역 지도 데이터를 불러오는 시간이 초과되었습니다."
        )

    except requests.exceptions.RequestException:
        raise ValueError(
            "행정구역 지도 데이터를 내려받지 못했습니다. "
            "잠시 후 다시 시도해 주세요."
        )

    except Exception:
        raise ValueError(
            "행정구역 지도 데이터를 읽는 중 문제가 발생했습니다."
        )


def find_boundary(geojson, region_code):
    """
    관광공사 법정동 코드와 정확히 일치하는
    시군구 GeoJSON 경계를 찾습니다.
    """

    matched_features = []

    for feature in geojson.get("features", []):
        properties = feature.get(
            "properties",
            {},
        )

        geo_code = str(
            properties.get("코드", "")
        ).strip().zfill(5)

        if geo_code == region_code:
            matched_features.append(feature)

    if not matched_features:
        return None

    return {
        "type": "FeatureCollection",
        "features": matched_features,
    }


# =========================================================
# 10. 지도 중심과 확대 수준 계산
# =========================================================
def collect_coordinates(value, result):
    """
    Polygon과 MultiPolygon 안의 좌표를
    하나의 목록으로 모읍니다.
    """

    if (
        isinstance(value, list)
        and len(value) >= 2
        and isinstance(value[0], (int, float))
        and isinstance(value[1], (int, float))
    ):
        result.append(
            (
                value[0],
                value[1],
            )
        )
        return

    if isinstance(value, list):
        for item in value:
            collect_coordinates(
                item,
                result,
            )


def get_boundary_view(boundary):
    """
    선택한 시군구 전체가 보이도록
    지도 중심과 확대 수준을 계산합니다.
    """

    coordinates = []

    for feature in boundary.get("features", []):
        geometry = feature.get(
            "geometry",
            {},
        )

        collect_coordinates(
            geometry.get("coordinates", []),
            coordinates,
        )

    if not coordinates:
        return 36.3, 127.8, 6

    longitudes = [
        point[0]
        for point in coordinates
    ]

    latitudes = [
        point[1]
        for point in coordinates
    ]

    min_lon = min(longitudes)
    max_lon = max(longitudes)
    min_lat = min(latitudes)
    max_lat = max(latitudes)

    center_lon = (
        min_lon + max_lon
    ) / 2

    center_lat = (
        min_lat + max_lat
    ) / 2

    spread = max(
        max_lon - min_lon,
        max_lat - min_lat,
    )

    if spread > 3:
        zoom = 6

    elif spread > 1.5:
        zoom = 7

    elif spread > 0.8:
        zoom = 8

    elif spread > 0.4:
        zoom = 9

    elif spread > 0.2:
        zoom = 10

    else:
        zoom = 11

    return center_lat, center_lon, zoom


# =========================================================
# 11. 지도 만들기
# =========================================================
def make_map(stay_df, boundary, selected_id=None):
    """
    시군구 경계와 숙박시설 위치를 지도에 표시합니다.

    선택된 숙박시설은 노란색으로 크게 표시합니다.
    """

    layers = []

    # -----------------------------------------------------
    # 시군구 경계
    # -----------------------------------------------------
    if boundary:
        center_lat, center_lon, zoom = get_boundary_view(
            boundary
        )

        layers.append(
            pdk.Layer(
                "GeoJsonLayer",
                boundary,
                id="sigungu-boundary",
                filled=True,
                stroked=True,
                get_fill_color=[52, 120, 246, 55],
                get_line_color=[34, 88, 190, 230],
                line_width_min_pixels=2,
                pickable=False,
            )
        )

    else:
        valid_stays = stay_df.dropna(
            subset=["경도", "위도"]
        )

        if valid_stays.empty:
            center_lat = 36.3
            center_lon = 127.8
            zoom = 6

        else:
            center_lat = float(
                valid_stays["위도"].mean()
            )

            center_lon = float(
                valid_stays["경도"].mean()
            )

            zoom = 10

    # -----------------------------------------------------
    # 좌표가 있는 숙박시설만 지도에 표시
    # -----------------------------------------------------
    valid_stays = stay_df.dropna(
        subset=["경도", "위도"]
    ).copy()

    selected_stays = valid_stays[
        valid_stays["stay_id"].astype(str)
        == str(selected_id)
    ].copy()

    normal_stays = valid_stays[
        valid_stays["stay_id"].astype(str)
        != str(selected_id)
    ].copy()

    # -----------------------------------------------------
    # 일반 숙박시설
    # -----------------------------------------------------
    if not normal_stays.empty:
        layers.append(
            pdk.Layer(
                "ScatterplotLayer",
                normal_stays,
                id="stay-points",
                get_position="[경도, 위도]",
                get_radius=350,
                radius_min_pixels=5,
                radius_max_pixels=14,
                get_fill_color=[238, 82, 83, 220],
                get_line_color=[255, 255, 255, 240],
                line_width_min_pixels=1,
                pickable=True,
                auto_highlight=True,
                highlight_color=[255, 210, 60, 220],
            )
        )

    # -----------------------------------------------------
    # 지도에서 선택한 숙박시설
    # -----------------------------------------------------
    if not selected_stays.empty:
        layers.append(
            pdk.Layer(
                "ScatterplotLayer",
                selected_stays,
                id="selected-stay",
                get_position="[경도, 위도]",
                get_radius=650,
                radius_min_pixels=11,
                radius_max_pixels=24,
                get_fill_color=[255, 193, 7, 250],
                get_line_color=[110, 70, 0, 255],
                line_width_min_pixels=3,
                pickable=True,
            )
        )

        center_lat = float(
            selected_stays.iloc[0]["위도"]
        )

        center_lon = float(
            selected_stays.iloc[0]["경도"]
        )

        zoom = max(
            zoom,
            11,
        )

    return pdk.Deck(
        map_style=(
            "https://basemaps.cartocdn.com/gl/"
            "positron-gl-style/style.json"
        ),
        initial_view_state=pdk.ViewState(
            latitude=center_lat,
            longitude=center_lon,
            zoom=zoom,
            pitch=15,
        ),
        layers=layers,
        tooltip={
            "html": (
                "<b>{숙박시설}</b><br>"
                "<span>{주소}</span>"
            ),
            "style": {
                "backgroundColor": "#263238",
                "color": "white",
                "fontSize": "13px",
            },
        },
    )


# =========================================================
# 12. 지도 점 선택 이벤트
# =========================================================
def on_map_select():
    """
    지도에서 숙박시설 점을 클릭했을 때
    선택한 숙박시설 ID를 세션에 저장합니다.
    """

    map_state = st.session_state.get(
        "stay_map",
        {},
    )

    selection = map_state.get(
        "selection",
        {},
    )

    objects_by_layer = selection.get(
        "objects",
        {},
    )

    selected_objects = (
        objects_by_layer.get(
            "selected-stay",
            [],
        )
        or objects_by_layer.get(
            "stay-points",
            [],
        )
    )

    if not selected_objects:
        return

    selected_id = str(
        selected_objects[0].get(
            "stay_id",
            "",
        )
    ).strip()

    if not selected_id:
        return

    st.session_state.selected_stay_id = selected_id

    # 검색 결과에 가려지지 않게 검색어를 지웁니다.
    st.session_state.search_keyword = ""


# =========================================================
# 13. 선택한 숙박시설 강조 카드
# =========================================================
def show_selected_card(selected_row):
    """
    지도에서 선택한 숙박시설을
    오른쪽 위의 강조 카드로 표시합니다.
    """

    title = html.escape(
        str(
            selected_row.get(
                "숙박시설",
                "",
            )
            or ""
        )
    )

    address = html.escape(
        str(
            selected_row.get(
                "주소",
                "",
            )
            or ""
        )
    )

    detail_address = html.escape(
        str(
            selected_row.get(
                "상세주소",
                "",
            )
            or ""
        )
    )

    telephone = html.escape(
        str(
            selected_row.get(
                "전화번호",
                "",
            )
            or ""
        )
    )

    full_address = " ".join(
        value
        for value in [
            address,
            detail_address,
        ]
        if value
    )

    if not full_address:
        full_address = "주소 정보 없음"

    if not telephone:
        telephone = "전화번호 정보 없음"

    # 한 줄 문자열로 만들면 HTML이 코드 블록으로 깨지는 현상을 막을 수 있습니다.
    card_html = (
        '<div style="'
        'border:2px solid #e6aa22;'
        'border-radius:12px;'
        'padding:16px 18px;'
        'margin:4px 0 14px 0;'
        'background-color:rgba(255,193,7,0.12);'
        'box-shadow:0 2px 8px rgba(0,0,0,0.06);'
        '">'
        '<div style="'
        'font-size:0.82rem;'
        'font-weight:700;'
        'color:#9a671b;'
        'margin-bottom:7px;'
        '">'
        '지도에서 선택한 숙박시설'
        '</div>'
        '<div style="'
        'font-size:1.08rem;'
        'font-weight:800;'
        'margin-bottom:8px;'
        '">'
        f"{title}"
        "</div>"
        '<div style="'
        'font-size:0.92rem;'
        'line-height:1.5;'
        'margin-bottom:5px;'
        '">'
        f"📍 {full_address}"
        "</div>"
        '<div style="'
        'font-size:0.88rem;'
        'opacity:0.82;'
        '">'
        f"☎ {telephone}"
        "</div>"
        "</div>"
    )

    st.markdown(
        card_html,
        unsafe_allow_html=True,
    )


# =========================================================
# 14. AI가 참고할 상세정보 만들기
# =========================================================
def normalize_name(text):
    """
    숙박시설 이름 비교를 위해 괄호, 공백,
    일부 특수문자를 제거합니다.
    """

    value = clean_text(text).lower()

    value = re.sub(
        r"[\s\(\)\[\]\{\}·ㆍ\-_,.]",
        "",
        value,
    )

    return value


def find_question_stays(
    question,
    stay_df,
    selected_id=None,
    maximum=3,
):
    """
    질문에 숙박시설명이 포함되어 있는지 확인합니다.

    지도에서 선택된 숙박시설도 상세조회 대상에 포함합니다.
    """

    if stay_df.empty:
        return []

    question_normalized = normalize_name(
        question
    )

    matched_indexes = []

    # 지도에서 선택된 숙박시설을 우선 추가합니다.
    if selected_id:
        selected_rows = stay_df[
            stay_df["stay_id"].astype(str)
            == str(selected_id)
        ]

        for index in selected_rows.index:
            matched_indexes.append(index)

    # 질문에 이름이 직접 포함된 숙박시설을 찾습니다.
    for index, row in stay_df.iterrows():
        title_normalized = normalize_name(
            row.get("숙박시설", "")
        )

        if (
            title_normalized
            and len(title_normalized) >= 3
            and title_normalized
            in question_normalized
        ):
            if index not in matched_indexes:
                matched_indexes.append(index)

        if len(matched_indexes) >= maximum:
            break

    return [
        stay_df.loc[index].to_dict()
        for index in matched_indexes[:maximum]
    ]


def format_stay_detail(
    service_key,
    stay,
):
    """
    개별 숙박시설의 TourAPI 상세정보를
    AI가 읽을 수 있는 한국어 텍스트로 바꿉니다.
    """

    title = clean_text(
        stay.get("숙박시설", "")
    )

    content_id = clean_text(
        stay.get("contentid", "")
    )

    content_type_id = clean_text(
        stay.get("contenttypeid", "32")
    ) or "32"

    common = {}
    intro = {}

    if content_id:
        try:
            common = load_stay_common_detail(
                service_key,
                content_id,
            )
        except ValueError:
            common = {}

        try:
            intro = load_stay_intro_detail(
                service_key,
                content_id,
                content_type_id,
            )
        except ValueError:
            intro = {}

    detail_lines = [
        f"숙박시설명: {title}",
        f"주소: {clean_text(stay.get('주소', ''))}",
        f"상세주소: {clean_text(stay.get('상세주소', ''))}",
        f"전화번호: {clean_text(stay.get('전화번호', ''))}",
    ]

    fields = [
        ("홈페이지", common.get("homepage")),
        ("개요", common.get("overview")),
        ("입실 시간", intro.get("checkintime")),
        ("퇴실 시간", intro.get("checkouttime")),
        ("객실 수", intro.get("roomcount")),
        ("수용 가능 인원", intro.get("accomcountlodging")),
        ("주차 안내", intro.get("parkinglodging")),
        ("예약 안내", intro.get("reservationlodging")),
        ("예약 주소", intro.get("reservationurl")),
        ("조리 가능 여부", intro.get("chkcooking")),
        ("식음료장", intro.get("foodplace")),
        ("픽업 서비스", intro.get("pickup")),
        ("객실 내 취사", intro.get("roomtype")),
        ("부대시설", intro.get("subfacility")),
        ("베니키아 여부", intro.get("benikia")),
        ("굿스테이 여부", intro.get("goodstay")),
        ("한옥 여부", intro.get("hanok")),
    ]

    for label, value in fields:
        cleaned_value = clean_text(value)

        if cleaned_value:
            detail_lines.append(
                f"{label}: {cleaned_value}"
            )

    return "\n".join(detail_lines)


def build_ai_context(
    question,
    stay_df,
    region_name,
    service_key,
    selected_id=None,
):
    """
    현재 지도와 TourAPI 데이터를
    Solar AI에게 전달할 참고자료로 만듭니다.
    """

    total_count = len(stay_df)

    coordinate_count = (
        stay_df.dropna(
            subset=["경도", "위도"]
        ).shape[0]
        if not stay_df.empty
        else 0
    )

    context_parts = [
        "[현재 지도 정보]",
        f"선택 지역: {region_name}",
        f"TourAPI에서 조회된 숙박시설 수: {total_count}개",
        f"지도에 좌표를 표시할 수 있는 숙박시설 수: {coordinate_count}개",
        "",
        "[현재 지역 숙박시설 목록]",
    ]

    if stay_df.empty:
        context_parts.append(
            "조회된 숙박시설이 없습니다."
        )

    else:
        # AI 입력 길이를 지나치게 늘리지 않도록
        # 숙박시설명, 주소, 전화번호 중심으로 전달합니다.
        for _, row in stay_df.iterrows():
            title = clean_text(
                row.get("숙박시설", "")
            )

            address = clean_text(
                row.get("주소", "")
            )

            detail_address = clean_text(
                row.get("상세주소", "")
            )

            telephone = clean_text(
                row.get("전화번호", "")
            )

            full_address = " ".join(
                value
                for value in [
                    address,
                    detail_address,
                ]
                if value
            )

            line = (
                f"- 숙박시설: {title}"
                f" | 주소: {full_address or '정보 없음'}"
                f" | 전화번호: {telephone or '정보 없음'}"
            )

            context_parts.append(line)

    # 질문과 관련된 숙박시설은 상세 API까지 추가로 조회합니다.
    related_stays = find_question_stays(
        question,
        stay_df,
        selected_id=selected_id,
        maximum=3,
    )

    if related_stays:
        context_parts.extend(
            [
                "",
                "[질문과 관련된 숙박시설의 TourAPI 상세정보]",
            ]
        )

        for stay in related_stays:
            context_parts.append(
                format_stay_detail(
                    service_key,
                    stay,
                )
            )

            context_parts.append("")

    context_parts.extend(
        [
            "[답변 원칙]",
            "위 자료는 현재 선택된 지역과 TourAPI에서 실제로 조회한 자료이다.",
            "자료에 없는 별점, 가격, 객실 재고, 이용 후기, 서비스 내용은 추측하지 않는다.",
            "자료에 없는 내용은 확인할 수 없다고 분명하게 말한다.",
            "숙박시설 수를 묻는 경우 현재 조회 결과의 개수를 기준으로 답한다.",
            "현재 선택 지역이 아닌 다른 지역을 묻는 경우에는 "
            "그 지역을 지도에서 먼저 선택해 달라고 안내한다.",
        ]
    )

    return "\n".join(context_parts)


# =========================================================
# 15. Solar API 스트리밍 답변
# =========================================================
def stream_solar_answer(
    solar_client,
    question,
    grounding_context,
):
    """
    Solar API로부터 답변을 스트리밍으로 받아
    글자가 실시간으로 나오도록 합니다.
    """

    system_prompt = (
        "너는 다정하고 따뜻한 성격의 국내 숙박시설 가이드야. "
        "반드시 순수 한국어로만 답해. "
        "한국관광공사 TourAPI와 사용자가 보고 있는 숙박 지도 자료만 "
        "사실의 근거로 사용해. "
        "자료에 없는 정보는 절대로 지어내거나 추측하지 마. "
        "확인할 수 없는 내용은 확인할 수 없다고 솔직하게 말해. "
        "별점, 숙박요금, 객실 재고, 후기처럼 제공되지 않은 정보는 "
        "임의로 만들어내지 마. "
        "답변은 친절하고 알아보기 쉽게 작성해."
    )

    # 대화가 지나치게 길어지지 않도록 최근 대화만 전달합니다.
    recent_history = st.session_state.chat_messages[-12:]

    messages = [
        {
            "role": "system",
            "content": system_prompt,
        }
    ]

    for message in recent_history:
        role = message.get("role")
        content = message.get("content", "")

        if role in {"user", "assistant"} and content:
            messages.append(
                {
                    "role": role,
                    "content": content,
                }
            )

    # 사용자 질문과 근거 데이터를 한 메시지에 함께 전달합니다.
    messages.append(
        {
            "role": "user",
            "content": (
                f"{question}\n\n"
                "아래 참고자료만 사실 근거로 사용해 답해 줘.\n\n"
                f"{grounding_context}"
            ),
        }
    )

    try:
        stream = solar_client.chat.completions.create(
            model=SOLAR_MODEL,
            messages=messages,
            reasoning_effort="none",
            stream=True,
        )

        for chunk in stream:
            if not chunk.choices:
                continue

            text = chunk.choices[0].delta.content

            if text:
                yield text

    except Exception as error:
        error_text = str(error).lower()

        if (
            "authentication" in error_text
            or "api key" in error_text
            or "401" in error_text
        ):
            raise ValueError(
                "Solar API 인증에 실패했습니다. "
                "비밀 금고의 SOLAR_API_KEY를 확인해 주세요."
            )

        if (
            "rate limit" in error_text
            or "429" in error_text
        ):
            raise ValueError(
                "현재 AI 요청이 많습니다. "
                "잠시 기다린 뒤 다시 질문해 주세요."
            )

        if (
            "timeout" in error_text
            or "timed out" in error_text
        ):
            raise ValueError(
                "AI의 응답이 늦어 요청 시간이 초과되었습니다. "
                "잠시 후 다시 시도해 주세요."
            )

        raise ValueError(
            "AI 답변을 불러오지 못했습니다. "
            "잠시 후 다시 질문해 주세요."
        )


# =========================================================
# 16. 비밀 금고의 API 키 확인
# =========================================================
try:
    tour_key = st.secrets["TOUR_KEY"]

except KeyError:
    st.error(
        "비밀 금고에 `TOUR_KEY`가 없습니다. "
        "Streamlit Cloud의 설정에서 인증키를 등록해 주세요."
    )
    st.stop()


try:
    solar_api_key = st.secrets["SOLAR_API_KEY"]

except KeyError:
    st.error(
        "비밀 금고에 `SOLAR_API_KEY`가 없습니다. "
        "Streamlit Cloud의 설정에서 Solar API 키를 등록해 주세요."
    )
    st.stop()


# Solar API 클라이언트를 만듭니다.
solar_client = OpenAI(
    api_key=solar_api_key,
    base_url=SOLAR_BASE_URL,
)


# =========================================================
# 17. 숙박 지도 앱 실행
# =========================================================
try:
    with st.spinner(
        "지역 정보를 불러오는 중입니다..."
    ):
        sido_list = load_regions(
            tour_key
        )

        geojson = load_geojson()

    if not sido_list:
        st.warning(
            "조회 가능한 시도 정보가 없습니다."
        )
        st.stop()

    # -----------------------------------------------------
    # 지역 선택
    # -----------------------------------------------------
    st.subheader("📍 지역 선택")
    st.caption(
        "시도와 시군구를 차례로 선택해 주세요."
    )

    region_column1, region_column2 = st.columns(2)

    with region_column1:
        sido_name = st.selectbox(
            "시도",
            [
                item["name"]
                for item in sido_list
            ],
        )

    selected_sido = next(
        item
        for item in sido_list
        if item["name"] == sido_name
    )

    sigungu_list = load_regions(
        tour_key,
        selected_sido["code"],
    )

    if not sigungu_list:
        st.warning(
            "선택한 시도의 시군구 정보가 없습니다."
        )
        st.stop()

    with region_column2:
        sigungu_name = st.selectbox(
            "시군구",
            [
                item["name"]
                for item in sigungu_list
            ],
        )

    selected_sigungu = next(
        item
        for item in sigungu_list
        if item["name"] == sigungu_name
    )

    # 관광공사 코드와 GeoJSON 코드의 연결에 사용합니다.
    region_code = (
        str(
            selected_sido["code"]
        ).zfill(2)
        + str(
            selected_sigungu["code"]
        ).zfill(3)
    )

    region_name = (
        f"{sido_name} {sigungu_name}"
    )

    # 지역이 바뀌면 지도 선택과 대화 기록을 초기화합니다.
    if (
        st.session_state.current_region
        != region_code
    ):
        st.session_state.current_region = region_code
        st.session_state.selected_stay_id = None
        st.session_state.search_keyword = ""
        st.session_state.chat_messages = []

        st.session_state.pop(
            "stay_map",
            None,
        )

    boundary = find_boundary(
        geojson,
        region_code,
    )

    # -----------------------------------------------------
    # 숙박시설 목록 조회
    # -----------------------------------------------------
    with st.spinner(
        f"{region_name} 숙박정보를 불러오는 중입니다..."
    ):
        stays = load_stays(
            tour_key,
            selected_sido["code"],
            selected_sigungu["code"],
        )

    stay_df = pd.DataFrame(
        stays,
        columns=[
            "stay_id",
            "contentid",
            "contenttypeid",
            "숙박시설",
            "주소",
            "상세주소",
            "전화번호",
            "대표이미지",
            "경도",
            "위도",
        ],
    )

    selected_id = st.session_state.get(
        "selected_stay_id"
    )

    # 현재 지역에 없는 숙박시설 ID가 남아 있으면 해제합니다.
    if selected_id and not stay_df.empty:
        current_ids = (
            stay_df["stay_id"]
            .astype(str)
            .tolist()
        )

        if str(selected_id) not in current_ids:
            st.session_state.selected_stay_id = None
            selected_id = None

    st.divider()

    # -----------------------------------------------------
    # 요약 정보
    # -----------------------------------------------------
    map_count = (
        stay_df.dropna(
            subset=["경도", "위도"]
        ).shape[0]
        if not stay_df.empty
        else 0
    )

    metric1, metric2, metric3 = st.columns(3)

    metric1.metric(
        "선택 지역",
        region_name,
    )

    metric2.metric(
        "숙박시설 수",
        f"{len(stay_df):,}곳",
    )

    metric3.metric(
        "지도 표시 가능",
        f"{map_count:,}곳",
    )

    if boundary is None:
        st.warning(
            f"`{region_name}`의 법정동 코드 `{region_code}`와 "
            "일치하는 시군구 경계를 찾지 못했습니다. "
            "지역명으로 임의 연결하지 않고 숙박시설 위치만 표시합니다."
        )

    # -----------------------------------------------------
    # 지도와 숙박시설 표
    # -----------------------------------------------------
    left, right = st.columns(
        [1.35, 1]
    )

    with left:
        st.subheader(
            "🗺️ 시군구별 숙박시설 한눈에"
        )

        st.pydeck_chart(
            make_map(
                stay_df,
                boundary,
                selected_id,
            ),
            key="stay_map",
            on_select=on_map_select,
            selection_mode="single-object",
            width="stretch",
            height=620,
        )

        st.caption(
            "파란색 영역은 선택한 시군구의 경계입니다. "
            "빨간색 점에 마우스를 올리면 기본정보가 나타나며, "
            "점을 클릭하면 해당 숙박시설이 노란색으로 강조됩니다. "
            "지명은 지도 제공자의 표기를 따릅니다."
        )

    with right:
        st.subheader(
            "🔎 숙박시설 검색"
        )

        keyword = st.text_input(
            "숙박시설 이름 또는 주소",
            placeholder="예: 호텔, 한옥, 리조트",
            key="search_keyword",
        ).strip()

        filtered_df = stay_df.copy()

        if keyword and not filtered_df.empty:
            name_mask = (
                filtered_df["숙박시설"]
                .str.contains(
                    keyword,
                    case=False,
                    na=False,
                )
            )

            address_mask = (
                filtered_df["주소"]
                .str.contains(
                    keyword,
                    case=False,
                    na=False,
                )
            )

            filtered_df = filtered_df[
                name_mask | address_mask
            ].reset_index(drop=True)

        else:
            filtered_df = filtered_df.reset_index(
                drop=True
            )

        selected_rows = stay_df[
            stay_df["stay_id"].astype(str)
            == str(selected_id)
        ]

        if not selected_rows.empty:
            show_selected_card(
                selected_rows.iloc[0]
            )

        st.caption(
            f"가나다순 · {len(filtered_df):,}개"
        )

        if filtered_df.empty:
            if stay_df.empty:
                st.info(
                    f"현재 `{region_name}`에서 조회되는 "
                    "숙박정보가 없습니다."
                )

            else:
                st.info(
                    "검색 조건에 해당하는 숙박시설이 없습니다."
                )

        else:
            table_df = filtered_df[
                [
                    "숙박시설",
                    "주소",
                    "상세주소",
                    "전화번호",
                ]
            ].copy()

            # 오른쪽 표는 조회 전용입니다.
            st.dataframe(
                table_df,
                width="stretch",
                height=500,
                hide_index=True,
                column_config={
                    "숙박시설": st.column_config.TextColumn(
                        "숙박시설",
                        width="medium",
                    ),
                    "주소": st.column_config.TextColumn(
                        "주소",
                        width="large",
                    ),
                    "상세주소": st.column_config.TextColumn(
                        "상세주소",
                        width="small",
                    ),
                    "전화번호": st.column_config.TextColumn(
                        "전화번호",
                        width="small",
                    ),
                },
            )

    # =====================================================
    # 18. Solar AI 숙박 가이드 채팅
    # =====================================================
    st.divider()

    st.subheader(
        "💬 AI 숙박 가이드🤓"
    )

    st.caption(
        f"현재 선택된 `{region_name}`의 숙박 지도와 "
        "한국관광공사 TourAPI 자료를 근거로 답변합니다. "
        "다른 지역을 물어보려면 먼저 위에서 해당 지역을 선택해 주세요."
    )

    # 대화 초기화 버튼
    chat_button_column1, chat_button_column2 = st.columns(
        [1, 5]
    )

    with chat_button_column1:
        if st.button(
            "대화 초기화",
            use_container_width=True,
        ):
            st.session_state.chat_messages = []
            st.rerun()

    # 이전 대화를 말풍선으로 표시합니다.
    for message in st.session_state.chat_messages:
        role = message.get(
            "role",
            "assistant",
        )

        content = message.get(
            "content",
            "",
        )

        with st.chat_message(role):
            st.markdown(content)

    # 질문 입력창과 질문하기 버튼
    with st.form(
        "accommodation_chat_form",
        clear_on_submit=True,
    ):
        question = st.text_input(
            "숙박 지도와 TourAPI에 관해 질문해 주세요.",
            placeholder=(
                "예: 강릉시에는 숙박시설이 몇 개나 있어? "
                "또는 지도에서 선택한 호텔의 입실 시간은 언제야?"
            ),
        )

        ask_button = st.form_submit_button(
            "질문하기",
            type="primary",
            use_container_width=True,
        )

    if ask_button:
        question = question.strip()

        if not question:
            st.warning(
                "질문 내용을 입력해 주세요."
            )

        else:
            # 사용자의 질문을 먼저 대화 기록에 저장합니다.
            st.session_state.chat_messages.append(
                {
                    "role": "user",
                    "content": question,
                }
            )

            with st.chat_message("user"):
                st.markdown(question)

            try:
                with st.spinner(
                    "한국관광공사 자료를 확인하고 있습니다..."
                ):
                    grounding_context = build_ai_context(
                        question=question,
                        stay_df=stay_df,
                        region_name=region_name,
                        service_key=tour_key,
                        selected_id=selected_id,
                    )

                with st.chat_message("assistant"):
                    answer = st.write_stream(
                        stream_solar_answer(
                            solar_client=solar_client,
                            question=question,
                            grounding_context=grounding_context,
                        )
                    )

                if answer:
                    st.session_state.chat_messages.append(
                        {
                            "role": "assistant",
                            "content": answer,
                        }
                    )

                else:
                    st.session_state.chat_messages.append(
                        {
                            "role": "assistant",
                            "content": (
                                "죄송해요. 이번에는 답변을 받지 못했어요. "
                                "잠시 후 다시 질문해 주세요."
                            ),
                        }
                    )

            except ValueError as error:
                friendly_message = (
                    f"죄송해요. {error}"
                )

                with st.chat_message("assistant"):
                    st.warning(
                        friendly_message
                    )

                st.session_state.chat_messages.append(
                    {
                        "role": "assistant",
                        "content": friendly_message,
                    }
                )

            except Exception:
                friendly_message = (
                    "죄송해요. AI 답변을 처리하는 중 문제가 발생했습니다. "
                    "잠시 후 다시 질문해 주세요."
                )

                with st.chat_message("assistant"):
                    st.warning(
                        friendly_message
                    )

                st.session_state.chat_messages.append(
                    {
                        "role": "assistant",
                        "content": friendly_message,
                    }
                )


# =========================================================
# 19. 전체 앱 오류 처리
# =========================================================
except ValueError as error:
    st.error(
        f"⚠️ {error}"
    )

except Exception:
    st.error(
        "앱을 실행하는 중 예상하지 못한 문제가 발생했습니다. "
        "잠시 후 페이지를 새로고침해 주세요."
    )
