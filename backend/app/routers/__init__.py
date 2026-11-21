"""业务路由汇总：所有模块共用一套资源路由，由领域服务完成分派。"""
from __future__ import annotations

from app.routers import evidence, resource

# 证据包路由含固定路径，必须先于资源路由的 /{module} 动态段注册。
ROUTERS = [evidence, resource]
