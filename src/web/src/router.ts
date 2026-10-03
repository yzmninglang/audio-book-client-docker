import { createRouter, createWebHistory } from 'vue-router'

export const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', redirect: '/projects' },
    {
      path: '/projects',
      name: 'projects',
      component: () => import('@/views/ProjectList.vue'),
      meta: { title: '工程列表' },
    },
    {
      path: '/projects/:name',
      name: 'bindings',
      component: () => import('@/views/ProjectBindings.vue'),
      props: true,
      meta: { title: '角色绑定' },
    },
    {
      path: '/projects/:name/synthesis',
      name: 'synthesis',
      component: () => import('@/views/Synthesis.vue'),
      props: true,
      meta: { title: '合成进度' },
    },
    {
      path: '/voices',
      name: 'voices',
      component: () => import('@/views/VoiceLibrary.vue'),
      meta: { title: '音色库' },
    },
    {
      path: '/settings',
      name: 'settings',
      component: () => import('@/views/Settings.vue'),
      meta: { title: '设置' },
    },
    { path: '/:pathMatch(.*)*', redirect: '/projects' },
  ],
})
