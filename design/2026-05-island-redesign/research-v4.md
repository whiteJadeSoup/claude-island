# v4 Research — Competitive UI Survey

User feedback on v3 (`prototype-v3.html`):

> 信息表达接受（session list + pending decision 信息架构 OK）
> 整体风格不喜欢，希望简洁、明了、信息展示清楚

v3 的失败模式：lab-console aesthetic 把所有文本都 mono、用琥珀 + 暖深色，
读起来 "hard / industrial"，跟用户期待的现代产品 UI 不在同一频道。

## 5 个相关参考

| 工具 | 调性 | 关键学习 |
|---|---|---|
| **Linear** | 现代产品 UI 极简标杆 | Inter Variable · `#08090A` bg · 紫蓝 `#5E6AD2` 主 · 状态 dot + 文字 · row hover 轻微 lift |
| **Vercel Dashboard** | 简洁深色 | Geist Sans + Geist Mono · `#0A0A0A` 全黑 · 1px outline · 圆角 6px · 数字 mono / 其余 sans |
| **Raycast** | Spotlight 加强版 | SF Pro · `#1E1E1E` · 8px radius · 极小 icon · 信息密度高 + 留白好 |
| **iOS Live Activities** | Dynamic Island 标杆 | SF Pro Display · `#000` 纯黑 · 鲜亮 accent · 圆角大 · 主信息一行说完 |
| **GitHub Actions** | 工程清单 | system sans · 浅深双模 · 圆角 6px · status icon 主导 · 不强调大字号 |

## 共识

1. **Sans-serif 主导，mono 仅给数字 / 代码 / 路径** — v3 全 mono 太硬
2. **状态色更友好** — waiting 用**橙** (`#fb923c`) 不是红；thinking 用**紫** (`#a78bfa`) 比琥珀更"思考感"
3. **状态指示用 inline dot + sans label** — 不要大字号 mono phase 名占视觉重量
4. **圆角 6-8px** — square corner 是 brutalist 信号，跟"简洁明了"冲突

## v4 三个候选方向

### v4a · Linear / Vercel 现代产品

- Geist Sans (UI) + Geist Mono (数字/代码/cwd)
- 纯黑 `#0a0a0a` + zinc 灰阶 + 蓝 `#3b82f6` accent
- Tailwind phase 调色：紫 / 翠绿 / 橙 / 蓝 / 灰
- 圆角 6-8px、hover lift
- 适合：技术开发者、追求"现代 SaaS 产品"感

### v4b · iOS Live Activities

- SF Pro Display / `-apple-system`
- 纯黑 `#000` + 鲜亮 accent
- 圆角 14-18px、信息密度极高
- 动态岛圆形环 + 进度环
- 适合：macOS 原生感、希望 floating 工具像系统组件

### v4c · GitHub / 工程清单

- system sans + 浅色 `#f6f8fa` 主 / 深色可切
- 高密度行（36-44px）
- 状态用 icon + 短 label
- 蓝 `#0969da` accent
- 适合：日常工程 dashboard、长时间观察不疲劳

## 决定

并行做 3 个精简 prototype（只含 capsule + 6-phase session list + waiting row + 一个 deck 缩略），用户挑方向后再深入完整 surface。
