# 流感疫苗社交媒体文本人工标注工具（Streamlit + Supabase）

## 项目目标
本项目提供一个可本地运行、可部署到 Streamlit Community Cloud、可通过网页分享给多位标注员使用的人工标注工具。

核心特性：
- 电脑/手机浏览器均可使用
- 标注员仅输入 `annotator_name` 即可开始
- 点击标签后**立即保存到 Supabase**并自动跳转下一条
- 自动恢复个人进度（同名标注员再次进入可续标）
- 支持“上一条”回看并修改
- 提供轻量管理员汇总/导出

---

## 目录结构（建议）
```text
.
├─ app.py
├─ requirements.txt
├─ README.md
├─ data/
│  └─ reference_manual_prepare.csv
├─ sql/
│  └─ setup.sql
└─ .streamlit/
   └─ secrets.toml.example
```

> `app.py` 默认读取：`data/reference_manual_prepare.csv`  
> 样本文件至少包含两列：`id`、`text`（列名大小写可自动识别）。

---

## 1) 本地运行

### 1. 安装依赖
```bash
pip install -r requirements.txt
```

### 2. 准备数据文件
将样本文件放到：
```text
data/reference_manual_prepare.csv
```
要求：
- 必须包含 `id` 列
- 必须包含 `text` 列（若无 text，可在预处理阶段先生成）

### 3. 配置 Supabase secrets
复制示例文件并填写真实值：
```bash
cp .streamlit/secrets.toml.example .streamlit/secrets.toml
```

编辑 `.streamlit/secrets.toml`：
```toml
SUPABASE_URL = "https://YOUR_PROJECT_ID.supabase.co"
SUPABASE_KEY = "YOUR_SUPABASE_KEY"
```

### 4. 初始化数据库
在 Supabase SQL Editor 中执行：
```sql
sql/setup.sql
```

### 5. 启动应用
```bash
streamlit run app.py
```

---

## 2) Supabase 设计说明

表：`public.annotations`

核心字段：
- `annotator_name`：标注员名称/代号
- `sample_id`：样本 id
- `text_content`：保存当时文本（便于追踪）
- `human_label`：人工标签（1/2/3/4）
- `labeled_at`、`updated_at`

唯一约束（关键）：
- `unique(annotator_name, sample_id)`

这保证同一标注员对同一条只会有一条记录，重复标注会走 upsert 更新。

---

## 3) 标签编码

页面显示：
- `1 支持`
- `2 无立场`
- `3 延迟接种`
- `4 拒绝接种`

---

## 4) 部署到 Streamlit Community Cloud

1. 将项目代码推送到 GitHub（包含 `app.py`、`requirements.txt`、`sql/setup.sql` 等）。
2. 打开 Streamlit Community Cloud，选择仓库并部署 `app.py`。
3. 在 App 的 `Settings -> Secrets` 配置：
   - `SUPABASE_URL`
   - `SUPABASE_KEY`
4. 部署成功后获得网页链接，分享给标注员。
5. 标注员只需浏览器和网络，无需安装 Python。

---

## 5) 多人使用与安全提醒

当前是“快速可用版本”：
- 通过 `annotator_name` 区分标注员
- 未做复杂账号认证

正式大规模使用建议：
- 使用 Supabase Auth 登录
- 强化 RLS 策略（按用户/角色限制读写）
- 使用更细粒度权限（避免 anon key 暴露过大权限）

---

## 6) 使用体验说明

界面遵循“顺手标注”优先：
- 文本完整展示（可滚动）
- 标签按钮大且适配手机
- 点击即保存
- 自动下一条
- 不显示任何机器标注痕迹（qwen/deepseek 等）

---

## 7) 管理员轻量功能

应用内 `管理员：汇总与导出` 区域支持：
- 查看各 `annotator_name` 已标注数量
- 下载全部标注记录 CSV

