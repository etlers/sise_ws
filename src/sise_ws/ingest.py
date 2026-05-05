from __future__ import annotations

"""
[개요]

이 모듈은 전체 실시간 시세 수집 프로세스의 "실행 진입점" 역할을 합니다.

주요 역할:
1. 설정(config) 및 승인키 로드
2. 세션(KOSPI, NXT 등) 결정
3. 종목 리스트 로드
4. 웹소켓 클라이언트 실행
5. CSV 저장 결과 리포트 생성

즉, "한 번 실행(run)" 시 전체 데이터 수집 파이프라인을 orchestrating(조율)하는 핵심 로직입니다.
"""

import asyncio
import logging

# 승인키 관련 로직 (발급/갱신)
from .approval import load_or_refresh_approval_key

# 설정 및 데이터 로드 관련
from .config import CONFIG_DIR, DATA_DIR, load_app_config, load_approval_key, load_stock_list

# 세션 및 구독 구성 관련
from .market import build_subscriptions, resolve_session, session_for_name

# CSV 저장소
from .storage import CsvStore

# 웹소켓 클라이언트
from .stream import KISWebSocketClient


logger = logging.getLogger(__name__)


def bootstrap() -> dict[str, int]:
    """
    [초기 상태 점검 함수]

    프로그램 실행 전, 필수 설정이 제대로 로드되었는지 확인하는 용도.

    반환값:
        - stocks: 종목 개수
        - app_key_loaded: 앱 키 로드 여부 (1 또는 0)
        - app_secret_loaded: 앱 시크릿 로드 여부
        - approval_key_loaded: 승인키 존재 여부
    """

    app_config = load_app_config()  # 앱 키/시크릿 로드
    approval_key = load_approval_key(CONFIG_DIR / "approval_key.json")  # 저장된 승인키 로드
    stock_items = load_stock_list()  # 종목 리스트 로드

    return {
        "stocks": len(stock_items),  # 종목 개수
        "app_key_loaded": 1 if app_config.app_key else 0,
        "app_secret_loaded": 1 if app_config.app_secret else 0,
        "approval_key_loaded": 1 if approval_key.approval_key else 0,
    }


async def run_once_async(
    refresh_approval: bool = False,
    stop_at=None,
    session_name: str | None = None
) -> dict[str, object]:
    """
    [비동기 실행 핵심 함수]

    전체 시세 수집 로직을 한 번 실행하는 함수.

    주요 흐름:
    1. 설정 및 세션 결정
    2. 승인키 로드 또는 갱신
    3. 웹소켓 구독 시작
    4. 데이터 수집 및 CSV 저장
    5. 결과 리포트 반환

    파라미터:
        refresh_approval: True이면 승인키 강제 갱신
        stop_at: 특정 시점에 자동 종료 (테스트용)
        session_name: 특정 세션 강제 지정 (예: "nxt")
    """

    # 1. 앱 설정 로드 (app_key, secret 등)
    app_config = load_app_config()

    # 2. 세션 결정
    # - session_name이 주어지면 해당 세션 사용
    # - 없으면 현재 시간 기준으로 자동 결정
    session = session_for_name(session_name) if session_name else resolve_session()

    # 3. 종목 리스트 로드
    stock_items = load_stock_list()

    # 4. 승인키 로드 또는 갱신
    approval_key = load_or_refresh_approval_key(
        app_config,
        CONFIG_DIR / "approval_key.json",
        refresh=refresh_approval,  # True면 강제 갱신
    )

    # ---------------------------------------------------
    # CSV 저장소 생성
    # 세션별로 저장 디렉토리가 다름
    # ---------------------------------------------------
    store = CsvStore(DATA_DIR / session.storage_dir_name)

    # ---------------------------------------------------
    # 웹소켓 클라이언트 생성
    # ---------------------------------------------------
    client = KISWebSocketClient(app_config, approval_key, session, store)

    # ---------------------------------------------------
    # 구독 등록
    # 종목 + 세션에 맞는 실시간 데이터 채널 구성
    # ---------------------------------------------------
    client.subscribe(build_subscriptions(stock_items, session))

    # ---------------------------------------------------
    # 웹소켓 실행 (여기서 실시간 데이터 수집 시작)
    # ---------------------------------------------------
    await client.run(stop_at=stop_at)

    # ---------------------------------------------------
    # 실행 결과 리포트 생성
    # ---------------------------------------------------
    report = {
        "session": session.name,
        **store.snapshot(),  # 파일 수, row 수 등
    }

    # ---------------------------------------------------
    # 요약 로그 출력
    # ---------------------------------------------------
    logger.info(
        "%s session summary files=%s rows=%s trade_rows=%s orderbook_rows=%s",
        session.name,
        report.get("files_written", 0),
        report.get("rows_written", 0),
        report.get("trade_rows_written", 0),
        report.get("orderbook_rows_written", 0),
    )

    return report


def run_once(
    refresh_approval: bool = False,
    stop_at=None,
    session_name: str | None = None
) -> dict[str, object]:
    """
    [동기 실행 wrapper]

    async 함수(run_once_async)를 동기 방식으로 실행하기 위한 래퍼.

    내부적으로 asyncio.run() 사용
    """
    return asyncio.run(
        run_once_async(
            refresh_approval=refresh_approval,
            stop_at=stop_at,
            session_name=session_name
        )
    )


def run(
    refresh_approval: bool = False,
    stop_at=None,
    session_name: str | None = None
) -> dict[str, object]:
    """
    [외부에서 사용하는 메인 실행 함수]

    사실상 run_once와 동일 (alias 역할)
    """
    return run_once(
        refresh_approval=refresh_approval,
        stop_at=stop_at,
        session_name=session_name
    )


def refresh_approval_key() -> None:
    """
    [승인키 수동 갱신 함수]

    스케줄러 또는 외부 요청에 의해
    승인키를 강제로 재발급 받아 파일에 저장합니다.

    사용 상황:
        - 승인키 만료
        - 인증 오류 발생 시
        - 주기적 갱신 스케줄
    """

    app_config = load_app_config()

    # 강제 refresh=True
    load_or_refresh_approval_key(
        app_config,
        CONFIG_DIR / "approval_key.json",
        refresh=True,
    )

    logger.info("approval key refreshed manually at request of scheduler")
