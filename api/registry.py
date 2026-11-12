"""
Provider 注册表 (registry)
==========================

【这个文件做什么】
把所有平台 Provider 类集中登记到一个字典里,提供:
  - @register 装饰器:每个 provider 模块用它把自己登记进来
  - get_provider(provider_id):创建独立 provider 实例
  - list_providers():列出所有已登记平台(给"添加账号"下拉框用)

【为什么要注册表】
UI 的"添加账号"表单要列出所有支持的平台;轮询层要根据账号的 provider_id
找到对应的 Provider 类。没有注册表就得在多处维护 if/else 分支,加平台容易漏。
有了注册表,新加一个平台只要:写 provider 模块 + @register + 在 providers/__init__.py
里 import 一下,其余地方全自动。

【会话隔离】
每次采集创建独立实例,采集结束关闭。账号验证也使用独立实例,
避免 Cookie 和 provider 可变状态跨账号共享。

【关键概念:装饰器】
@register 写在 class 定义上方,等价于"定义完类后,把它作为参数传给 register"。
register 把类记进字典再原样返回,所以被装饰的类仍可正常使用。
"""
import logging
from typing import Dict, List

from api.provider_base import QuotaProvider

logger = logging.getLogger(__name__)

# provider_id -> Provider 类
PROVIDER_REGISTRY: Dict[str, type] = {}


def register(cls):
    """类装饰器:把 Provider 类登记进 PROVIDER_REGISTRY。"""
    if not cls.provider_id:
        raise ValueError(f"{cls.__name__} 缺少 provider_id")
    PROVIDER_REGISTRY[cls.provider_id] = cls
    return cls


def get_provider(provider_id: str) -> QuotaProvider:
    """创建独立实例;网络调用者负责在结束后 close()。"""
    cls = PROVIDER_REGISTRY.get(provider_id)
    if cls is None:
        raise KeyError(f"未知 provider_id: {provider_id}")
    return cls()


def list_providers() -> List[dict]:
    """
    列出所有已登记平台,给"添加账号"下拉框用。

    【顺序】Python 3.7+ 的 dict 保持插入顺序 = 各模块在 providers/__init__.py
    里的 import 顺序,所以下拉框的排列在那里集中控制。
    """
    return [
        {
            "provider_id": cls.provider_id,
            "display_name": cls.display_name,
            "category": cls.category,
        }
        for cls in PROVIDER_REGISTRY.values()
        if cls.provider_id != "zenmux"
    ]
