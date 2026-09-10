const axios = require('axios')
const accountManager = require('./account.js')
const { logger } = require('./logger')
const { getSsxmodItna, getSsxmodItna2 } = require('./ssxmod-manager')
const { getProxyAgent, getChatBaseUrl, applyProxyToAxiosConfig } = require('./proxy-helper')


/**
 * 发送聊天请求
 * @param {Object} body - 请求体
 * @param {number} retryCount - 当前重试次数
 * @param {string} lastUsedEmail - 上次使用的邮箱（用于错误记录）
 * @returns {Promise<Object>} 响应结果
 */
const sendChatRequest = async (body) => {
    try {
        // 获取可用的令牌
        const currentToken = accountManager.getAccountToken()

        if (!currentToken) {
            logger.error('无法获取有效的访问令牌', 'TOKEN')
            return {
                status: false,
                response: null
            }
        }

        const chatBaseUrl = getChatBaseUrl()
        const proxyAgent = getProxyAgent()

        // 构建请求配置
        const realCookie = accountManager.accountTokens?.[0]?.cookie || `ssxmod_itna=${getSsxmodItna()};ssxmod_itna2=${getSsxmodItna2()}`;
        const requestConfig = {
            headers: {
                'Authorization': `Bearer ${currentToken}`,
                "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:135.0) Gecko/20100101 Firefox/135.0",
                "Connection": "keep-alive",
                "Accept": "application/json",
                "Accept-Encoding": "gzip, deflate",
                "Content-Type": "application/json",
                "x-device-id": "34d32819-9bcb-429c-905d-2e8a3b1b8859",
                "source": "web",
                "Version": "0.1.13",
                "bx-v": "2.5.31",
                "Origin": chatBaseUrl,
                "Sec-Fetch-Site": "same-origin",
                "Sec-Fetch-Mode": "cors",
                "Sec-Fetch-Dest": "empty",
                "Referer": `${chatBaseUrl}/c/guest`,
                "Accept-Language": "zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7",
                "Cookie": realCookie,
            },
            responseType: 'stream', // Always use streaming (upstream doesn't support stream=false)
            timeout: 300 * 1000,
        }

        // 添加代理配置
        if (proxyAgent) {
            requestConfig.httpsAgent = proxyAgent
            requestConfig.proxy = false // 禁用axios默认代理，使用httpsAgent
        }

        // console.log(body)
        // console.log(requestConfig)

        const chat_id = await generateChatID(currentToken, body.model)

        logger.network(`发送聊天请求`, 'REQUEST')
        const response = await axios.post(`${chatBaseUrl}/api/v2/chat/completions?chat_id=` + chat_id, {
            ...body,
            stream: true, // Always request streaming (upstream doesn't support stream=false)
            chat_id: chat_id
        }, requestConfig)

        // 请求成功
        if (response.status === 200) {
            // console.log(response.data)
            return {
                currentToken: currentToken,
                status: true,
                response: response.data
            }
        }

    } catch (error) {
        console.log(error)
        logger.error('发送聊天请求失败', 'REQUEST', '', error.message)
        return {
            status: false,
            response: null
        }
    }
}

/**
 * 生成chat_id
 * @param {*} currentToken 
 * @returns {Promise<string|null>} 返回生成的chat_id，如果失败则返回null
 */
const generateChatID = async (currentToken, model) => {
    try {
        const chatBaseUrl = getChatBaseUrl()
        const proxyAgent = getProxyAgent()

        const realCookie = accountManager.accountTokens?.[0]?.cookie || `ssxmod_itna=${getSsxmodItna()};ssxmod_itna2=${getSsxmodItna2()}`;
        const requestConfig = {
            headers: {
                'Authorization': `Bearer ${currentToken}`,
                "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:135.0) Gecko/20100101 Firefox/135.0",
                "Connection": "keep-alive",
                "Accept": "application/json",
                "Accept-Encoding": "gzip, deflate",
                "Content-Type": "application/json",
                "x-device-id": "34d32819-9bcb-429c-905d-2e8a3b1b8859",
                "source": "web",
                "Version": "0.1.13",
                "bx-v": "2.5.31",
                "Origin": chatBaseUrl,
                "Sec-Fetch-Site": "same-origin",
                "Sec-Fetch-Mode": "cors",
                "Sec-Fetch-Dest": "empty",
                "Referer": `${chatBaseUrl}/c/guest`,
                "Accept-Language": "zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7",
                "Cookie": realCookie,
            }
        }

        // 添加代理配置
        if (proxyAgent) {
            requestConfig.httpsAgent = proxyAgent
            requestConfig.proxy = false
        }

        const response_data = await axios.post(`${chatBaseUrl}/api/v2/chats/new`, {
            "title": "New Chat",
            "models": [
                model
            ],
            "chat_mode": "local",
            "chat_type": "t2t",
            "timestamp": new Date().getTime()
        }, requestConfig)

        // console.log(response_data.data)

        return response_data.data?.data?.id || null

    } catch (error) {
        logger.error('生成chat_id失败', 'CHAT', '', error.message)
        return null
    }
}

module.exports = {
    sendChatRequest,
    generateChatID
}