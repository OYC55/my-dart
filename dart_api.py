"""DART(전자공시) OpenAPI 클라이언트 + 로컬 SQLite 캐시.

필요한 최소 API만 사용한다:
- corpCode.xml            : 전체 기업 고유번호 목록 (1회 다운로드, 캐시)
- company.json            : 기업개황 (업종코드 induty_code 확보용)
- fnlttMultiAcnt.json     : 다중회사 주요계정 (매출액/영업이익, 최대 100개사/호출)
- accnutAdtorNmNdAdtOpinion.json : 회계감사인의 명칭 및 감사의견
"""
from __future__ import annotations

import io
import sqlite3
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable
from xml.etree import ElementTree as ET

import requests

BASE_URL = "https://opendart.fss.or.kr/api"
DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(exist_ok=True)
DB_PATH = DATA_DIR / "cache.db"

REPORT_CODES = {
    "사업보고서 (연간)": "11011",
    "반기보고서": "11012",
    "1분기보고서": "11013",
    "3분기보고서": "11014",
}

REVENUE_NAMES = {"매출액", "수익(매출액)", "영업수익"}
OPERATING_PROFIT_NAMES = {"영업이익", "영업이익(손실)"}


class DartError(Exception):
    """DART API가 status != '000' 을 반환했을 때"""


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS corp (
            corp_code TEXT PRIMARY KEY,
            corp_name TEXT,
            stock_code TEXT,
            induty_code TEXT,
            induty_fetched INTEGER DEFAULT 0
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS financials (
            corp_code TEXT, bsns_year TEXT, reprt_code TEXT,
            revenue REAL, operating_profit REAL, fetched_at REAL,
            PRIMARY KEY (corp_code, bsns_year, reprt_code)
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS auditor (
            corp_code TEXT, bsns_year TEXT, reprt_code TEXT,
            adtor TEXT, adt_opinion TEXT, fetched_at REAL,
            PRIMARY KEY (corp_code, bsns_year, reprt_code)
        )"""
    )
    return conn


def _check_status(payload: dict) -> None:
    status = payload.get("status")
    if status not in ("000", None):
        raise DartError(f"[{status}] {payload.get('message', '알 수 없는 오류')}")


def _get_json(path: str, params: dict, api_key: str, timeout: int = 20) -> dict:
    r = requests.get(f"{BASE_URL}/{path}", params={**params, "crtfc_key": api_key}, timeout=timeout)
    r.raise_for_status()
    payload = r.json()
    _check_status(payload)
    return payload


def _parse_amount(raw: str | None) -> float | None:
    if raw is None:
        return None
    raw = raw.strip().replace(",", "")
    if raw in ("", "-"):
        return None
    try:
        return float(raw)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# 1. 전체 기업 고유번호 목록 (corpCode.xml)
# ---------------------------------------------------------------------------

def corp_count(only_listed: bool = True) -> int:
    conn = _connect()
    try:
        if only_listed:
            return conn.execute("SELECT COUNT(*) FROM corp WHERE stock_code != ''").fetchone()[0]
        return conn.execute("SELECT COUNT(*) FROM corp").fetchone()[0]
    finally:
        conn.close()


def download_corp_codes(api_key: str, force: bool = False) -> int:
    """상장기업 고유번호 목록을 내려받아 캐시한다. 반환값: 캐시된 상장기업 수."""
    if not force and corp_count() > 0:
        return corp_count()

    r = requests.get(f"{BASE_URL}/corpCode.xml", params={"crtfc_key": api_key}, timeout=60)
    r.raise_for_status()

    if not r.content.startswith(b"PK"):
        # zip이 아니면 에러 응답(xml)
        try:
            root = ET.fromstring(r.content)
            status = root.findtext("status")
            message = root.findtext("message")
            raise DartError(f"[{status}] {message}")
        except ET.ParseError:
            raise DartError("corpCode.xml 응답을 해석할 수 없습니다.")

    zf = zipfile.ZipFile(io.BytesIO(r.content))
    xml_bytes = zf.read(zf.namelist()[0])
    root = ET.fromstring(xml_bytes)

    rows = []
    for item in root.iter("list"):
        corp_code = (item.findtext("corp_code") or "").strip()
        corp_name = (item.findtext("corp_name") or "").strip()
        stock_code = (item.findtext("stock_code") or "").strip()
        if stock_code:  # 상장기업만 사용
            rows.append((corp_code, corp_name, stock_code))

    conn = _connect()
    try:
        conn.executemany(
            "INSERT OR IGNORE INTO corp (corp_code, corp_name, stock_code) VALUES (?, ?, ?)",
            rows,
        )
        conn.commit()
    finally:
        conn.close()
    return corp_count()


# ---------------------------------------------------------------------------
# 2. 업종코드 캐시 (company.json 의 induty_code)
# ---------------------------------------------------------------------------

def industry_cache_progress() -> tuple[int, int]:
    """(업종코드 확보된 기업 수, 전체 상장기업 수)"""
    conn = _connect()
    try:
        total = conn.execute("SELECT COUNT(*) FROM corp WHERE stock_code != ''").fetchone()[0]
        done = conn.execute(
            "SELECT COUNT(*) FROM corp WHERE stock_code != '' AND induty_fetched = 1"
        ).fetchone()[0]
        return done, total
    finally:
        conn.close()


def _fetch_induty(api_key: str, corp_code: str) -> tuple[str, str | None]:
    try:
        payload = _get_json("company.json", {"corp_code": corp_code}, api_key)
        return corp_code, (payload.get("induty_code") or "").strip() or None
    except Exception:
        return corp_code, None


def build_industry_cache(
    api_key: str,
    max_workers: int = 8,
    progress_cb: Callable[[int, int], None] | None = None,
) -> None:
    """아직 induty_code가 없는 상장기업들에 대해 company.json을 호출해 채운다."""
    conn = _connect()
    try:
        pending = [
            row[0]
            for row in conn.execute(
                "SELECT corp_code FROM corp WHERE stock_code != '' AND induty_fetched = 0"
            ).fetchall()
        ]
    finally:
        conn.close()

    if not pending:
        if progress_cb:
            done, total = industry_cache_progress()
            progress_cb(done, total)
        return

    done, total = industry_cache_progress()
    conn = _connect()
    try:
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = [pool.submit(_fetch_induty, api_key, c) for c in pending]
            for fut in as_completed(futures):
                corp_code, induty_code = fut.result()
                conn.execute(
                    "UPDATE corp SET induty_code = ?, induty_fetched = 1 WHERE corp_code = ?",
                    (induty_code, corp_code),
                )
                done += 1
                if done % 20 == 0:
                    conn.commit()
                if progress_cb:
                    progress_cb(done, total)
        conn.commit()
    finally:
        conn.close()


@dataclass
class IndustryGroup:
    induty_code: str
    count: int
    sample_names: list[str]


def list_industry_groups(min_count: int = 1) -> list[IndustryGroup]:
    conn = _connect()
    try:
        rows = conn.execute(
            """SELECT induty_code, corp_name FROM corp
               WHERE stock_code != '' AND induty_code IS NOT NULL
               ORDER BY induty_code"""
        ).fetchall()
    finally:
        conn.close()

    groups: dict[str, list[str]] = {}
    for induty_code, corp_name in rows:
        groups.setdefault(induty_code, []).append(corp_name)

    result = [
        IndustryGroup(code, len(names), names[:4])
        for code, names in groups.items()
        if len(names) >= min_count
    ]
    result.sort(key=lambda g: g.induty_code)
    return result


def companies_in_industry(induty_code: str) -> list[tuple[str, str, str]]:
    """(corp_code, corp_name, stock_code) 목록"""
    conn = _connect()
    try:
        return conn.execute(
            "SELECT corp_code, corp_name, stock_code FROM corp WHERE induty_code = ?",
            (induty_code,),
        ).fetchall()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 3. 매출액 / 영업이익 (다중회사 주요계정, 최대 100개사/호출)
# ---------------------------------------------------------------------------

def _chunked(seq: list, size: int) -> Iterable[list]:
    for i in range(0, len(seq), size):
        yield seq[i : i + size]


def _resolve_accounts(items: list[dict]) -> dict[str, dict[str, float | None]]:
    """fnlttMultiAcnt 응답 -> {corp_code: {"revenue":..., "operating_profit":...}}"""
    by_corp: dict[str, dict[str, dict[str, float | None]]] = {}
    for it in items:
        corp_code = it.get("corp_code") or it.get("stock_code") or ""
        fs_div = it.get("fs_div", "")
        account_nm = (it.get("account_nm") or "").strip()
        amount = _parse_amount(it.get("thstrm_amount"))
        by_corp.setdefault(corp_code, {}).setdefault(fs_div, {})[account_nm] = amount

    resolved: dict[str, dict[str, float | None]] = {}
    for corp_code, by_fs in by_corp.items():
        fs = by_fs.get("CFS") or by_fs.get("OFS") or {}
        revenue = next((fs[n] for n in REVENUE_NAMES if n in fs), None)
        op = next((fs[n] for n in OPERATING_PROFIT_NAMES if n in fs), None)
        resolved[corp_code] = {"revenue": revenue, "operating_profit": op}
    return resolved


def get_financials(
    api_key: str,
    corp_codes: list[str],
    bsns_year: str,
    reprt_code: str,
    use_cache: bool = True,
) -> dict[str, dict[str, float | None]]:
    """corp_code -> {"revenue":..., "operating_profit":...}. 캐시를 우선 사용, 없는 것만 API 호출."""
    conn = _connect()
    result: dict[str, dict[str, float | None]] = {}
    missing = []
    try:
        if use_cache:
            rows = conn.execute(
                f"SELECT corp_code, revenue, operating_profit FROM financials "
                f"WHERE bsns_year=? AND reprt_code=? AND corp_code IN "
                f"({','.join('?' * len(corp_codes))})",
                (bsns_year, reprt_code, *corp_codes),
            ).fetchall()
            cached = {r[0]: {"revenue": r[1], "operating_profit": r[2]} for r in rows}
            result.update(cached)
            missing = [c for c in corp_codes if c not in cached]
        else:
            missing = list(corp_codes)

        for chunk in _chunked(missing, 100):
            try:
                payload = _get_json(
                    "fnlttMultiAcnt.json",
                    {"corp_code": ",".join(chunk), "bsns_year": bsns_year, "reprt_code": reprt_code},
                    api_key,
                )
            except DartError:
                # 해당 청크에 데이터가 전혀 없는 경우 등 -> 빈 값으로 채움
                for c in chunk:
                    result[c] = {"revenue": None, "operating_profit": None}
                continue

            resolved = _resolve_accounts(payload.get("list", []))
            now = time.time()
            for c in chunk:
                vals = resolved.get(c, {"revenue": None, "operating_profit": None})
                result[c] = vals
                conn.execute(
                    "INSERT OR REPLACE INTO financials "
                    "(corp_code, bsns_year, reprt_code, revenue, operating_profit, fetched_at) "
                    "VALUES (?,?,?,?,?,?)",
                    (c, bsns_year, reprt_code, vals["revenue"], vals["operating_profit"], now),
                )
        conn.commit()
    finally:
        conn.close()
    return result


# ---------------------------------------------------------------------------
# 4. 회계감사인 / 감사의견
# ---------------------------------------------------------------------------

def get_auditor(
    api_key: str,
    corp_code: str,
    bsns_year: str,
    reprt_code: str,
    use_cache: bool = True,
) -> dict[str, str | None]:
    conn = _connect()
    try:
        if use_cache:
            row = conn.execute(
                "SELECT adtor, adt_opinion FROM auditor WHERE corp_code=? AND bsns_year=? AND reprt_code=?",
                (corp_code, bsns_year, reprt_code),
            ).fetchone()
            if row:
                return {"adtor": row[0], "adt_opinion": row[1]}

        try:
            payload = _get_json(
                "accnutAdtorNmNdAdtOpinion.json",
                {"corp_code": corp_code, "bsns_year": bsns_year, "reprt_code": reprt_code},
                api_key,
            )
            items = payload.get("list", [])
            adtor = items[0].get("adtor") if items else None
            adt_opinion = items[0].get("adt_opinion") if items else None
            if adtor:
                adtor = " ".join(adtor.split())
            if adt_opinion:
                adt_opinion = " ".join(adt_opinion.split())
        except DartError:
            adtor, adt_opinion = None, None

        conn.execute(
            "INSERT OR REPLACE INTO auditor (corp_code, bsns_year, reprt_code, adtor, adt_opinion, fetched_at) "
            "VALUES (?,?,?,?,?,?)",
            (corp_code, bsns_year, reprt_code, adtor, adt_opinion, time.time()),
        )
        conn.commit()
        return {"adtor": adtor, "adt_opinion": adt_opinion}
    finally:
        conn.close()


def get_auditors_bulk(
    api_key: str,
    corp_codes: list[str],
    bsns_year: str,
    reprt_code: str,
    max_workers: int = 6,
) -> dict[str, dict[str, str | None]]:
    result = {}
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futs = {
            pool.submit(get_auditor, api_key, c, bsns_year, reprt_code): c for c in corp_codes
        }
        for fut in as_completed(futs):
            c = futs[fut]
            result[c] = fut.result()
    return result
