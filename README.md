# BakeOven

烘焙占炉排程：发酵+烘烤半开区间占用炉位，冲突检测与下一可开工窗口。

## 启动

```bash
docker compose up --build
```

| 服务 | 地址 |
| --- | --- |
| 前端 | http://localhost:4500 |
| API | http://localhost:9500 |
| API 文档 | http://localhost:9500/docs |
| Postgres | localhost:5446 |

健康检查：`GET http://localhost:9500/api/health`

## 页面

- `/products` — 产品
- `/ovens` — 炉位
- `/batches` — 批次
- `/gantt` — 甘特
- `/conflicts` — 冲突
- `/windows` — 可开工

## 使用说明

1. 查看产品配方时长与炉位。
2. 创建生产批次，系统按半开区间占炉并检测冲突。
3. 甘特查看占用；冲突与可开工窗口辅助排产。

## 排炉核对

可重复执行的核对，分采集（批次端点、甘特色块、重叠判断三处取数）、判定（端点与色块一致、端点相接不误报、半开真重叠必找到）、入口三步：

```bash
docker compose exec api python -m app.checks              # 核对当前库
docker compose exec api python -m app.checks --fixture tests/fixtures/overlap_two_batches.json
```

退出码：`0` 通过；`1` 发现半开真重叠（输出点名两个批次号）；`2` 采集缺端点或色块（输出写明缺哪项）；`3` 端点与色块不一致 / 相接被误报 / 真重叠没找到。

## 开发与测试

```bash
docker compose exec api pytest -q
```
