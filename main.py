import requests
import pandas as pd
import pydeck as pdk
import streamlit as st

# ---------------------------------------------------------
# 기본 설정
# ---------------------------------------------------------
st.set_page_config(
    page_title="대한민국 숙박여행 지도",
    page_icon="🏨",
    layout="wide",
)

st.title("🏨 대한민국 숙박여행 지도")
st.caption("한국관광공사 국문 관광정보 서비스 · 법정동 코드 기준")

BASE_URL = "https://apis.data.go.kr/B551011/KorService2"

# 전국 시군구 경계 GeoJSON
GEOJSON_URL = (
    "https://raw.githubusercontent.com/greatsong/modudata/"
    "main/data/boundaries/sigungu_kr.geojson"
)


# ---------------------------------------------------------
# 한국관광공사 API 호출
# ---------------------------------------------------------
@st.cache_data(ttl=3600, show_spinner=False)
def call_api(endpoint, service_key, **extra_params):
    """한국관광공사 API를 호출합니다."""

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

        # 인증 오류 등은 JSON 대신 XML로 올 수 있습니다.
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
    """API 결과가 1개이거나 여러 개여도 목록으로 변환합니다."""

    items = body.get("items") or {}
    rows = items.get("item", []) if isinstance(items, dict) else []

    if isinstance(rows, dict):
        return [rows]

    return rows if isinstance(rows, list) else []


# ---------------------------------------------------------
# 시도·시군구 법정동 코드 조회
# ---------------------------------------------------------
@st.cache_data(ttl=86400, show_spinner=False)
def load_regions(service_key, sido_code=""):
    """시도 또는 시군구 법정동 코드 목록을 가져옵니다."""

    params = {"lDongListYn": "N"}

    if sido_code:
        params["lDongRegnCd"] = sido_code

    body = call_api("ldongCode2", service_key, **params)
    result = []

    for row in get_items(body):
        name = str(row.get("name", "")).strip()
        code = str(row.get("code", "")).strip()

        if name and code:
            result.append({"name": name, "code": code})

    return sorted(result, key=lambda item: item["name"])


# ---------------------------------------------------------
# 숙박정보 조회
# ---------------------------------------------------------
@st.cache_data(ttl=1800, show_spinner=False)
def load_stays(service_key, sido_code, sigungu_code):
    """선택한 시군구의 숙박시설을 가져옵니다."""

    body = call_api(
        "searchStay2",
        service_key,
        arrange="A",
        lDongRegnCd=sido_code,
        lDongSignguCd=sigungu_code,
    )

    result = []

    for row in get_items(body):
        title = str(row.get("title", "")).strip()

        if not title:
            continue

        try:
            longitude = float(row.get("mapx"))
            latitude = float(row.get("mapy"))
        except (TypeError, ValueError):
            longitude = None
            latitude = None

        result.append(
            {
                "숙박시설": title,
                "주소": str(row.get("addr1", "")).strip(),
                "상세주소": str(row.get("addr2", "")).strip(),
                "전화번호": str(row.get("tel", "")).strip(),
                "경도": longitude,
                "위도": latitude,
            }
        )

    # 한글 숙박시설 이름 기준 오름차순
    return sorted(result, key=lambda item: item["숙박시설"])


# ---------------------------------------------------------
# 시군구 GeoJSON 불러오기
# ---------------------------------------------------------
@st.cache_data(ttl=86400, show_spinner=False)
def load_geojson():
    """GitHub에서 전국 시군구 경계 GeoJSON을 내려받습니다."""

    try:
        response = requests.get(GEOJSON_URL, timeout=30)
        response.raise_for_status()

        data = response.json()

        if not isinstance(data, dict) or "features" not in data:
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
    """5자리 법정동 코드가 정확히 일치하는 경계를 찾습니다."""

    matched = []

    for feature in geojson.get("features", []):
        properties = feature.get("properties", {})
        geo_code = str(properties.get("코드", "")).strip().zfill(5)

        if geo_code == region_code:
            matched.append(feature)

    if not matched:
        return None

    return {
        "type": "FeatureCollection",
        "features": matched,
    }


# ---------------------------------------------------------
# GeoJSON 좌표 범위 계산
# ---------------------------------------------------------
def collect_coordinates(value, coordinates):
    """Polygon과 MultiPolygon의 모든 좌표를 한 목록으로 모읍니다."""

    if (
        isinstance(value, list)
        and len(value) >= 2
        and isinstance(value[0], (int, float))
        and isinstance(value[1], (int, float))
    ):
        coordinates.append((value[0], value[1]))
        return

    if isinstance(value, list):
        for item in value:
            collect_coordinates(item, coordinates)


def get_boundary_view(boundary):
    """선택한 경계가 화면에 들어오도록 지도 중심과 확대값을 계산합니다."""

    coordinates = []

    for feature in boundary.get("features", []):
        geometry = feature.get("geometry", {})
        collect_coordinates(
            geometry.get("coordinates", []),
            coordinates,
        )

    if not coordinates:
        return 36.3, 127.8, 6

    longitudes = [point[0] for point in coordinates]
    latitudes = [point[1] for point in coordinates]

    min_lon, max_lon = min(longitudes), max(longitudes)
    min_lat, max_lat = min(latitudes), max(latitudes)

    center_lon = (min_lon + max_lon) / 2
    center_lat = (min_lat + max_lat) / 2
    spread = max(max_lon - min_lon, max_lat - min_lat)

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


# ---------------------------------------------------------
# 지도 생성
# ---------------------------------------------------------
def make_map(stay_df, boundary):
    """시군구 실제 경계와 숙박시설 위치를 함께 표시합니다."""

    layers = []

    # 경계 데이터가 있으면 실제 행정구역을 반투명하게 표시합니다.
    if boundary:
        center_lat, center_lon, zoom = get_boundary_view(boundary)

        boundary_layer = pdk.Layer(
            "GeoJsonLayer",
            boundary,
            filled=True,
            stroked=True,
            get_fill_color=[52, 120, 246, 55],
            get_line_color=[34, 88, 190, 230],
            line_width_min_pixels=2,
            pickable=False,
        )

        layers.append(boundary_layer)

    else:
        # 경계를 찾지 못하면 숙박시설 좌표를 기준으로 지도를 맞춥니다.
        valid = stay_df.dropna(subset=["경도", "위도"])

        if valid.empty:
            center_lat, center_lon, zoom = 36.3, 127.8, 6
        else:
            center_lat = valid["위도"].mean()
            center_lon = valid["경도"].mean()
            zoom = 10

    # 좌표가 있는 숙박시설만 점으로 표시합니다.
    valid_stays = stay_df.dropna(subset=["경도", "위도"]).copy()

    if not valid_stays.empty:
        stay_layer = pdk.Layer(
            "ScatterplotLayer",
            valid_stays,
            get_position="[경도, 위도]",
            get_radius=350,
            radius_min_pixels=5,
            radius_max_pixels=14,
            get_fill_color=[238, 82, 83, 220],
            get_line_color=[255, 255, 255, 240],
            line_width_min_pixels=1,
            pickable=True,
        )

        layers.append(stay_layer)

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


# ---------------------------------------------------------
# 앱 실행
# ---------------------------------------------------------
try:
    # Streamlit Cloud의 비밀 금고에서 인증키를 불러옵니다.
    tour_key = st.secrets["TOUR_KEY"]

except KeyError:
    st.error(
        "비밀 금고에 `TOUR_KEY`가 없습니다. "
        "Streamlit Cloud의 Settings → Secrets에 인증키를 등록해 주세요."
    )
    st.stop()


try:
    with st.spinner("지역 정보를 불러오는 중입니다..."):
        sido_list = load_regions(tour_key)
        geojson = load_geojson()

    if not sido_list:
        st.warning("조회 가능한 시도 법정동 코드가 없습니다.")
        st.stop()

    # -----------------------------------------------------
    # 시군구 선택
    # -----------------------------------------------------
    st.subheader("📍 지역 선택")
    st.caption("시도와 시군구를 차례로 선택해 주세요.")

    col1, col2 = st.columns(2)

    with col1:
        sido_name = st.selectbox(
            "시도",
            [item["name"] for item in sido_list],
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
        st.warning("선택한 시도의 시군구 코드가 없습니다.")
        st.stop()

    with col2:
        sigungu_name = st.selectbox(
            "시군구",
            [item["name"] for item in sigungu_list],
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

    region_name = f"{sido_name} {sigungu_name}"

    # 코드가 정확히 일치하는 GeoJSON 경계만 사용합니다.
    boundary = find_boundary(geojson, region_code)

    with st.spinner(f"{region_name} 숙박정보를 불러오는 중입니다..."):
        stays = load_stays(
            tour_key,
            selected_sido["code"],
            selected_sigungu["code"],
        )

    stay_df = pd.DataFrame(
        stays,
        columns=[
            "숙박시설",
            "주소",
            "상세주소",
            "전화번호",
            "경도",
            "위도",
        ],
    )

    st.divider()

    # -----------------------------------------------------
    # 요약 정보
    # -----------------------------------------------------
    map_count = (
        stay_df.dropna(subset=["경도", "위도"]).shape[0]
        if not stay_df.empty
        else 0
    )

    m1, m2, m3 = st.columns(3)
    m1.metric("선택 지역", region_name)
    m2.metric("숙박시설 수", f"{len(stay_df):,}곳")
    m3.metric("지도 표시 가능", f"{map_count:,}곳")

    if boundary is None:
        st.warning(
            f"`{region_name}`의 법정동 코드 `{region_code}`와 일치하는 "
            "GeoJSON 경계를 찾지 못했습니다. "
            "지역명을 이용해 임의로 연결하지 않고 숙박시설 위치만 표시합니다."
        )

    # -----------------------------------------------------
    # 지도와 목록
    # -----------------------------------------------------
    left, right = st.columns([1.35, 1])

    with left:
        st.subheader("🗺️ 시군구 경계와 숙박시설")

        st.pydeck_chart(
            make_map(stay_df, boundary),
            use_container_width=True,
        )

        st.caption(
            "파란색 영역은 선택한 시군구의 실제 경계이며, "
            "빨간색 점은 숙박시설 위치입니다."
        )

    with right:
        st.subheader("🔎 숙박시설 검색")

        keyword = st.text_input(
            "숙박시설 이름 또는 주소",
            placeholder="예: 호텔, 한옥, 리조트",
        ).strip()

        filtered_df = stay_df.copy()

        if keyword and not filtered_df.empty:
            mask = (
                filtered_df["숙박시설"].str.contains(
                    keyword,
                    case=False,
                    na=False,
                )
                | filtered_df["주소"].str.contains(
                    keyword,
                    case=False,
                    na=False,
                )
            )

            filtered_df = filtered_df[mask]

        st.caption(f"가나다순 · {len(filtered_df):,}개")

        if filtered_df.empty:
            st.info(
                f"현재 `{region_name}`에서 조회되는 숙박정보가 없습니다."
            )

        else:
            st.dataframe(
                filtered_df[
                    ["숙박시설", "주소", "상세주소", "전화번호"]
                ],
                use_container_width=True,
                hide_index=True,
                height=500,
                column_config={
                    "숙박시설": st.column_config.TextColumn(
                        "숙박시설",
                        width="medium",
                    ),
                    "주소": st.column_config.TextColumn(
                        "주소",
                        width="large",
                    ),
                },
            )

except ValueError as error:
    st.error(f"⚠️ {error}")

except Exception as error:
    st.error(
        "앱을 실행하는 중 예상하지 못한 문제가 발생했습니다."
    )

    # Streamlit Cloud 로그에서 실제 오류를 확인할 수 있게 남깁니다.
    st.exception(error)
