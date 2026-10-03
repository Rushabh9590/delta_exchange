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
   * @param {string} underlying - e.g. 'BTC', 'ETH'
   */
  async getTicker(underlying = 'BTC') {
    const res = await fetch(`/api/ticker?underlying=${encodeURIComponent(underlying)}`);
    return await res.json();
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
