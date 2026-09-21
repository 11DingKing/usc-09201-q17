"""林业项目组合驾驶舱服务入口。"""

from __future__ import annotations

import os

from .api import create_server


def main() -> None:
    """启动服务。"""

    port = int(os.environ.get("PORT", "3000"))
    data_dir = os.environ.get("DATA_DIR", "data")
    server = create_server(port=port, data_dir=data_dir)
    print(f"服务已启动：http://0.0.0.0:{port}（数据目录：{data_dir}）")
    server.serve_forever()


if __name__ == "__main__":
    main()
