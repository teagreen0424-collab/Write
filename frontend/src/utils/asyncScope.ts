/** 作品切换与组件卸载后，旧请求只能完成自身工作，不能更新当前页面。 */
export interface AsyncScopeToken {
  identity: string
  revision: number
}

export function createAsyncScope(identity: () => string) {
  let revision = 0
  let active = true
  return {
    capture: (): AsyncScopeToken => ({ identity: identity(), revision }),
    isCurrent: (token: AsyncScopeToken) => active
      && token.revision === revision && token.identity === identity(),
    invalidate: () => { revision += 1 },
    dispose: () => { active = false; revision += 1 },
  }
}
