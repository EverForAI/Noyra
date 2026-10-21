# 发布和备份的磁盘生命周期

Ubuntu 安装器在停止旧服务前检查发布卷、备份卷和数据卷。相同设备的需求相加，不同设备分别检查；为新的 venv、加密备份、中间副本及恢复验证留出余量。不足时停止升级，旧服务继续运行。

升级生成的加密备份会恢复到加密数据卷上的临时目录，由原运行版本验证 AEAD、清单哈希和 SQLite，再删除临时恢复目录。通过后记录不含密钥的验证标记。新发布通过 readiness 后才记录发布标记并尝试清理。

默认保留最新 5 个已验证发布和备份，并额外保护 current、previous、被运行进程引用的发布及这些发布引用的备份。备份策略另行保护至少 2 个已验证恢复点，并检查 90 天年龄和 10 GB 空间上限。达到恢复点下限或被发布引用的备份不会因年龄或空间规则删除；计划会在 `backup_summary.space_status` 说明空间上限仍未满足的原因。没有本工具验证标记的手动备份不会自动删除，超过 7 天宽限期会在 `backup_attention` 中提示部署者。备份被修改或没有已验证替代品时，备份清理暂缓。它不代替不同主机上的恢复验收。

在安装源目录运行以下命令可预览清理；`--apply` 才执行。工具要求 Linux root，并在同一安装锁内重新生成清理计划。

```bash
python3 scripts/deployment-maintenance.py gc
python3 scripts/deployment-maintenance.py gc --apply --keep 5
python3 scripts/deployment-maintenance.py gc \
  --keep-verified-count 5 \
  --minimum-recovery-points 2 \
  --max-age-days 30 \
  --max-bytes 5000000000 \
  --unverified-grace-days 7
```

输出中的 `backup_details` 为每个待删除备份提供名称、大小、SHA-256 和 reason code；`--apply` 在安装锁内重新计算 plan，并再次核对文件摘要和验证标记后才删除。GC 失败不会把已通过健康检查的升级变成失败。自动清理不会绕过权限、删除任意路径或把未验证备份当作恢复点。首次启用时未标记的历史产物需部署者核对后手动处置。

Ubuntu 安装器会启用 `noyra-soak.timer`，启动两分钟后开始采样，此后每五分钟运行一次只读收集器。每条 JSONL 记录包含 live/ready、服务状态和重启计数、进程内存、数据与备份目录顶层文件总量及剩余空间、SQLite/WAL/SHM 大小、Provider 状态计数和最近 retention 结果。目录总量不递归扫描内部私有文件名；数据库只读查询只输出状态摘要，不含 provider ID、prompt、令牌或数据库内容。日志位于 `/var/log/noyra/soak.jsonl`，最多保留 10 MB 的最新记录；查看 timer 和采样结果：

```bash
systemctl status noyra-soak.timer --no-pager
tail -n 3 /var/log/noyra/soak.jsonl
```

收集器不会重启服务、请求模型或触发 SQLite checkpoint/VACUUM。至少七天 soak 的通过结论仍需结合采样、journal 和真实 Provider 调用证据，不由安装定时器自动宣告。
