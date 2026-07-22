import math
import requests
import pandas as pd
import pydeck as pdk
import streamlit as st

# ---------------------------------------------------------
# 기본 화면 설정
# ---------------------------------------------------------
st.set_page_config(
    page_title="대한민국 숙박여행 지도",
    page_icon="🏨",
    layout="wide",
)

st.title("🏨 대한민국 숙박여행 지도")
st.caption("한국관광공사 국문 관광정보 서비스 · 법정동 코드 기준")

BASE_URL = "https://apis.data.go.kr/B551011/KorService2"


# ---------------------------------------------------------
# API 공통 호출 함수
# ---------------------------------------------------------
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

        # JSON 요청이어도 인증 오류 등이 XML로 반환될 수 있습니다.
        text = response.text

        if "faultInfo" in text or "OpenAPI_ServiceResponse" in text:
            raise ValueError(
                "공공데이터포털에서 오류 응답을 보냈습니다. "
                "인증키와 API 활용 신청 상태를 확인해 주세요."
            )

        data = response.json()

        # 일부 오류는 JSON 최상위에 resultCode로 반환됩니다.
        if data.get("resultCode") and data.get("resultCode") != "0000":
            raise ValueError(
                data.get("resultMsg", "API 요청 처리 중 오류가 발생했습니다.")
            )

        header = data.get("response", {}).get("header", {})

        if header.get("resultCode") != "0000":
            raise ValueError(
                header.get("resultMsg", "API 요청이 정상 처리되지 않았습니다.")
            )

        return data.get("response", {}).get("body", {})

    except requests.exceptions.Timeout:
        raise ValueError(
            "한국관광공사 서버의 응답이 늦어 요청 시간이 초과되었습니다. "
            "잠시 후 다시 시도해 주세요."
        )
    except requests.exceptions.RequestException:
        raise ValueError(
            "한국관광공사 API에 연결하지 못했습니다. "
            "인터넷 연결이나 API 서비스 상태를 확인해 주세요."
        )
    except ValueError:
        raise
    except Exception:
        raise ValueError(
            "응답을 읽는 과정에서 문제가 발생했습니다. "
            "인증키가 올바른지 확인해 주세요."
        )


def get_items(body):
    """API의 item이 목록 또는 한 개의 객체일 때 모두 처리합니다."""

    items = body.get("items") or {}
    item = items.get("item", []) if isinstance(items, dict) else []

    if isinstance(item, dict):
        return [item]

    return item if isinstance(item, list) else []


# ---------------------------------------------------------
# 법정동 코드 불러오기
# ---------------------------------------------------------
@st.cache_data(ttl=86400, show_spinner=False)
def load_regions(service_key, sido_code=""):
    """시도 또는 시군구 법정동 코드 목록을 불러옵니다."""

    params = {"lDongListYn": "N"}

    if sido_code:
        params["lDongRegnCd"] = sido_code

    body = call_api("ldongCode2", service_key, **params)
    rows = get_items(body)

    result = []

    for row in rows:
        name = str(row.get("name", "")).strip()
        code = str(row.get("code", "")).strip()

        if name and code:
            result.append({"name": name, "code": code})

    return sorted(result, key=lambda x: x["name"])


# ---------------------------------------------------------
# 숙박정보 불러오기
# ---------------------------------------------------------
@st.cache_data(ttl=1800, show_spinner=False)
def load_stays(service_key, sido_code, sigungu_code=""):
    """선택한 법정동 코드에 해당하는 숙박시설을 불러옵니다."""

    params = {
        "arrange": "A",  # 제목순
        "lDongRegnCd": sido_code,
    }

    if sigungu_code:
        params["lDongSignguCd"] = sigungu_code

    body = call_api("searchStay2", service_key, **params)
    rows = get_items(body)

    stays = []

    for row in rows:
        title = str(row.get("title", "")).strip()

        # 지도에 사용할 좌표를 안전하게 숫자로 변환합니다.
        try:
            longitude = float(row.get("mapx"))
            latitude = float(row.get("mapy"))
        except (TypeError, ValueError):
            longitude = None
            latitude = None

        if title:
            stays.append(
                {
                    "숙박시설": title,
                    "주소": str(row.get("addr1", "")).strip(),
                    "상세주소": str(row.get("addr2", "")).strip(),
                    "전화번호": str(row.get("tel", "")).strip(),
                    "대표 이미지": str(row.get("firstimage", "")).strip(),
                    "경도": longitude,
                    "위도": latitude,
                }
            )

    # 한글 숙박시설 이름 기준 오름차순
    return sorted(stays, key=lambda x: x["숙박시설"])


# ---------------------------------------------------------
# 지도 생성
# ---------------------------------------------------------
def make_map(map_df):
    """숙박시설 위치와 선택 지역의 중심을 강조한 지도를 만듭니다."""

    valid = map_df.dropna(subset=["경도", "위도"]).copy()

    if valid.empty:
        return pdk.Deck(
            map_style="https://basemaps.cartocdn.com/gl/positron-gl-style/style.json",
            initial_view_state=pdk.ViewState(
                latitude=36.3,
                longitude=127.8,
                zoom=6,
            ),
        )

    center_lat = valid["위도"].mean()
    center_lon = valid["경도"].mean()

    # 시설이 넓게 퍼져 있을수록 지도를 더 축소합니다.
    spread = max(
        valid["위도"].max() - valid["위도"].min(),
        valid["경도"].max() - valid["경도"].min(),
    )

    if spread > 3:
        zoom = 6
    elif spread > 1:
        zoom = 7
    elif spread > 0.3:
        zoom = 9
    else:
        zoom = 11

    # 선택 지역을 나타내는 반투명 중심 원
    center_df = pd.DataFrame(
        [{"경도": center_lon, "위도": center_lat}]
    )

    highlight_layer = pdk.Layer(
        "ScatterplotLayer",
        center_df,
        get_position="[경도, 위도]",
        get_radius=max(10000, spread * 45000),
        get_fill_color=[48, 126, 255, 35],
        get_line_color=[48, 126, 255, 150],
        stroked=True,
        line_width_min_pixels=2,
    )

    stay_layer = pdk.Layer(
        "ScatterplotLayer",
        valid,
        get_position="[경도, 위도]",
        get_radius=450,
        get_fill_color=[255, 92, 92, 210],
        get_line_color=[255, 255, 255, 230],
        line_width_min_pixels=1,
        pickable=True,
    )

    return pdk.Deck(
        map_style="https://basemaps.cartocdn.com/gl/positron-gl-style/style.json",
        initial_view_state=pdk.ViewState(
            latitude=center_lat,
            longitude=center_lon,
            zoom=zoom,
            pitch=20,
        ),
        layers=[highlight_layer, stay_layer],
        tooltip={
            "html": "<b>{숙박시설}</b><br>{주소}",
            "style": {"backgroundColor": "#263238", "color": "white"},
        },
    )


# ---------------------------------------------------------
# 앱 실행
# ---------------------------------------------------------
try:
    # Streamlit Cloud의 Secrets에서 인증키를 가져옵니다.
    tour_key = st.secrets["TOUR_KEY"]

except KeyError:
    st.error(
        "비밀 금고에 `TOUR_KEY`가 없습니다. "
        "Streamlit Cloud의 Settings → Secrets에 인증키를 등록해 주세요."
    )
    st.stop()


try:
    with st.spinner("법정동 코드 목록을 불러오는 중입니다..."):
        sido_list = load_regions(tour_key)

    if not sido_list:
        st.warning("조회 가능한 시도 법정동 코드가 없습니다.")
        st.stop()

    st.subheader("📍 지역 선택")
    st.caption("시도와 시군구를 차례로 선택해 주세요.")

    col1, col2 = st.columns(2)

    # 시도 선택
    with col1:
        sido_name = st.selectbox(
            "시도",
            [item["name"] for item in sido_list],
        )

    selected_sido = next(
        item for item in sido_list
        if item["name"] == sido_name
    )

    # 선택한 시도의 시군구 목록 불러오기
    sigungu_list = load_regions(
        tour_key,
        selected_sido["code"],
    )

    if not sigungu_list:
        st.warning("선택한 시도의 시군구 코드가 없습니다.")
        st.stop()

    # 시군구 선택
    with col2:
        sigungu_name = st.selectbox(
            "시군구",
            [item["name"] for item in sigungu_list],
        )

    selected_sigungu = next(
        item for item in sigungu_list
        if item["name"] == sigungu_name
    )

    sigungu_code = selected_sigungu["code"]
    region_name = f"{sido_name} {sigungu_name}"

    with st.spinner(f"{region_name} 숙박정보를 불러오는 중입니다..."):
        stays = load_stays(
            tour_key,
            selected_sido["code"],
            sigungu_code,
        )

    st.divider()

    if not stays:
        st.info(f"현재 `{region_name}`에서 조회되는 숙박정보가 없습니다.")
        st.pydeck_chart(make_map(pd.DataFrame()), use_container_width=True)
        st.stop()

    stay_df = pd.DataFrame(stays)

    # 간단한 요약 지표
    m1, m2, m3 = st.columns(3)
    m1.metric("선택 지역", region_name)
    m2.metric("숙박시설 수", f"{len(stay_df):,}곳")
    m3.metric(
        "지도 표시 가능",
        f"{stay_df[['경도', '위도']].dropna().shape[0]:,}곳",
    )

    left, right = st.columns([1.35, 1])

    with left:
        st.subheader("🗺️ 숙박시설 지도")
        st.pydeck_chart(
            make_map(stay_df),
            use_container_width=True,
            height=620,
        )

    with right:
        st.subheader("🔎 숙박시설 검색")

        keyword = st.text_input(
            "숙박시설 이름 또는 주소",
            placeholder="예: 호텔, 해운대, 한옥",
        ).strip()

        filtered_df = stay_df.copy()

        if keyword:
            mask = (
                filtered_df["숙박시설"].str.contains(
                    keyword, case=False, na=False
                )
                | filtered_df["주소"].str.contains(
                    keyword, case=False, na=False
                )
            )
            filtered_df = filtered_df[mask]

        st.caption(f"가나다순 · {len(filtered_df):,}개")

        st.dataframe(
            filtered_df[
                ["숙박시설", "주소", "상세주소", "전화번호"]
            ],
            use_container_width=True,
            hide_index=True,
            height=555,
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

except Exception:
    st.error(
        "앱을 실행하는 중 예상하지 못한 문제가 발생했습니다. "
        "잠시 후 다시 시도해 주세요."
    )
