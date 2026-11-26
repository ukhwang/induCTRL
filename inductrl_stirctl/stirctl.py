#!/usr/bin/env python3
"""InduCTRL 교반 완료 판정·자동 정지 프로그램 진입점.

예) python3 stirctl.py run --mode mock --material saltwater --batch-id demo-1 --fast
기존 main.py(읽기 전용 로거)와는 별개 프로그램입니다.
"""
import sys

from inductrl.cli import main

if __name__ == "__main__":
    sys.exit(main())
