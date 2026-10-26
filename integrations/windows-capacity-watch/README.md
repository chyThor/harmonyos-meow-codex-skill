# 可选：Codex 模型容量提醒（Windows）

当本机 Codex 主任务记录明确的模型容量不足错误时，通过已有 MeoW 接收配置发送简短提醒。例如：

> 模型容量不足：当前服务繁忙，任务可能正在自动重试。可稍候查看，或切换其他可用模型。

## 先确认是否适合安装

**本组件面向已经有兼容 Windows MeoW/Codex 自动通知部署的用户。基础 `meow-push` Skill 的本地加密配置不能直接替代这个部署。**

需要：

- Windows 和 Python 3.10+。
- 本机 Codex 的 `CODEX_HOME`（通常为用户目录下的 `.codex`），含可读取的 `logs_*.sqlite` 与会话来源资料。
- 自己现有的通知部署目录，内含 `MeowPush.ps1`、`CodexNotify.ps1`、`classify_codex_thread.py` 及自己的 `meow.config.json`。
- `MeowPush.ps1` 需兼容 `-Title`、`-Message`、`-Source`、`-EventKey`、`-RetryCount` 和 `-Quiet` 参数；当前发送接口使用 GET 路径参数。

本组件不附带接收账号、接口地址或上述私人配置，也不把基础 Skill 的 DPAPI 配置转换成另一份配置。尚无兼容自动通知部署的用户，可先使用[基础 Skill](../../README.md)的按需通知。

## 工作方式

- 每 2 秒只读检查本机日志数据库的增量记录，不向模型服务器轮询。
- 限定原生模型请求错误来源及明确容量错误代码或短语。聊天引用、工具输出、缓存 `capacity` 指标、普通网络故障和通用 429/503 不触发。
- 只提醒已确认的主任务。子智能体、标题/建议请求及未知来源静默；用户主动创建和 fork 的正常主任务仍可提醒。
- 有轮次编号时同轮只提醒一次；同一任务还有 5 分钟冷却。发送前保存事件预留记录，不确定的发送结果不自动重发。
- 首次启动及重启从当前位置开始，不补发旧日志；运行期间的新日志库只处理本次运行后的近期错误。
- 提醒表示模型暂时繁忙，任务可能仍在重试。原有任务完成通知继续工作。

## 安装

先在当前组件目录执行完整性校验和离线测试：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\Verify-Package.ps1
python -X utf8 .\test_capacity_watch.py
```

然后填写这台电脑实际使用的目录与解释器路径：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\Install-CapacityWatch.ps1 -DeploymentDirectory "已有通知部署目录" -PythonExecutable "本机python.exe绝对路径" -DeviceLabel 工位
```

设备标签可用 `工位` 或 `笔记本`；不传时读取已有通知设置。标题为 `GPT(设备标签)·项目名`。

安装器只部署容量监听器和自己的设置，备份旧容量配置，隐藏启动并注册当前用户登录启动。只有进程存活、心跳正常、日志表完整且可读时才报告成功。请把运行目录放在本地私人位置，不在 Git 仓库中保存接收信息。

## 查询、停止、卸载

```powershell
python "部署目录\capacity_watch.py" status --config "部署目录\capacity-watch.settings.json"
python "部署目录\capacity_watch.py" stop --config "部署目录\capacity-watch.settings.json"
```

正常状态为 `running`、`healthy: true`、`source_ready: true`。请使用与安装时相同的用户权限查询；受限沙盒可能无权核对其他权限环境中的进程。

停止只影响当前进程，保留登录启动设置。重新执行安装命令可启动；给安装命令增加 `-Uninstall` 则停止本监听并移除它自己的登录启动项，保留原任务完成通知及备份。

备份位于实际部署目录的 `.state/capacity-watch-backups`。恢复时只处理本组件的脚本、设置和启动项，不覆盖其他 Hook 或接收配置。

## 已验证范围

33 项新增离线测试及 52 项原有通知回归通过；独立审查覆盖误触发、来源过滤、去重、未知发送、日志切换、进程状态和安装健康检查。

容量错误识别使用与实际原生记录结构一致的模拟数据验证；当前验证样本没有自然发生的容量不足事件，不能据此声称手机已显示真实容量错误提醒。测试不向真实账号发送消息。

Codex 未来版本可能改变原生日志表或错误格式，需要重新验收。安装器会拒绝将缺少必需字段的日志源报告为就绪。

## 隐私

监听器不复制原始错误或聊天正文到推送。运行状态保存游标、事件哈希、时间与安全结果；接收账号和地址由原有私人配置提供。

不要提交 `capacity-watch.settings.json`、接收配置、`.state`、备份、日志数据库或本地测试记录。发布目录只含通用代码、测试、文档和校验清单。

感谢 MeoW 应用与接口开发者及维护者提供消息接收能力；相关鸣谢与公开文档链接见[项目首页](../../README.md#特别鸣谢)。
