import html

import pandas as pd
import pydeck as pdk
import requests
import streamlit as st


# =========================================================
# 기본 설정
# =========================================================
st.set_page_config(
    page_title="대한민국 숙박 지도",
    page_icon="🏨",
    layout="wide",
)

st.title("🏨 대한민국 숙박 지도")
st.caption("한국관광공사 국문 관광정보 서비스 · 법정동 코드 기준")

BASE_URL = "https://apis.data.go.kr/B551011/KorService2"

GEOJSON_URL = (
    "https://raw.githubusercontent.com/greatsong/modudata/"
    "main/data/boundaries/sigungu_kr.geojson"
)


# =========================================================
# 한국관광공사 API 호출
# =========================================================
@st.cache_data(ttl=3600, show_spinner=False)
def call_api(endpoint, service_key, **extra_params):
    """한국관광공사 API를 호출하고 응답 본문을 반환합니다."""

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
            f"{BASE_URL}/{endpoint}",
            params=params,
            timeout=20,
        )
        response.raise_for_status()

        # 인증 오류 등이 JSON이 아닌 XML 형태로 반환되는 경우입니다.
        if (
            "faultInfo" in response.text
            or "OpenAPI_ServiceResponse" in response.text
        ):
            raise ValueError(
                "공공데이터포털에서 오류 응답을 보냈습니다. "
                "인증키와 API 활용 신청 상태를 확인해 주세요."
            )

        data = response.json()
        api_response = data.get("response", {})
        header = api_response.get("header", {})

        if header.get("resultCode") != "0000":
            raise ValueError(
                header.get(
                    "resultMsg",
                    "API 요청이 정상적으로 처리되지 않았습니다.",
                )
            )

        return api_response.get("body", {})

    except requests.exceptions.Timeout:
        raise ValueError(
            "한국관광공사 서버의 응답이 늦어 요청 시간이 초과되었습니다."
        )

    except requests.exceptions.RequestException:
        raise ValueError(
            "한국관광공사 API에 연결하지 못했습니다. "
            "잠시 후 다시 시도해 주세요."
        )

    except ValueError:
        raise

    except Exception:
        raise ValueError(
            "API 응답을 읽는 과정에서 문제가 발생했습니다. "
            "인증키가 올바른지 확인해 주세요."
        )


def get_items(body):
    """API 결과가 한 개이거나 여러 개여도 항상 목록으로 변환합니다."""

    items = body.get("items") or {}
    rows = items.get("item", []) if isinstance(items, dict) else []

    if isinstance(rows, dict):
        return [rows]

    if isinstance(rows, list):
        return rows

    return []


# =========================================================
# 법정동 시도·시군구 코드 조회
# =========================================================
@st.cache_data(ttl=86400, show_spinner=False)
def load_regions(service_key, sido_code=""):
    """시도 또는 시군구 법정동 코드 목록을 불러옵니다."""

    params = {
        "lDongListYn": "N",
    }

    if sido_code:
        params["lDongRegnCd"] = sido_code

    body = call_api(
        "ldongCode2",
        service_key,
        **params,
    )

    result = []

    for row in get_items(body):
        name = str(row.get("name", "")).strip()
        code = str(row.get("code", "")).strip()

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
# 숙박시설 정보 조회
# =========================================================
@st.cache_data(ttl=1800, show_spinner=False)
def load_stays(service_key, sido_code, sigungu_code):
    """선택한 시군구의 숙박시설 정보를 불러옵니다."""

    body = call_api(
        "searchStay2",
        service_key,
        arrange="A",
        lDongRegnCd=sido_code,
        lDongSignguCd=sigungu_code,
    )

    result = []

    for number, row in enumerate(get_items(body)):
        title = str(row.get("title", "")).strip()

        if not title:
            continue

        try:
            longitude = float(row.get("mapx"))
            latitude = float(row.get("mapy"))

        except (TypeError, ValueError):
            longitude = None
            latitude = None

        # contentid를 지도와 오른쪽 정보 카드 연결용 ID로 사용합니다.
        stay_id = str(row.get("contentid", "")).strip()

        # contentid가 비어 있는 경우 중복 가능성이 낮은 임시 ID를 만듭니다.
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
                "숙박시설": title,
                "주소": str(row.get("addr1", "")).strip(),
                "상세주소": str(row.get("addr2", "")).strip(),
                "전화번호": str(row.get("tel", "")).strip(),
                "대표이미지": str(row.get("firstimage", "")).strip(),
                "경도": longitude,
                "위도": latitude,
            }
        )

    # 숙박시설 이름을 기준으로 가나다순 정렬합니다.
    return sorted(
        result,
        key=lambda item: item["숙박시설"],
    )


# =========================================================
# 시군구 GeoJSON 불러오기
# =========================================================
@st.cache_data(ttl=86400, show_spinner=False)
def load_geojson():
    """GitHub에서 전국 시군구 경계 GeoJSON을 내려받습니다."""

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

    except Exception:
        raise ValueError(
            "시군구 경계 데이터를 불러오지 못했습니다. "
            "잠시 후 다시 시도해 주세요."
        )


def find_boundary(geojson, region_code):
    """5자리 법정동 코드가 정확히 일치하는 경계만 반환합니다."""

    matched_features = []

    for feature in geojson.get("features", []):
        properties = feature.get("properties", {})

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
# 경계 좌표와 지도 중심 계산
# =========================================================
def collect_coordinates(value, result):
    """Polygon과 MultiPolygon에 포함된 좌표를 모읍니다."""

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
    """선택한 시군구 경계에 맞는 지도 중심과 확대 수준을 계산합니다."""

    coordinates = []

    for feature in boundary.get("features", []):
        geometry = feature.get("geometry", {})

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

    center_lon = (min_lon + max_lon) / 2
    center_lat = (min_lat + max_lat) / 2

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
# 지도 만들기
# =========================================================
def make_map(stay_df, boundary, selected_id=None):
    """
    시군구 경계와 숙박시설을 지도에 표시합니다.

    선택된 숙박시설은 노란색으로 크게 표시합니다.
    """

    layers = []

    # -----------------------------------------------------
    # 시군구 경계 표시
    # -----------------------------------------------------
    if boundary:
        center_lat, center_lon, zoom = get_boundary_view(
            boundary
        )

        boundary_layer = pdk.Layer(
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

        layers.append(boundary_layer)

    else:
        valid_stays = stay_df.dropna(
            subset=["경도", "위도"]
        )

        if valid_stays.empty:
            center_lat = 36.3
            center_lon = 127.8
            zoom = 6

        else:
            center_lat = valid_stays["위도"].mean()
            center_lon = valid_stays["경도"].mean()
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
    # 일반 숙박시설 점
    # -----------------------------------------------------
    if not normal_stays.empty:
        normal_layer = pdk.Layer(
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

        layers.append(normal_layer)

    # -----------------------------------------------------
    # 선택한 숙박시설 점
    # -----------------------------------------------------
    if not selected_stays.empty:
        selected_layer = pdk.Layer(
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

        layers.append(selected_layer)

        # 선택한 점이 지도 화면의 중심에 오도록 합니다.
        center_lat = float(
            selected_stays.iloc[0]["위도"]
        )

        center_lon = float(
            selected_stays.iloc[0]["경도"]
        )

        zoom = max(zoom, 11)

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
# 지도 점 선택 이벤트
# =========================================================
def on_map_select():
    """지도에서 숙박시설 점을 클릭했을 때 실행됩니다."""

    map_state = st.session_state.get(
        "stay_map",
        {},
    )

    selection = map_state.get(
        "selection",
        {},
    )

    selected_objects_by_layer = selection.get(
        "objects",
        {},
    )

    # 일반 점이나 이미 선택된 노란색 점을 클릭한 경우를 모두 처리합니다.
    selected_objects = (
        selected_objects_by_layer.get(
            "selected-stay",
            [],
        )
        or selected_objects_by_layer.get(
            "stay-points",
            [],
        )
    )

    if not selected_objects:
        return

    selected_object = selected_objects[0]

    selected_id = str(
        selected_object.get(
            "stay_id",
            "",
        )
    ).strip()

    if not selected_id:
        return

    st.session_state.selected_stay_id = selected_id

    # 검색 결과에 가려지지 않도록 지도 점을 클릭하면 검색어를 지웁니다.
    st.session_state.search_keyword = ""


# =========================================================
# 선택 숙박시설 강조 카드
# =========================================================
def show_selected_card(selected_row):
    """지도에서 선택한 숙박시설 정보를 강조해서 표시합니다."""

    title = html.escape(
        str(selected_row.get("숙박시설", "") or "")
    )
    address = html.escape(
        str(selected_row.get("주소", "") or "")
    )
    detail_address = html.escape(
        str(selected_row.get("상세주소", "") or "")
    )
    telephone = html.escape(
        str(selected_row.get("전화번호", "") or "")
    )

    full_address = " ".join(
        value
        for value in [address, detail_address]
        if value
    )

    if not full_address:
        full_address = "주소 정보 없음"

    if not telephone:
        telephone = "전화번호 정보 없음"

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
        f'{title}'
        '</div>'
        '<div style="'
        'font-size:0.92rem;'
        'line-height:1.5;'
        'margin-bottom:5px;'
        '">'
        f'📍 {full_address}'
        '</div>'
        '<div style="'
        'font-size:0.88rem;'
        'opacity:0.82;'
        '">'
        f'☎ {telephone}'
        '</div>'
        '</div>'
    )

    st.markdown(
        card_html,
        unsafe_allow_html=True,
    )


# =========================================================
# API 키 확인
# =========================================================
try:
    tour_key = st.secrets["TOUR_KEY"]

except KeyError:
    st.error(
        "비밀 금고에 `TOUR_KEY`가 없습니다. "
        "Streamlit Cloud의 Settings → Secrets에 "
        "한국관광공사 인증키를 등록해 주세요."
    )
    st.stop()


# =========================================================
# 앱 실행
# =========================================================
try:
    with st.spinner("지역 정보를 불러오는 중입니다..."):
        sido_list = load_regions(tour_key)
        geojson = load_geojson()

    if not sido_list:
        st.warning(
            "조회 가능한 시도 법정동 코드가 없습니다."
        )
        st.stop()

    # -----------------------------------------------------
    # 지역 선택
    # -----------------------------------------------------
    st.subheader("📍 지역 선택")
    st.caption(
        "시도와 시군구를 차례로 선택해 주세요."
    )

    col1, col2 = st.columns(2)

    with col1:
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
            "선택한 시도의 시군구 코드가 없습니다."
        )
        st.stop()

    with col2:
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

    # 관광공사 시도 코드 2자리와 시군구 코드 3자리를 결합합니다.
    region_code = (
        str(selected_sido["code"]).zfill(2)
        + str(selected_sigungu["code"]).zfill(3)
    )

    region_name = (
        f"{sido_name} {sigungu_name}"
    )

    # 지역이 변경되면 이전 숙박시설 선택 상태를 초기화합니다.
    if (
        st.session_state.get("current_region")
        != region_code
    ):
        st.session_state.current_region = region_code
        st.session_state.selected_stay_id = None
        st.session_state.search_keyword = ""

        # 이전 지역의 지도 선택 이벤트도 삭제합니다.
        st.session_state.pop(
            "stay_map",
            None,
        )

    # 코드가 정확히 일치하는 GeoJSON 경계를 찾습니다.
    boundary = find_boundary(
        geojson,
        region_code,
    )

    # -----------------------------------------------------
    # 숙박시설 조회
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

    # 선택된 ID가 현재 지역의 목록에 없으면 선택 상태를 해제합니다.
    if (
        selected_id
        and not stay_df.empty
        and str(selected_id)
        not in stay_df["stay_id"].astype(str).values
    ):
        st.session_state.selected_stay_id = None
        selected_id = None

    st.divider()

    # -----------------------------------------------------
    # 요약 정보
    # -----------------------------------------------------
    if stay_df.empty:
        map_count = 0
    else:
        map_count = stay_df.dropna(
            subset=["경도", "위도"]
        ).shape[0]

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
            f"`{region_name}`의 법정동 코드 "
            f"`{region_code}`와 일치하는 GeoJSON 경계를 "
            "찾지 못했습니다. 지역명으로 임의 연결하지 않고 "
            "숙박시설 위치만 표시합니다."
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
            "파란색 영역은 선택한 시군구 경계입니다. "
            "빨간색 점을 클릭하면 해당 숙박시설이 노란색으로 표시되고, "
            "오른쪽에 상세 정보가 강조됩니다."
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
            name_mask = filtered_df[
                "숙박시설"
            ].str.contains(
                keyword,
                case=False,
                na=False,
            )

            address_mask = filtered_df[
                "주소"
            ].str.contains(
                keyword,
                case=False,
                na=False,
            )

            filtered_df = filtered_df[
                name_mask | address_mask
            ].reset_index(drop=True)

        else:
            filtered_df = filtered_df.reset_index(
                drop=True
            )

        # 지도에서 선택한 숙박시설의 강조 카드를 표시합니다.
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

            # 표는 조회용으로만 표시합니다.
            # 행 선택 이벤트와 on_select 기능은 사용하지 않습니다.
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

except ValueError as error:
    st.error(
        f"⚠️ {error}"
    )

except Exception as error:
    st.error(
        "앱을 실행하는 중 예상하지 못한 문제가 발생했습니다."
    )

    # 실제 오류 내용은 Streamlit Cloud 로그에서도 확인할 수 있습니다.
    st.exception(error)
