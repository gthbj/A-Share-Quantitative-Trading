# PRD_20260520_11 — MaxCompute 缓存读取 end_date 裁剪丢数据修复

## 1. 元信息

| 项目 | 内容 |
|------|------|
| LLM 型号 | claude-sonnet-4-6 |
| 输出时间戳 | 2026-05-20 |
| 文档编号 | PRD_20260520_11 |
| 关联 Commit | — |
| 需求优先级 | P1（虚拟盘每日运行必现） |

---

## 2. 问题描述

### 现象

`PaperTrader.run_once(date=T)` 在本地缓存命中时，读取到的分钟行情**缺少当天（T 日）全部 K 线**，导致策略无法执行，输出日志"无任何当日行情，跳过"。

### 根因

`local_storage.LocalStorage.load_bars()` 对 `end_date` 的过滤逻辑如下：

```python
if end_date:
    df = df[df["date"] <= end_date]   # 例如 end_date = "20240115"
```

分钟级数据的 `date` 列格式为 12 位 `YYYYMMDDHHMM`，例如 `"202401150930"`、`"202401151500"`。

Python 字符串比较中：

```
"202401150930" <= "20240115"
→ 前 8 位相同，"202401150930" 继续延伸，判为更大
→ 结果：False → 该行被过滤掉
```

因此，T 日的所有分钟 Bar 全部被过滤，返回的 DataFrame 里没有 T 日数据。

相比之下，`maxcompute_source.get_bars()` 走**非缓存路径**时，第 419 行正确地用了：

```python
df["date"] <= str(end_date) + "9999"   # "202401159999"，包含当天全部分钟
```

缓存路径未做同样处理，形成不一致。

### 最小复现步骤

1. 任意运行一次回测，令本地生成分钟级缓存（例如 `510300.SH` 的 `15min` 数据）。
2. 调用：
   ```python
   from data_layer.local_storage import LocalStorage
   s = LocalStorage()
   df = s.load_bars("510300", start_date="20240101", end_date="20240115", period="15min")
   print(df[df["date"].str.startswith("20240115")])   # 期望有数据，实际为空
   ```
3. 可观察到返回结果中 20240115 当天数据全部缺失。

### 影响范围

- **回测**：影响轻微。回测一次性预加载长区间，end 通常在缓存中段，边界概率低。
- **虚拟盘**：每次 `run_once` 拉取 `[T - lookback_days, T]` 的窄区间，T 日即为 end_date，必然触发。

---

## 3. 影响模块声明

| 模块 | 是否受影响 |
|------|-----------|
| `data_layer` | ✅ 是（`local_storage.py`） |
| `engine` | 否 |
| `account` | 否 |
| `strategy` | 否 |
| `analytics` | 否 |
| `utils` | 否 |
| `config` | 否 |

**是否影响回测可复现性**：不影响（修复后缓存命中路径与非缓存路径行为一致，结果向正确值收敛）。

---

## 4. 关键文件路径与现有函数签名

```python
# data_layer/local_storage.py — 第 67-92 行

def load_bars(
    self,
    code: str,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    period: str = "daily",
    fmt: str = "parquet",
) -> pd.DataFrame:
    """读取单只股票K线数据，支持日期过滤。"""
    ...
    if start_date:
        df = df[df["date"] >= start_date]
    if end_date:
        df = df[df["date"] <= end_date]          # ← Bug 所在行
    return df.reset_index(drop=True)
```

```python
# data_layer/maxcompute_source.py — 第 390-393 行（缓存命中路径）

if cmin <= str(start_date) and cmax >= str(end_date):
    df = self.storage.load_bars(
        norm_code, start_date, end_date, period=period    # ← end_date 未加 "9999"
    )
```

```python
# data_layer/maxcompute_source.py — 第 419 行（非缓存路径，正确）

mask = (df["date"] >= str(start_date)) & (df["date"] <= str(end_date) + "9999")
```

---

## 5. 需求详情

### 功能目标

修复 `load_bars()` 的 `end_date` 过滤逻辑，使其对分钟级（12 位日期）和日线（8 位日期）均正确截止。

### 修改方式

将 `end_date` 过滤由严格字符串匹配改为加 `"9999"` 后缀比较：

```python
if end_date:
    df = df[df["date"] <= end_date + "9999"]
```

**为什么对日线也安全**：

| 场景 | 修改前 | 修改后 |
|------|--------|--------|
| 日线，end_date="20240115"，该日有数据 `"20240115"` | `"20240115" <= "20240115"` ✓ | `"20240115" <= "202401159999"` ✓ |
| 日线，隔天 `"20240116"` | `"20240116" <= "20240115"` ✗ 正确排除 | `"20240116" <= "202401159999"` ✗ 正确排除（"6">"5"） |
| 分钟线，end_date="20240115"，当天首 bar `"202401150930"` | `"202401150930" <= "20240115"` ✗ **错误排除** | `"202401150930" <= "202401159999"` ✓ 正确保留 |
| 分钟线，次日首 bar `"202401160900"` | `"202401160900" <= "20240115"` ✗ 正确排除 | `"202401160900" <= "202401159999"` ✗ 正确排除 |

---

## 6. 配置变更

无。

---

## 7. 不可改动的红线区域

- `load_bars()` 的方法签名（参数名、类型、返回值）不可变更
- `load_bars_raw()`、`save_bars()`、`save_stock_list()`、`load_stock_list()` 不可改动
- `maxcompute_source.py` 中除注释外不做任何修改
- 回测引擎、策略层、账户层代码不可触碰

---

## 8. 修改范围与位置

**主要修改文件**：`data_layer/local_storage.py`

- 方法：`load_bars()`
- 位置：`if end_date:` 分支，第 91 行
- 改动：`df["date"] <= end_date` → `df["date"] <= end_date + "9999"`

**同步更新文档**：
- `TODO.md`：将 TBD-5 移入"已修复"表
- `ARCHITECTURE.md`：无结构变化，无需更新

**不修改的文件**：除上述两个文档外，所有其他文件均不改动。

---

## 9. 验收标准

**AC-1：分钟线缓存命中时末日数据完整**

```
前置：本地有 510300_15min.parquet，包含 20240101 ~ 20240115 的数据
输入：load_bars("510300", "20240101", "20240115", period="15min")
修改前：返回的 DataFrame 中，date 以 "20240115" 开头的行为 0 条
修改后：返回的 DataFrame 中，date 以 "20240115" 开头的行 > 0 条
```

**AC-2：日线数据过滤行为不变**

```
前置：本地有 510300.parquet，包含 20240101 ~ 20240120 的日线数据
输入：load_bars("510300", "20240101", "20240115", period="daily")
修改前：返回 20240101 ~ 20240115，共 N 行
修改后：返回同样的 N 行，无多余数据（20240116 不被包含）
```

**AC-3：end_date 为 None 时行为不变**

```
输入：load_bars("510300", "20240101", end_date=None, period="15min")
修改前后：均返回 20240101 之后所有数据，无变化
```

---

## 10. 备注

- 本修复不影响 `load_bars_raw()`（该方法不做日期过滤，直接返回全量缓存，无 bug）。
- `start_date` 的过滤逻辑（`>=`）对日线和分钟线均正确，无需修改。
- 修复后，回测缓存命中路径与非缓存路径的行为完全一致，消除潜在的结果不一致风险。
