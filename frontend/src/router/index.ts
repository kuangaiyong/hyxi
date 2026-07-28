import { createRouter, createWebHistory } from 'vue-router'

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
]

const router = createRouter({
  history: createWebHistory(),
  routes,
})

export default router
