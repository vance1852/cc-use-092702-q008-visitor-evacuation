# 联动游客承载预约与应急疏散基础平台

本项目是一套可离线运行的 Python 服务端平台，供国家森林公园管理局、保护站、生态监测人员和巡护队管理野生动植物观察、采样标本、保护站资源、巡护路线、风险告警与处置工单。业务状态、角色权限、幂等结果和审计事件保存在 SQLite 中，可在单个 Linux 应用容器内运行。

## 目录

- src/collection_logistics/：保护站、巡护路线、应急资源、调拨计划和治理情景；
- src/taxonomy_lab/：调查协议、观察记录、异常复核、分析任务租约和生态结论；
- src/biosafety_ops/：园区监测、风险告警、处置工单和资源分配；
- src/visitor_orchestration/：国庆预约统一承载编排——分时入园名额、峡谷步道方向容量、摆渡接驳、停车泊位、重点人群协助与临时关闭窗口的同一版本快照，带原因的保留/改签/拒绝/疏散方案、原子占用、版本失效与应急疏散批次交接；
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
PYTHONPATH=src python3 -m visitor_orchestration.acceptance --workspace .
~~~

四条命令会在临时 SQLite 数据库中完成保护站和路线登记、资源调拨、生态观察分析、风险处置，以及国庆预约的统一快照编排、原子占用、容量版本失效和气象预警疏散交接，不访问外部网络。

### 承载编排关键规则

- 四类容量（entry_slot/trail/shuttle/parking）各自维护修订版本；每次编排生成内容寻址的统一版本快照，方案中的每个决定都带 `capacity_sources`（资源版本、名义与生效容量、叠加的关闭窗口、预警硬关闭标记）。
- 方案确认在单事务内原子占用整单的全部关联资源；确认时重新推导当前容量，任一关联资源的修订版本、生效容量、关闭窗口或预警状态相对快照发生变化，整单失败且不留下任何部分占用。
- 已入园游客在预警触发时只生成 `evacuate` 决定，绝不自动改签到未来时段；未入园游客才允许改签稍后时段或取消释放。
- 疏散按重点人群（医疗、轮椅、老人、童车、向导）优先编入可执行批次，随附五项交接清单；发车、抵达、交接逐批推进，只有完成交接才释放在园名额。
- 交接回执按幂等键回放首次结果，重复回执不会重复释放名额；查询结果同时给出仍未闭环的安全动作台账。

## HTTP 服务

~~~bash
PYTHONPATH=src python3 -m collection_logistics.api --database park.sqlite3 --host 127.0.0.1 --port 8080
PYTHONPATH=src python3 -m taxonomy_lab.api --database ecology.sqlite3 --host 127.0.0.1 --port 8081
PYTHONPATH=src python3 -m biosafety_ops.api --database safety.sqlite3 --host 127.0.0.1 --port 8082
PYTHONPATH=src python3 -m visitor_orchestration.api --database orchestration.sqlite3 --host 127.0.0.1 --port 8083
~~~

服务提供浏览器无关的 JSON 接口和健康检查。进程重启后可以继续读取 SQLite 中的业务状态与审计历史。
