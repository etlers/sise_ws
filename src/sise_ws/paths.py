"""
시세 수집에 사용되는 KRX, NXT 디렉토리 경로를 정의하는 모듈입니다.
"""

from __future__ import annotations

from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[2]
CONFIG_DIR = ROOT_DIR / "config"
DATA_DIR = ROOT_DIR / "data"
GRAPHIFY_DIR = ROOT_DIR / "graphify-out"
KRX_DIR = DATA_DIR / "krx"
NXT_DIR = DATA_DIR / "nxt"


def ensure_data_directories() -> None:
    KRX_DIR.mkdir(parents=True, exist_ok=True)
    NXT_DIR.mkdir(parents=True, exist_ok=True)

