<script setup lang="ts">
import { ref } from 'vue'
import ExperimentScoreChart from './ExperimentScoreChart.vue'

type Stage = 'stage16a5' | 'stage16a6'
const copied = ref('')
const copyError = ref(false)
const reportText = ref('')
const reportTitle = ref('')
const reportDialog = ref<HTMLDialogElement | null>(null)

const sources: Record<Stage, {
  summary: string; summaryPath: string; latest: string; manifest: string; manifestSha: string
  report: string; reportSha: string; datasetSha: string; protocol: string; protocolSha?: string
}> = {
  stage16a5: {
    summary: 'cc3afcb09f5b72447dfaa48ffeb74ff19acd882ee496fdb2fbcf5e5009ef5fad',
    summaryPath: 'public-data/stage16a5-summary.json',
    latest: 'public-data/latest.json',
    manifest: 'public-data/experiment-manifest.json',
    manifestSha: '96215159ed460834dfffad4f916af815aed8c7cb2071b29643c2492c520c6c99',
    report: 'docs/public-experiment-results.md',
    reportSha: '7311b9a69030888db68569e89723baf51b45cea3eebb25b91eb19e34381cec57',
    datasetSha: 'ea9028f3c4633d9daf4fba6e4a078a2a8f83ec0d1b12580d3ef65a212282f452',
    protocol: 'stage16a5-compact-answer-judge-v1',
  },
  stage16a6: {
    summary: '9e18c59fa91bc428371bebb44429b390a0b59d4d44912632497c9ed57054fc27',
    summaryPath: 'public-data/stage16a6-summary.json',
    latest: 'public-data/latest.json',
    manifest: 'public-data/experiment-manifest.json',
    manifestSha: '96215159ed460834dfffad4f916af815aed8c7cb2071b29643c2492c520c6c99',
    report: 'docs/public-experiment-results.md',
    reportSha: '7311b9a69030888db68569e89723baf51b45cea3eebb25b91eb19e34381cec57',
    datasetSha: '9d3d96c29a926b7c97e736bfb8fcd60326fae157cd31862bc10030132fbffd1d',
    protocol: 'cited-evidence-judge-v4',
    protocolSha: '3a738ff69733146e5bbcc6379f1e619dd65424a6b5c1d0212a6887834b7f9108',
  },
} as const

async function copySha(stage: Stage, key: keyof typeof sources.stage16a5 | keyof typeof sources.stage16a6) {
  copied.value = ''
  copyError.value = false
  const value = sources[stage][key]
  if (typeof value !== 'string') return
  try {
    await navigator.clipboard.writeText(value)
    copied.value = `${stage}-${key}`
    window.setTimeout(() => { copied.value = '' }, 1800)
  } catch {
    copyError.value = true
  }
}

async function openReport(stage: Stage) {
  const source = sources[stage]
  reportTitle.value = stage === 'stage16a5' ? 'Stage 16A5 开发集报告摘要' : 'Stage 16A6 独立测试报告摘要'
  reportText.value = stage === 'stage16a5'
    ? `本页展示 Stage 16A5 新增 48 道开发题；72 题总集有 66 组完整 QA 配对。\n\n新增题中 47/48 完成 Basic 与 Hybrid 初始答案，45/47 组有有效 Judge 配对。Accuracy 均值 3.422→3.867，Faithfulness 3.756→3.933，都是 LLM Judge 的 0–4 分量表。原有 24 题使用不同 Judge 协议，单独列示。\n\nRepair 实际触发 2 次，两组评分均为 4→4，没有观察到质量变化。正式 qrels 覆盖不足，不报告正式检索排序指标。\n\n锁定报告：${source.report}\nSHA-256：${source.reportSha}`
    : `Stage 16A6 包含 30 道独立文本测试题，共 60 个运行条件；QA 成功 48/60，19 组完整 QA 配对中有 17 组有效 Judge 评分。\n\nAccuracy 均值 Basic 3.412、Hybrid 联合流程 3.588；Faithfulness 3.765、4.000。对照流程同时包含 Hybrid、Verifier 与 Repair，不能把差异归因于单独模块。Repair 未证明质量收益。正式 qrels 为 0，不报告正式检索排序指标。六道 NASA 视觉辅助题不计入文本主指标。\n\n锁定报告：${source.report}\nSHA-256：${source.reportSha}`
  reportDialog.value?.showModal()
}
</script>

<template>
  <section class="reports-page evaluation-page">
    <div class="notes-head">
      <div><h1>真实 RAG 实验结果</h1><p class="muted">只读展示已保存的开发集与独立测试结果；本页不会触发 Chat、Embedding 或 Judge 请求。</p></div>
      <span class="evaluation-badge">锁定汇总</span>
    </div>

    <div class="evaluation-callout"><strong>解读范围</strong><span>分数来自独立 LLM Judge，不是人工评分。两个阶段使用不同 Judge 协议，因此分开展示。没有充分正式 qrels 的检索排名指标不报告。</span></div>

    <div class="evaluation-grid">
      <article class="evaluation-card">
        <div class="evaluation-card-head"><div><h2>Stage 16A5 · 新增开发题</h2><p>Basic vs Hybrid 初始答案 · compact Judge v1</p></div><span>开发集</span></div>
        <div class="evaluation-stats">
          <div><small>完成 QA 配对 <abbr class="metric-help" tabindex="0" title="两种初始 QA 条件都成功并保存的题目数；分母为新增开发题 48 道。">i</abbr></small><strong>47 / 48</strong></div>
          <div><small>有效 Judge 配对 <abbr class="metric-help" tabindex="0" title="在 47 组已完成 QA 配对中，两个答案均获得有效 Judge 分数的题数；另有 2 组评分失败。">i</abbr></small><strong>45 / 47</strong></div>
          <div><small>未知向量结果 <abbr class="metric-help" tabindex="0" title="DEV-041 的查询 Embedding 结果未知，因此没有执行该题 QA；未知不视为成功或失败。">i</abbr></small><strong>1</strong></div>
        </div>

        <div class="evaluation-substats">
          <span><abbr class="metric-help" tabindex="0" title="Basic 与 Hybrid 各完成 47/48 个初始答案请求；另有 1 题因查询向量结果未知而未运行。">QA 成功率</abbr>：Basic 47/48 · Hybrid 47/48</span>
          <span>有效评分样本：n=45</span>
        </div>
        <ExperimentScoreChart :sample-size="45" judge-protocol="stage16a5-compact-answer-judge-v1" basic-label="Basic" hybrid-label="Hybrid" :accuracy-basic="3.422222222222222" :accuracy-hybrid="3.8666666666666667" :accuracy-basic-pct="85.6" :accuracy-hybrid-pct="96.7" :accuracy-delta-pp="11.1" :faithfulness-basic="3.7555555555555555" :faithfulness-hybrid="3.933333333333333" :faithfulness-basic-pct="93.9" :faithfulness-hybrid-pct="98.3" :faithfulness-delta-pp="4.4" />

        <div class="evaluation-table-wrap"><table class="evaluation-table"><thead><tr><th>指标 / LLM Judge 0–4 <abbr class="metric-help" tabindex="0" title="百分比是原始0–4均分除以4后归一化；不是人工答对率或事实支持率。Accuracy评价答案关键事实；Faithfulness评价事实性声明是否受证据支持。">i</abbr></th><th>Basic · n=45</th><th>Hybrid · n=45</th><th>配对差（分 / pp）</th><th>改善 / 不变 / 退化</th></tr></thead><tbody><tr><th>Answer Accuracy</th><td>85.6%（LLM Judge 3.422/4分）</td><td>96.7%（LLM Judge 3.867/4分）</td><td>+0.444 / +11.1 pp</td><td>11 / 33 / 1</td></tr><tr><th>Faithfulness</th><td>93.9%（LLM Judge 3.756/4分）</td><td>98.3%（LLM Judge 3.933/4分）</td><td>+0.178 / +4.4 pp</td><td>6 / 38 / 1</td></tr></tbody></table></div>

        <div class="evaluation-detail"><strong>Verifier <abbr class="metric-help" tabindex="0" title="程序对每个 Claim 及其引用证据运行的自动支持判断；状态不是独立金标准，也不能直接作为答案 Faithfulness 分数。">i</abbr></strong><span>217 Claims：有证据支持 172 · 未核实 43 · 部分支持 2。</span></div>
        <div class="evaluation-detail"><strong>Repair <abbr class="metric-help" tabindex="0" title="只统计实际触发并有修复前后有效评分的样本；答案变化本身不代表质量改善。">i</abbr></strong><span>触发 2 次；两组 Accuracy 与 Faithfulness 均为 4→4，无评分变化。</span></div>
        <div class="evaluation-detail"><strong>调用与耗时</strong><span>Chat 143 次；累计 Provider Chat 时长 1,873.4 秒（非端到端延迟）。Judge 49 次，其中成功 47 次；Judge Token 121,752。QA Token 与费用 unknown。</span></div>

        <details class="evaluation-provenance"><summary>版本、来源与 SHA-256</summary>
          <dl><dt>Judge 协议</dt><dd>{{ sources.stage16a5.protocol }}</dd><dt>开发集快照</dt><dd>{{ sources.stage16a5.datasetSha }}</dd><dt>汇总文件</dt><dd>{{ sources.stage16a5.summaryPath }}</dd><dt>汇总 SHA-256</dt><dd>{{ sources.stage16a5.summary }}</dd><dt>报告 SHA-256</dt><dd>{{ sources.stage16a5.reportSha }}</dd><dt>最新指针</dt><dd>{{ sources.stage16a5.latest }}</dd><dt>Manifest SHA-256</dt><dd>{{ sources.stage16a5.manifestSha }}</dd><dt>Manifest</dt><dd>{{ sources.stage16a5.manifest }}</dd><dt>报告</dt><dd>{{ sources.stage16a5.report }}</dd></dl>
          <div class="evaluation-sha-actions"><button @click="copySha('stage16a5', 'summary')">{{ copied === 'stage16a5-summary' ? '汇总 SHA 已复制' : '复制汇总 SHA' }}</button><button @click="copySha('stage16a5', 'reportSha')">{{ copied === 'stage16a5-reportSha' ? '报告 SHA 已复制' : '复制报告 SHA' }}</button><button @click="openReport('stage16a5')">查看报告摘要</button><span v-if="copyError" role="status">复制失败；SHA 已在上方显示。</span></div>
        </details>
      </article>

      <article class="evaluation-card">
        <div class="evaluation-card-head"><div><h2>Stage 16A6 · 独立文本测试</h2><p>Basic vs Hybrid + Verifier/Repair 联合流程 · cited-evidence Judge v4</p></div><span>独立测试</span></div>
        <div class="evaluation-stats">
          <div><small>条件终态 <abbr class="metric-help" tabindex="0" title="30 道测试题分别运行 Basic 与整合 Hybrid 条件，共 60 个运行条件；失败与未知也计入终态分母。">i</abbr></small><strong>60 / 60</strong></div>
          <div><small>QA 成功 <abbr class="metric-help" tabindex="0" title="Basic 成功 23/30；Hybrid+Verifier/Repair 联合流程成功 25/30，合计 48/60。">i</abbr></small><strong>48 / 60</strong></div>
          <div><small>完整配对 / 有效评分 <abbr class="metric-help" tabindex="0" title="19 道题有两种条件的完整 QA；其中 17 题获得有效 Judge 配对评分。">i</abbr></small><strong>19 / 17</strong></div>
        </div>
        <div class="evaluation-substats">
          <span>QA 成功率：Basic 23/30 · 整合流程 25/30</span>
          <span><abbr class="metric-help" tabindex="0" title="未被正式人工 qrels 判定的候选保持未知；不把它们当成无关文档。">unjudged</abbr> 不计作无关 · <abbr class="metric-help" tabindex="0" title="请求可能已到达服务端但客户端未取得确定结果；不自动当作失败后重发。">outcome_unknown</abbr> 保留未知</span>
        </div>
        <ExperimentScoreChart :sample-size="17" judge-protocol="cited-evidence-judge-v4" basic-label="Basic" hybrid-label="Hybrid + 核验/修复" :accuracy-basic="3.411764705882353" :accuracy-hybrid="3.588235294117647" :accuracy-basic-pct="85.3" :accuracy-hybrid-pct="89.7" :accuracy-delta-pp="4.4" :faithfulness-basic="3.764705882352941" :faithfulness-hybrid="4" :faithfulness-basic-pct="94.1" :faithfulness-hybrid-pct="100" :faithfulness-delta-pp="5.9" />

        <div class="evaluation-table-wrap"><table class="evaluation-table"><thead><tr><th>指标 / LLM Judge 0–4 <abbr class="metric-help" tabindex="0" title="百分比是原始0–4均分除以4后归一化；不是人工答对率或事实支持率。此处比较Basic与Hybrid+Verifier/Repair联合流程，不能拆分单独模块贡献。">i</abbr></th><th>Basic · n=17</th><th>联合流程 · n=17</th><th>配对差（分 / pp）</th><th>改善 / 不变 / 退化</th></tr></thead><tbody><tr><th>Answer Accuracy</th><td>85.3%（LLM Judge 3.412/4分）</td><td>89.7%（LLM Judge 3.588/4分）</td><td>+0.176 / +4.4 pp</td><td>2 / 15 / 0</td></tr><tr><th>Faithfulness</th><td>94.1%（LLM Judge 3.765/4分）</td><td>100.0%（LLM Judge 4.000/4分）</td><td>+0.235 / +5.9 pp</td><td>1 / 16 / 0</td></tr></tbody></table></div>

        <div class="evaluation-detail"><strong>Verifier <abbr class="metric-help" tabindex="0" title="报告中的 Claim 状态是系统自动核验输出；没有独立逐 Claim 金标准，无法据此计算误判率。">i</abbr></strong><span>99 Claims：有证据支持 84 · 未核实 15；无独立逐 Claim 金标准。</span></div>
        <div class="evaluation-detail"><strong>Repair <abbr class="metric-help" tabindex="0" title="Repair 仅触发一次且在修订/复检时失败；因此没有成功修订样本可用于质量收益判断。">i</abbr></strong><span>触发 1 次；修订/复检以 ChatProviderError 结束，未证明质量收益。</span></div>
        <div class="evaluation-detail"><strong>调用与延迟</strong><span>Basic 23/30；整合流程 25/30。终态任务均值：Basic 12.795 秒、整合流程 21.797 秒（各 n=30，包含失败/未知）。Chat 102 次；查询 Embedding 31 次；Judge Token 186,507。QA Token 与费用 unknown。</span></div>

        <details class="evaluation-provenance"><summary>版本、来源与 SHA-256</summary>
          <dl><dt>Judge 协议</dt><dd>{{ sources.stage16a6.protocol }}</dd><dt>运行快照 SHA-256</dt><dd>{{ sources.stage16a6.datasetSha }}</dd><dt>实验协议 SHA-256</dt><dd>{{ sources.stage16a6.protocolSha }}</dd><dt>汇总文件</dt><dd>{{ sources.stage16a6.summaryPath }}</dd><dt>汇总 SHA-256</dt><dd>{{ sources.stage16a6.summary }}</dd><dt>报告 SHA-256</dt><dd>{{ sources.stage16a6.reportSha }}</dd><dt>最新指针</dt><dd>{{ sources.stage16a6.latest }}</dd><dt>Manifest SHA-256</dt><dd>{{ sources.stage16a6.manifestSha }}</dd><dt>Manifest</dt><dd>{{ sources.stage16a6.manifest }}</dd><dt>报告</dt><dd>{{ sources.stage16a6.report }}</dd></dl>
          <div class="evaluation-sha-actions"><button @click="copySha('stage16a6', 'summary')">{{ copied === 'stage16a6-summary' ? '汇总 SHA 已复制' : '复制汇总 SHA' }}</button><button @click="copySha('stage16a6', 'reportSha')">{{ copied === 'stage16a6-reportSha' ? '报告 SHA 已复制' : '复制报告 SHA' }}</button><button @click="openReport('stage16a6')">查看报告摘要</button><span v-if="copyError" role="status">复制失败；SHA 已在上方显示。</span></div>
        </details>
      </article>
    </div>

    <details class="evaluation-limit"><summary>限制与口径</summary><ul><li>归一化百分比 = LLM Judge 原始 0–4 均分 ÷ 4 × 100；不是人工答案正确率或事实支持率。百分点差由未舍入的原始均值计算。</li><li>Judge 是自动评分，不等于人工评审；两阶段协议不同，不能合并均值。Faithfulness 归一化到 100.0% 不证明没有幻觉。</li><li>Stage 16A6 比较 Basic 与 Hybrid+Verifier/Repair 联合流程，不能拆分各模块的质量贡献。</li><li>Stage 16A5 正式 qrels 覆盖不足；Stage 16A6 正式 qrels 为 0。不报告正式 Precision@k、Recall@k、MRR 或 nDCG。</li><li>Repair 尚无经有效评分验证的质量改善。失败、未知和未评分样本保留在各自分母或状态中。</li><li>QA Token 与服务费用不可可靠取得时标记 unknown；unjudged 不作为负例，outcome_unknown 不自动重放。</li></ul></details>

    <dialog ref="reportDialog" class="evaluation-report-dialog" @click.self="reportDialog?.close()">
      <header><h2>{{ reportTitle }}</h2><button autofocus aria-label="关闭报告" @click="reportDialog?.close()">关闭</button></header>
      <pre>{{ reportText }}</pre>
    </dialog>
  </section>
</template>
