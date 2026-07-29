"""Vercel 入口：re-export 根目录 app.py 的 Flask 实例。

Vercel Python runtime 只把 /api 目录下的文件转成 serverless 函数。Flask 实例
仍在根 app.py 创建（app = Flask(__name__)），root_path = 仓库根，所以
templates/ static/ data/ 的相对路径不变；本地 `flask --app app run` 也不受影响。
"""
from app import app  # noqa: F401
