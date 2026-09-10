<template>
    <div class="w-full min-h-screen p-4">
        <div class="container mx-auto">
            <div class="flex flex-col md:flex-row justify-between items-center mb-6 px-4 space-y-4 md:space-y-0 pt-5">
                <h1 class="text-3xl font-bold">系统设置</h1>
                <router-link to="/"
                    class="action-button font-bold border border-blue-200 bg-blue-50 text-blue-900 px-4 py-2 rounded-xl shadow-sm hover:bg-blue-100 hover:border-blue-400 transition-all duration-300 transform hover:-translate-y-1 active:translate-y-0 text-center">
                    返回Token管理
                </router-link>
            </div>
            <div class="grid grid-cols-1 gap-6 p-4">
                <!-- API Key 管理 -->
                <div class="setting-card relative overflow-hidden rounded-2xl p-6 flex flex-col gap-4">
                    <div class="absolute inset-0 bg-white/30 backdrop-blur-md border border-white/30 rounded-2xl"></div>
                    <div class="relative flex flex-col gap-4">
                        <label class="text-gray-700 font-semibold text-lg">🔑 API Key 管理</label>

                        <!-- 管理员密钥 -->
                        <div class="bg-yellow-50 border border-yellow-200 rounded-lg p-4">
                            <div class="flex items-center gap-2 mb-2">
                                <span class="text-yellow-600 font-semibold">👑 管理员密钥</span>
                                <span class="text-xs bg-yellow-200 text-yellow-800 px-2 py-1 rounded">不可修改</span>
                            </div>
                            <input :value="settings.adminKey" type="text" readonly
                                class="w-full rounded-lg border-gray-300 bg-gray-100 shadow-sm h-10 text-sm px-3 cursor-not-allowed">
                        </div>

                        <!-- 普通密钥列表 -->
                        <div class="space-y-2">
                            <div class="flex items-center justify-between">
                                <span class="text-gray-700 font-semibold">🔐 普通密钥</span>
                                <button @click="showAddKeyModal = true"
                                    class="bg-green-500 text-white px-3 py-1 rounded-lg text-sm hover:bg-green-600 transition-all">
                                    + 添加密钥
                                </button>
                            </div>

                            <div v-if="settings.regularKeys.length === 0" class="text-gray-500 text-center py-4">
                                暂无普通密钥
                            </div>

                            <div v-for="(key, index) in settings.regularKeys" :key="index"
                                class="flex items-center gap-2 bg-gray-50 border border-gray-200 rounded-lg p-3">
                                <input :value="key" type="text" readonly
                                    class="flex-1 rounded-lg border-gray-300 bg-white shadow-sm h-8 text-sm px-3">
                                <button @click="deleteRegularKey(index)"
                                    class="bg-red-500 text-white px-3 py-1 rounded-lg text-sm hover:bg-red-600 transition-all">
                                    删除
                                </button>
                            </div>
                        </div>
                    </div>
                </div>

                <!-- 其他设置项 -->
                <div class="grid grid-cols-1 md:grid-cols-2 gap-6">
                    <!-- 自动刷新 -->
                    <div class="setting-card relative overflow-hidden rounded-2xl p-6 flex flex-col gap-4">
                        <div class="absolute inset-0 bg-white/30 backdrop-blur-md border border-white/30 rounded-2xl">
                        </div>
                        <div class="relative flex flex-col gap-2">
                            <label class="text-gray-700 font-semibold">🔄 自动刷新</label>
                            <div class="flex items-center gap-2">
                                <input v-model="settings.autoRefresh" type="checkbox"
                                    class="h-5 w-5 rounded border-gray-300 text-indigo-600 focus:ring-indigo-500">
                                <span>启用自动刷新</span>
                            </div>
                            <label class="text-gray-700">刷新间隔（秒）</label>
                            <input v-model.number="settings.autoRefreshInterval" type="number"
                                class="mt-1 block w-full rounded-xl border-gray-300 bg-white/60 shadow-sm focus:border-indigo-500 focus:ring-indigo-500 transition-all duration-300 h-12 text-base px-4">
                            <button @click="saveAutoRefresh"
                                class="w-full mt-2 bg-black text-white rounded-lg py-2 hover:bg-white hover:text-black border border-black transition-all duration-300">保存</button>
                        </div>
                    </div>
                    <!-- 批量登录并发数 -->
                    <div class="setting-card relative overflow-hidden rounded-2xl p-6 flex flex-col gap-4">
                        <div class="absolute inset-0 bg-white/30 backdrop-blur-md border border-white/30 rounded-2xl">
                        </div>
                        <div class="relative flex flex-col gap-2">
                            <label class="text-gray-700 font-semibold">⚡ 批量登录并发数</label>
                            <label class="text-gray-700">账号批量添加时的登录并发数</label>
                            <input v-model.number="settings.batchLoginConcurrency" type="number" min="1" max="20"
                                class="mt-1 block w-full rounded-xl border-gray-300 bg-white/60 shadow-sm focus:border-indigo-500 focus:ring-indigo-500 transition-all duration-300 h-12 text-base px-4">
                            <span class="text-xs text-gray-500">建议范围 1-20，并发越高越容易触发上游风控或本地网络抖动</span>
                            <button @click="saveBatchLoginConcurrency"
                                class="w-full mt-2 bg-black text-white rounded-lg py-2 hover:bg-white hover:text-black border border-black transition-all duration-300">保存</button>
                        </div>
                    </div>
                    <!-- 思考输出 -->
                    <div class="setting-card relative overflow-hidden rounded-2xl p-6 flex flex-col gap-4">
                        <div class="absolute inset-0 bg-white/30 backdrop-blur-md border border-white/30 rounded-2xl">
                        </div>
                        <div class="relative flex flex-col gap-2">
                            <label class="text-gray-700 font-semibold">💡 思考输出</label>
                            <div class="flex items-center gap-2">
                                <input v-model="settings.outThink" type="checkbox"
                                    class="h-5 w-5 rounded border-gray-300 text-indigo-600 focus:ring-indigo-500">
                                <span>启用思考输出</span>
                            </div>
                            <button @click="saveOutThink"
                                class="w-full mt-2 bg-black text-white rounded-lg py-2 hover:bg-white hover:text-black border border-black transition-all duration-300">保存</button>
                        </div>
                    </div>
                    <!-- 搜索信息模式 -->
                    <div class="setting-card relative overflow-hidden rounded-2xl p-6 flex flex-col gap-4">
                        <div class="absolute inset-0 bg-white/30 backdrop-blur-md border border-white/30 rounded-2xl">
                        </div>
                        <div class="relative flex flex-col gap-2">
                            <label class="text-gray-700 font-semibold">🔍 搜索信息显示模式</label>
                            <select v-model="settings.searchInfoMode"
                                class="mt-1 block w-full rounded-xl border-gray-300 bg-white/60 shadow-sm focus:border-indigo-500 focus:ring-indigo-500 transition-all duration-300 h-12 text-base px-4">
                                <option value="table">表格模式</option>
                                <option value="text">文本模式</option>
                            </select>
                            <button @click="saveSearchInfoMode"
                                class="w-full mt-2 bg-black text-white rounded-lg py-2 hover:bg-white hover:text-black border border-black transition-all duration-300">保存</button>
                        </div>
                    </div>
                    <!-- 简化模型映射 -->
                    <div class="setting-card relative overflow-hidden rounded-2xl p-6 flex flex-col gap-4">
                        <div class="absolute inset-0 bg-white/30 backdrop-blur-md border border-white/30 rounded-2xl">
                        </div>
                        <div class="relative flex flex-col gap-2">
                            <label class="text-gray-700 font-semibold">🎯 简化模型映射</label>
                            <div class="flex items-center gap-2">
                                <input v-model="settings.simpleModelMap" type="checkbox"
                                    class="h-5 w-5 rounded border-gray-300 text-indigo-600 focus:ring-indigo-500">
                                <span>只返回基础模型，不包含thinking、search、image等变体</span>
                            </div>
                            <button @click="saveSimpleModelMap"
                                class="w-full mt-2 bg-black text-white rounded-lg py-2 hover:bg-white hover:text-black border border-black transition-all duration-300">保存</button>
                        </div>
                    </div>
                </div>
            </div>

            <!-- 添加API Key模态框 -->
            <div v-if="showAddKeyModal"
                class="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50">
                <div class="bg-white rounded-lg p-6 w-96 max-w-90vw">
                    <h3 class="text-lg font-semibold mb-4">添加普通API Key</h3>
                    <input v-model="newApiKey" type="text" placeholder="请输入API Key"
                        class="w-full rounded-lg border-gray-300 shadow-sm h-10 text-sm px-3 mb-4">
                    <div class="flex gap-2 justify-end">
                        <button @click="showAddKeyModal = false"
                            class="px-4 py-2 bg-gray-300 text-gray-700 rounded-lg hover:bg-gray-400 transition-all">
                            取消
                        </button>
                        <button @click="addRegularKey"
                            class="px-4 py-2 bg-blue-500 text-white rounded-lg hover:bg-blue-600 transition-all">
                            添加
                        </button>
                    </div>
                </div>
            </div>
        </div>
    </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import axios from 'axios'

const settings = ref({
    apiKey: localStorage.getItem('apiKey'),
    adminKey: '',
    regularKeys: [],
    defaultHeaders: '',
    defaultCookie: '',
    autoRefresh: false,
    autoRefreshInterval: 21600,
    batchLoginConcurrency: 5,
    outThink: false,
    searchInfoMode: 'table',
    simpleModelMap: false
})

const showAddKeyModal = ref(false)
const newApiKey = ref('')

const loadSettings = async () => {
    try {
        const res = await axios.get('/api/settings', {
            headers: {
                'Authorization': localStorage.getItem('apiKey')
            }
        })
        settings.value.apiKey = res.data.apiKey
        settings.value.adminKey = res.data.adminKey || ''
        settings.value.regularKeys = res.data.regularKeys || []
        settings.value.defaultHeaders = JSON.stringify(res.data.defaultHeaders)
        settings.value.defaultCookie = res.data.defaultCookie
        settings.value.autoRefresh = res.data.autoRefresh
        settings.value.autoRefreshInterval = res.data.autoRefreshInterval
        settings.value.batchLoginConcurrency = res.data.batchLoginConcurrency
        settings.value.outThink = res.data.outThink
        settings.value.searchInfoMode = res.data.searchInfoMode
        settings.value.simpleModelMap = res.data.simpleModelMap
    } catch (error) {
        console.error('加载设置失败:', error)
    }
}

const saveApiKey = async () => {
    try {
        await axios.post('/api/setApiKey', { apiKey: settings.value.apiKey }, {
            headers: { 'Authorization': localStorage.getItem('apiKey') || '' }
        })
        alert('API Key保存成功')
    } catch (error) {
        alert('API Key保存失败: ' + error.message)
    }
}
const saveAutoRefresh = async () => {
    try {
        await axios.post('/api/setAutoRefresh', {
            autoRefresh: settings.value.autoRefresh,
            autoRefreshInterval: settings.value.autoRefreshInterval
        }, {
            headers: { 'Authorization': localStorage.getItem('apiKey') || '' }
        })
        alert('自动刷新设置保存成功')
    } catch (error) {
        alert('自动刷新设置保存失败: ' + error.message)
    }
}
const saveBatchLoginConcurrency = async () => {
    try {
        await axios.post('/api/setBatchLoginConcurrency', {
            batchLoginConcurrency: settings.value.batchLoginConcurrency
        }, {
            headers: { 'Authorization': localStorage.getItem('apiKey') || '' }
        })
        alert('批量登录并发数保存成功')
    } catch (error) {
        alert('批量登录并发数保存失败: ' + error.message)
    }
}
const saveOutThink = async () => {
    try {
        await axios.post('/api/setOutThink', { outThink: settings.value.outThink }, {
            headers: { 'Authorization': localStorage.getItem('apiKey') || '' }
        })
        alert('思考输出设置保存成功')
    } catch (error) {
        alert('思考输出设置保存失败: ' + error.message)
    }
}
const saveSearchInfoMode = async () => {
    try {
        await axios.post('/api/search-info-mode', { searchInfoMode: settings.value.searchInfoMode }, {
            headers: { 'Authorization': localStorage.getItem('apiKey') || '' }
        })
        alert('搜索信息模式保存成功')
    } catch (error) {
        alert('搜索信息模式保存失败: ' + error.message)
    }
}
const saveSimpleModelMap = async () => {
    try {
        await axios.post('/api/simple-model-map', { simpleModelMap: settings.value.simpleModelMap }, {
            headers: { 'Authorization': localStorage.getItem('apiKey') || '' }
        })
        alert('简化模型映射设置保存成功')
    } catch (error) {
        alert('简化模型映射设置保存失败: ' + error.message)
    }
}

// API Key 管理相关函数
const addRegularKey = async () => {
    if (!newApiKey.value.trim()) {
        alert('请输入API Key')
        return
    }

    try {
        await axios.post('/api/addRegularKey', { apiKey: newApiKey.value.trim() }, {
            headers: { 'Authorization': localStorage.getItem('apiKey') || '' }
        })
        alert('API Key添加成功')
        newApiKey.value = ''
        showAddKeyModal.value = false
        await loadSettings()
    } catch (error) {
        alert('API Key添加失败: ' + error.message)
    }
}

const deleteRegularKey = async (index) => {
    if (!confirm('确定要删除此API Key吗？')) return

    const keyToDelete = settings.value.regularKeys[index]
    try {
        await axios.post('/api/deleteRegularKey', { apiKey: keyToDelete }, {
            headers: { 'Authorization': localStorage.getItem('apiKey') || '' }
        })
        alert('API Key删除成功')
        await loadSettings()
    } catch (error) {
        alert('API Key删除失败: ' + error.message)
    }
}

onMounted(() => {
    loadSettings()
})
</script>

<style lang="css" scoped>
.setting-card {
    background: linear-gradient(135deg, rgba(255, 255, 255, 0.7), rgba(255, 255, 255, 0.3));
    box-shadow: 0 8px 32px 0 rgba(31, 38, 135, 0.10);
    transition: box-shadow 0.3s, transform 0.3s;
    position: relative;
}

.setting-card:hover {
    box-shadow: 0 12px 36px 0 rgba(31, 38, 135, 0.18);
    transform: translateY(-2px) scale(1.01);
}

.action-button {
    backdrop-filter: blur(4px);
    -webkit-backdrop-filter: blur(4px);
}
</style>
