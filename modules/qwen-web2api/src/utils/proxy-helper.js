const config = require('../config/index.js')
const { HttpsProxyAgent } = require('https-proxy-agent')

// 缓存代理 agent 实例
let proxyAgentInstance = null

/**
 * 获取代理 Agent
 * @returns {HttpsProxyAgent|undefined}
 */
const getProxyAgent = () => {
    if (config.proxyUrl) {
        if (!proxyAgentInstance) {
            proxyAgentInstance = new HttpsProxyAgent(config.proxyUrl)
        }
        return proxyAgentInstance
    }
    return undefined
}

/**
 * 获取 Chat API 基础 URL
 * @returns {string}
 */
const getChatBaseUrl = () => config.qwenChatProxyUrl

/**
 * 获取 CLI API 基础 URL
 * @returns {string}
 */
const getCliBaseUrl = () => config.qwenCliProxyUrl

/**
 * 为 axios 请求配置添加代理设置
 * @param {Object} requestConfig - axios 请求配置对象
 * @returns {Object} 添加了代理配置的请求对象
 */
const applyProxyToAxiosConfig = (requestConfig = {}) => {
    const proxyAgent = getProxyAgent()
    if (proxyAgent) {
        requestConfig.httpsAgent = proxyAgent
        requestConfig.proxy = false
    }
    return requestConfig
}

/**
 * 为 fetch 请求配置添加代理设置
 * @param {Object} fetchOptions - fetch 请求配置对象
 * @returns {Object} 添加了代理配置的请求对象
 */
const applyProxyToFetchOptions = (fetchOptions = {}) => {
    const proxyAgent = getProxyAgent()
    if (proxyAgent) {
        fetchOptions.agent = proxyAgent
    }
    return fetchOptions
}

module.exports = {
    getProxyAgent,
    getChatBaseUrl,
    getCliBaseUrl,
    applyProxyToAxiosConfig,
    applyProxyToFetchOptions
}
