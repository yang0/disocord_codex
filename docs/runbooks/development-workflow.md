# 受管文件：由 evolution/scripts/bootstrap_governed_repo.py 生成。请在 evolution/templates/governed-repo 下修改模板。

# 仓库开发工作流

本仓库属于 `projectHome` 受管仓库，默认遵循以下开发方式：

- 先建中文 Issue，再开始正式开发
- 正式开发默认使用独立 `worktree`
- Pull Request 保持小而聚焦
- 可以并行的任务，优先拆成多个子任务并行推进
- 优先小步快跑，避免长时间隐藏式开发

## 推荐流程

1. 先明确一个中文 Issue
2. 如果目标较大，先拆成父 Issue 与多个子 Issue
3. 为当前任务创建独立 `worktree`
4. 在小范围内完成实现与验证
5. 提交一个聚焦的 PR 或等价的小批次变更

## 规范来源

- 全局开发规则：`/home/yang0/projectHome/evolution/docs/policies/projecthome-development-workflow-v1.md`
- 仓库地图与治理说明：`/home/yang0/projectHome/evolution/docs/runbooks/projecthome-repo-map.md`

## 不要直接改这里

如果需要调整这份 runbook 的通用内容，请去修改中央模板：

- `evolution/templates/governed-repo/docs/runbooks/development-workflow.md`
