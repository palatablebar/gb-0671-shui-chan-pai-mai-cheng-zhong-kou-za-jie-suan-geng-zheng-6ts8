# 深度登记 水产拍卖结算

规模：中型。使用者：水产批发市场结算员。核心规则：先毛皮配对，再按校准异常时段做开拍准入（异常货包复秤后才上板），再扣杂计价，按更正链选择唯一生效版，只支付差额。

## 功能模块

| 模块 | 使用者动作 | 入口 | 核心文件 | 测试 |
|---|---|---|---|---|
| M1 称重采信 | 查看并执行称重采信 | `POST /api/intake` | `app/modules/intake/rules.py` | `tests/test_intake.py` |
| M2 货品换算 | 查看并执行货品换算 | `POST /api/conversion` | `app/modules/conversion/rules.py` | `tests/test_conversion.py` |
| M3 毛皮配对 | 查看并执行毛皮配对 | `POST /api/pairing` | `app/modules/pairing/rules.py` | `tests/test_pairing.py` |
| M4 开拍准入 | 查看当天可开拍货包与卡点 | `POST /api/admission`、`POST /api/admission/switch` | `app/modules/admission/rules.py`、`app/admission_ledger.py` | `tests/test_admission.py` |
| M5 扣杂竞价 | 查看并执行扣杂竞价 | `POST /api/auction` | `app/modules/auction/rules.py` | `tests/test_auction.py` |
| M6 更正裁决 | 查看并执行更正裁决 | `POST /api/correction` | `app/modules/correction/rules.py` | `tests/test_correction.py` |
| M7 应付对账 | 查看并执行应付对账 | `POST /api/reconcile` | `app/modules/reconcile/rules.py` | `tests/test_reconcile.py` |
| M8 结算归档 | 查看并执行结算归档 | `POST /api/archive` | `app/modules/archive/rules.py` | `tests/test_archive.py` |

## 陷阱登记

| TRAP | 现象 | 朴素实现 | 本项目处理 | 处理位置 | 测试 |
|---|---|---|---|---|---|
| TRAP-01 | 旧编码中文票据 | 静默错算或丢失证据 | 显式归一与原因裁决 | `app/modules/intake/rules.py` | `tests/test_intake.py::test_legacy_encoding` |
| TRAP-02 | BOM表头 | 静默错算或丢失证据 | 显式归一与原因裁决 | `app/modules/intake/rules.py` | `tests/test_intake.py::test_bom_real` |
| TRAP-04 | 分段表头 | 静默错算或丢失证据 | 显式归一与原因裁决 | `app/modules/intake/rules.py` | `tests/test_intake.py::test_repeated_header_real` |
| TRAP-05 | 缺列记录 | 静默错算或丢失证据 | 显式归一与原因裁决 | `app/modules/intake/rules.py` | `tests/test_intake.py::test_bad_columns_real` |
| TRAP-07 | 空值哨兵 | 静默错算或丢失证据 | 显式归一与原因裁决 | `app/modules/conversion/rules.py` | `tests/test_conversion.py::test_sentinel_real` |
| TRAP-08 | 前导零票号 | 静默错算或丢失证据 | 显式归一与原因裁决 | `app/modules/intake/rules.py` | `tests/test_intake.py::test_leading_zero_real` |
| TRAP-14 | 重复编号不同原值 | 静默错算或丢失证据 | 显式归一与原因裁决 | `app/modules/intake/rules.py` | `tests/test_intake.py::test_duplicate_conflict_real` |
| TRAP-18 | 三类空态 | 静默错算或丢失证据 | 显式归一与原因裁决 | `app/modules/intake/rules.py` | `tests/test_intake.py::test_empty_states` |
| TRAP-15 | 吨与千克混合 | 静默错算或丢失证据 | 显式归一与原因裁决 | `app/modules/conversion/rules.py` | `tests/test_conversion.py::test_tonnes_counterexample` |
| TRAP-06 | 全角千分位 | 静默错算或丢失证据 | 显式归一与原因裁决 | `app/modules/conversion/rules.py` | `tests/test_conversion.py::test_fullwidth_thousands` |
| TRAP-10 | 金额半分舍入 | 静默错算或丢失证据 | 显式归一与原因裁决 | `app/modules/auction/rules.py` | `tests/test_auction.py::test_half_cent_rounding` |
| TRAP-26 | 陈旧更正分支 | 静默错算或丢失证据 | 显式归一与原因裁决 | `app/modules/correction/rules.py` | `tests/test_correction.py::test_stale_branch` |
| TRAP-27 | 迟到校准记录 | 重跑时用后来补录的异常替换已开拍依据，或把货包重复上板 | 场次按到达时点切分；已开拍整行写入冻结台账，重跑原样恢复，迟到记录仅留档 | `app/modules/admission/rules.py`、`app/admission_ledger.py` | `tests/test_admission.py::test_late_calibration_cannot_swap_basis` |
| TRAP-28 | 未确认货包混入竞价 | 把暂缓货包一并送进竞价或计价总额 | 未过准入货包单列，`settlements` 排除、`unadmitted` 展示卡点 | `app/modules/auction/rules.py` | `tests/test_admission.py::test_unadmitted_excluded_from_auction` |
| TRAP-29 | 切换失败丢状态 | 切换方案失败后原准入被覆盖或证据被删 | 先算候选+`check_switch` 校验，失败 409 不落库；台账与证据账只增不改 | `app/modules/admission/handler.py`、`app/modules/admission/rules.py` | `tests/test_admission.py::test_switch_api_keeps_original_on_failure` |
| TRAP-30 | 同批重跑重复开拍 | 重跑把已开拍货包重新分配给别场或重复出现 | 台账按 `scope` 冻结，重跑恢复原场次与原依据；切换校验拒绝撤板/换场/换依据/重复 | `app/admission_ledger.py` | `tests/test_admission.py::test_rerun_keeps_board_and_run_id`、`::test_switch_guard_rejects_moving_opened` |

## 准入判定摘要

- 异常时段：原称重 `weighed_at` 落在同秤台 `anomaly` 区间内。场次只认开拍前到达的校准记录，迟到记录不回改已开拍场次。
- 复秤要求：复秤须在开拍前到达、自身不落在已知异常区间；否则货包留在暂缓清单。
- 冻结：已开拍结论整行入账（`row_json`），重跑按台账恢复；原始称重证据全部留存，复秤只追加。
- 策略：`staged` 未受影响先开拍；`whole` 等到本场所有「有配对」货包确认才开拍。两方案在同一批证据上做对照推演。
- 失败保护：切换校验不通过返回 409，不写入候选结果，原准入状态与原始证据不变。

## 对账规则

输入=采纳原始行+隔离行+空行+同值重复行+重复表头。计价版本总额与生效票总额分别展示，避免把更正前后重复求和。最终应付=已付+待付，负待付为退款。竞价与计价范围只含已开拍货包；暂缓货包在 `unadmitted` 单列。

## 已知不覆盖

无称重设备实时接入，采用现场导出文件。无支付执行，只生成可复核结算证据。
