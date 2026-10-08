"""运行配置：站点地址、本地状态目录与下载目录。环境变量仅用于测试或改路径。"""

import os
from datetime import timedelta, timezone
from pathlib import Path

MOODLE_URL = os.environ.get("UNNC_MOODLE_URL", "https://moodle.nottingham.ac.uk").rstrip("/")

# 本地状态：token 与登录用的独立 Chrome 配置目录，均只对当前用户可读写
STATE_DIR = Path(os.environ.get("UNNC_MOODLE_STATE_DIR", "~/.unnc-moodle-mcp")).expanduser()
TOKEN_FILE = STATE_DIR / "token.json"
BROWSER_PROFILE = STATE_DIR / "chrome-profile"

# 课件下载根目录，结构为 <课程>/<章节>/<文件>
DOWNLOAD_DIR = Path(
    os.environ.get("UNNC_MOODLE_DOWNLOAD_DIR", "~/Desktop/Academic/Moodle")
).expanduser()

MOBILE_SERVICE = "moodle_mobile_app"
USER_AGENT = "unnc-moodle-mcp/0.1"
LOGIN_TIMEOUT_S = 300

# 所有对外展示的时间统一为 UTC+8
TZ = timezone(timedelta(hours=8))
