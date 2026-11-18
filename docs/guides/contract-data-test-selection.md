# `state:modified.contract.data_tests`：按契约变化选择数据测试

## 背景

`state:modified.contract` 只能指出**哪个模型**的契约变了；测试选择随后经
indirect selection 按模型展开——`orders.amount` 改了类型，`orders.status` 上
独立的 `not_null` 也会重跑。本改动新增一种 state 选择方法，把选择粒度细化到
**列**，同时能找出写在别张表上、却引用了变化列的检查（如 `relationships` 的
引用端）。

用法：

```bash
# 预览将要运行的数据测试（只依赖本地项目与状态文件）
dbt ls --select "state:modified.contract.data_tests" --state <上一次发布的产物目录>

# 在同一份状态下实际运行这些测试
dbt test --select "state:modified.contract.data_tests" --state <同上>
```

该选择器只选中数据测试（data tests），可以与现有的交集（`,`）、排除
（`--exclude`）、图操作符、`--resource-type` 等组合使用。
`state:modified.contract` 与默认测试选择的行为不变。

## 契约差异怎么算

对照 `--state` 中的 manifest 与本次项目的 manifest，**只比较两版都存在且启用
的模型**。模型按 unique_id 匹配，因此同名但来自不同包、或同一模型的不同版本，
都各自独立比较。新增/删除/禁用的模型不在本方法范围内（由 `state:new` /
`state:modified` 等覆盖）。

- **按列计**：契约处于启用状态的模型，其列的新增、删除、`data_type` 变化
  （比较前会做大小写与空白规范化，`INTEGER` 与 `integer` 不算变化）、列级约束
  变化（约束列表按集合比较，与书写顺序无关）。
- **按整张模型计**：契约开关切换（启用 ↔ 停用）、模型级约束（`constraints`）
  变化。此时该模型的所有列都视为已变化。
- **不计**：只改描述、调整 YAML 中列的声明顺序、修改模型 SQL、tags/meta 等，
  不会选中任何测试。
- 快路径：两版 `contract.checksum` 相同则直接跳过（与
  `state:modified.contract` 的"无变化"判定保持一致）。

`--state` 目录中的 manifest 可以是未修改的 dbt-core 1.10.15 保存的产物（本改动
不向 manifest 写入任何新字段）。读不到它（目录中没有 `manifest.json` 或文件不
可解析）时会给出明确错误，提示 `--state` 需要指向包含可读 `manifest.json` 的
目录。

## 测试读哪些列：列依赖从哪来

不引入新的解析阶段，完全复用现有解析产物与 manifest 依赖信息：

- `GenericTestNode.test_metadata`（`name` / `namespace` / `kwargs`）、
  `config.where` / `config.fail_calc`、`depends_on.nodes` / `depends_on.macros`、
  `attached_node`，以及当前 manifest 的 `macros`。

满足以下全部条件的测试称为**列清晰**，可以精确到列地排除：

1. 是内置 `not_null`、`unique` 或 `relationships`（`namespace` 为空或为 `dbt`）；
2. 没有 `where` 配置、没有非默认的 `fail_calc`；
3. 对应的测试宏没有被项目或依赖包覆盖（包括 `test_not_null` 这类直接覆盖和
   `duckdb__test_not_null` 这类适配器 dispatch 覆盖；`dbt` 全局项目与适配器
   自带包 `dbt_<adapter_type>` 除外）；
4. `column_name`（以及 `relationships` 的 `field`）是简单标识符，不是表达式；
5. `attached_node` 是模型；`relationships` 还要求 `depends_on` 中恰好能解析出
   另一个模型作为引用端。

列清晰的测试，当其读取的 `(模型, 列)` 与变化集合相交时被选中。
`relationships` 同时读取两端的列，**哪一端变化都会选中同一个测试**。

## 证据不足时回退到多大范围

以下测试**读列不可知**，退化为按依赖图保守选择：singular test、自定义 generic
test、带 `where` / 宏覆盖 / 表达式参数的内置测试。

保守规则：只要任一发生变化的模型是该测试在**当前依赖图**中的祖先（直接依赖
或传递依赖），就无法证明该测试与变化无关，予以保留；完全独立的分支不必运行。
同一测试被多条路径命中只选中一次（集合语义）。

整模型级变化（开关切换、模型级约束）等价于该模型所有列变化：模型上的列清晰
测试全部选中，下游不可知测试同样按图传递保留。

## 分析什么时候做、成本怎么控制

- **惰性、按需**：分析只在选择器被调用时进行（`dbt ls` / `dbt test` 等使用该
  选择器时）。日常解析、`dbt run`、不使用该选择器的命令完全不受影响；
  manifest 不新增字段，解析与产物零额外成本。
- **计算量**：
  - 模型契约 diff 为 O(模型数 × 列数)，且 `checksum` 相同的模型 O(1) 跳过；
  - 测试分类为 O(测试数) 的字段查找；
  - 下游闭包仅在"确有契约变化"且"存在不可知测试"时，从变化模型出发沿
    `child_map` 做一次 BFS，规模限于变化的下游子图；
  - 每次选择调用只计算一次（结果缓存在选择器实例上）。
- **partial parsing 一致性**：选择结果是两版 manifest 的纯函数，不依赖解析
  路径。改过测试参数、`where` 或宏之后，沿用 partial parsing 缓存与从头解析
  得到的测试集合一致（已验证）。

## 哪些情况下会多跑测试（保守边界）

宁可多跑也不错漏，以下情况会选择超出严格必要范围的测试：

- 项目或依赖覆盖了内置测试宏 → 该内置测试全部退化为保守选择（例如覆盖了
  `test_not_null`，则所有 `not_null` 按模型/下游保守选中）；
- 测试带 `where`、非默认 `fail_calc` 或表达式列参数 → 该测试退化；
- 自定义 generic test 与 singular test → 退化；变化模型的**全部下游**不可知
  测试都会被保留（传递闭包）；
- 契约开关切换或模型级约束变化 → 该模型上的所有测试；
- 适配器自带包的识别基于 `dbt_<adapter_type>` 命名约定，识别不了的适配器实现
  会被当作覆盖 → 退化（多跑，不会少跑）。

一个有意的精度取舍：列清晰的测试**不**沿依赖图向下游传递。例如
`orders.amount` 变化时，`order_summary.total` 上的 `not_null`（读的是未变化
模型的未变化列）不会运行。判据是"测试 SQL 实际读取的列集合与变化列集合不相
交"。如果下游模型的列在数据上由上游列推导而来（SQL 血统），这类影响不在契
约范围内；需要覆盖该风险的，应对下游模型也启用契约，或改用
`state:modified.contract+` 这类按模型展开的既有选择。

## 验证

Python 3.12 + 本地 DuckDB 项目，两版独立 manifest（对照 manifest 由未修改的
dbt-core 1.10.15 生成）：

- 只改 `amount` 列类型；
- `relationships` 两端分别单独变化（选中同一个测试）；
- 宏覆盖、`where` 无法判明（退化为保守选择）；
- 影响沿依赖图传到下游（singular test 直接/传递下游）；
- 完全无关的分支（不运行）；
- 契约开关、模型级约束、列增删、仅描述/列顺序/SQL 变化；
- 每个场景均核对 `dbt ls` 列出的测试 ID 与 `dbt test` 实际执行的 ID 一致；
- 改测试参数 / `where` / 宏后，partial parsing 缓存与全量解析的选择结果一致。
