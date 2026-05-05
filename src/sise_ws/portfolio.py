"""
프리마켓/장전 시세 수집을 위해 사용자가 설정한 StockItem 목록을 실제 시세 수집 대상 목록으로 변환하는 로직입니다.
"""

from __future__ import annotations

# dataclass: 데이터 저장용 클래스를 간결하게 정의하기 위해 사용합니다.
from dataclasses import dataclass

# csv: nxt_component_stock.csv 파일을 읽기 위해 사용합니다.
import csv

# Path: CSV 파일 경로를 객체 형태로 다루기 위해 사용합니다.
from pathlib import Path

# CONFIG_DIR:
# 기본 구성 종목 CSV 파일 위치를 찾기 위해 사용합니다.
#
# StockItem:
# 사용자가 config에서 설정한 원본 종목 정보를 표현하는 객체입니다.
from .config import CONFIG_DIR
from .config import StockItem


@dataclass(frozen=True)
class ExpandedStockItem:
    """
    실제 시세 수집 대상이 되는 종목 정보입니다.

    StockItem은 사용자가 설정한 원본 종목이고,
    ExpandedStockItem은 웹소켓 구독에 실제로 사용할 종목입니다.

    예를 들어 사용자가 어떤 대표 종목/지수성 종목을 설정했고,
    해당 종목의 호가 수집이 활성화되어 있다면,
    이 로직은 그 종목 자체가 아니라
    nxt_component_stock.csv에 들어 있는 구성 종목들로 확장할 수 있습니다.
    """

    # 사용자가 원래 설정한 종목 코드입니다.
    # 확장된 구성 종목이라도, 어떤 원본 종목에서 파생되었는지 추적하기 위해 사용합니다.
    source_code: str

    # 사용자가 원래 설정한 종목명입니다.
    source_name: str

    # 실제 시세를 가져올 종목 코드입니다.
    #
    # 확장되지 않은 경우:
    #   source_code와 code가 같습니다.
    #
    # 확장된 경우:
    #   source_code는 원본 종목 코드,
    #   code는 구성 종목 코드입니다.
    code: str

    # 실제 시세를 가져올 종목명입니다.
    name: str

    # 짧은 종목명입니다.
    # 기존 StockItem 구조와 호환하기 위해 유지합니다.
    short_nm: str

    # 시장/구분값입니다.
    # 예: kospi, kosdaq, nxt 등
    div: str

    # 구성 종목 내 비중입니다.
    # nxt_component_stock.csv의 weight_rt 값을 사용합니다.
    #
    # 확장되지 않은 일반 종목이면 None일 수 있습니다.
    weight_rt: float | None = None

    @property
    def is_expanded(self) -> bool:
        """
        이 종목이 원본 종목에서 확장된 구성 종목인지 확인합니다.

        source_code != code 이면,
        사용자가 직접 설정한 종목이 아니라
        원본 종목의 구성 종목으로 확장된 것입니다.
        """

        return self.source_code != self.code


def load_premarket_component_map(
    path: Path | None = None,
    top_n: int = 10,
) -> dict[str, list[ExpandedStockItem]]:
    """
    장전/프리마켓용 구성 종목 매핑 파일을 읽어옵니다.

    기본 파일:
        CONFIG_DIR / "nxt_component_stock.csv"

    반환 구조:
        {
            "원본종목코드": [
                ExpandedStockItem(...구성종목1...),
                ExpandedStockItem(...구성종목2...),
                ...
            ]
        }

    즉, 특정 원본 종목을 어떤 실제 구성 종목들로 확장할지
    미리 만들어두는 함수입니다.

    top_n:
        원본 종목별로 비중이 높은 상위 N개 구성 종목만 사용합니다.
    """

    # 별도 path가 전달되지 않으면 config 디렉터리의 기본 CSV를 사용합니다.
    target = CONFIG_DIR / "nxt_component_stock.csv" if path is None else path

    # 원본 종목 코드별 구성 종목 목록을 저장할 dict입니다.
    component_map: dict[str, list[ExpandedStockItem]] = {}

    # utf-8-sig:
    # CSV 파일이 엑셀에서 저장되어 BOM이 포함된 경우에도
    # 첫 컬럼명을 정상적으로 읽기 위해 사용합니다.
    with open(target, newline="", encoding="utf-8-sig") as file_pointer:
        reader = csv.DictReader(file_pointer)

        # CSV 각 row를 구성 종목 정보로 변환합니다.
        for row in reader:
            # source_code:
            # 구성 종목들이 속한 원본 종목 코드입니다.
            # zfill(6)으로 6자리 종목코드 형식을 맞춥니다.
            source_code = str(row.get("source_code") or "").strip().zfill(6)

            # 원본 종목명입니다.
            source_name = str(row.get("source_name") or "").strip()

            # 실제 구성 종목 코드입니다.
            stock_cd = str(row.get("stock_cd") or "").strip().zfill(6)

            # 실제 구성 종목명입니다.
            stock_nm = str(row.get("stock_nm") or "").strip()

            # 구성 종목 비중입니다.
            # 정렬 기준으로 사용됩니다.
            weight_rt = row.get("weight_rt")

            # 원본 코드 또는 구성 종목 코드가 없으면 사용할 수 없는 row이므로 건너뜁니다.
            if not source_code or not stock_cd:
                continue

            # 원본 종목 코드별로 구성 종목을 누적합니다.
            component_map.setdefault(source_code, []).append(
                ExpandedStockItem(
                    source_code=source_code,
                    source_name=source_name,
                    code=stock_cd,
                    name=stock_nm,
                    short_nm=stock_nm,

                    # 구성 종목 CSV에서 읽은 항목은 NXT 기준 확장 종목으로 취급합니다.
                    div="nxt",

                    # weight_rt가 있으면 float으로 변환하고,
                    # 없으면 None으로 둡니다.
                    weight_rt=float(weight_rt) if weight_rt not in (None, "") else None,
                )
            )

    # 원본 종목별 구성 종목 목록을 비중 내림차순으로 정렬한 뒤,
    # 상위 top_n개만 남깁니다.
    for source_code in component_map:
        component_map[source_code].sort(
            key=lambda item: item.weight_rt or 0.0,
            reverse=True,
        )
        component_map[source_code] = component_map[source_code][:top_n]

    return component_map


def expand_premarket_items(
    stock_items: list[StockItem],
    top_n: int = 10,
) -> list[ExpandedStockItem]:
    """
    사용자가 설정한 StockItem 목록을 실제 시세 수집 대상 목록으로 변환합니다.

    핵심 역할:
    - 일반 종목은 그대로 수집 대상에 포함합니다.
    - is_orderbook_enabled=True인 종목은
      nxt_component_stock.csv에 정의된 구성 종목 상위 N개로 확장합니다.

    즉, 이 함수의 결과가 이후 웹소켓 구독 대상이 됩니다.

    예시:

    입력 StockItem:
        [
            StockItem(code="123456", name="A", is_orderbook_enabled=True),
            StockItem(code="005930", name="삼성전자", is_orderbook_enabled=False),
        ]

    nxt_component_stock.csv:
        source_code=123456 에 대해
        000001, 000002, 000003 구성 종목 존재

    결과 ExpandedStockItem:
        [
            000001,
            000002,
            000003,
            005930
        ]
    """

    # 원본 종목 코드별 구성 종목 매핑을 로드합니다.
    component_map = load_premarket_component_map(top_n=top_n)

    # 최종적으로 반환할 실제 시세 수집 대상 목록입니다.
    expanded: list[ExpandedStockItem] = []

    # 중복 종목 추가를 방지하기 위한 set입니다.
    #
    # 여러 원본 종목의 구성 종목에 같은 종목이 들어있을 수 있으므로,
    # 동일 code는 한 번만 수집 대상에 넣습니다.
    seen: set[str] = set()

    # 사용자가 config에 설정한 원본 종목들을 순회합니다.
    for item in stock_items:
        # 호가 확장 대상이 아닌 일반 종목인 경우입니다.
        #
        # 이 경우에는 구성 종목으로 펼치지 않고,
        # item 자체를 실제 수집 대상으로 추가합니다.
        if not item.is_orderbook_enabled:
            # 이미 같은 종목 코드가 추가되어 있으면 중복 추가하지 않습니다.
            if item.code not in seen:
                expanded.append(
                    ExpandedStockItem(
                        # 확장되지 않았으므로 source_code와 code가 같습니다.
                        source_code=item.code,
                        source_name=item.name,
                        code=item.code,
                        name=item.name,
                        short_nm=item.short_nm,
                        div=item.div,
                    )
                )

                # 중복 방지를 위해 추가 완료된 종목코드를 기록합니다.
                seen.add(item.code)

            # 일반 종목 처리가 끝났으므로 다음 item으로 넘어갑니다.
            continue

        # 여기까지 왔다면 item.is_orderbook_enabled=True인 종목입니다.
        #
        # 이 종목은 직접 수집 대상에 넣지 않고,
        # component_map에서 구성 종목 목록을 찾아 확장합니다.
        components = component_map.get(item.code, [])

        # 원본 종목에 매핑된 구성 종목들을 순회합니다.
        for component in components:
            # 이미 추가된 종목이면 중복 추가하지 않습니다.
            if component.code in seen:
                continue

            # 구성 종목을 실제 수집 대상으로 추가합니다.
            expanded.append(
                ExpandedStockItem(
                    # source_code/source_name은 원본 item 기준으로 기록합니다.
                    # 나중에 이 구성 종목이 어떤 원본 종목에서 나온 것인지 추적할 수 있습니다.
                    source_code=item.code,
                    source_name=item.name,

                    # 실제 수집 대상은 component의 종목 코드/이름입니다.
                    code=component.code,
                    name=component.name,
                    short_nm=component.short_nm,

                    # 확장된 구성 종목은 NXT 기준으로 취급합니다.
                    div="nxt",

                    # 구성 종목 비중을 유지합니다.
                    weight_rt=component.weight_rt,
                )
            )

            # 중복 방지를 위해 추가 완료된 구성 종목 코드를 기록합니다.
            seen.add(component.code)

    return expanded
