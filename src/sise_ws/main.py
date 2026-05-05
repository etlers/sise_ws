"""
sise_ws 패키지의 진입점 모듈
- 이 파일이 실행될 때, CLI 프로그램이 시작됨
- main() 함수는 cli.py에 정의되어 있으며, 프로그램의 전체 흐름을 제어함
- __name__ == "__main__" 조건을 사용하여, 이 파일이 직접 실행될 때만 main() 함수가 호출되도록 함
"""

from __future__ import annotations
# 타입 힌트를 문자열처럼 지연 평가하도록 설정
# → 순환 참조 방지, forward reference 문제 해결
# → Python 3.10 이하에서도 안정적으로 타입 힌트 사용 가능


if __name__ == "__main__":
    # 이 파일이 "직접 실행될 때만" 아래 코드가 실행됨
    # (다른 파일에서 import될 경우에는 실행되지 않음)

    from .cli import main
    # 현재 패키지 내부의 cli.py에서 main 함수 import
    # ※ import를 여기서 하는 이유:
    #   - 프로그램 실행 시에만 로딩 (불필요한 import 방지)
    #   - 순환 import 문제 예방 (매우 중요)

    main()
    # CLI 프로그램의 실제 시작점
    # → cli.py에 정의된 main() 함수가 실행되면서
    #   전체 애플리케이션 로직이 시작됨
