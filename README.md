# 联动游客承载预约与应急疏散基础平台

本项目是一套可离线运行的 Python 服务端平台，供国家森林公园管理局、保护站、生态监测人员和巡护队管理野生动植物观察、采样标本、保护站资源、巡护路线、风险告警与处置工单。业务状态、角色权限、幂等结果和审计事件保存在 SQLite 中，可在单个 Linux 应用容器内运行。

## 目录

- src/collection_logistics/：保护站、巡护路线、应急资源、调拨计划和治理情景；
- src/taxonomy_lab/：调查协议、观察记录、异常复核、分析任务租约和生态结论；
- src/biosafety_ops/：园区监测、风险告警、处置工单和资源分配；
- src/carrying_orchestration/：国庆预约统一承载编排——分时入园名额、步道方向容量、交通接驳、停车区、重点人群协助、临时关闭窗口与气象预警纳入同一版本快照，生成带原因的保留/改签/释放/疏散方案，确认时原子占用关联资源，并输出可执行疏散批次与交接清单；
- fixtures/：离线验收使用的调查协议与结构化观察记录；
- tests/：领域规则、事务边界、权限、HTTP API 和命令行验收测试。

## 环境

- Linux
- Python 3.11 或更高版本
- 运行时只依赖 Python 标准库与 SQLite

## 测试

~~~bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
~~~

## 构建检查

~~~bash
python3 -m compileall -q src tests
~~~

## 离线验收

~~~bash
PYTHONPATH=src python3 -m collection_logistics.acceptance --workspace .
PYTHONPATH=src python3 -m taxonomy_lab.acceptance --workspace .
PYTHONPATH=src python3 -m biosafety_ops.acceptance
PYTHONPATH=src python3 -m carrying_orchestration.acceptance --workspace .
~~~

这些命令会在临时 SQLite 数据库中完成保护站和路线登记、资源调拨、生态观察分析、风险处置以及国庆承载编排与气象预警疏散，不访问外部网络。

## HTTP 服务

~~~bash
PYTHONPATH=src python3 -m collection_logistics.api --database park.sqlite3 --host 127.0.0.1 --port 8080
PYTHONPATH=src python3 -m taxonomy_lab.api --database ecology.sqlite3 --host 127.0.0.1 --port 8081
PYTHONPATH=src python3 -m biosafety_ops.api --database safety.sqlite3 --host 127.0.0.1 --port 8082
PYTHONPATH=src python3 -m carrying_orchestration.api --database carrying.sqlite3 --host 127.0.0.1 --port 8083
~~~

服务提供浏览器无关的 JSON 接口和健康检查。进程重启后可以继续读取 SQLite 中的业务状态与审计历史。

### 统一承载编排接口（端口 8083）

- `POST /capacity_versions`：登记分时入园名额、步道方向、摆渡车、停车区的容量版本（含来源修订）；
- `POST /closure_windows`：登记临时关闭窗口（生效时段内按 capacity_percent 压减容量）；
- `POST /weather_alerts`、`POST /weather_alerts/{id}/lift`：触发/解除气象预警，可按 `kind`、`kind|resource`、`kind|resource|scope` 粒度强制关闭容量来源；
- `POST /reservations`：提交预约（必须占用一个 entry-slot，可关联步道方向、摆渡车、停车区与重点人群协助需求，幂等键防重）；
- `POST /reservations/{id}/check_in`、`POST /reservations/{id}/assistance/{kind}`：检票入园、落实协助安排；
- `POST /adjustment_plans`：固化当前容量版本快照并生成带原因的保留/在园保留/改签/释放/疏散方案（重复输入回放同一方案）；
- `POST /adjustment_plans/{id}/confirm`：原子确认；确认瞬间重取快照，任一容量版本变化即整单失败、不写入任何占用；
- `GET /adjustment_plans/{id}`、`GET /snapshots/{revision}`、`GET /evacuation_batches/{id}`：查询决定依据的容量来源（版本号/来源修订/关闭窗口/预警）与仍未完成的安全动作；
- `POST /evacuation_batches/{id}/receipts`：交接步骤幂等回执；只有末步 `handover_received` 回执才释放名额，重复回执不重复释放。
