# 声画织场 · 离线交付工具与中断演练手册

本目录位于仓库（`/workspace`）之外，**不修改任何生产服务**。所有演练数据都在
本目录的 `drills/` 临时区，工具在 `tool/`，小工作空间在 `workspace/`（只放
清单与计划，从不在本地复制整份媒体）。

## 目录

```
loom-delivery/
├── tool/
│   ├── build_fixture.py   # 用真实 reel_loom 服务生成可辨识音视频、字幕、新旧修订、印本
│   ├── loom_delivery.py   # 交付工具：plan / deliver / verify（纯标准库）
│   └── offline_verify.py  # 随包分发的接收者离线验证器（无数据库、无服务、可离线）
├── workspace/
│   └── delivery-plan.json # 钉死的交付计划（小块工作空间，无媒体字节）
└── drills/
    ├── src-volume/        # 模拟生产卷（含 _PRIVATE_DO_NOT_DELIVER 私密材料）
    ├── prod-workspace/    # reel_loom 服务数据（SQLite + 内容寻址 blob）
    ├── removable-drive/   # 模拟经常拔插的移动盘（最终交付包）
    ├── receiver/          # 隔离的独立接收者环境
    └── fault-unplug.json  # “拔盘”故障注入配置
```

## 一、生成可辨识的真实媒体与新旧修订

`build_fixture.py build` 在进程内驱动真实的 `reel_loom`（PyAV 16.1 +
FFmpeg 7.0.2），生成 4 个剪辑卷，每卷 2 个修订：

- 每卷使用不同的 FFmpeg 视频源（testsrc2 / smptebars / gradients / testsrc）
  与不同频率音调，媒体彼此可辨识；
- 每修订产出真实 MP4 成片、PCM WAV、冻结字幕投影 SRT、人类可读冻结稿件、
  以及服务自身的确定性印本 ZIP；
- 另生成 `question-bank.md` 与 `audit-notes.md` 两份**禁止外发**材料。

```bash
/home/node/.local/venvs/loom/bin/python -I tool/build_fixture.py build \
  --out drills/src-volume --workspace drills/prod-workspace
```

## 二、冻结交付计划（钉死，不随上游“最新稿”漂移）

```bash
python3 -I tool/loom_delivery.py plan \
  --catalog drills/src-volume/source-catalog.json \
  --plan workspace/delivery-plan.json
```

`plan` 对所选的每个字节计算一次 SHA-256 并写入计划，附 `plan_sha256`。
此后所有 `deliver` 只复制计划钉住的哈希；上游再生成新修订也不会被悄悄采用。
计划同时显式登记 `excluded_paths`，私密材料永不被枚举或复制。

## 三、可恢复传输与中断演练

故障注入文件 `drills/fault-unplug.json` 设定复制 2,000,000 字节后触发
EIO 式错误，模拟移动盘中途断开。工具随即干净停止，保留 `.part` 半文件但
**绝不信任**它；“拔盘/重插”用目录重命名模拟（本环境无 FUSE/挂载命名空间）。

实际演练的四次运行（均记录在 `acceptance-record.json`）：

| 运行 | 注入/事件 | 结果与退出码 |
| --- | --- | --- |
| 1 | 2 MB 处拔盘 | 10 份完成、1 个半文件，`interrupted_resumable`，退出 2 |
| 2 | 重插；上游新增 r3；篡改 1 个未复制源；损坏 1 个已复制盘文件 | 半文件丢弃重传；损坏文件按钉住源重传修复；漂移源 `source_drift` 拒绝，39/40，退出 3 |
| 3 | 从服务内容寻址存储按**钉住哈希**恢复源字节（非新稿） | 最后 1 份重传，40/40 `complete`，退出 0 |
| 4 | `--revalidate` 强制全量重读 | 40/40 复核通过，退出 0 |

关键保证：

- **恢复不复制第二份媒体仓库**：目标盘是媒体字节的唯一落点；工作空间仅 20K。
- **半文件不算完成**：完成判定只认“整文件哈希 == 计划哈希”，`.part` 每次删除重流。
- **不转最新稿**：运行 2 时上游 r3 已存在，计划仍只交付 r1/r2；被篡改源
  报 `source_drift` 而非用新内容顶替。
- **损坏已复制内容可自愈**：运行 2 中被损坏的 harbor WAV 因哈希不符被识别并重传。

## 四、机器可读验收记录

`removable-drive/acceptance-record.json` 累积每次运行：逐文件状态
（`verified` / `retransmitted` / `source_drift` / `source_missing` / `failed`）、
计数、故障注入标记与图例；完成时另生成 `package-manifest.json` 并把
`offline_verify.py` 复制进包。

## 五、独立接收者离线复验

接收者目录只有系统 Python（`env -i` 清空环境、`-I` 隔离模式），无数据库、
无 reel_loom、无网络：

```bash
env -i PATH=/usr/bin:/bin python3 -I offline_verify.py \
  --package . --expect-complete     # 退出 0 = 可接收
```

验证器逐文件核对摘要与大小、校验印本 ZIP 内部成员、确认每个修订的
媒体↔字幕↔冻结稿件五角色齐备、扫描私密材料与残留半文件，并写出
`verification-report.txt`。负向测试确认：篡改 1 字节或夹带题库文件都会
得到 `FAIL … must retransmit`（退出 4）。

## 退出码约定

| 码 | 含义 |
| --- | --- |
| 0 | 完成且全部核验通过 |
| 2 | 移动盘中断，重连后重跑即可续传 |
| 3 | 源漂移/源缺失，需人工决定（重新导出或显式重选） |
| 4 | 包损坏且无法在当前源下修复 |
| 1 | 其他用法/运行错误 |
