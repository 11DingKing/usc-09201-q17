"""服务入口，向后兼容 `python3 -m service.main`。

实际路由与领域逻辑位于 service.app。
"""

from __future__ import annotations

from .app import create_server, main  # noqa: F401

if __name__ == "__main__":
    main()
