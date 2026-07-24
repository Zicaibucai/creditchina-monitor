# 中建探员

本项目用于监控固定企业的信用中国行政许可、行政处罚及上海住建信用分，并保存变更历史、官网截图与可校验的证据包。项目包含 Python API/采集器和 Web 管理看板，所有运行数据默认保存在本机 `output/`。

## 环境要求

- Python 3.9 或更高版本
- Node.js 22.13 或更高版本（包含 npm）
- Google Chrome
- macOS、Linux 或 Windows

MySQL、代理和验证码识别服务均为可选项。仅使用网页看板时，系统默认使用本机 SQLite，不需要单独安装数据库。

## 首次安装

在项目根目录执行：

```bash
python3 setup_project.py
```

Windows 可使用：

```powershell
py setup_project.py
```

安装脚本会创建隔离的 `.venv`、安装前后端依赖，并从安全模板生成不会纳入版本控制的 `.env.local`。如需自动识别验证码或使用快代理，请随后编辑 `.env.local`；不要把真实凭据发给他人或提交到 Git。

## 启动

```bash
python3 start_project.py
```

浏览器打开 [http://localhost:3000](http://localhost:3000)。启动脚本会同时运行：

- 管理看板：`http://localhost:3000`
- 后端健康检查：`http://127.0.0.1:8765/api/v1/health`
- API 文档：`http://127.0.0.1:8765/docs`

按 `Ctrl+C` 可同时停止前后端。脚本使用项目自身位置解析路径，因此可以从任意工作目录启动。

如需分别启动：

```bash
.venv/bin/python api_server.py
cd frontend && npm run dev
```

Windows 后端命令为 `.venv\Scripts\python.exe api_server.py`。

## 基本使用

1. 在看板的“企业清单”中添加企业，或逐行编辑 `monitor_companies.txt`。
2. 点击“手动更新一轮”采集行政许可、行政处罚和证据材料。
3. 点击“采集信用分”单独采集上海住建信用评价。
4. 在企业详情、更新公告或导出入口查看结果。

默认不会在启动时自动采集。首次采集只建立基线；第二次起，新增、变更和停止公示记录会生成公告。运行数据、SQLite、Excel 和截图均写入 `output/`，该目录不会提交到 Git。

## 配置

所有可配置项都列在 `.env.example`。常用项如下：

| 配置 | 用途 | 默认值 |
| --- | --- | --- |
| `JFBYM_TOKEN` | 后台自动识别验证码 | 空 |
| `CREDITCHINA_REQUEST_INTERVAL_SECONDS` | 同一 IP 的官网请求最小间隔 | `1` |
| `CREDITCHINA_OUTPUT` | 运行结果目录，相对路径以项目根目录为准 | `output` |
| `CREDITCHINA_MONITOR_COMPANIES` | 固定企业名单文件 | `monitor_companies.txt` |
| `KDL_DPS_API_URL` | 快代理单 IP 提取地址，必须包含 `num=1` | 空 |
| `KDL_DPS_SECRET_ID` / `KDL_DPS_SECRET_KEY` | 快代理 HMAC-SHA1 动态签名方式 | 空 |
| `KDL_DPS_SECRET_TOKEN` | 快代理令牌鉴权；与 SecretKey 方式二选一 | 空 |
| `SH_ZJW_PROXY` | 上海住建接口专用代理 | 空（直连） |
| `CREDITCHINA_AUTO_DAILY` | 每日自动执行 | `0`（关闭） |

环境变量优先于 `.env.local`。代理凭据只用于运行时请求，不会写入日志、SQLite 或导出文件。

前端通常会自动连接当前访问主机的 `8765` 端口。后端位于其他地址时，可参考 `frontend/.env.example` 设置 `VITE_CRAWLER_API_BASE`。为 API 设置了 `CREDITCHINA_API_TOKEN` 时，前端需同步设置 `VITE_CRAWLER_API_TOKEN`。

## 命令行采集

网页之外也可以直接使用命令行：

```bash
.venv/bin/python spider_main.py --company "示例有限公司"
.venv/bin/python spider_main.py --companies-file monitor_companies.txt --xlsx-current
.venv/bin/python spider_main.py --keyword "示例关键词"
```

输出位置可通过 `--output` 指定。旧接口兼容模式仍可使用：

```bash
.venv/bin/python spider_main.py \
  --api-mode legacy \
  --transport urllib \
  --company "示例有限公司"
```

### 可选 MySQL

只有需要写入既有 MySQL 业务表或从 `company_test` 读取任务时才配置 `.env.local` 中的 `CREDITCHINA_DB_*`：

```bash
.venv/bin/python spider_main.py --init-db
.venv/bin/python spider_main.py --write-db --company "示例有限公司"
.venv/bin/python spider_main.py --db-source --db-limit 100 --workers 4
```

## 开发与验证

后端测试不访问官网，也不需要 MySQL：

```bash
.venv/bin/python -m unittest discover -s tests -v
```

前端检查：

```bash
cd frontend
npm test
npm run lint
```

主要目录：

```text
creditchina_merged/  Python 采集、存储、导出与 API
frontend/            Vite + React 管理看板
tests/               Python 单元测试
monitor_companies.txt 默认监控企业名单
output/              本机运行数据（自动生成，不交付）
```

## 注意事项

- 请遵守目标网站服务条款、robots 规则和适用法律，保持合理请求频率。
- 自动截图和 SHA-256 清单用于内部核验；正式诉讼或争议场景请由法务评估公证、可信时间戳或第三方电子存证。
- 网站接口或验证码规则可能变化。出现持续 `403`、`412` 或 `429` 时，系统会保留断点并停止或更换代理，不会用空结果覆盖上一份有效快照。
