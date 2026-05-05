"""
시장 세션(장전/NXT vs 정규장/KRX)에 따라 어떤 데이터(체결/호가)를 구독할지 결정하는 핵심 로직
"""

from __future__ import annotations
# 파이썬 타입 힌트를 문자열로 지연 평가 (순환참조 방지, Python 3.11 이전 호환)

from dataclasses import dataclass
# 데이터 구조를 간단하게 정의하기 위한 데코레이터

from datetime import datetime, time
# 현재 시간 판단 및 장 시작 시간 비교용

from zoneinfo import ZoneInfo
# 타임존 (여기서는 서울 시간 기준)

from .config import StockItem
# 종목 정보 (code, 이름, 옵션 등 포함)

from .portfolio import expand_premarket_items
# 장전(NXT)용 종목 확장 로직 (ex. 상위 거래량 종목 추가)

# 순환 참조 방지용 (SubscriptionSpec은 stream.py에 정의되어 있지만, 여기서 참조해야 함)
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .stream import SubscriptionSpec


# =========================
# 📌 타임존 설정
# =========================
SEOUL_TZ = ZoneInfo("Asia/Seoul")
# 한국 시장 기준으로 시간 판단을 하기 위해 서울 타임존 사용


# =========================
# 📌 시장 세션 정의 클래스
# =========================
@dataclass(frozen=True)
class MarketSession:
    """
    시장 세션 정보 정의

    name               : 시장 이름 (krx / nxt)
    trade_tr_id        : 체결 데이터 TR ID
    orderbook_tr_id    : 호가 데이터 TR ID
    storage_dir_name   : 데이터 저장 디렉토리 이름
    """
    name: str
    trade_tr_id: str
    orderbook_tr_id: str
    storage_dir_name: str


# =========================
# 📌 실제 세션 정의 (KRX / NXT)
# =========================
KRX_SESSION = MarketSession(
    name="krx",
    trade_tr_id="H0STCNT0",     # KRX 체결 데이터 TR
    orderbook_tr_id="H0STASP0", # KRX 호가 데이터 TR
    storage_dir_name="krx",
)

NXT_SESSION = MarketSession(
    name="nxt",
    trade_tr_id="H0NXCNT0",     # NXT 체결 데이터 TR
    orderbook_tr_id="H0NXASP0", # NXT 호가 데이터 TR
    storage_dir_name="nxt",
)


# =========================
# 📌 현재 시간 기준 세션 결정
# =========================
def resolve_session(now: datetime | None = None) -> MarketSession:
    """
    현재 시간을 기준으로 어떤 시장 세션을 사용할지 결정

    - 09:00 이전 → 장전 (NXT)
    - 09:00 이후 → 정규장 (KRX)
    """
    current = now or datetime.now(SEOUL_TZ)
    # now가 없으면 현재 서울 시간 사용

    current_time = current.time()

    # 장 시작 전 → NXT
    if current_time < time(9, 0):
        return NXT_SESSION

    # 장 시작 이후 → KRX
    return KRX_SESSION


# =========================
# 📌 이름으로 세션 조회
# =========================
def session_for_name(name: str) -> MarketSession:
    """
    문자열로 세션을 지정할 때 사용

    ex)
    "krx" → KRX_SESSION
    "nxt" → NXT_SESSION
    """
    normalized = str(name or "").strip().lower()

    if normalized == "nxt":
        return NXT_SESSION

    if normalized == "krx":
        return KRX_SESSION

    # 잘못된 값이면 에러 발생
    raise ValueError(f"Unknown market session: {name}")


# =========================
# 📌 구독 스펙 생성 (핵심 로직)
# =========================
def build_subscriptions(stock_items: list[StockItem], session: MarketSession) -> list["SubscriptionSpec"]:
    """
    종목 리스트 + 시장 세션을 받아서
    실제 WebSocket/스트림 구독 스펙을 생성

    반환값: SubscriptionSpec 리스트
    """

    from .stream import SubscriptionSpec
    # 순환 import 방지용 (함수 내부 import)

    specs: list[SubscriptionSpec] = []

    # =========================
    # 📌 장전(NXT) 로직
    # =========================
    if session.name == "nxt":
        """
        특징:
        - 체결 데이터 안씀
        - 호가(orderbook)만 구독
        - 종목도 expand 해서 상위 N개 추가
        """

        # 장전용 종목 확장 (예: 거래량 상위 10개 추가)
        expanded_items = expand_premarket_items(stock_items, top_n=10)

        for item in expanded_items:
            specs.append(
                SubscriptionSpec(
                    market=session.name,                  # "nxt"
                    kind="orderbook",                     # 호가 데이터
                    tr_id=session.orderbook_tr_id,        # H0NXASP0
                    tr_type="1",
                    tr_key=item.code,                     # 종목 코드
                    symbol=item.code,
                    columns=ORDERBOOK_COLUMNS[session.orderbook_tr_id],
                    # TR에 맞는 컬럼 구조
                )
            )

        return specs


    # =========================
    # 📌 정규장(KRX) 로직
    # =========================
    """
    특징:
    - 체결(trade) + 호가(orderbook) 둘 다 가능
    - 종목별로 옵션에 따라 호가 구독 여부 결정
    """

    for item in stock_items:
        # -------------------------
        # 📌 체결 데이터 구독 (기본)
        # -------------------------
        specs.append(
            SubscriptionSpec(
                market=session.name,               # "krx"
                kind="trade",                      # 체결 데이터
                tr_id=session.trade_tr_id,         # H0STCNT0
                tr_type="1",
                tr_key=item.code,
                symbol=item.code,
                columns=TRADE_COLUMNS[session.trade_tr_id],
            )
        )

        # -------------------------
        # 📌 호가 데이터 구독 (옵션)
        # -------------------------
        if item.is_orderbook_enabled:
            specs.append(
                SubscriptionSpec(
                    market=session.name,
                    kind="orderbook",
                    tr_id=session.orderbook_tr_id,   # H0STASP0
                    tr_type="1",
                    tr_key=item.code,
                    symbol=item.code,
                    columns=ORDERBOOK_COLUMNS[session.orderbook_tr_id],
                )
            )

    return specs


TRADE_COLUMNS = {
    "H0STCNT0": [
        "MKSC_SHRN_ISCD",
        "STCK_CNTG_HOUR",
        "STCK_PRPR",
        "PRDY_VRSS_SIGN",
        "PRDY_VRSS",
        "PRDY_CTRT",
        "WGHN_AVRG_STCK_PRC",
        "STCK_OPRC",
        "STCK_HGPR",
        "STCK_LWPR",
        "ASKP1",
        "BIDP1",
        "CNTG_VOL",
        "ACML_VOL",
        "ACML_TR_PBMN",
        "SELN_CNTG_CSNU",
        "SHNU_CNTG_CSNU",
        "NTBY_CNTG_CSNU",
        "CTTR",
        "SELN_CNTG_SMTN",
        "SHNU_CNTG_SMTN",
        "CNTG_CLS_CODE",
        "SHNU_RATE",
        "PRDY_VOL_VRSS_ACML_VOL_RATE",
        "OPRC_HOUR",
        "OPRC_VRSS_PRPR_SIGN",
        "OPRC_VRSS_PRPR",
        "HGPR_HOUR",
        "HGPR_VRSS_PRPR_SIGN",
        "HGPR_VRSS_PRPR",
        "LWPR_HOUR",
        "LWPR_VRSS_PRPR_SIGN",
        "LWPR_VRSS_PRPR",
        "BSOP_DATE",
        "NEW_MKOP_CLS_CODE",
        "TRHT_YN",
        "ASKP_RSQN1",
        "BIDP_RSQN1",
        "TOTAL_ASKP_RSQN",
        "TOTAL_BIDP_RSQN",
        "VOL_TNRT",
        "PRDY_SMNS_HOUR_ACML_VOL",
        "PRDY_SMNS_HOUR_ACML_VOL_RATE",
        "HOUR_CLS_CODE",
        "MRKT_TRTM_CLS_CODE",
        "VI_STND_PRC",
    ],
    "H0NXCNT0": [
        "MKSC_SHRN_ISCD",
        "STCK_CNTG_HOUR",
        "STCK_PRPR",
        "PRDY_VRSS_SIGN",
        "PRDY_VRSS",
        "PRDY_CTRT",
        "WGHN_AVRG_STCK_PRC",
        "STCK_OPRC",
        "STCK_HGPR",
        "STCK_LWPR",
        "ASKP1",
        "BIDP1",
        "CNTG_VOL",
        "ACML_VOL",
        "ACML_TR_PBMN",
        "SELN_CNTG_CSNU",
        "SHNU_CNTG_CSNU",
        "NTBY_CNTG_CSNU",
        "CTTR",
        "SELN_CNTG_SMTN",
        "SHNU_CNTG_SMTN",
        "CNTG_CLS_CODE",
        "SHNU_RATE",
        "PRDY_VOL_VRSS_ACML_VOL_RATE",
        "OPRC_HOUR",
        "OPRC_VRSS_PRPR_SIGN",
        "OPRC_VRSS_PRPR",
        "HGPR_HOUR",
        "HGPR_VRSS_PRPR_SIGN",
        "HGPR_VRSS_PRPR",
        "LWPR_HOUR",
        "LWPR_VRSS_PRPR_SIGN",
        "LWPR_VRSS_PRPR",
        "BSOP_DATE",
        "NEW_MKOP_CLS_CODE",
        "TRHT_YN",
        "ASKP_RSQN1",
        "BIDP_RSQN1",
        "TOTAL_ASKP_RSQN",
        "TOTAL_BIDP_RSQN",
        "VOL_TNRT",
        "PRDY_SMNS_HOUR_ACML_VOL",
        "PRDY_SMNS_HOUR_ACML_VOL_RATE",
        "HOUR_CLS_CODE",
        "MRKT_TRTM_CLS_CODE",
        "VI_STND_PRC",
    ],
}

ORDERBOOK_COLUMNS = {
    "H0STASP0": [
        "MKSC_SHRN_ISCD",
        "BSOP_HOUR",
        "HOUR_CLS_CODE",
        "ASKP1",
        "ASKP2",
        "ASKP3",
        "ASKP4",
        "ASKP5",
        "ASKP6",
        "ASKP7",
        "ASKP8",
        "ASKP9",
        "ASKP10",
        "BIDP1",
        "BIDP2",
        "BIDP3",
        "BIDP4",
        "BIDP5",
        "BIDP6",
        "BIDP7",
        "BIDP8",
        "BIDP9",
        "BIDP10",
        "ASKP_RSQN1",
        "ASKP_RSQN2",
        "ASKP_RSQN3",
        "ASKP_RSQN4",
        "ASKP_RSQN5",
        "ASKP_RSQN6",
        "ASKP_RSQN7",
        "ASKP_RSQN8",
        "ASKP_RSQN9",
        "ASKP_RSQN10",
        "BIDP_RSQN1",
        "BIDP_RSQN2",
        "BIDP_RSQN3",
        "BIDP_RSQN4",
        "BIDP_RSQN5",
        "BIDP_RSQN6",
        "BIDP_RSQN7",
        "BIDP_RSQN8",
        "BIDP_RSQN9",
        "BIDP_RSQN10",
        "TOTAL_ASKP_RSQN",
        "TOTAL_BIDP_RSQN",
        "OVTM_TOTAL_ASKP_RSQN",
        "OVTM_TOTAL_BIDP_RSQN",
        "ANTC_CNPR",
        "ANTC_CNQN",
        "ANTC_VOL",
        "ANTC_CNTG_VRSS",
        "ANTC_CNTG_VRSS_SIGN",
        "ANTC_CNTG_PRDY_CTRT",
        "ACML_VOL",
        "TOTAL_ASKP_RSQN_ICDC",
        "TOTAL_BIDP_RSQN_ICDC",
        "OVTM_TOTAL_ASKP_ICDC",
        "OVTM_TOTAL_BIDP_ICDC",
        "STCK_DEAL_CLS_CODE",
    ],
    "H0NXASP0": [
        "MKSC_SHRN_ISCD",
        "BSOP_HOUR",
        "HOUR_CLS_CODE",
        "ASKP1",
        "ASKP2",
        "ASKP3",
        "ASKP4",
        "ASKP5",
        "ASKP6",
        "ASKP7",
        "ASKP8",
        "ASKP9",
        "ASKP10",
        "BIDP1",
        "BIDP2",
        "BIDP3",
        "BIDP4",
        "BIDP5",
        "BIDP6",
        "BIDP7",
        "BIDP8",
        "BIDP9",
        "BIDP10",
        "ASKP_RSQN1",
        "ASKP_RSQN2",
        "ASKP_RSQN3",
        "ASKP_RSQN4",
        "ASKP_RSQN5",
        "ASKP_RSQN6",
        "ASKP_RSQN7",
        "ASKP_RSQN8",
        "ASKP_RSQN9",
        "ASKP_RSQN10",
        "BIDP_RSQN1",
        "BIDP_RSQN2",
        "BIDP_RSQN3",
        "BIDP_RSQN4",
        "BIDP_RSQN5",
        "BIDP_RSQN6",
        "BIDP_RSQN7",
        "BIDP_RSQN8",
        "BIDP_RSQN9",
        "BIDP_RSQN10",
        "TOTAL_ASKP_RSQN",
        "TOTAL_BIDP_RSQN",
        "OVTM_TOTAL_ASKP_RSQN",
        "OVTM_TOTAL_BIDP_RSQN",
        "ANTC_CNPR",
        "ANTC_CNQN",
        "ANTC_VOL",
        "ANTC_CNTG_VRSS",
        "ANTC_CNTG_VRSS_SIGN",
        "ANTC_CNTG_PRDY_CTRT",
        "ACML_VOL",
        "TOTAL_ASKP_RSQN_ICDC",
        "TOTAL_BIDP_RSQN_ICDC",
        "OVTM_TOTAL_ASKP_ICDC",
        "OVTM_TOTAL_BIDP_ICDC",
        "STCK_DEAL_CLS_CODE",
        "KMID_PRC",
        "KMID_TOTAL_RSQN",
        "KMID_CLS_CODE",
        "NMID_PRC",
        "NMID_TOTAL_RSQN",
        "NMID_CLS_CODE",
    ],
}
