r"""대시보드 서버 실행 스크립트 — 어느 위치에서 실행해도 프로젝트 루트를 기준으로 동작한다.

    .venv\Scripts\python.exe scripts\serve_dashboard.py [--host 127.0.0.1] [--port 8000]
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for stream in (sys.stdout, sys.stderr):  # 콘솔 코드페이지와 무관하게 한글 로그가 깨지지 않도록
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="replace")
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

from app.main import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main(["serve", *sys.argv[1:]]))
