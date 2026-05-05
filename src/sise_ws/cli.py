from __future__ import annotations

"""
[개요]

이 모듈은 CLI(Command Line Interface) 진입점입니다.

즉, 터미널에서 다음과 같은 명령을 실행했을 때:
    python -m sise_ws bootstrap
    python -m sise_ws run
    python -m sise_ws scheduler

어떤 함수가 실행될지 연결해주는 역할을 합니다.

실제 데이터 처리 로직은 없고,
"명령어 → 실행 함수 매핑"만 담당합니다.
"""

import argparse
import json
import logging

# 실제 실행 로직
from .ingest import bootstrap, run

# 스케줄러 실행 로직
from .scheduler import run_scheduler


def build_parser() -> argparse.ArgumentParser:
    """
    [CLI 명령어 구조 정의]

    사용할 수 있는 명령어:
        - bootstrap  : 설정 상태 점검
        - run        : 웹소켓 수집 실행
        - scheduler  : 자동 스케줄러 실행

    반환:
        argparse.ArgumentParser 객체
    """

    parser = argparse.ArgumentParser(prog="sise_ws")

    # 서브 명령어 그룹 생성
    subparsers = parser.add_subparsers(dest="command")

    # ---------------------------------------------------
    # bootstrap 명령어
    # ---------------------------------------------------
    # 설정이 정상적으로 로드되는지 확인하는 용도
    subparsers.add_parser("bootstrap", help="load config and report readiness")

    # ---------------------------------------------------
    # run 명령어
    # ---------------------------------------------------
    # 웹소켓 수집 실행
    run_parser = subparsers.add_parser("run", help="start the websocket ingest loop")

    # 옵션: 승인키 강제 갱신
    run_parser.add_argument(
        "--refresh-approval",
        action="store_true",  # 옵션 존재 시 True
        help="force approval key refresh"
    )

    # ---------------------------------------------------
    # scheduler 명령어
    # ---------------------------------------------------
    # 장 시작/종료 기준 자동 실행
    subparsers.add_parser("scheduler", help="run the market-day scheduler")

    return parser


def main() -> None:
    """
    [프로그램 실행 진입점]

    전체 흐름:
    1. 로깅 설정
    2. CLI 파서 생성
    3. 사용자 입력 명령 파싱
    4. 명령에 따라 분기 실행
    """

    # ---------------------------------------------------
    # 로깅 기본 설정
    # ---------------------------------------------------
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    # ---------------------------------------------------
    # CLI 파서 생성 및 입력 파싱
    # ---------------------------------------------------
    parser = build_parser()
    args = parser.parse_args()

    # ---------------------------------------------------
    # 명령어 분기 처리
    # ---------------------------------------------------

    # 1. bootstrap
    # → 설정 및 준비 상태 확인
    if args.command == "bootstrap":
        print(json.dumps(bootstrap(), ensure_ascii=False))
        return

    # 2. run
    # → 실시간 데이터 수집 시작
    if args.command == "run":
        run(refresh_approval=args.refresh_approval)
        return

    # 3. scheduler
    # → 자동 스케줄러 실행
    if args.command == "scheduler":
        run_scheduler()
        return

    # ---------------------------------------------------
    # 명령어가 없는 경우 help 출력
    # ---------------------------------------------------
    parser.print_help()
