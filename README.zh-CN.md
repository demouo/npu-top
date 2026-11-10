# npu-top

昇腾 NPU 终端监控工具。只读调用 `npu-smi info`，显示 AI Core 利用率、内存、温度、板卡功耗、进程及最近 60 秒的利用率变化。

[English](README.md)

![Ascend 910C 上的 npu-top](preview.png)

## 安装与运行

需要 Linux、Python 3.10 以上及昇腾驱动。先确认 `npu-smi info` 能正常运行。

```bash
git clone https://github.com/demouo/npu-top.git
cd npu-top
python3 -m venv .venv
.venv/bin/python -m pip install .
.venv/bin/npu-top --doctor
.venv/bin/npu-top
```

Python 依赖只有 Rich，不需要 PyTorch、torch_npu 或训练框架。按 `q` 或 `Ctrl+C` 退出，方向键左右或 `n`、`p` 翻页。

## 其他昇腾机器能否使用

程序不固定卡数、每卡芯片数、物理编号或内存容量。只要目标机器的 `npu-smi info` 输出符合支持的表格格式，就可以运行。

| 环境 | 验证情况 |
| --- | --- |
| Ascend 910C，8 卡 16 die，aarch64 Linux，npu-smi 25.5.2 | 已在真实硬件运行 |
| Ascend 950DT，8 个 NPU，aarch64 Linux，npu-smi 25.1.rc2 | 已在真实硬件运行 |
| 相同表格格式的其他 910、910B、910C 机器 | 预期可用，未逐机型实测 |
| 单芯片、仅有 `Memory-Usage(MB)` 的布局，包括 310 系列使用的同类布局 | 合成样例测试通过，没有 310 硬件实测 |
| Linux x86_64 | 无架构专用代码，没有该架构的硬件实测 |
| Windows、macOS 或其他输出格式 | 不支持；可编写布局插件适配 |

内置两种布局适配器：`ascend_910` 解析含 `NPU Name`、`Chip Phy-ID`、`AICore(%)` 的三列表格；`ascend_950` 解析含 `NPU ID`、`Name`、`Health`、`NPU Util(%)` 的四列表格。内存优先读取 `HBM-Usage(MB)`，否则读取 `Memory-Usage(MB)` 或 `DDR-Usage(MB)`。缺失指标显示为不可用，不记成 0；驱动没有返回进程表时，明确显示进程信息不可用。功耗按板卡去重。

## 布局插件

驱动输出格式不同，又不想改程序时，可以把一个 Python 文件放进目录，用 `--plugins 目录`（可重复）或 `NPU_TOP_PLUGINS=目录1:目录2` 加载。文件定义 `LAYOUT`，包含唯一的字符串 `key`、可选的 `description`、`detect(lines)` 和 `parse(lines, duration)`：

```python
import time
from npu_top import Device, Snapshot

class MyLayout:
    key = "my-driver"
    description = "每个 NPU 一行"

    def detect(self, lines):
        return any("MY UTIL(%)" in line for line in lines)

    def parse(self, lines, duration):
        return Snapshot(
            [Device(0, 0, 0, "AscendX", "OK", 100.0, 40.0, 50.0, 1024.0, 8192.0, "HBM")],
            [], time.time(), duration, False)

LAYOUT = MyLayout()
```

`lines` 只包含以 `|` 开头的表格行。`detect` 只对自己负责的格式返回 `True`；`parse` 返回 `Snapshot`，其中 `layout` 可以留空，由程序补上适配器名。插件文件以当前用户权限执行 Python 代码。`--doctor` 会列出可用布局，检测有歧义时可用 `--layout KEY` 强制指定。

容器内需要能访问驱动命令、驱动库及获准使用的设备。显示范围以 `npu-smi` 实际可见设备为准，不按 `ASCEND_RT_VISIBLE_DEVICES` 过滤。

## 常用命令

```bash
npu-top --doctor
npu-top -i 1
npu-top --once --no-color
npu-top --json
npu-top --npu-smi /usr/local/Ascend/driver/tools/npu-smi
npu-top --layout ascend_950
npu-top --plugins /path/to/layouts
```

默认每 2 秒查询一次，最短间隔 0.5 秒。也可通过 `NPU_SMI` 指定命令路径。未指定时先查找 `PATH`，再查找驱动标准目录。

重定向输入或输出时只打印一次快照。每次驱动查询最多等待 10 秒，交互模式在失败后重试并标出旧数据。`--json` 的缺失指标为 `null`。

如果 `--doctor` 失败，先在相同环境运行 `npu-smi info`，检查驱动与设备权限。输出格式不兼容时，可在 issue 中提供机型、驱动版本和脱敏后的输出。

测试命令与完整参数见 [English README](README.md)。源码采用 Apache-2.0 许可证，昇腾驱动及 `npu-smi` 由华为另行提供。
