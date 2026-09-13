# -*- coding: utf-8 -*-
"""根级 conftest.py。

存在意义有两个：
1. 让 pytest 把项目根目录加入 sys.path，这样 tests/ 下可以直接 ``import protocol``、
   ``from localtonet... import ...``，无需安装成包。
2. 收敛通用夹具（异步场景统一用 ``asyncio.run`` 包一层，不引入 pytest-asyncio 依赖）。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Awaitable, Callable, TypeVar

import pytest

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

T = TypeVar("T")


@pytest.fixture
def run_async() -> Callable[[Callable[[], Awaitable[T]]], T]:
    """把协程场景跑在一个独立事件循环里。

    用法::

        def test_xxx(run_async):
            assert run_async(lambda: scenario()) == 42
    """

    def _run(factory: Callable[[], Awaitable[T]]) -> T:
        return asyncio.run(factory())

    return _run
