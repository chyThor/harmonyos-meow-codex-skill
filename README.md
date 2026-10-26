# 鸿蒙用户的 GPT 任务通知 · MeoW Push Skill

面向 **鸿蒙 / HarmonyOS NEXT 设备持有者**：让本地 Codex / GPT 把你指定的任务完成结果或进度节点，推送到手机上的 MeoW。无需一直守着电脑看任务。

MeoW push notifications for HarmonyOS users — a local Codex agent skill for task results and requested milestones.

每位使用者配置自己的 MeoW 接收信息。本项目不提供共享账号，也不附带作者的账号、推送地址或个人配置。

## v1.1.0：可选的模型容量提醒

新增 [Windows 模型容量提醒组件](integrations/windows-capacity-watch/README.md)：真实主任务遇到明确的模型容量错误时，发送简短提醒；同一轮去重，同一任务设 5 分钟冷却，并过滤子智能体、标题和建议请求。

**这项高级组件需要已有兼容的 Windows MeoW/Codex 自动通知部署。仅安装下面的基础 Skill 不满足这个前提。** 请先阅读组件说明中的依赖和安装条件，再决定是否启用。基础 Skill 的安装与按需通知方式保持不变。

## 谁可以用

- **接收端**：安装了 MeoW 的鸿蒙设备，并已开启通知权限。
- **执行端**：支持加载本地 Skill、运行 Python 3.10+ 和访问网络的环境，例如本地 Codex 桌面端、CLI 或 IDE 插件。无需安装额外 Python 库。
- 当前 Windows 版有首次配置向导，已在 Windows 验证。macOS / Linux 使用私有文件配置，见[其他系统配置](docs/其他系统配置.md)；目前未在这两个系统进行实机验证。

接收手机和执行电脑可以使用不同系统。**普通 ChatGPT 网页或手机聊天不能直接运行这个本地脚本**；要从这些端使用，需要另行接入具备执行能力的服务。

## 1. 安装

从 [Releases](https://github.com/chyThor/harmonyos-meow-codex-skill/releases) 下载发布 ZIP 并解压，将其中的 `skills/meow-push` 整个文件夹放入 Codex 的用户技能目录。也可以通过页面上的 Code → Download ZIP 下载源码包。

| 系统 | 放好后的文件位置 |
| --- | --- |
| Windows | `%USERPROFILE%\.agents\skills\meow-push\SKILL.md` |
| macOS / Linux | `~/.agents/skills/meow-push/SKILL.md` |

Windows 可在资源管理器地址栏输入 `%USERPROFILE%\.agents\skills`，没有该目录就先创建。注意不要多套一层 `meow-push` 文件夹。已有同名 Skill 时先核对版本，避免重复安装。

安装后打开新任务，输入 `$meow-push`。如果找不到，重启 Codex，并检查上面的文件位置。部分旧版使用 `~/.codex/skills`；以实际 Skill 列表为准，同一份 Skill 只安装一次。

## 2. 首次配置自己的 MeoW（Windows）

先在鸿蒙手机登录自己的 MeoW，并从 MeoW 的说明或接口设置中取得以下信息；接口形式可参考 [MeoW 公开 API 文档](https://www.chuckfang.com/MeoW/api_doc.html)：

- **接口基础地址**：不包含接收标识、标题和正文的地址部分。
- **接收标识**：MeoW 接口使用的接收昵称或标识，不是华为账号密码。

当前脚本使用的接口形式是：`基础地址 / 接收标识 / 标题 / 正文`，通过 GET 发送。若你的接口要求不同，请先核对兼容性。

在电脑打开 PowerShell，执行：

```powershell
python "$env:USERPROFILE\.agents\skills\meow-push\scripts\meow_push.py" setup --device-label "我的电脑"
```

按提示输入自己的基础地址和接收标识。**输入时不会显示字符，这是正常的；不要把这些信息发到 GPT 聊天里。** 出现 `configured` 表示已保存。

`我的电脑` 可换成 `笔记本`、`工位` 等名称，推送标题会显示为 `GPT(我的电脑)·项目名`。配置保存在当前 Windows 用户的私有目录中并加密，不写进 Skill 文件夹。换电脑或 Windows 用户后需要重新配置。

检查配置：

```powershell
python "$env:USERPROFILE\.agents\skills\meow-push\scripts\meow_push.py" status
```

看到 `"configured": true` 即可。这一步只检查配置，不发送消息。

如果提示找不到 `python`，先安装 Python 3.10 或更新版本，并让终端可以运行 `python`。

## 3. 发一条测试消息

在 Codex 中发送这句话：

> 使用 $meow-push 给我发送一条测试通知，项目名是“通知测试”，内容是“配置完成”。只发送一次。

然后在鸿蒙手机查看 MeoW。脚本返回 `sent` 表示服务接口已接受；是否显示在手机上，还需要你实际确认。

## 4. 日常使用

**只要最终结果：**

> 完成这项任务并检查结果后，使用 $meow-push 给我推送一条最终摘要。执行中不推送。

**需要指定进度节点：**

> 使用 $meow-push，在数据检查完成和最终结果验证完成时，各给我推送一次简短摘要。

Skill 会按你的要求主动发送。它本身不会安装“每个任务都自动通知”的 Hook，也不会因为开始执行、生成建议或子任务结束就主动推送。每个任务都要自动通知，需要另行配置完成通知。

如果你已经安装了自动完成通知，通常让这个 Skill 只发送额外的指定节点，以免最终结果收到两条。

## 常见问题

- **手机没收到**：先检查 MeoW 的通知权限和账号是否正确，再查看 GPT 给出的安全状态码。不要把完整接口地址或配置文件贴到聊天或公开问题里。
- **返回 `duplicate`**：同一个节点已经发送过，程序跳过了重复发送。
- **返回 `delivery_unknown`**：结果不确定，先查看手机；程序不会自动重发，避免重复通知。
- **要更换接收账号或电脑名称**：在首次配置命令末尾加 `--replace`，重新输入自己的信息。
- **要分享给别人**：只分享本项目发布包。不要附带你的私有配置、日志或运行记录；对方完成自己的首次配置即可。

## 特别鸣谢

特别鸣谢 **MeoW 应用与推送接口的开发者及维护者**：感谢你们为鸿蒙设备提供消息接收能力、开放推送接口并维护使用文档，让电脑上的任务结果能够送达手机。

- [MeoW 公开接口文档](https://www.chuckfang.com/MeoW/api_doc.html)
- [fkccoding 的 MeoW Push Skill 公开项目](https://github.com/fkccoding/meow-push-skill)

本项目独立实现本地私有配置、节点去重和通知发送流程；MeoW 应用及服务由原开发者提供。服务规则以 MeoW 当前文档为准。

## 项目信息

源码、问题反馈和版本下载均通过 [GitHub 仓库](https://github.com/chyThor/harmonyos-meow-codex-skill) 提供。

- 基础 Skill：`skills/meow-push`。
- 可选容量提醒：`integrations/windows-capacity-watch`。
- [版本变化](CHANGELOG.md)。

安装目录和分发边界参考 [OpenAI 官方 Skill 文档](https://learn.chatgpt.com/docs/build-skills)。
