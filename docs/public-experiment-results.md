# PaperTrail：公开实验结果摘要

本文件只包含脱敏汇总指标，不包含题目、Gold、论文原文、逐题答案、检索候选或原始请求日志。

## Stage 16A5 开发集

48 道新增开发题中 47 组完成 Basic/Hybrid QA，45 组获得有效配对 LLM Judge 评分（n=45）。Answer Accuracy：Basic 85.6%（3.422/4）→ Hybrid 96.7%（3.867/4），差值 +11.1 pp。Faithfulness：Basic 93.9%（3.756/4）→ Hybrid 98.3%（3.933/4），差值 +4.4 pp。Judge 协议为 `stage16a5-compact-answer-judge-v1`。Repair 两个有效配对均未观察到评分变化。

## Stage 16A6 独立文本测试

30 道文本题共 60 个运行条件，48 次 QA 成功，17 组获得有效配对 LLM Judge 评分（n=17）。比较为 Basic 与 Hybrid + Verifier/Repair 联合流程。Answer Accuracy：Basic 85.3%（3.412/4）→联合流程 89.7%（3.588/4），差值 +4.4 pp。Faithfulness：Basic 94.1%（3.765/4）→联合流程 100.0%（4.000/4），差值 +5.9 pp。Judge 协议为 `cited-evidence-judge-v4`。

## 解释边界

百分比是 0–4 分均值除以 4 的归一化结果，不是人工答案正确率或事实支持率。Faithfulness 100% 不证明不存在幻觉。两个阶段使用不同 Judge 协议，不能合并均值。Stage 16A6 对照是联合流程，不能分离各模块贡献。正式 qrels 覆盖不足，不报告正式 Precision、Recall、MRR 或 nDCG。失败和未知状态保留在原始阶段报告中；本公开版不发布逐题数据。
