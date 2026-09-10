const path = require('path');
const { execSync } = require('child_process');
const puppeteer = require('puppeteer-core');
const { logger } = require('./logger');

class BrowserBridge {
    constructor() {
        this.browser = null;
        this.page = null;
        this.isReady = false;
        this.isInitializing = false;
        this.queue = [];
        this.isProcessing = false;
        this.activeTurn = null;
    }

    getFirefoxData() {
        const scriptPath = path.join(__dirname, 'extract_firefox.py');
        try {
            const out = execSync(`python3 "${scriptPath}"`, { encoding: 'utf8', timeout: 10000 });
            return JSON.parse(out);
        } catch (e) {
            logger.error('Failed to extract Firefox session data', 'BROWSER', '', e.message);
            throw e;
        }
    }

    async init() {
        if (this.isReady || this.isInitializing) return;
        this.isInitializing = true;
        logger.info('Initializing Qwen Browser Bridge...', 'BROWSER');

        try {
            const { cookies, ls } = this.getFirefoxData();

            this.browser = await puppeteer.launch({
                headless: 'new',
                executablePath: '/usr/bin/google-chrome',
                args: [
                    '--no-sandbox',
                    '--disable-setuid-sandbox',
                    '--disable-dev-shm-usage',
                    '--disable-blink-features=AutomationControlled'
                ]
            });

            this.page = await this.browser.newPage();
            await this.page.setUserAgent('Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36');

            // Expose streaming hooks
            await this.page.exposeFunction('onBridgeChunk', (text) => {
                if (this.activeTurn?.onChunk) {
                    this.activeTurn.onChunk(text);
                }
            });

            await this.page.exposeFunction('onBridgeEnd', () => {
                if (this.activeTurn?.onDone) {
                    this.activeTurn.onDone();
                }
            });

            await this.page.exposeFunction('onBridgeError', (err) => {
                if (this.activeTurn?.onError) {
                    this.activeTurn.onError(new Error(err));
                }
            });

            // Tee fetch streams in real-time
            await this.page.evaluateOnNewDocument(() => {
                const origFetch = window.fetch;
                window.fetch = async function (...args) {
                    const url = typeof args[0] === 'string' ? args[0] : (args[0]?.url || '');
                    if (url.includes('/chat/completions')) {
                        const response = await origFetch.apply(this, args);
                        if (response.ok && response.body) {
                            const [s1, s2] = response.body.tee();
                            (async () => {
                                try {
                                    const reader = s1.getReader();
                                    const decoder = new TextDecoder();
                                    while (true) {
                                        const { done, value } = await reader.read();
                                        if (done) {
                                            window.onBridgeEnd();
                                            break;
                                        }
                                        window.onBridgeChunk(decoder.decode(value, { stream: true }));
                                    }
                                } catch (e) {
                                    window.onBridgeError(e.message || String(e));
                                }
                            })();
                            return new Response(s2, response);
                        }
                        return response;
                    }
                    return origFetch.apply(this, args);
                };
            });

            for (const c of cookies) {
                try { await this.page.setCookie(c); } catch (_) {}
            }

            await this.page.goto('https://chat.qwen.ai', { waitUntil: 'domcontentloaded', timeout: 35000 });
            await this.page.evaluate((items) => {
                for (const [k, v] of Object.entries(items)) {
                    try { localStorage.setItem(k, v); } catch (_) {}
                }
            }, ls);

            await this.page.goto('https://chat.qwen.ai', { waitUntil: 'networkidle2', timeout: 35000 });
            await this.page.waitForSelector('textarea', { timeout: 15000 });

            this.isReady = true;
            logger.info('Qwen Browser Bridge is READY and authenticated', 'BROWSER');
        } catch (e) {
            logger.error('Failed to initialize Qwen Browser Bridge', 'BROWSER', '', e.message);
            this.isReady = false;
            if (this.browser) {
                try { await this.browser.close(); } catch (_) {}
                this.browser = null;
            }
            throw e;
        } finally {
            this.isInitializing = false;
        }
    }

    async ensureReady() {
        if (!this.isReady) {
            await this.init();
        }
    }

    async sendPrompt({ prompt, onChunk, onDone, onError }) {
        return new Promise((resolve, reject) => {
            this.queue.push({
                prompt,
                onChunk,
                onDone: () => {
                    if (onDone) onDone();
                    resolve();
                },
                onError: (err) => {
                    if (onError) onError(err);
                    reject(err);
                }
            });
            this.processQueue();
        });
    }

    async processQueue() {
        if (this.isProcessing || this.queue.length === 0) return;
        this.isProcessing = true;
        const task = this.queue.shift();

        try {
            await this.ensureReady();
            this.activeTurn = task;

            // Set prompt into textarea atomically without keystroke newline triggers
            await this.page.waitForSelector('textarea', { timeout: 15000 });
            await this.page.evaluate((text) => {
                const ta = document.querySelector('textarea');
                if (!ta) throw new Error('Textarea not found');
                ta.focus();
                const nativeSetter = Object.getOwnPropertyDescriptor(
                    window.HTMLTextAreaElement.prototype,
                    'value'
                ).set;
                nativeSetter.call(ta, text);
                ta.dispatchEvent(new Event('input', { bubbles: true }));
                ta.dispatchEvent(new Event('change', { bubbles: true }));
            }, task.prompt);

            // Click Send button (fallback to pressing Enter once on textarea)
            try {
                const sendBtnSelector = 'button[aria-label="Send"], .send-button';
                await this.page.waitForSelector(sendBtnSelector, { timeout: 4000 });
                await this.page.click(sendBtnSelector);
            } catch (clickErr) {
                logger.warn('Send button click failed, falling back to Enter key', 'BROWSER', '', clickErr.message);
                await this.page.focus('textarea');
                await this.page.keyboard.press('Enter');
            }

            // Safety timeout: 180s per completion
            const timeoutId = setTimeout(() => {
                if (this.activeTurn === task) {
                    task.onError(new Error('Generation timed out after 180s'));
                }
            }, 180000);

            const origDone = task.onDone;
            task.onDone = () => {
                clearTimeout(timeoutId);
                this.activeTurn = null;
                origDone();
                this.isProcessing = false;
                this.resetForNextTurn().then(() => this.processQueue());
            };

            const origError = task.onError;
            task.onError = (err) => {
                clearTimeout(timeoutId);
                this.activeTurn = null;
                origError(err);
                this.isProcessing = false;
                this.resetForNextTurn().then(() => this.processQueue());
            };
        } catch (e) {
            logger.error('Error during chat processing', 'BROWSER', '', e.message);
            task.onError(e);
            this.isProcessing = false;
            this.processQueue();
        }
    }

    async resetForNextTurn() {
        try {
            if (this.page) {
                await this.page.goto('https://chat.qwen.ai', { waitUntil: 'networkidle2', timeout: 20000 });
            }
        } catch (_) {}
    }

    async close() {
        this.isReady = false;
        if (this.browser) {
            try { await this.browser.close(); } catch (_) {}
            this.browser = null;
            this.page = null;
        }
    }
}

const browserBridge = new BrowserBridge();

module.exports = {
    browserBridge,
    BrowserBridge
};
