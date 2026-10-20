"""随包自带：site 模块会自动导入这个文件。

只做一件事——把**用户自己的** per-user site-packages 从 sys.path 上摘掉。

为什么需要它：解释器的 `python313._pth` 里有 `import site`（pywin32 要靠
site 处理 `pywin32.pth`，否则 `pywintypes` 的 DLL 挂不上）。但 site 同时会把
`%APPDATA%\\Python\\Python313\\site-packages` 加进来——那是别人机器上的
东西，不应该影响这个包的运行。
"""

import site
import sys

site.ENABLE_USER_SITE = False

_user = site.getusersitepackages()
if isinstance(_user, str) and _user in sys.path:
    sys.path.remove(_user)
