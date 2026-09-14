# 本地 E2E benchmark

用例在 `tests/e2e/test_full_pipeline.py` 中动态生成本地 Git 仓库，不访问远程模型或远程 Git。它们通过真实 Temporal、Postgres、MinIO、Docker 沙箱验证完整工作流；因此运行时必须先启动 Docker Desktop。

```powershell
uv run python -m benchmarks.run_e2e --output benchmark-results/latest.json
uv run python -m benchmarks.run_e2e --output benchmark-results/next.json --baseline benchmark-results/latest.json
```

报告按 `cases.json` 中固定的六个场景给出通过率、运行时间、模型调用数、估算费用、沙箱时间等。这里的模型输出是确定性的假响应，费用指标只能用于比较工作流开销，不代表真实线上 token 价格。失败或跳过用例仍会写报告，脚本返回非零退出码。输出文件不覆盖已有报告。也可使用 `--junit path/to/results.xml` 只解析已有 pytest JUnit 文件，不启动基础设施。
