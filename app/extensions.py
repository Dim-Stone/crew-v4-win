"""Flask 扩展实例。

单独一个模块，避免 app 工厂与模型定义互相 import 造成的循环依赖。
"""

from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()
