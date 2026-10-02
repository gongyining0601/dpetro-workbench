# DPetroWorkbench 智谱 AI 接入改造说明

> 日期：2026-10-02
> 范围：写稿（ai_writer.py）+ AI 初选过滤（ai_filter.py）默认服务商切换为智谱 GLM，原服务商保留为后备。

---

## 一、改了什么

把两个 AI 环节的默认服务商从「原服务商」切换为**智谱 GLM 免费模型**，并保留原服务商作为失败后备：

| 环节 | 原默认 | 现默认（智谱） | 后备 |
|------|--------|--------------|------|
| AI 写稿 `ai_writer.py` | 腾讯云 deepseek | GLM-4.7-Flash | 腾讯云 deepseek |
| AI 初选过滤 `ai_filter.py` | 硅基流动 Qwen2.5-7B | GLM-4.7-Flash | 硅基流动 Qwen2.5-7B |

**目标**：写稿与过滤长期走免费模型，省去腾讯云 / 硅基流动的额度消耗；原服务商作为兜底，保证任何情况下功能不中断。

---

## 二、改动文件明细

| 文件 | 改动内容 |
|------|---------|
| `.env` | 新增 `ZHIPU_API_KEY=<你的智谱Key>`（已 gitignore，不提交） |
| `config.py` | 新增智谱配置：`ZHIPU_API_KEY`、`ZHIPU_CHAT_URL`、`ZHIPU_CHAT_MODEL`（默认 GLM-4.7-Flash） |
| `ai_writer.py` | 写稿改为「智谱 → 回退腾讯」双服务商逻辑 |
| `ai_filter.py` | AI 初选改为「智谱 → 回退硅基」双服务商逻辑，保留关键词硬规则兜底与熔断 |
| `.env.example` | 补 `ZHIPU_API_KEY` 占位（供新环境初始化） |
| `README.md` | 新增「AI 服务商切换说明」小节 |

---

## 三、新增环境变量

| 环境变量 | 含义 | 是否必填 |
|---------|------|---------|
| `ZHIPU_API_KEY` | 智谱 Key（注册 https://bigmodel.cn → API 密钥 新建） | 不填则自动走原服务商 |
| `ZHIPU_CHAT_MODEL` | 智谱免费模型名，默认 `GLM-4.7-Flash` | 可选，一般用默认 |

> 不配置 `ZHIPU_API_KEY` 时，写稿自动走腾讯 deepseek、过滤自动走硅基流动 Qwen，**原有功能完全不受影响**。

---

## 四、行为逻辑（重要）

1. **写稿 `write_article(...)`**：
   - 若配置了 `ZHIPU_API_KEY` → 先调智谱 GLM；
   - 智谱调用失败（网络 / 429 限流 / 无返回）→ 自动回退腾讯 deepseek；
   - 返回结构 `{title, body, ok, error}` 保持不变，对 `app.py` 透明。

2. **AI 初选 `is_relevant(...)`**：
   - 先执行**石化关键词硬规则兜底**（不依赖 LLM，命中即返回）；
   - 未命中关键词 → 走智谱 GLM 判断；
   - 智谱失败 → 回退硅基流动 Qwen；
   - 保留连续失败熔断（`FAIL_CIRCUIT_BREAKER`）与失败回退计数。

3. **429 限流说明**：智谱免费模型高峰时段可能返回 429「访问量过大，请稍后再试」，代码已自动回退原服务商兜底，**不会中断功能**；但回退会消耗原服务商（腾讯/硅基）配额，量小可忽略。

---

## 五、验证结果（2026-10-02 实测）

- ✅ 语法编译检查通过（config / ai_writer / ai_filter）
- ✅ 无智谱 Key → 写稿回退腾讯成功（返回标题+正文）、过滤回退硅基成功（相关/无关判断正确）
- ✅ 配智谱 Key → 过滤走智谱正常；写稿拦截确认「智谱 429 → 回退腾讯 200 → 成功」

---

## 六、以后怎么维护

- **更换免费模型名**：智谱官方免费模型若有变动，改 `config.py` 的 `ZHIPU_CHAT_MODEL`（或设环境变量 `ZHIPU_CHAT_MODEL`）即可，无需改代码逻辑。
- **停用智谱**：删掉 `.env` 里的 `ZHIPU_API_KEY` 行，程序自动回到原服务商。
- **接入新环境**：复制 `.env.example` 为 `.env`，填入 `ZHIPU_API_KEY` 与原有 `DATABASE_URL`、`TENCENTCLOUD_API_KEY`、`SILICONFLOW_API_KEY`。

---

## 七、安全提醒

- `.env` 已被 gitignore，**不会提交到 GitHub**；云端部署请用 Streamlit Secrets / GitHub Actions secrets 注入，勿写进代码或提交。
- API Key 属于敏感凭证，不要完整贴在聊天记录 / 截图 / 公开文档中；如不慎泄露，请到智谱控制台重置。
