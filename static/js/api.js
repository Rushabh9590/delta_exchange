/**
 * ==============================================================================
 * DELTA EXCHANGE - REST API CLIENT
 * Centralized API communication layer for Dashboard, Positions, Tickers, Multi-Leg
 * ==============================================================================
 */

const ApiService = {
  /**
   * Fetches consolidated dashboard data (wallet + positions + summary)
   */
  async getDashboard() {
    const res = await fetch('/api/dashboard');
    return await res.json();
  },

  /**
   * Fetches live ticker quote for high-frequency price streaming
   * @param {string} underlyingOrSymbol - e.g. 'BTC', 'ETH' or 'C-BTC-89200-051026'
   */
  async getTicker(underlyingOrSymbol = 'BTC') {
    const isFullSymbol = underlyingOrSymbol.includes('-') || underlyingOrSymbol.endsWith('USD');
    const param = isFullSymbol ? `symbol=${encodeURIComponent(underlyingOrSymbol)}` : `underlying=${encodeURIComponent(underlyingOrSymbol)}`;
    const res = await fetch(`/api/ticker?${param}`);
    return await res.json();
  },

  /**
   * Fetches live ticker quotes for multiple symbols in batch
   * @param {Array<string>} symbols
   */
  async getBatchTickers(symbols = []) {
    if (!symbols || symbols.length === 0) return { success: true, tickers: {} };
    try {
      const res = await fetch(`/api/tickers/batch?symbols=${encodeURIComponent(symbols.join(','))}`);
      if (res.ok) {
        return await res.json();
      }
    } catch (e) {}

    // Fallback: parallel fetch for individual symbols
    const results = {};
    await Promise.all(symbols.map(async (s) => {
      try {
        const d = await this.getTicker(s);
        if (d && d.success) {
          results[s.toUpperCase()] = {
            symbol: s.toUpperCase(),
            ltp: d.ltp,
            mark_price: d.mark_price,
            bid: d.futures_price || d.ltp,
            ask: d.futures_price || d.ltp,
            source: d.source || 'rest'
          };
        }
      } catch (err) {}
    }));
    return { success: true, tickers: results };
  },

  /**
   * Fetches available option expiries, strikes, and contract specifications
   * @param {string} underlying - e.g. 'BTC', 'ETH'
   */
  async getOptionsExpiries(underlying = 'BTC') {
    const res = await fetch(`/api/options/expiries?underlying=${encodeURIComponent(underlying)}`);
    return await res.json();
  },

  /**
   * Fetches full option chain matrix for underlying and expiry
   * @param {string} underlying - e.g. 'BTC', 'ETH'
   * @param {string} expiry - e.g. '051026'
   */
  async getOptionChain(underlying = 'BTC', expiry = '') {
    const expParam = expiry ? `&expiry=${encodeURIComponent(expiry)}` : '';
    try {
      const res = await fetch(`/api/options/chain?underlying=${encodeURIComponent(underlying)}${expParam}`);
      if (res.ok) {
        const text = await res.text();
        if (text.startsWith('{')) {
          const data = JSON.parse(text);
          if (data && data.success) return data;
        }
      }
    } catch (e) {}

    // Fallback: Construct option chain directly from expiries metadata
    try {
      const expData = await this.getOptionsExpiries(underlying);
      if (expData && expData.success) {
        const expiries = expData.expiries || [];
        const selExp = expiry || (expiries[0]?.expiry || '');
        const expObj = expiries.find(e => e.expiry === selExp) || expiries[0] || {};
        const strikes = expObj.strikes || [];
        const spot = expData.spot_price || expData.underlying_price || 0;

        const chain = strikes.map(s => {
          return {
            strike: s,
            is_atm: (s === (expObj.atm_strike || expData.atm_strike)),
            call: {
              symbol: `C-${underlying}-${s}-${selExp}`,
              contract_type: 'call',
              strike: s,
              expiry: selExp,
              is_itm: (spot > 0 && s < spot),
              contract_value: expData.contract_value || (underlying === 'BTC' ? 0.001 : 0.01),
              ltp: null,
              mark_price: null
            },
            put: {
              symbol: `P-${underlying}-${s}-${selExp}`,
              contract_type: 'put',
              strike: s,
              expiry: selExp,
              is_itm: (spot > 0 && s > spot),
              contract_value: expData.contract_value || (underlying === 'BTC' ? 0.001 : 0.01),
              ltp: null,
              mark_price: null
            }
          };
        });

        return {
          success: true,
          underlying: underlying,
          selected_expiry: selExp,
          spot_price: spot,
          atm_strike: expObj.atm_strike || expData.atm_strike,
          contract_value: expData.contract_value || (underlying === 'BTC' ? 0.001 : 0.01),
          expiries: expiries,
          chain: chain,
          total_strikes: chain.length
        };
      }
    } catch (err) {
      console.error('Fallback option chain error:', err);
    }
    return { success: false, error: 'Failed to load option chain' };
  },

  /**
   * Searches available tradable products
   */
  async searchProducts(underlying = '', contractType = '', query = '') {
    const params = new URLSearchParams();
    if (underlying) params.append('underlying', underlying);
    if (contractType) params.append('contract_type', contractType);
    if (query) params.append('q', query);
    const res = await fetch(`/api/products/search?${params.toString()}`);
    return await res.json();
  },

  /**
   * Executes all legs of a multi-leg strategy simultaneously
   * @param {Array} legs - Array of leg objects
   */
  async executeMultiLeg(legs) {
    const res = await fetch('/api/orders/multi-leg', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ legs })
    });
    return await res.json();
  },

  /**
   * Executes a single manual order
   */
  async executeSingleOrder(orderData) {
    const res = await fetch('/api/orders/single', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(orderData)
    });
    return await res.json();
  },

  /**
   * Squares off a single open position at market price
   * @param {string|number} productId
   * @param {string} symbol
   */
  async squareOffPosition(productId, symbol) {
    const res = await fetch('/api/orders/square-off', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ product_id: productId, symbol: symbol })
    });
    return await res.json();
  },

  /**
   * Squares off all open positions simultaneously at market
   */
  async squareOffAllPositions() {
    const res = await fetch('/api/orders/square-off-all', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' }
    });
    return await res.json();
  },

  /**
   * Fetches master scrips sync status
   */
  async getMasterStatus() {
    const res = await fetch('/api/master/status');
    return await res.json();
  },

  /**
   * Triggers immediate download & sync of master scrips from Delta Exchange
   */
  async refreshMasterScrips() {
    const res = await fetch('/api/master/refresh', { method: 'POST' });
    return await res.json();
  }
};
