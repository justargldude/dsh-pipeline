const axios = require('axios')
const accountManager = require('../utils/account.js')
const { getSsxmodItna, getSsxmodItna2 } = require('../utils/ssxmod-manager')
const { getProxyAgent, getChatBaseUrl, applyProxyToAxiosConfig } = require('../utils/proxy-helper')

let cachedModels = null
let fetchPromise = null

const getLatestModels = async (force = false) => {
    // 如果有缓存且不强制刷新，直接返回
    if (cachedModels && !force) {
        return cachedModels
    }

    // 如果正在获取，返回当前的 Promise
    if (fetchPromise) {
        return fetchPromise
    }

    const chatBaseUrl = getChatBaseUrl()
    const proxyAgent = getProxyAgent()

    const requestConfig = {
        headers: {
            'Authorization': `Bearer ${accountManager.getAccountToken()}`,
            'Content-Type': 'application/json',
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            ...(getSsxmodItna() && { 'Cookie': `ssxmod_itna=${getSsxmodItna()};ssxmod_itna2=${getSsxmodItna2()}` })
        }
    }

    // 添加代理配置
    if (proxyAgent) {
        requestConfig.httpsAgent = proxyAgent
        requestConfig.proxy = false
    }

    fetchPromise = axios.get(`${chatBaseUrl}/api/models`, requestConfig).then(response => {
        // console.log(response)
        cachedModels = response.data.data
        fetchPromise = null
        return cachedModels
    }).catch(error => {
        console.error('Error fetching latest models:', error)
        fetchPromise = null
        return []
    })

    return fetchPromise
}

module.exports = { getLatestModels }