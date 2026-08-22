# FFXIV 国服历史招募板搜索

> 一个记录并搜索《最终幻想14》国服招募板（Party Finder）历史数据的小工具。
>
> 🌐 **在线站点**：[https://ffbqy.xyz](https://ffbqy.xyz)
>
> 🤖 **本项目代码由 AI 辅助生成，功能已通过本地测试验证。**

---

## ✨ 功能

- 每 90 秒自动抓取国服招募数据（含随机 1-3 秒延迟），持续入库
- 支持按 **大区 / 服务器 / 任务类型 / 副本 / 状态 / 队长名 / 日期范围** 筛选
- 支持**关键词搜索**招募说明（如「绝亚」「7换1」「萌新教学」）
- 繁中服（伊弗利特、利維坦等）默认隐藏，可一键切换显示
- 绿色边框 = 进行中，红色边框 = 已关闭
- 深色 / 浅色主题，动画交互

---

## 🚀 快速开始

### 环境要求
- Python 3.9 或更高版本

### 第 1 步：配置 User-Agent（重要！否则可能被上游拒绝）

1. 复制项目根目录下的 `config.example.json`，重命名为 `config.json`
2. 打开 `config.json`，修改以下两项：

```json
{
  "api": {
    "user_agent_project": "你的项目名（随便起，英文）",
    "contact_email": "你的真实邮箱@example.com"
  }
}
```

> ⚠ **邮箱务必填真实可用的**：上游作者会通过该邮箱联系异常调用的开发者，填假邮箱可能直接被封 IP。

### 第 2 步：启动

```bash
cd ffRecruit
python run.py
```

脚本会自动安装依赖并启动两个窗口：
- 一个窗口跑爬虫（实时显示抓到的新招募）
- 一个窗口跑 API 服务

打开浏览器访问 **http://localhost:8000/** 即可使用。

| 地址 | 说明 |
|------|------|
| http://localhost:8000/ | 前端界面 |
| http://localhost:8000/docs | API 文档 (Swagger) |

---

## ⚙️ 配置文件说明

本项目通过 `config.json`（或默认示例 `config.example.json`）控制抓取参数：

| 配置键 | 默认值 | 说明 |
|--------|--------|------|
| `api.user_agent_project` | `FFXIV-PF-History-Bot` | 请求头 User-Agent 中的**项目名** |
| `api.contact_email` | `your-email@example.com` | 请求头 User-Agent 中的**联系邮箱**（请务必改为真实邮箱） |
| `api.referer` | `https://xivpf.ff14.xin/` | 请求头 Referer，一般无需修改 |
| `api.api_url` | `https://xivpf.littlenightmare.top/api/listings` | 上游 API 地址，一般无需修改 |
| `scraper.interval_seconds` | `90` | 轮询间隔（秒），建议不要小于 60 秒；每轮等待时会额外加上 1-3 秒随机延迟 |
| `scraper.per_page` | `100` | 每页抓取数量，最大 100 |

> 💡 `config.json` 已加入 `.gitignore`，不会被提交到 GitHub，可放心填写隐私信息。

---

## 📁 项目文件

```
ffRecruit/
├── run.py                  # 一键启动脚本
├── scraper.py              # 数据抓取程序
├── main.py                 # FastAPI 接口服务
├── static/
│   └── index.html          # 前端页面
├── requirements.txt        # Python 依赖
├── config.example.json     # 配置文件示例（首次使用请复制为 config.json）
└── README.md
```

> 数据库文件 `ffrecruit.db` 和个人配置 `config.json` 会在首次运行时自动生成 / 按需创建，均不纳入 Git 版本管理。

---

## 🔗 数据来源与 API 规范

### 上游来源

| 项目 | 地址 |
|------|------|
| 官方网站 | [xivpf.ff14.xin](https://xivpf.ff14.xin/) |
| 上游项目 | [LittleNightmare/remote-party-finder](https://github.com/LittleNightmare/remote-party-finder) |
| 实际调用 API | `https://xivpf.littlenightmare.top/api/listings` |

### 调用规范（必须遵守）

根据上游作者要求，调用 API 时必须在 `User-Agent` 中注明**项目名称**和**联系方式**。**具体值请在 `config.json` 中配置**，程序会自动拼接为以下格式：

```
User-Agent: {你配置的项目名} (contact: {你配置的邮箱})
Referer:    https://xivpf.ff14.xin/
Accept:     application/json
```

示例（当配置正确时）：
```
User-Agent: MyFFXIV-PF-Tool (contact: me@example.com)
```

> 如果未正确配置邮箱，启动时会在控制台打印醒目警告，请尽快修改。

### 抓取策略

- 抓取频率：**每 90 秒** 一次（轮询间隔 + 随机 1-3 秒延迟），全量分页拉取（可在配置中调整）
- 新招募：首次发现时记录入库
- 已有招募：更新最后存活时间和队伍成员状态
- 招募消失：自动标记为「已关闭」

---

## 🛠 技术栈

| 部分 | 技术 |
|------|------|
| 后端接口 | Python + FastAPI + Uvicorn |
| 数据抓取 | Python + httpx |
| 数据库 | SQLite（零配置，开箱即用） |
| 前端 | Vue 3 + Element Plus + Tailwind CSS（CDN 单文件） |

---

## ⚠️ 免责声明

1. **非官方项目**：本项目与史克威尔艾尼克斯（Square Enix）、盛趣游戏及《最终幻想14》运营团队无关，仅供学习交流使用。
2. **AI 辅助生成**：本项目代码（含前端界面、后端接口、爬虫逻辑）由 AI 辅助生成，作者已在本地测试核心功能；但代码中仍可能存在未覆盖的边界问题，使用前请自行评估风险。
3. **数据归属**：所有招募板原始数据版权归上游项目 [xivpf.ff14.xin](https://xivpf.ff14.xin/) 及其作者所有；本项目仅提供数据的**历史存档与检索功能**，不对数据真实性、完整性、及时性负责。
4. **合规使用**：
   - 使用者必须遵守上游 API 的调用规范（合理频率、在 `config.json` 中配置正确的项目名 + 真实联系邮箱）；
   - 因滥用 API、伪造 User-Agent、设置过短轮询间隔等行为导致的 IP 封禁、账号处罚或法律责任，由使用者自行承担。
5. **隐私提醒**：
   - 生成的 `ffrecruit.db` 数据库中包含玩家角色名、所属服务器等信息，**请勿公开分享或上传至公开仓库**；
   - 个人填写的 `config.json`（含邮箱等信息）已被 `.gitignore` 忽略，请勿主动提交到公开仓库。
6. **无担保**：代码按现状提供，不做任何明示或暗示的可用性、稳定性、适用性担保，因使用本项目产生的任何直接或间接损失与作者无关。

---

## 📜 许可证

MIT License
