# Murmur

语音口述 → AI 轻度整理 → 存入 [Memos](https://github.com/usememos/memos)。

用手机自带的语音输入法对着网页说话，AI 帮你改正语音识别的错字、理顺语句（保留口语风格），按日期归位后写进你的 Memos。

## 功能

- **后台整理**：提交后立即返回，AI 在后台处理，可连续口述多条
- **多天拆分**：一次口述里讲了好几天的内容，按日期自动拆条归位（设置 displayTime，Memos 时间线顺序正确）
- **空缺提醒**：主页显示近两周哪几天没写，点日期块直接补写
- **同日合并**：当天已有记录时自动合并整理为一条
- **错字词表**：常用人名、地名等专有名词表，AI 据此校正语音输入的同音错字；可从历史日记一键提取候选词
- **失败暂存**：AI 或 Memos 调用失败时原文自动存为草稿，可一键恢复重试
- **整理旧记录**（/organize）：扫描库里没有标签的旧 memo，AI 分类为日记/笔记/密码，日记修正发布日期并轻度润色，笔记/密码只打标签不动原文；所有改动需人工勾选确认
- **PWA**：手机浏览器"添加到主屏幕"后像 App 一样用

## 部署

```bash
git clone https://github.com/VaneEcho/murmur.git
cd murmur
cp .env.example .env   # 如需密码登录，设置 MURMUR_PASSWORD
docker compose up -d
```

打开 `http://服务器IP:8080/settings`，填写：

| 配置 | 说明 |
|---|---|
| Memos 地址 + API Token | Memos 设置页可生成 Token |
| LLM API 地址 + Key | 兼容 OpenAI 格式（DeepSeek、Ollama 的 `/v1` 等）|
| 模型名称 | 点"拉取可用模型列表"按钮选择 |
| 旧记录整理用模型 | 可选，整理 1000+ 条旧记录时建议选个快的 |

本地开发：

```bash
pip install -r requirements.txt
python3 -m uvicorn backend.main:app --reload --port 8080
```

## 注意事项

- **隐私**：所有经手的内容（包括"整理旧记录"扫描时的每一条 memo）都会发给你配置的 LLM。用云端 API（如 DeepSeek）时，密码类 memo 也会被发出去——整理旧记录建议用本地模型（Ollama）跑
- **不建议公网裸奔**：本项目按内网/本地使用设计，认证只是简单的密码 cookie。要公网用请套反向代理 + HTTPS
- **整理旧记录前备份** Memos 数据库（Docker 挂载目录里的 `memos_prod.db`）
- LLM 超时为 300 秒，本地大模型慢属正常；云端 API 通常几秒返回
- 日记靠 `#日记` 标签识别（可在设置改名）；笔记/密码整理后带 `#笔记`/`#密码` 伞标签，重扫自动跳过
