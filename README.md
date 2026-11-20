# SOLO 流式字符编码服务、原件保真修订库与纸带复用配料

本题面定义的 SOLO 编码（仅实现题面格式，不接入任何外部编码标准）的 Python 3.11
实现，在其之上构建的、按字符区间打补丁但保持未修改原始字节的修订库，以及把固定
修订版本当作实物孔谱、为整批订单选择旧带胚/新带并交付补孔机单的纸带复用配料模块。

## 交付物

| 路径 | 内容 |
| --- | --- |
| `solo/codec.py` | 流式解码器 `Decoder`、检查点 `Checkpoint`、整件解码 `decode_document` / `decode_all` / `iter_decode`、独立编码入口 `encode_text` / `encode_document`，错误类型与路径记录 |
| `solo/revision.py` | 修订库 `RevisionLibrary`、`replace` / `preserve` 补丁、版本与批次记录、边界模式调整、字节保真校验 |
| `solo/sample.py` | 确定性合成原件生成器（ESC/PAIR 故意跨 128 KiB 块，含模式切换、空替换、连续补丁与非法尾部所需素材） |
| `solo/tape.py` | 孔谱 `HoleSpectrum`（节距/孔道、段查询、修订溯源）、带胚 `TapeBlank`、订单 `TapeOrder`、倒装几何、精确配料 `plan_batch` / `MaterialPlanner`、补孔机单 `MachineOrder` |
| `solo/demo.py` | 演示：真实原件、两批修订、未修改字节核对、错误报告、较大语料，写出 `artifacts/` |
| `solo/tape_demo.py` | 纸带演示：题面 X/Y 示例及两个新带变体、真实修订溯源、仅倒装、同文本异字节，写出可回放机单到 `artifacts/tape/` |
| `solo/__main__.py` | CLI：`decode/check/encode/revise/demo/tapedemo` |
| `tools/replay_order.py` | **零项目依赖**的独立小读者（仅标准库）：回验孔谱摘要、逐孔回放机单、把机器返回坐标落回版本段记录与溯源 |
| `tests/test_solo.py` | 独立参考状态机、差分测试、跨块、检查点、修订、规模测试 |
| `tests/test_tape.py` | 孔谱/段/溯源、倒装几何、配料优化对暴力枚举的差分、独立读者子进程回放、上限规模测试 |
| `artifacts/` | 运行 `python3 -m solo.demo` 与 `python3 -m solo tapedemo` 生成的真实原件、版本字节、孔谱、库存、机单与清单 |

## SOLO 编码（题面定义）

- 正文从 `SINGLE` 模式开始。
- `SINGLE`：`00..7F` → 同码点 ASCII；`FF` 为 ESC；`80..FE` 非法。
- ESC 后单字节指令：`01` → 切到 `PAIR`；`00` → 切回 `SINGLE`；`FF` → 字面
  `U+00FF` 且不改模式；其它 → 错误。模式切换本身不产字符。
- `PAIR`：除 `FF`（仍是 ESC）外，两个字节组成无符号大端整数 `n`，映射
  `U+4E00 + n`，限定 `0 ≤ n ≤ 0x0FFF`。第一字节正常时，第二字节即使是 `FF`
  也属于该数值（`00 FF` = n=0x00FF），不会重新开转义。
- 正文结束遇到半个 PAIR 或半条 ESC 均为错误。
- 错误报告原字节偏移与到达该状态的路径（`DecodeError.offset` / `.path`），绝不
  用 `?` 替换后继续。解码失败后解码器进入失败态，不会假成功。

## 流式语义

`Decoder` 按任意字节块增量运行：`feed()` / `feed_into()`，偏移始终是原件正文
偏移。跨块半条 ESC、半个 PAIR 会被保存为待续状态，下一块首字节续接，因此不会
像旧的按块重新初始化解码器那样丢掉块首字符。`finish()` 校验正文末尾并返回结束
模式。

### 检查点

`Decoder.checkpoint()` 返回 `Checkpoint`，包含位置、模式、待续状态与**前缀
SHA-256**；`Checkpoint.resume(body)` / `Decoder.from_checkpoint()` 只在给定正文
前缀摘要一致时才从检查点继续（`verify=False` 可跳过，供已自行校验前缀的调用方
使用）。`Version.checkpoint_at(byte_offset)` 可直接从某个版本继续解码。

## 修订库

- 版本是不可变的真实原件字节 + 有序补丁批次；一个库先经 `add_original()` 载入
  原件（会完整解码校验，失败即拒绝）。
- `replace(start_char, end_char, text)`：按某固定原件版本的**字符**半开区间替换
  为 Unicode 文本。`preserve(start_byte, end_byte)`：要求该**字节**区间原样保留
  （可以落在字符内部；包含它的字符因此不可被替换；若边界落在纯切换字节上则拒绝）。
- 一批补丁先校验：同一基准版本、替换区间两两不相交（相邻允许）、preserve 区间
  互不重叠且不被替换触碰、替换文本可编码；任一失败则整批不发布。
- 组装：未替换字符按原字节复制（`kept_runs` 记录旧字符区间与新字节区间的对应），
  只对替换文本编码；当前输出模式与下一段原始字节解码模式不一致时，才在拼接边界
  插入合法切换（`BoundaryAdjustment`，`splice` / `boundary` / `final`）。整文件
  从不重新编码，因此不会掩盖原件差异。
- 新版本结束模式强制为 `SINGLE`，不会给后续保留字节留下悬空模式。
- 发布前独立校验：重新流式解码新正文，逐段核对未替换区间字节一致、preserve
  区间逐字节一致，并核对解码文本等于“旧 Unicode 做同批替换”的结果。全部通过
  才登记新版本；否则抛出 `RevisionError`，库不变化。

### 版本记录

`Version.records` / `BatchRecord` 记录每个替换的旧字符区间、旧字节区间、新字符
区间、新字节区间、新增字节与边界切换；`boundary_adjustments` 记录新字节偏移、
前后模式和切换字节；`kept_runs` 与 `dropped_switch_spans` 记录原样复制和未保留的
纯切换跨度。

## 纸带孔谱与复用配料

### 孔谱（资料室，按固定修订版本签出）

- 一个正文字节占一个**节距**；字节低位到高位对应孔道 0..7，置一为孔。开头/末尾
  实际保留的零字符切换指令也逐字节占节距，没有例外。
- `build_spectrum(library, version_id)` 生成不可变 `HoleSpectrum`，每个节距都可
  查回：所属段（可见字符及其内部字节偏移、解码模式，或零字符切换及前后模式）、
  字节值、孔道集合、以及沿整棵修订链的**溯源终点**。
- 溯源终点三类：`original`（原件版本的真实字节）、`boundary`（某批修订在边界
  补入的切换，带 splice/boundary/final 角色）、`new_text`（替换文本编码出的新增
  正文，带补丁字符区间与段内字节序号）。`spectrum.trace(pitch)` 逐跳给出
  kept→kept→…→终点。
- 一切以**原件版本的实际字节**为准：孔谱内嵌正文 SHA-256，`HoleSpectrum.load()`
  独立加载时重新哈希并逐节距核对孔道；正文被改动即拒绝。两条带解码出同一 Unicode
  但字节不同（例如 `41` 与 `ff00 41` 同解为 `A`）时孔谱不同、带胚不能互换。

### 带胚、订单与装夹（复刻间）

- `TapeOrder` 是一张连续目标孔谱，并声明 `allow_reverse`。`TapeBlank` 是架上
  部分打孔的实物带胚。
- 一条带胚至多切给一个订单，可在任一连续窗口取材，窗外节距计为报废。正装逐节距
  对齐；倒装同时**反转节距次序和八个孔道次序**（`reverse_byte` 查表）。
- 可行性判据：窗口内旧孔必须是目标对应位置孔集合的**子集**（孔能补、不能堵）。
  补孔坐标一律以**带胚原坐标**返回；`MachineOrder.locate_returned(pitch, channel)`
  再把机器返回的坐标落回该版本的目标节距、孔道、字节、段记录与溯源。
- 另有足量按订单长度裁好的全空新带；新带机单上每个目标孔都是一次补孔。

### 整批优化（精确解，非启发式）

`plan_batch(orders, blanks)` 用最小费用最大流求严格字典序目标：

1. 新带条数最少；
2. 总补孔数最少；
3. 报废旧带节距最少；并列时任选（按带胚/订单编号确定化）。

上限：24 个订单、40 条旧带、每条 2000 节距，超限在批次边界直接拒绝。
`MaterialPlanner` 缓存已签出的孔谱：现场换一批带胚后再次 `plan()` 只重跑配料，
不会重新解码任何版本。

### 机单与独立小读者

`MachineOrder.dump()` 写出自描述 JSON（`solo-punch-order-v1`），含固定版本摘要、
装夹方向、窗口、旧孔证据、补孔清单与目标孔表。`tools/replay_order.py` 不 import
本项目任何模块，仅凭标准库即可：

```bash
python3 tools/replay_order.py verify-spectrum SPECTRUM.json
python3 tools/replay_order.py replay ORDER.json --spectrum SPECTRUM.json --stock stock.json
python3 tools/replay_order.py replay-batch artifacts/tape/2_revision
```

回放会重建每条实物带：校验旧孔子集、确认补孔只增不堵、成品逐字节等于固定版本，
并把每个孔映射回字符/切换段与溯源；篡改正文摘要或加入目标外孔均以退出码 2 拒绝。

## 运行

```bash
python3 -m unittest discover -s tests -v      # 全部测试
python3 -m solo.demo                          # 编码/修订演示，写 artifacts/
python3 -m solo tapedemo                      # 纸带配料演示，写 artifacts/tape/
python3 -m solo check artifacts/original.bin
python3 -m solo decode --checkpoint 10 artifacts/version_1.bin
python3 -m solo revise artifacts/original.bin patches.json > new.bin
python3 tools/replay_order.py replay-batch artifacts/tape/1_statement
```

`patches.json` 格式：

```json
{"patches": [
  {"replace": [0, 3, "新文本"]},
  {"preserve": [100, 200]}
]}
```

## 测试覆盖

- 1/2/7/4096 字节分块对独立朴素状态机逐字符、逐字节区间核对（含非法输入与
  错误偏移）。
- 生成器把 ESC/PAIR 精确放在 128 KiB 边界上；跨块、模式切换无字符、替换为空、
  连续/相邻补丁、非法尾部均有专项测试。
- 修订：未修改区间字节等同、每版本可重开解码、失败批次不发布、preserve 逐字节
  保留、新文本等于同批替换、无悬空模式。
- 规模：10 MiB 原件、一批 100 个补丁（`ScaleTests`），未修改运行按原字节复制。
- 纸带：孔道/倒装位运算真值表；孔谱段划分、独立加载与摘要篡改拒绝；修订链
  original/boundary/new_text 三类溯源与多跳 kept 链；正装/倒装窗口枚举与子集
  冲突；配料三级目标对 60 组随机小实例的**暴力枚举**差分；题面 X/Y/U/V 示例及
  两个新带变体；同文本异字节不可互换；独立读者子进程回放（含篡改拒绝）；24 订单
  ×40 带胚×2000 节距上限批次端到端。
