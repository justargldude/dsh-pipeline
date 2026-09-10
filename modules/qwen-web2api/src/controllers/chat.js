const { isJson, generateUUID } = require('../utils/tools.js')
const { createUsageObject } = require('../utils/precise-tokenizer.js')
const { sendChatRequest } = require('../utils/request.js')
const accountManager = require('../utils/account.js')
const config = require('../config/index.js')
const axios = require('axios')
const { logger } = require('../utils/logger')
const { browserBridge } = require('../utils/browser-bridge.js')

/**
 * 设置响应头
 * @param {object} res - Express 响应对象
 * @param {boolean} stream - 是否流式响应
 */
const setResponseHeaders = (res, stream) => {
    try {
        if (stream) {
            res.set({
                'Content-Type': 'text/event-stream',
                'Cache-Control': 'no-cache',
                'Connection': 'keep-alive',
            })
        } else {
            res.set({
                'Content-Type': 'application/json',
            })
        }
    } catch (e) {
        logger.error('处理聊天请求时发生错误', 'CHAT', '', e)
    }
}

const getImageMarkdownListFromDelta = (delta) => {
    // 常规聊天在触发 image_gen_tool 时，仅使用 image_list 中用于展示的图片链接
    const imageList = []
    const displayImages = delta?.extra?.image_list || []

    for (const item of displayImages) {
        if (item?.image) {
            imageList.push(`![image](${item.image})`)
        }
    }

    return imageList
}

/**
 * 处理流式响应
 * @param {object} res - Express 响应对象
 * @param {object} response - 上游响应流
 * @param {boolean} enable_thinking - 是否启用思考模式
 * @param {boolean} enable_web_search - 是否启用网络搜索
 * @param {object} requestBody - 原始请求体，用于提取prompt信息
 */
const handleStreamResponse = async (res, response, enable_thinking, enable_web_search, requestBody = null) => {
    try {
        const message_id = generateUUID()
        const decoder = new TextDecoder('utf-8')
        let web_search_info = null
        let thinking_start = false
        let thinking_end = false
        let buffer = ''
        let emittedImageMarkdownSet = new Set()
        let pendingImageMarkdownList = []

        // Token消耗量统计
        let totalTokens = {
            prompt_tokens: 0,
            completion_tokens: 0,
            total_tokens: 0
        }
        let completionContent = '' // 收集完整的回复内容用于token估算

        // 提取prompt文本用于token估算
        let promptText = ''
        if (requestBody && requestBody.messages) {
            promptText = requestBody.messages.map(msg => {
                if (typeof msg.content === 'string') {
                    return msg.content
                } else if (Array.isArray(msg.content)) {
                    return msg.content.map(item => item.text || '').join('')
                }
                return ''
            }).join('\n')
        }

        response.on('data', async (chunk) => {
            const decodeText = decoder.decode(chunk, { stream: true })
            // raw debug removed
            buffer += decodeText

            const chunks = []
            let startIndex = 0

            while (true) {
                const dataStart = buffer.indexOf('data: ', startIndex)
                if (dataStart === -1) break

                const dataEnd = buffer.indexOf('\n\n', dataStart)
                if (dataEnd === -1) break

                const dataChunk = buffer.substring(dataStart, dataEnd).trim()
                chunks.push(dataChunk)

                startIndex = dataEnd + 2
            }

            if (startIndex > 0) {
                buffer = buffer.substring(startIndex)
            }

            for (const item of chunks) {
                try {
                    let dataContent = item.replace("data: ", '')
                    let decodeJson = isJson(dataContent) ? JSON.parse(dataContent) : null
                    if (decodeJson === null || !decodeJson.choices || decodeJson.choices.length === 0) {
                        continue
                    }

                    // 提取真实的usage信息（如果上游API提供）
                    if (decodeJson.usage) {
                        totalTokens = {
                            prompt_tokens: decodeJson.usage.prompt_tokens || totalTokens.prompt_tokens,
                            completion_tokens: decodeJson.usage.completion_tokens || totalTokens.completion_tokens,
                            total_tokens: decodeJson.usage.total_tokens || totalTokens.total_tokens
                        }
                    }

                    const delta = decodeJson.choices[0].delta

                    // 处理 web_search 信息
                    if (delta && delta.name === 'web_search') {
                        web_search_info = delta.extra.web_search_info
                    }

                    const imageMarkdownList = getImageMarkdownListFromDelta(delta)
                    if (imageMarkdownList.length > 0) {
                        const newImageMarkdownList = imageMarkdownList.filter(item => !emittedImageMarkdownSet.has(item))

                        // 如果仍处于思考块内，则先缓存图片，等 </think> 输出后再插入，避免被客户端折叠
                        if (thinking_start && !thinking_end) {
                            for (const imageMarkdown of newImageMarkdownList) {
                                if (!pendingImageMarkdownList.includes(imageMarkdown)) {
                                    pendingImageMarkdownList.push(imageMarkdown)
                                }
                            }
                        } else if (newImageMarkdownList.length > 0) {
                            // 将图片工具结果转换成普通聊天内容，兼容下游仅识别 OpenAI 文本增量的场景
                            const imageContent = `${newImageMarkdownList.join('\n\n')}\n\n`
                            completionContent += imageContent
                            newImageMarkdownList.forEach(item => emittedImageMarkdownSet.add(item))

                            const streamImageTemplate = {
                                "id": `chatcmpl-${message_id}`,
                                "object": "chat.completion.chunk",
                                "created": Math.round(new Date().getTime() / 1000),
                                "choices": [
                                    {
                                        "index": 0,
                                        "delta": {
                                            "content": imageContent
                                        },
                                        "finish_reason": null
                                    }
                                ]
                            }

                            res.write(`data: ${JSON.stringify(streamImageTemplate)}\n\n`)
                        }
                    }

                    if (!delta || !delta.content ||
                        (delta.phase !== 'think' && delta.phase !== 'answer')) {
                        continue
                    }

                    let content = delta.content
                    completionContent += content // 累计完整的回复内容用于token估算

                    if (delta.phase === 'think' && !thinking_start) {
                        thinking_start = true
                        if (web_search_info) {
                            content = `<think>\n\n${await accountManager.generateMarkdownTable(web_search_info, config.searchInfoMode)}\n\n${content}`
                        } else {
                            content = `<think>\n\n${content}`
                        }
                    }
                    if (delta.phase === 'answer' && !thinking_end && thinking_start) {
                        thinking_end = true
                        if (pendingImageMarkdownList.length > 0) {
                            const pendingImageContent = `${pendingImageMarkdownList.join('\n\n')}\n\n`
                            content = `\n\n</think>\n${pendingImageContent}${content}`
                            completionContent += pendingImageContent
                            pendingImageMarkdownList.forEach(item => emittedImageMarkdownSet.add(item))
                            pendingImageMarkdownList = []
                        } else {
                            content = `\n\n</think>\n${content}`
                        }
                    }

                    const StreamTemplate = {
                        "id": `chatcmpl-${message_id}`,
                        "object": "chat.completion.chunk",
                        "created": Math.round(new Date().getTime() / 1000),
                        "choices": [
                            {
                                "index": 0,
                                "delta": {
                                    "content": content
                                },
                                "finish_reason": null
                            }
                        ]
                    }

                    res.write(`data: ${JSON.stringify(StreamTemplate)}\n\n`)
                } catch (error) {
                    logger.error('流式数据处理错误', 'CHAT', '', error)
                    res.status(500).json({ error: "服务错误!!!" })
                }
            }
        })

        response.on('end', async () => {
            try {
                // 处理最终的搜索信息
                if ((config.outThink === false || !enable_thinking) && web_search_info && config.searchInfoMode === "text") {
                    const webSearchTable = await accountManager.generateMarkdownTable(web_search_info, "text")
                    res.write(`data: ${JSON.stringify({
                        "id": `chatcmpl-${message_id}`,
                        "object": "chat.completion.chunk",
                        "created": Math.round(new Date().getTime() / 1000),
                        "choices": [
                            {
                                "index": 0,
                                "delta": {
                                    "content": `\n\n---\n${webSearchTable}`
                                },
                                "finish_reason": null
                            }
                        ]
                    })}\n\n`)
                }

                // 计算最终的token使用量
                if (totalTokens.prompt_tokens === 0 && totalTokens.completion_tokens === 0) {
                    totalTokens = createUsageObject(requestBody?.messages || promptText, completionContent, null)
                    logger.info(`流式使用tiktoken计算 - Prompt: ${totalTokens.prompt_tokens}, Completion: ${totalTokens.completion_tokens}, Total: ${totalTokens.total_tokens}`, 'CHAT')
                } else {
                    logger.info(`流式使用上游真实Token - Prompt: ${totalTokens.prompt_tokens}, Completion: ${totalTokens.completion_tokens}, Total: ${totalTokens.total_tokens}`, 'CHAT')
                }

                // 确保token数量的有效性
                totalTokens.prompt_tokens = Math.max(0, totalTokens.prompt_tokens || 0)
                totalTokens.completion_tokens = Math.max(0, totalTokens.completion_tokens || 0)
                totalTokens.total_tokens = totalTokens.prompt_tokens + totalTokens.completion_tokens

                // 发送最终的finish chunk，包含finish_reason
                res.write(`data: ${JSON.stringify({
                    "id": `chatcmpl-${message_id}`,
                    "object": "chat.completion.chunk",
                    "created": Math.round(new Date().getTime() / 1000),
                    "choices": [
                        {
                            "index": 0,
                            "delta": {},
                            "finish_reason": "stop"
                        }
                    ]
                })}\n\n`)

                // 发送usage信息chunk（符合OpenAI API标准）
                res.write(`data: ${JSON.stringify({
                    "id": `chatcmpl-${message_id}`,
                    "object": "chat.completion.chunk",
                    "created": Math.round(new Date().getTime() / 1000),
                    "choices": [],
                    "usage": totalTokens
                })}\n\n`)

                // 发送结束标记
                res.write(`data: [DONE]\n\n`)
                res.end()
            } catch (e) {
                logger.error('流式响应处理错误', 'CHAT', '', e)
                res.status(500).json({ error: "服务错误!!!" })
            }
        })
    } catch (error) {
        logger.error('聊天处理错误', 'CHAT', '', error)
        res.status(500).json({ error: "服务错误!!!" })
    }
}

/**
 * 处理非流式响应（从流式数据累积完整响应）
 * @param {object} res - Express 响应对象
 * @param {object} response - 上游响应流
 * @param {boolean} enable_thinking - 是否启用思考模式
 * @param {boolean} enable_web_search - 是否启用网络搜索
 * @param {string} model - 模型名称
 * @param {object} requestBody - 原始请求体，用于提取prompt信息
 */
const handleNonStreamResponse = async (res, response, enable_thinking, enable_web_search, model, requestBody = null) => {
    try {
        const decoder = new TextDecoder('utf-8')
        let buffer = ''
        let fullContent = ''
        let web_search_info = null
        let thinking_start = false
        let thinking_end = false
        let appendedImageMarkdownSet = new Set()
        let pendingImageMarkdownList = []

        // Token消耗量统计
        let totalTokens = {
            prompt_tokens: 0,
            completion_tokens: 0,
            total_tokens: 0
        }

        // 提取prompt文本用于token估算
        let promptText = ''
        if (requestBody && requestBody.messages) {
            promptText = requestBody.messages.map(msg => {
                if (typeof msg.content === 'string') {
                    return msg.content
                } else if (Array.isArray(msg.content)) {
                    return msg.content.map(item => item.text || '').join('')
                }
                return ''
            }).join('\n')
        }

        // 处理流式响应并累积内容
        await new Promise((resolve, reject) => {
            response.on('data', async (chunk) => {
                const decodeText = decoder.decode(chunk, { stream: true })
                buffer += decodeText

                const chunks = []
                let startIndex = 0

                while (true) {
                    const dataStart = buffer.indexOf('data: ', startIndex)
                    if (dataStart === -1) break

                    const dataEnd = buffer.indexOf('\n\n', dataStart)
                    if (dataEnd === -1) break

                    const dataChunk = buffer.substring(dataStart, dataEnd).trim()
                    chunks.push(dataChunk)

                    startIndex = dataEnd + 2
                }

                if (startIndex > 0) {
                    buffer = buffer.substring(startIndex)
                }

                for (const item of chunks) {
                    try {
                        let dataContent = item.replace("data: ", '')
                        let decodeJson = isJson(dataContent) ? JSON.parse(dataContent) : null
                        if (decodeJson === null || !decodeJson.choices || decodeJson.choices.length === 0) {
                            continue
                        }

                        // 提取真实的usage信息（如果上游API提供）
                        if (decodeJson.usage) {
                            totalTokens = {
                                prompt_tokens: decodeJson.usage.prompt_tokens || totalTokens.prompt_tokens,
                                completion_tokens: decodeJson.usage.completion_tokens || totalTokens.completion_tokens,
                                total_tokens: decodeJson.usage.total_tokens || totalTokens.total_tokens
                            }
                        }

                        const delta = decodeJson.choices[0].delta

                        // 处理 web_search 信息
                        if (delta && delta.name === 'web_search') {
                            web_search_info = delta.extra.web_search_info
                        }

                        const imageMarkdownList = getImageMarkdownListFromDelta(delta)
                        if (imageMarkdownList.length > 0) {
                            const newImageMarkdownList = imageMarkdownList.filter(item => !appendedImageMarkdownSet.has(item))

                            // 非流式模式下如果仍处于思考块内，则延后到 </think> 后再追加图片
                            if (thinking_start && !thinking_end) {
                                for (const imageMarkdown of newImageMarkdownList) {
                                    if (!pendingImageMarkdownList.includes(imageMarkdown)) {
                                        pendingImageMarkdownList.push(imageMarkdown)
                                    }
                                }
                            } else if (newImageMarkdownList.length > 0) {
                                // 非流式模式下同样追加 Markdown 图片链接，保持与现有图片返回格式一致
                                fullContent += `${newImageMarkdownList.join('\n\n')}\n\n`
                                newImageMarkdownList.forEach(item => appendedImageMarkdownSet.add(item))
                            }
                        }

                        if (!delta || !delta.content ||
                            (delta.phase !== 'think' && delta.phase !== 'answer')) {
                            continue
                        }

                        let content = delta.content

                        // 处理thinking模式
                        if (delta.phase === 'think' && !thinking_start) {
                            thinking_start = true
                            if (web_search_info) {
                                const webSearchTable = await accountManager.generateMarkdownTable(web_search_info, config.searchInfoMode)
                                content = `<think>\n\n${webSearchTable}\n\n${content}`
                            } else {
                                content = `<think>\n\n${content}`
                            }
                        }
                        if (delta.phase === 'answer' && !thinking_end && thinking_start) {
                            thinking_end = true
                            if (pendingImageMarkdownList.length > 0) {
                                content = `\n\n</think>\n${pendingImageMarkdownList.join('\n\n')}\n\n${content}`
                                pendingImageMarkdownList.forEach(item => appendedImageMarkdownSet.add(item))
                                pendingImageMarkdownList = []
                            } else {
                                content = `\n\n</think>\n${content}`
                            }
                        }

                        fullContent += content
                    } catch (error) {
                        logger.error('非流式数据处理错误', 'CHAT', '', error)
                    }
                }
            })

            response.on('end', () => {
                resolve()
            })

            response.on('error', (error) => {
                logger.error('非流式响应流读取错误', 'CHAT', '', error)
                reject(error)
            })
        })

        // 处理最终的搜索信息
        if ((config.outThink === false || !enable_thinking) && web_search_info && config.searchInfoMode === "text") {
            const webSearchTable = await accountManager.generateMarkdownTable(web_search_info, "text")
            fullContent += `\n\n---\n${webSearchTable}`
        }

        // 计算最终的token使用量
        if (totalTokens.prompt_tokens === 0 && totalTokens.completion_tokens === 0) {
            totalTokens = createUsageObject(requestBody?.messages || promptText, fullContent, null)
            logger.info(`非流式使用tiktoken计算 - Prompt: ${totalTokens.prompt_tokens}, Completion: ${totalTokens.completion_tokens}, Total: ${totalTokens.total_tokens}`, 'CHAT')
        } else {
            logger.info(`非流式使用上游真实Token - Prompt: ${totalTokens.prompt_tokens}, Completion: ${totalTokens.completion_tokens}, Total: ${totalTokens.total_tokens}`, 'CHAT')
        }

        // 确保token数量的有效性
        totalTokens.prompt_tokens = Math.max(0, totalTokens.prompt_tokens || 0)
        totalTokens.completion_tokens = Math.max(0, totalTokens.completion_tokens || 0)
        totalTokens.total_tokens = totalTokens.prompt_tokens + totalTokens.completion_tokens

        // 返回完整的JSON响应
        const bodyTemplate = {
            "id": `chatcmpl-${generateUUID()}`,
            "object": "chat.completion",
            "created": Math.round(new Date().getTime() / 1000),
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": fullContent
                    },
                    "finish_reason": "stop"
                }
            ],
            "usage": totalTokens
        }
        res.json(bodyTemplate)
    } catch (error) {
        logger.error('非流式聊天处理错误', 'CHAT', '', error)
        res.status(500)
            .json({
                error: "服务错误!!!"
            })
    }
}


/**
 * 主要的聊天完成处理函数
 * @param {object} req - Express 请求对象
 * @param {object} res - Express 响应对象
 */
function extractPromptText(body) {
    if (typeof body.prompt === 'string' && body.prompt) return body.prompt;
    if (Array.isArray(body.messages)) {
        return body.messages.map(m => {
            const content = typeof m.content === 'string' ? m.content : (Array.isArray(m.content) ? m.content.map(c => c.text || '').join('\n') : JSON.stringify(m.content));
            return m.role === 'system' ? `[System: ${content}]` : content;
        }).join('\n\n');
    }
    return '';
}

const handleChatCompletion = async (req, res) => {
    const { stream, model } = req.body;
    const prompt = extractPromptText(req.body);

    if (!prompt.trim()) {
        return res.status(400).json({ error: "Prompt or messages cannot be empty" });
    }

    try {
        if (stream) {
            setResponseHeaders(res, true);
            const reqId = 'chatcmpl-' + generateUUID();
            let hasWritten = false;

            await browserBridge.sendPrompt({
                prompt,
                onChunk: (rawChunk) => {
                    const lines = rawChunk.split('\n');
                    for (const line of lines) {
                        if (line.startsWith('data: ') && !line.includes('[DONE]')) {
                            try {
                                const parsed = JSON.parse(line.slice(6));
                                const delta = parsed.choices?.[0]?.delta;
                                if (delta?.content) {
                                    hasWritten = true;
                                    res.write(`data: ${JSON.stringify({
                                        id: reqId,
                                        object: 'chat.completion.chunk',
                                        created: Math.floor(Date.now() / 1000),
                                        model: model || 'qwen3.7-plus',
                                        choices: [{ index: 0, delta: { content: delta.content }, finish_reason: null }]
                                    })}\n\n`);
                                }
                            } catch (_) {}
                        }
                    }
                },
                onDone: () => {
                    res.write(`data: ${JSON.stringify({
                        id: reqId,
                        object: 'chat.completion.chunk',
                        created: Math.floor(Date.now() / 1000),
                        model: model || 'qwen3.7-plus',
                        choices: [{ index: 0, delta: {}, finish_reason: 'stop' }]
                    })}\n\n`);
                    res.write('data: [DONE]\n\n');
                    res.end();
                },
                onError: (err) => {
                    logger.error('Bridge error during streaming', 'CHAT', '', err.message);
                    if (!hasWritten) {
                        res.write(`data: ${JSON.stringify({ error: { message: err.message } })}\n\n`);
                    }
                    res.end();
                }
            });
        } else {
            setResponseHeaders(res, false);
            let fullAnswer = '';

            await browserBridge.sendPrompt({
                prompt,
                onChunk: (rawChunk) => {
                    const lines = rawChunk.split('\n');
                    for (const line of lines) {
                        if (line.startsWith('data: ') && !line.includes('[DONE]')) {
                            try {
                                const parsed = JSON.parse(line.slice(6));
                                const delta = parsed.choices?.[0]?.delta;
                                if (delta?.content) {
                                    fullAnswer += delta.content;
                                }
                            } catch (_) {}
                        }
                    }
                },
                onDone: () => {
                    res.json({
                        id: 'chatcmpl-' + generateUUID(),
                        object: 'chat.completion',
                        created: Math.floor(Date.now() / 1000),
                        model: model || 'qwen3.7-plus',
                        choices: [
                            {
                                index: 0,
                                message: {
                                    role: 'assistant',
                                    content: fullAnswer
                                },
                                finish_reason: 'stop'
                            }
                        ],
                        usage: {
                            prompt_tokens: Math.round(prompt.length / 4),
                            completion_tokens: Math.round(fullAnswer.length / 4),
                            total_tokens: Math.round((prompt.length + fullAnswer.length) / 4)
                        }
                    });
                },
                onError: (err) => {
                    logger.error('Bridge error during non-streaming', 'CHAT', '', err.message);
                    res.status(500).json({ error: { message: err.message } });
                }
            });
        }
    } catch (error) {
        logger.error('聊天处理错误', 'CHAT', '', error.message);
        if (!res.headersSent) {
            res.status(500).json({ error: error.message || '服务内部错误' });
        }
    }
};

module.exports = {
    handleChatCompletion,
    handleStreamResponse,
    handleNonStreamResponse,
    setResponseHeaders
}
