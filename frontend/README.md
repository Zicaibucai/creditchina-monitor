# 管理看板前端

该目录是中建探员的信息中国信息采集看板，数据全部来自项目根目录的 Python API，不包含演示数据。

推荐在项目根目录运行 `python3 setup_project.py` 和 `python3 start_project.py`，由脚本统一安装、启动和停止前后端。

单独开发前端时：

```bash
npm ci
npm run dev
```

默认打开 `http://localhost:3000`，并连接当前访问主机的 `http://<主机>:8765/api/v1`。如果 API 位于其他地址，复制 `.env.example` 为 `.env.local`，设置 `NEXT_PUBLIC_CRAWLER_API_BASE` 后重新启动。

交付前检查：

```bash
npm test
npm run lint
```

`dist/`、`node_modules/`、`.vinext/` 和 `.wrangler/` 均为可再生成内容，不应交付或提交。
