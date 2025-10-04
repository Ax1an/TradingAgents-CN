// 市场参数规范化：将各类板块/交易所/缩写统一为分析模块支持的 A股/美股/港股
export const normalizeMarketForAnalysis = (market: any): string => {
  const raw = String(market ?? '').trim()
  const upper = raw.toUpperCase()
  const cn = raw
  const isA = [
    'A股', '主板', '创业板', '科创板', '中小板', '沪市', '深市', '上交所', '深交所', '北交所'
  ].includes(cn) || ['CN', 'SH', 'SZ', 'SSE', 'SZSE'].includes(upper)
  const isHK = ['港股', '港交所'].includes(cn) || ['HK', 'HKEX'].includes(upper)
  const isUS = ['美股', '纳斯达克', '纽交所'].includes(cn) || ['US', 'NASDAQ', 'NYSE', 'AMEX'].includes(upper)
  if (isA) return 'A股'
  if (isHK) return '港股'
  if (isUS) return '美股'
  // 默认按A股处理
  return 'A股'
}

/**
 * 将交易所代码转换为市场类型
 * @param exchangeCode 交易所代码（如 "sz", "sh", "hk", "us"）
 * @returns 市场类型（"A股", "港股", "美股"）
 */
export const exchangeCodeToMarket = (exchangeCode: string): string => {
  const code = String(exchangeCode ?? '').toLowerCase().trim()

  // A股交易所代码
  if (['sz', 'sh', 'bj', 'sse', 'szse', 'bse'].includes(code)) {
    return 'A股'
  }

  // 港股交易所代码
  if (['hk', 'hkex'].includes(code)) {
    return '港股'
  }

  // 美股交易所代码
  if (['us', 'nasdaq', 'nyse', 'amex'].includes(code)) {
    return '美股'
  }

  // 默认返回A股
  return 'A股'
}

/**
 * 根据股票代码判断市场类型
 * @param stockCode 股票代码
 * @returns 市场类型（"A股", "港股", "美股"）
 */
export const getMarketByStockCode = (stockCode: string): string => {
  const code = String(stockCode ?? '').trim()

  // A股：6位数字
  if (/^\d{6}$/.test(code)) {
    return 'A股'
  }

  // 港股：数字+.HK 或 纯数字（4位）
  if (code.endsWith('.HK') || /^\d{4,5}$/.test(code)) {
    return '港股'
  }

  // 美股：字母开头
  if (/^[A-Z]+$/.test(code.toUpperCase())) {
    return '美股'
  }

  // 默认返回A股
  return 'A股'
}

export default {
  normalizeMarketForAnalysis,
  exchangeCodeToMarket,
  getMarketByStockCode
}

