# 发布与验收流程

## 持续集成边界

[CI workflow](../.github/workflows/ci.yml) 在每次推送 `main` 和 Pull Request 时执行：

1. 在 Ubuntu 与 Windows 的 Python 3.10、3.11 上安装包。
2. 运行单元测试及默认禁用的真机集成测试。
3. 构建 wheel 并上传为 workflow artifact。

GitHub Actions 不持有真实设备、`tests/integration.json`、AScript 密码或 `iproxy`。因此它只能证明客户端行为和打包质量，不能替代真机验收。

## 本地发布检查

发布人应在合并前执行：

```bat
py -m unittest discover -s tests -v
py -m pip install --user --upgrade .
py -m asclient status
```

再执行配置化只读真机 smoke：

```bat
copy tests\integration.example.json tests\integration.json
edit tests\integration.json
powershell -ExecutionPolicy Bypass -File scripts\windows-smoke.ps1 -Install
```

`tests/integration.json` 必须有：

- 正确的目标地址或 USB 回环地址；
- `enabled: true`；
- 当前目标 App 中唯一、经过 Inspector 验证的 `expected_selector`。

该脚本不会点击、输入、上传、删除、运行项目或执行 `eval`。它生成的 `artifacts/integration/<timestamp>` 应作为发布证据保存。

## 版本策略

- 补丁版本：修复缺陷、文档纠正、无行为扩展的小改动。
- 次版本：增加向后兼容的公开 API、命令或配置字段。
- 主版本：破坏现有公开 API、配置格式或 CLI 默认行为时才使用。

每个版本必须同步更新 `pyproject.toml`、`setup.py`、API 文档首行和变更说明。不要仅修改 wheel 文件名或手工修改 `site-packages`。

## 发布步骤

1. 确认工作区干净，审阅变更和文档。
2. 运行 CI 对应的本地单元测试。
3. 在至少一台目标 iPhone 上完成 smoke，并保留工件。
4. 更新版本、变更说明与兼容性矩阵。
5. 合并到 `main`，确认 GitHub Actions 通过。
6. 使用已验证提交创建 Git tag，例如 `v0.6.6`。
7. 在 Windows 从新克隆或干净虚拟环境执行安装验证。
8. 在目标真机运行 `py -m asclient doctor --report artifacts\doctor.json`；USB 场景还须完成 `tunnel`、`status` 和 `log` 验收。

## 回滚

客户端回滚不需要修改 IPA：

```bat
git checkout <last-known-good-commit-or-tag>
py -m pip install --user --upgrade .
py -m asclient status
```

记录失败版本、设备/AScript/目标 App 版本、`manifest.json`、截图、XML、日志及命令输出。回滚后不要删除失败证据。
