import { createRouter, createWebHistory } from 'vue-router'
import { useToast } from '@/composables/useToast'

const routes = [
  {
    path: '/',
    redirect: '/tasks',
  },
  {
    path: '/config',
    name: 'Config',
    component: () => import('@/views/ConfigView.vue'),
  },
  {
    path: '/tasks',
    name: 'TaskManagement',
    component: () => import('@/views/TaskManagementView.vue'),
  },
  {
    path: '/sentiment',
    name: 'SentimentIndex',
    component: () => import('@/views/SentimentIndexView.vue'),
  },
  {
    path: '/sources',
    name: 'Sources',
    component: () => import('@/views/SourcesView.vue'),
  },
  {
    path: '/schedules',
    name: 'Schedules',
    component: () => import('@/views/ScheduleView.vue'),
  },
  {
    path: '/tasks/:id/progress',
    name: 'TaskProgress',
    component: () => import('@/views/TaskProgressView.vue'),
    props: true,
  },
  {
    path: '/tasks/:id/results',
    name: 'TaskResults',
    component: () => import('@/views/ResultsView.vue'),
    props: true,
  },
  {
    path: '/tasks/:id/sentiment',
    name: 'Sentiment',
    component: () => import('@/views/SentimentView.vue'),
    props: true,
  },
  {
    path: '/:pathMatch(.*)*',
    name: 'NotFound',
    component: () => import('@/views/NotFoundView.vue'),
  },
]

const router = createRouter({
  history: createWebHistory(),
  routes,
})

/**
 * 懒加载 chunk 拿不到时，**绝不能什么都不发生**。
 *
 * 上面每条路由都是 `() => import('@/views/XxxView.vue')`，构建出来的 chunk 文件名
 * 带内容哈希。页面开着的时候服务端换了一份构建（升级便携包、或者那个端口后面换了
 * 个服务），旧名字就全部 404，`import()` 于是 reject。而 **vue-router 对导航失败
 * 不做任何界面反馈** —— 不注册这个处理器就是静默吞掉：URL 不变、标题不变、
 * 侧栏高亮不变，用户点了一下什么都没发生，只有控制台里有一行报错。
 * 用户实测报过：「点击【LLM 配置】菜单没有任何反应」。
 *
 * 处理办法是**整页刷到目标路径**：那会重新拿 index.html，里面是新的 chunk 名。
 * 这也正是「升级了便携包但没硬刷新」那条路的正解。
 */
const RELOAD_FLAG = 'hyxi_chunk_reload'

// 浏览器对 dynamic import 失败的措辞各不相同，三种都要认（Chrome / Firefox / Safari）。
// **`Unable to preload CSS` 不能漏**：带自己 CSS 的视图（结果页、舆情详情页 —— 恰好是
// 最常用的两个）走的是另一条路，Vite 的 __vitePreload 先等 CSS link 加载，失败时抛的是
// 这句，而且是在 import() 执行**之前**就抛，所以拿到的永远是 CSS 那条报错。认不出它，
// 下面直接 return，那两个页面照样「点了没反应」—— 而且注册了 onError 之后，
// vue-router 自己的 console.error 兜底也没了，连控制台都不留痕（发版前评审实测）
const CHUNK_GONE =
  /dynamically imported module|Importing a module script failed|error loading dynamically imported|Unable to preload CSS/i

function readFlag(): string {
  // 隐私模式 / 禁用站点数据时 sessionStorage 会直接抛，不能让它把错误处理本身搞挂
  try {
    return sessionStorage.getItem(RELOAD_FLAG) || ''
  } catch {
    return ''
  }
}

/** 返回是否真的存下了 —— 存不下就不能刷，见 onError */
function writeFlag(value: string): boolean {
  try {
    if (value) sessionStorage.setItem(RELOAD_FLAG, value)
    else sessionStorage.removeItem(RELOAD_FLAG)
    return true
  } catch {
    return false
  }
}

router.onError((error, to) => {
  if (!CHUNK_GONE.test(String((error as Error)?.message ?? error))) return

  // **只自动刷一次**。刷完还拿不到就不是「换了构建」，是服务端真的没了 ——
  // 再刷就是无限循环，页面会一直闪，比原来的静默失败更糟。
  // 标记**存不下**时同样不许刷：那时每次都判成「还没刷过」，就是那个无限循环
  // （实测：禁用站点数据的浏览器上 8 秒整页导航 296 次，一个提示都没有）
  if (readFlag() === to.fullPath || !writeFlag(to.fullPath)) {
    writeFlag('')
    useToast().add('页面资源加载失败，可能是后端服务已停止。请确认服务在运行后手动刷新', 'error', 0)
    return
  }
  window.location.assign(to.fullPath)
})

// 成功到站就把标记清掉，否则下次再点同一个路由会被当成「刷过一次仍失败」
router.afterEach(() => writeFlag(''))

export default router
