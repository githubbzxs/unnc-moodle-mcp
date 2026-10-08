# UNNC Moodle MCP

宁波诺丁汉大学 Moodle（`moodle.nottingham.ac.uk`）的只读 MCP 服务。让 Claude Code / Codex 直接查询课程、公告、作业截止时间与成绩，并把课件（PPT/PDF/Word）增量下载到本地、抽取文字供 AI 阅读。

## 工作原理

- **登录**：与官方 Moodle App 相同的 SSO 流程。`login` 打开一个独立的 Chrome 窗口 → 自动点击「Student and staff sign in」→ 你完成微软登录 → Moodle 回调 `moodlemobile://token=…` → 校验 passport 后保存移动端 Web Service token。不需要密码，也不读取浏览器 Cookie。
- **数据**：所有查询都走 Moodle 官方 REST Web Service（与 App 同一套接口），不抓网页。
- **下载**：文件经 `webservice/pluginfile.php` 下载，token 只放在 POST 请求体中，不出现在 URL、日志或返回给 AI 的文本里；只允许下载本站文件。
- **只读**：不提交作业、不发帖、不标记已读、不修改任何 Moodle 数据。

## 安装

需要 [uv](https://docs.astral.sh/uv/) 和 Google Chrome。

```sh
cd ~/Desktop/Academic/moodle-mcp
uv sync
uv run unnc-moodle-mcp login     # 弹出 Chrome，完成学校登录
uv run unnc-moodle-mcp status    # 确认登录成功
```

注册到 Claude Code（用户级，所有项目可用）：

```sh
claude mcp add --scope user unnc-moodle -- uv run --directory ~/Desktop/Academic/moodle-mcp unnc-moodle-mcp
```

Codex（`~/.codex/config.toml`）：

```toml
[mcp_servers.unnc-moodle]
command = "uv"
args = ["run", "--directory", "/Users/<你>/Desktop/Academic/moodle-mcp", "unnc-moodle-mcp"]
```

## 工具

| 工具 | 作用 |
| --- | --- |
| `login` / `whoami` | 浏览器 SSO 登录；查看当前账号 |
| `list_courses` | 已选课程（本学期 / 过去 / 全部 / 隐藏） |
| `course_outline` | 一门课的章节与活动，含 cmid、文件名、大小、更新时间、截止时间 |
| `find_materials` | 按关键词在活动名和文件名中搜索（如 `week 3 lecture`） |
| `read_activity` | 读取页面、书、链接、作业要求等活动正文 |
| `upcoming_deadlines` | 未来 N 天待办（与 Moodle 时间线一致） |
| `list_assignments` | 作业、截止时间、提交与评分状态 |
| `announcements` | 课程公告最新帖子与正文 |
| `list_discussions` / `read_discussion` | 论坛主题与帖子 |
| `notifications` | Moodle 通知 |
| `grades` | 各课程总评或某门课的评分项与反馈 |
| `sync_files` | 增量同步课件到本地，可按扩展名过滤、可预览 |
| `download_activity` | 下载某个活动（资源、文件夹、作业附件等）的全部文件，可直接抽取文字 |
| `download_url` | 下载正文里出现的 pluginfile 链接 |
| `read_local_file` | 抽取下载目录中 PPTX（含讲者备注）/ PDF / DOCX 的文字，可按页读取 |

课程参数既可以填 id，也可以填名称或简称关键词（如 `physics`、`RWAC`、`DSEEF011`）。所有时间按 UTC+8 显示。

对 AI 说的话举例：

- 「这周有哪些作业要交？」
- 「把 Foundation Physics 新的课件都下下来」
- 「RWAC 最近有什么公告？」
- 「读一下 Scientific Method 第 3 周的 lecture，帮我总结」

## 课件同步

```sh
uv run unnc-moodle-mcp sync                       # 本学期全部正式课程
uv run unnc-moodle-mcp sync physics --types pptx,pdf
uv run unnc-moodle-mcp sync --dry-run             # 只看会下载什么
```

- 保存到 `~/Desktop/Academic/Moodle/<课程全名>/<序号 章节名>/<文件>`；文件夹活动或多文件资源再多一层活动名目录。
- 每门课目录下的 `.moodle-sync.json` 记录已同步文件的修改时间和大小；再次同步只下载新增或老师更新过的文件，文件修改时间设为 Moodle 上的时间。
- 目标位置已有**内容不同**的同名文件（例如你自己的批注版）时，不会覆盖，新文件保存为 `名称 (Moodle).ext`；同名同大小的文件直接登记为已同步。
- 不指定课程时只同步带课程代码的正式课程（如 DSEEF011、CELEN048）；Academic Services Office、FoSE、Writing Lab 等学院/机构页面文件很多（ASO 有 6000+ 个考试反馈文件），需要时单独指定课程名同步。
- 「本学期」包含 Moodle 归为进行中的课程，以及结束日期在 120 天内的课程（Foundation Physics 的结束日期被设成了开学第三周，会被 Moodle 误归为过去课程）。
- Moodle 5.x 的子章节（subsection）放在父章节下的子目录里。
- 默认同步 `resource`（文件）和 `folder`（文件夹）活动；页面、作业附件等用 `download_activity` 按需下载。

## 登录备选：复用 Chrome 登录态

`login` 弹出的是独立 Chrome 配置，不共享日常 Chrome 的登录。若日常 Chrome 已登录学校账号，可以让 AI 用 Ego 浏览器（先同步 Chrome 登录态）打开
`admin/tool/mobile/launch.php?service=moodle_mobile_app&passport=<随机串>&urlscheme=moodlemobile`，在微软账号选择页选学校账号，截获
`moodlemobile://token=…` 回调后用 `auth.decode_app_token` 校验并 `auth.save_session` 保存。

## 本地文件与配置

| 位置 | 内容 |
| --- | --- |
| `~/.unnc-moodle-mcp/token.json` | Moodle token（权限 600） |
| `~/.unnc-moodle-mcp/chrome-profile/` | 登录专用的独立 Chrome 配置，下次登录可免去重复输入 |
| `~/Desktop/Academic/Moodle/` | 课件下载根目录 |

环境变量（一般不需要）：`UNNC_MOODLE_DOWNLOAD_DIR` 改下载目录，`UNNC_MOODLE_STATE_DIR` 改状态目录。

退出登录：`uv run unnc-moodle-mcp logout` 删除本机 token；要让服务器端 token 同时失效，在 Moodle「Preferences → Security keys」中重置 *Moodle mobile web service*。

## 开发

```sh
uv run pytest
```

测试使用模拟的 Moodle 接口，覆盖 SSO 回调校验、token 不进 URL、跨站下载拦截、增量同步、不覆盖用户文件、失败重试、课程模糊匹配、PPTX/DOCX 抽取等。

## 参考

登录流程参考了 [moodler-mcp](https://github.com/GhaithAlHallak8/moodler-mcp)（MIT）的思路；本项目针对 UNNC 重写，并增加了按课程/章节的增量课件同步。
