export type Xp = { xp: number; streak: number; last: string | null; usd: number }
export type Art = { fire: string; firePop: string; water: string; waterPop: string }

declare module 'claude-code' {
  interface PluginState {
    'codarc': { xp: Xp | null; art: Art | null; mode: string | null; cel: string | null; pop: boolean | null; pend: string | null }
  }
}
