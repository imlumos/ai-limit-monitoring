"""
Provider 包初始化
==================

【这个文件做什么】
import 所有平台 provider 模块,利用"导入即注册"的副作用把每个 provider 登记进 registry。
加新平台只需两步:写 api/providers/xxx.py(带 @register)+ 在这里加一行 import。

【为什么要这个文件】
registry.register 是装饰器,只有在模块被 import 时才会执行。
如果没人 import provider 模块,它就不会被登记,get_provider/list_providers 找不到它。
本文件集中做这件事,顺序也决定了"添加账号"下拉框里平台的排列。

【关键概念:导入即注册】
Python import 一个模块时会执行它的顶层代码。provider 模块顶层有 @register 装饰类,
import 它 = 执行装饰器 = 登记进 PROVIDER_REGISTRY。这叫"用副作用做注册"。
"""
# Phase 1:GLM(逐字移植自旧 glm_client.py)
from api.providers import glm  # noqa: F401

# Phase 2:简单 API-key 平台(顺序 = "添加账号"下拉框排列)
from api.providers import glm_en, glm_team      # noqa: F401
from api.providers import kimi, minimax, zenmux  # noqa: F401
from api.providers import deepseek, stepfun, siliconflow, openrouter, novita  # noqa: F401

# Phase 3:自定义鉴权
from api.providers import volcengine, grok  # noqa: F401

# Phase 4:官方 CLI OAuth(Claude/Codex/Gemini 读本地 token;Copilot 填 GitHub token)
from api.providers import claude, codex, gemini, copilot  # noqa: F401

# Phase 5:Cursor(读本机 Cursor 客户端登录状态,或手填网页 Cookie)
from api.providers import cursor  # noqa: F401
