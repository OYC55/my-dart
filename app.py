"""DART 산업별 매출액·영업이익·감사인 분석 웹앱 (Streamlit)."""
import io
import os
from datetime import date
from pathlib import Path

import pandas as pd
import streamlit as st
from dotenv import load_dotenv

import dart_api as api
from ksic import label_for

load_dotenv(Path(__file__).parent / ".env")

st.set_page_config(page_title="DART 산업별 감사보고서 분석", layout="wide")
st.title("📊 DART 산업별 매출액 · 영업이익 · 감사인 분석")
st.caption("DART Open API 기반 — 업종 내 매출액 상위 기업을 뽑아 매출액·영업이익·감사인(회계법인)을 연도별로 비교합니다.")

# ---------------------------------------------------------------------------
# 사이드바: API 키 + 기업/업종 캐시
# ---------------------------------------------------------------------------
def _default_api_key() -> str:
    key = os.environ.get("OPENDART_API_KEY", "")
    if key:
        return key
    try:  # Streamlit Community Cloud secrets
        return st.secrets.get("OPENDART_API_KEY", "")
    except Exception:
        return ""


with st.sidebar:
    st.header("1. API 인증키")
    default_key = _default_api_key()
    api_key = st.text_input(
        "DART Open API 인증키 (40자리)",
        value=default_key,
        type="password",
        help="https://opendart.fss.or.kr 에서 발급받은 인증키. "
        "환경변수 OPENDART_API_KEY로 설정해두면 자동으로 채워집니다.",
    )

    st.header("2. 기업 · 업종코드 캐시")
    done, total = api.industry_cache_progress()
    if total:
        st.caption(f"업종코드 확보: **{done:,} / {total:,}** 개 상장기업")
    else:
        st.caption("아직 상장기업 목록이 없습니다.")

    st.caption("최초 1회는 상장기업 전체의 업종코드를 조회하느라 수 분 걸릴 수 있습니다. 이후에는 캐시를 재사용합니다.")

    if st.button("기업 목록 · 업종코드 캐시 구축/갱신", disabled=not api_key, use_container_width=True):
        try:
            with st.spinner("상장기업 목록 다운로드 중..."):
                n = api.download_corp_codes(api_key)
            st.success(f"상장기업 {n:,}개 목록 확보")
        except api.DartError as e:
            st.error(f"기업 목록 다운로드 실패: {e}")
            st.stop()

        progress_bar = st.progress(0.0)
        status_text = st.empty()

        def _cb(done_: int, total_: int) -> None:
            if total_:
                progress_bar.progress(min(done_ / total_, 1.0))
                status_text.caption(f"업종코드 조회 중... {done_:,}/{total_:,}")

        try:
            api.build_industry_cache(api_key, progress_cb=_cb)
            st.success("업종코드 캐시 구축 완료")
        except Exception as e:
            st.error(f"업종코드 캐시 구축 중 오류: {e}")
        st.rerun()

# ---------------------------------------------------------------------------
# 조회 조건
# ---------------------------------------------------------------------------
st.header("조회 조건")

groups = api.list_industry_groups()
if not groups:
    st.info("먼저 왼쪽 사이드바에서 '기업 목록 · 업종코드 캐시'를 구축해주세요.")
    st.stop()

group_options: dict[str, str] = {}
for g in groups:
    sample = ", ".join(g.sample_names)
    label = f"{label_for(g.group_key)} — {g.count}개사 (예: {sample} 등)"
    group_options[label] = g.group_key

labels_list = list(group_options.keys())
keys_list = list(group_options.values())

name_query = st.text_input(
    "회사명으로 업종 빠르게 찾기 (선택)",
    placeholder="예: 크래프톤, 삼성전자, 카카오게임즈",
    help="업종 코드 자체는 회사마다 표기 자릿수가 달라(예: 게임업종이 '582'/'5821'/'58211' 등으로 혼재) "
    "아는 회사명을 검색해 어느 그룹에 속하는지 먼저 확인하는 게 정확합니다.",
)
default_index = 0
if name_query:
    matches = api.find_company_group(name_query)
    if matches:
        found_keys = sorted({gk for _, gk in matches}, key=lambda k: keys_list.index(k) if k in keys_list else 999)
        preview = ", ".join(f"{nm}→{label_for(gk)}" for nm, gk in matches[:5])
        st.caption(f"검색 결과: {preview}")
        if found_keys and found_keys[0] in keys_list:
            default_index = keys_list.index(found_keys[0])
    else:
        st.caption("일치하는 회사가 없습니다. 업종코드 캐시가 아직 없거나 비상장사일 수 있습니다.")

col1, col2 = st.columns([3, 1])
with col1:
    selected_label = st.selectbox("① 업종 선택", labels_list, index=default_index)
    selected_induty = group_options[selected_label]
with col2:
    top_n = st.number_input("② 상위 몇 개 회사?", min_value=1, max_value=100, value=10, step=1)

col3, col4, col5 = st.columns(3)
with col3:
    report_label = st.selectbox("보고서 종류", list(api.REPORT_CODES.keys()))
    reprt_code = api.REPORT_CODES[report_label]
with col4:
    base_year = st.number_input(
        "기준 연도 (매출액 순위 기준)",
        min_value=2015,
        max_value=date.today().year,
        value=date.today().year - 1,
        step=1,
    )
with col5:
    n_years = st.number_input("④ 과거 몇 개년 조회?", min_value=1, max_value=10, value=3, step=1)

years = [str(int(base_year) - i) for i in range(int(n_years))]
st.caption(f"조회 연도: {', '.join(years)}")

run = st.button("🔍 분석 실행", type="primary", disabled=not api_key)

# ---------------------------------------------------------------------------
# 분석 실행
# ---------------------------------------------------------------------------
if run:
    companies = api.companies_in_industry(selected_induty)
    if not companies:
        st.warning("선택한 업종에 회사가 없습니다.")
        st.stop()

    corp_codes = [c[0] for c in companies]
    name_map = {c[0]: c[1] for c in companies}
    stock_map = {c[0]: c[2] for c in companies}

    with st.spinner(f"{base_year}년 매출액 기준 업종 내 {len(corp_codes)}개사 순위 계산 중... (배치 조회)"):
        try:
            base_fin = api.get_financials(api_key, corp_codes, str(base_year), reprt_code)
        except api.DartError as e:
            st.error(str(e))
            st.stop()

    ranked_all = sorted(
        corp_codes,
        key=lambda c: (base_fin.get(c, {}).get("revenue") is None, -(base_fin.get(c, {}).get("revenue") or 0)),
    )
    ranked = [c for c in ranked_all if base_fin.get(c, {}).get("revenue") is not None][: int(top_n)]

    if not ranked:
        st.warning(f"{base_year}년 {report_label} 매출액 데이터가 있는 회사가 없습니다. 연도나 보고서 종류를 바꿔보세요.")
        st.stop()

    rank_map = {c: i for i, c in enumerate(ranked, start=1)}

    rows = []
    progress = st.progress(0.0)
    for i, yr in enumerate(years):
        with st.spinner(f"{yr}년 매출액/영업이익/감사인 조회 중..."):
            try:
                fin = api.get_financials(api_key, ranked, yr, reprt_code)
                auditors = api.get_auditors_bulk(api_key, ranked, yr, reprt_code)
            except api.DartError as e:
                st.error(f"{yr}년 데이터 조회 실패: {e}")
                st.stop()
        for c in ranked:
            f = fin.get(c, {})
            a = auditors.get(c, {})
            revenue = f.get("revenue")
            op = f.get("operating_profit")
            rows.append(
                {
                    "순위": rank_map[c],
                    "연도": yr,
                    "회사명": name_map[c],
                    "종목코드": stock_map[c],
                    "매출액(억원)": round(revenue / 1e8, 1) if revenue is not None else None,
                    "영업이익(억원)": round(op / 1e8, 1) if op is not None else None,
                    "감사인": a.get("adtor"),
                    "감사의견": a.get("adt_opinion"),
                }
            )
        progress.progress((i + 1) / len(years))

    st.session_state["result_df"] = pd.DataFrame(rows)
    st.session_state["result_meta"] = {
        "industry": selected_label,
        "base_year": str(base_year),
        "years": years,
        "report_label": report_label,
    }

# ---------------------------------------------------------------------------
# 결과 표시
# ---------------------------------------------------------------------------
if "result_df" in st.session_state:
    df: pd.DataFrame = st.session_state["result_df"]
    meta = st.session_state["result_meta"]

    st.header("결과")
    st.caption(f"{meta['industry']} · 기준연도 {meta['base_year']} 매출액 순위 · {meta['report_label']}")

    base_df = df[df["연도"] == meta["base_year"]].sort_values("순위")

    tab1, tab2 = st.tabs(["📋 전체 결과", "📈 기준연도 매출액 순위"])

    with tab1:
        view = df.sort_values(["순위", "연도"], ascending=[True, False])
        st.dataframe(view, use_container_width=True, hide_index=True)

        buf = io.BytesIO()
        with pd.ExcelWriter(buf, engine="openpyxl") as writer:
            df.sort_values(["순위", "연도"], ascending=[True, False]).to_excel(
                writer, index=False, sheet_name="분석결과"
            )
        st.download_button(
            "📥 엑셀로 다운로드",
            data=buf.getvalue(),
            file_name=f"dart_industry_analysis_{meta['base_year']}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

    with tab2:
        st.bar_chart(base_df.set_index("회사명")["매출액(억원)"])
        st.dataframe(
            base_df[["순위", "회사명", "매출액(억원)", "영업이익(억원)", "감사인", "감사의견"]],
            use_container_width=True,
            hide_index=True,
        )
