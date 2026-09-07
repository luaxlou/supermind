# 能力库建设与自举验收

Supermind 自身维护建设能力库的代码、测试和分发产物。插件调用锁定版本的本地
Python CLI；升级 Supermind 时一起获得这些能力，不依赖用户手动修改 Skill。
本轮不改变 Skill，也不将 Supermind 的工程文件自动登记成可复用能力。

## 已落实的检查

- `evaluate`、`register` 与复用判断共用 `quality.py` 的基础准入检查。
- 缺少名称、摘要、契约、来源、版本或内容标识，不能作为合格条目登记或推荐。
- 代码条目仅指向 `plugin.json`、`package.json` 等清单文件时拒绝准入。
- `audit` 检查已有条目，不发现新条目、不修改权威事件、不批准复用。
- 本地源码在 Git 仓库中时，比较记录版本中的对应文件与当前文件；不能解析版本
  就明确报告未验证。远程来源不冒充已完成版本核验，也不使用替代检索策略。

```bash
supermind-memory audit --format json
supermind-memory audit --capability-id digitagent.green-planet-phone-identity-service --format json
```

结果中的 `issues` 是机器发现的问题；`human_review_required` 是尚待人的判断。
`needs_human_review` 不代表可复用，`reuse_authorized` 始终为 `false`。
审查失败不会替用户修改或删除能力。

## 机器检查的边界

非空摘要不等于准确摘要；正收益分数不等于有真实需求；通过项目测试不等于
验证了该能力；源码文件一致不等于整个依赖闭包一致。
当前检查只验证字段、来源及所声明单文件版本；不自动证明完整依赖或内容哈希正确。
每个能力仍应结合具体需求说明必要性、资产位置、输入输出、业务边界、接入方法及
专属验证证据。没有抽象的业务实现保持待抽象；任何具体复用仍需人的确认。

## 如何自举验收

1. 在 Supermind 项目中修改运行时并运行质量、服务和 CLI 回归测试。
2. 同步 Python 包与插件版本，构建 wheel，更新锁文件中的 SHA-256。
3. 执行 `bash scripts/verify.sh`，完成分发一致性检查和全量测试；失败时停止发布。
4. 提交已验证的源码和分发产物，按确认的集成方式发布到远端 `main`，核对远端提交。
5. 从远端 marketplace 更新本地插件，不能把开发目录安装当成正式发布：

   ```bash
   codex plugin marketplace add luaxlou/supermind --ref main
   codex plugin marketplace upgrade supermind
   codex plugin add supermind@supermind
   ```

6. 核对安装来源、版本与 wheel 哈希，再用安装后的 CLI 执行 `health` 与 `audit`。

正常顺序是项目修改 → 验证 → 远端发布 → 本地插件更新 → 安装验收。
开发目录安装仅用于明确标注的开发测试，不能替代远端发布，也不能据此宣布正式更新完成。

验收必须区分源码已实现、安装版本已更新、真实能力审查结果，不能把其中一个
当作全部完成。未知问题保留明确的未验证结果，不自动放行。
