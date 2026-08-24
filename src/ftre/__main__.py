"""Entry point for running ftre as a module: python -m ftre"""
# 中文说明：模块化入口：仅把执行权交给 ftre.main.app，不在这里创建 Gateway、Service 或后台任务。

from ftre.main import app

if __name__ == "__main__":
    app()
