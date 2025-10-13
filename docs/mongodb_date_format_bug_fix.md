# MongoDB 日期格式 Bug 修复文档

## 📋 问题描述

用户报告在分析股票时，后端日志显示：

```
2025-10-12 19:12:36,788 | dataflows | WARNING | ⚠️ [数据来源: MongoDB] 未找到daily数据: 601288，降级到其他数据源
2025-10-12 19:12:36,790 | dataflows | ERROR | 🔄 mongodb失败，尝试备用数据源获取daily数据...
```

但是，MongoDB 的 `stock_daily_quotes` 集合中**确实存在** 601288 的 daily 数据。

## 🔍 问题分析

### 1. 数据存在性验证

通过 MongoDB 客户端查询，确认数据存在：

```javascript
db.getCollection("stock_daily_quotes").find({ "symbol": "601288" }).limit(1000).skip(0)
```

结果显示：
- ✅ 有大量 601288 的数据
- ✅ `period` 字段为 `"daily"`
- ✅ `trade_date` 字段为字符串格式（如 `"2024-10-09"`）

### 2. 查询失败原因

通过调试脚本发现，问题出在 `app/routers/stocks.py` 第 202 行：

**错误代码**：
```python
start_date = (datetime.now() - timedelta(days=limit * 2)).strftime("%Y-%m-d")
#                                                                         ↑
#                                                                    少了一个 %
```

**正确代码**：
```python
start_date = (datetime.now() - timedelta(days=limit * 2)).strftime("%Y-%m-%d")
#                                                                         ↑
#                                                                    应该是 %d
```

### 3. Bug 影响

**错误格式示例**：
- 应该是：`"2025-03-26"`
- 实际是：`"2025-03-d"`

这导致 MongoDB 查询条件变成：

```python
{
    "symbol": "601288",
    "period": "daily",
    "trade_date": {"$gte": "2025-03-d", "$lte": "2025-10-12"}  # ❌ 错误的日期格式
}
```

由于 `"2025-03-d"` 不是有效的日期字符串，MongoDB 的字符串比较会失败，导致查询不到任何数据。

## ✅ 修复方案

### 修改的文件

**文件**：`app/routers/stocks.py`

**修改位置**：第 202 行

**修改内容**：

```python
# 修复前
start_date = (datetime.now() - timedelta(days=limit * 2)).strftime("%Y-%m-d")

# 修复后
start_date = (datetime.now() - timedelta(days=limit * 2)).strftime("%Y-%m-%d")
```

## 🧪 测试验证

### 测试脚本

创建了 `scripts/test_date_format_fix.py` 测试脚本。

### 测试结果

```
❌ 修复前的格式（错误）：
  end_date: 2025-10-12
  start_date: 2025-03-d
  ⚠️ start_date 格式错误！应该是 YYYY-MM-DD，实际是 YYYY-MM-d

✅ 修复后的格式（正确）：
  end_date: 2025-10-12
  start_date: 2025-03-26
  ✅ start_date 格式正确！

✅ 查询成功！找到 131 条数据
  日期范围: 2025-03-26 ~ 2025-10-09

✅ 适配器查询成功！找到 131 条数据
  日期范围: 2025-03-26 ~ 2025-10-09
```

## 📊 修复效果

### 修复前

- ❌ MongoDB 查询失败（日期格式错误）
- ❌ 降级到外部 API（akshare）
- ❌ 查询速度慢
- ❌ 日志显示警告和错误

### 修复后

- ✅ MongoDB 查询成功
- ✅ 直接使用缓存数据
- ✅ 查询速度快
- ✅ 无警告和错误

## 🔍 根本原因

这是一个**拼写错误**（typo）：

- Python 的 `strftime` 格式化字符串中：
  - `%Y` = 4位年份（如 2025）
  - `%m` = 2位月份（如 03）
  - `%d` = 2位日期（如 26）

- 错误写成 `"%Y-%m-d"` 会被解释为：
  - `%Y` = 2025
  - `%m` = 03
  - `-d` = 字面量字符串 "d"（不是格式化符号）

## 💡 预防措施

### 1. 代码审查

在代码审查时，特别注意日期格式化字符串的正确性。

### 2. 单元测试

为日期格式化添加单元测试：

```python
def test_date_format():
    """测试日期格式化"""
    limit = 100
    start_date = (datetime.now() - timedelta(days=limit * 2)).strftime("%Y-%m-%d")
    
    # 验证格式
    assert len(start_date) == 10  # YYYY-MM-DD 长度为 10
    assert start_date[4] == '-'
    assert start_date[7] == '-'
    assert start_date[-1].isdigit()  # 最后一个字符应该是数字，不是 'd'
```

### 3. 类型检查

使用类型提示和静态分析工具（如 mypy）来检测潜在的格式错误。

### 4. 日志验证

在查询前记录日期参数，方便调试：

```python
logger.debug(f"📅 查询日期范围: {start_date} ~ {end_date}")
```

## 📝 相关文件

1. ✅ `app/routers/stocks.py` - 修复日期格式错误
2. ✅ `scripts/test_date_format_fix.py` - 测试脚本
3. ✅ `scripts/debug_mongodb_query.py` - 调试脚本
4. ✅ `scripts/debug_mongodb_daily_data.py` - 数据验证脚本

## 🎯 总结

这是一个简单但影响重大的 Bug：

- **原因**：日期格式化字符串拼写错误（`%Y-%m-d` 应该是 `%Y-%m-%d`）
- **影响**：导致 MongoDB 查询失败，系统降级到外部 API
- **修复**：修正格式化字符串
- **验证**：通过测试脚本验证修复效果

修复后，MongoDB 缓存功能恢复正常，查询速度显著提升！🎉

## 📅 修复日期

2025-10-12

