# 上下文压缩后数据流

## 压缩触发时机

```python
# engineer.py
if not is_stateful and self._allow_compression:
    fixed_tokens = system_tokens + memory_tokens
    target_session_tokens = max(0, budget - fixed_tokens)
    session_groups = await self._compressor.compress_groups_async(
        session_groups, target_tokens=target_session_tokens, model_adapter=...
    )
```

压缩在**组装消息之前**执行，输入是 `session_groups`（事务组列表），输出是**替换后的** `session_groups`。

## 压缩前 vs 压缩后的消息序列

**压缩前（迭代 5，假设 8 个 group 超预算）：**
```
groups = [G₀(user_task), G₁(asst+tools→result), G₂, G₃, G₄, G₅, G₆, G₇]
                                                                    ↑ recent 2 组保护
```

**压缩后（SUMMARIZATION 策略）：**
```
groups = [S(frozen_summary), G₆, G₇]
            ↑ G₀~G₅ 的 LLM 摘要     ↑ 最近 2 组保持原样
```

**最终消息序列：**
```
[0] system (frozen prefix)                           ← IMMUTABLE ✅
[1] user("<conversation-summary>G₀~G₅ 摘要</...>")  ← summary group
[2] assistant(tool_calls=[...])                       ← G₆
[3] tool(result)                                      ← G₆
[4] assistant(tool_calls=[...])                       ← G₇
[5] tool(result)                                      ← G₇
[6] user("<context-update>memories...</context-update>")  ← injection (if any)
```

## KV Cache 影响分析

**关键问题：压缩将 `[G₀ G₁ G₂ G₃ G₄ G₅]` 替换为 `[S]`，这意味着 `messages[1]` 从 `G₀(user_task)` 变成了 `S(summary)`。**

```
压缩前迭代 4:
  [system₀] [G₀] [G₁] [G₂] [G₃] [G₄] [G₅] [G₆] [G₇]
  ├──────── KV cached ──────────────────────────────┘

压缩后迭代 5 (首次压缩):
  [system₀] [S]  [G₆] [G₇]
   KV HIT   MISS  MISS  MISS
             ↑ 完全不同的内容，messages[1] 开始全部 cache miss
```

**这是不可避免的 — 压缩本质上就是用摘要替换原始历史，token 序列必然不同。** 但压缩只在超预算时触发（长对话），且触发后：

```
压缩后迭代 6 (frozen summary 复用):
  [system₀] [S]  [G₆] [G₇] [G₈] [G₉]
   KV HIT   HIT   HIT  HIT  MISS  MISS
  ↑ S 未变化（frozen_summary hash 匹配）→ 从压缩后开始正常累积 cache

压缩后迭代 7:
  [system₀] [S]  [G₆] [G₇] [G₈] [G₉] [G₁₀] [G₁₁]
   KV HIT   HIT   HIT  HIT  HIT   HIT  MISS   MISS
  ↑ 全部复用 ✅
```

**直到下一次压缩触发（S 被扩展为 S'），才再次 cache miss。**

## 二次压缩的增量机制

```
迭代 10 (再次超预算):
  当前 groups = [S, G₆, G₇, G₈, G₉, G₁₀, G₁₁, G₁₂, G₁₃]

  compress:
    old = [S, G₆, G₇, G₈, G₉, G₁₀, G₁₁]  (可压缩)
    recent = [G₁₂, G₁₃]                      (保护)

    uncovered = [G₆, G₇, G₈, G₉, G₁₀, G₁₁] (S 已覆盖 G₀~G₅)
    → LLM(previous_summary=S.text, new_text=G₆~G₁₁)
    → S' (合并摘要, version=2)

  结果: [S', G₁₂, G₁₃]

KV cache:
  [system₀] [S']  [G₁₂] [G₁₃]
   KV HIT   MISS   MISS   MISS    ← 压缩触发时一次性 miss

  然后再次正常累积...
```

## 压缩策略对比

| 策略 | 行为 | KV Cache 影响 |
|---|---|---|
| **NONE** | 不压缩 | 无影响，但可能超预算 |
| **TRUNCATION** | 丢弃最旧 groups，保留最近 2 组 | 同 SUMMARIZATION，token 序列变化 → miss |
| **SUMMARIZATION** | LLM 增量摘要旧 groups，保留最近 2 组 | 触发时 miss，之后 frozen summary 复用 |
| **HYBRID** | 摘要旧 groups + 保留最近 N 组完整 | 同 SUMMARIZATION |

## Frozen Summary 复用机制

```python
# compressor.py
# 每次压缩前检查：
current_source_hash = self._compute_cache_key(old_groups)
frozen_hash_valid = (
    self._frozen_summary is not None
    and self._frozen_summary.source_hash == current_source_hash
)

# 如果 hash 匹配（old_groups 没变）→ 直接复用 frozen summary
# 如果 hash 不匹配 → 只压缩 uncovered 部分（增量）
uncovered_start = self._frozen_summary_group_count
uncovered_groups = old_groups[uncovered_start:]
# LLM(previous_summary + uncovered_groups) → 新 merged summary
```

## 压缩边界规则

| 组件 | 是否参与压缩 | 原因 |
|---|---|---|
| **Frozen Prefix (system)** | ❌ 永不压缩 | 不可变，KV cache 锚点 |
| **Session History (groups)** | ✅ 唯一压缩对象 | 占用最多 token |
| **Saved Memories** | ❌ 永不压缩 | 在 injection 中，不在 groups 中 |
| **Protected Groups** | ❌ 保护不压缩 | 最近 2 组 + 多模态内容 |
| **Injection Message** | ❌ 不参与 | 独立于 session_groups |

## 完整生命周期

```
Run Start (iter=0)
  │
  ├─ reset_compressor()                → 清除上一 run 的 frozen summary
  │
  ├─ iter 0~4: 正常迭代，groups 累积
  │   messages: [system₀] [G₀] [G₁] ... [G₇] [injection?]
  │   KV cache: 前缀递增缓存 ✅
  │
  ├─ iter 5: session_tokens > target_session_tokens → 触发压缩
  │   compress([G₀~G₇]) → [S, G₆, G₇]
  │   messages: [system₀] [S] [G₆] [G₇] [injection?]
  │   KV cache: system₀ HIT, 其余 MISS (一次性代价)
  │   frozen_summary = S (hash=xxx, covered=6)
  │
  ├─ iter 6~9: 正常迭代，groups 在 S 之后累积
  │   messages: [system₀] [S] [G₆] ... [G₁₁] [injection?]
  │   KV cache: system₀ + S + 旧 turns 全部 HIT ✅
  │
  ├─ iter 10: 再次超预算 → 增量压缩
  │   uncovered = [G₆~G₁₁] (S 已覆盖 G₀~G₅)
  │   LLM(previous=S.text, new=G₆~G₁₁) → S'
  │   messages: [system₀] [S'] [G₁₂] [G₁₃] [injection?]
  │   KV cache: system₀ HIT, 其余 MISS (一次性代价)
  │   frozen_summary = S' (hash=yyy, version=2, covered=12)
  │
  └─ ... 循环
```

## 总结

| 阶段 | KV Cache 行为 | 原因 |
|---|---|---|
| **正常迭代（无压缩）** | 前缀 + 全部旧 history = HIT，仅最新 turn MISS | append-only，前缀不变 |
| **首次压缩触发** | 仅 system₀ HIT，其余全 MISS | 历史被摘要替换，token 序列改变 |
| **压缩后迭代（frozen summary 复用）** | system₀ + S + 旧 turns = HIT，仅最新 MISS | S 不变（hash 匹配复用） |
| **二次压缩触发** | 仅 system₀ HIT，其余全 MISS | S 被 S' 替换 |

**压缩导致的 KV cache miss 是不可避免的代价（用 1 次 cache miss 换取 context 预算释放），但两次压缩之间的所有迭代都正常复用缓存。当前架构是正确的。**
