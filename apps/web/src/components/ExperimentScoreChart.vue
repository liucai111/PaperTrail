<script setup lang="ts">
defineProps<{
  sampleSize: number
  judgeProtocol: string
  basicLabel: string
  hybridLabel: string
  accuracyBasic: number
  accuracyHybrid: number
  faithfulnessBasic: number
  faithfulnessHybrid: number
  accuracyBasicPct: number
  accuracyHybridPct: number
  accuracyDeltaPp: number
  faithfulnessBasicPct: number
  faithfulnessHybridPct: number
  faithfulnessDeltaPp: number
}>()

const rows = (props: {
  accuracyBasic: number; accuracyHybrid: number
  faithfulnessBasic: number; faithfulnessHybrid: number
  accuracyBasicPct: number; accuracyHybridPct: number; accuracyDeltaPp: number
  faithfulnessBasicPct: number; faithfulnessHybridPct: number; faithfulnessDeltaPp: number
}) => [
  { label: 'Answer Accuracy', basic: props.accuracyBasic, hybrid: props.accuracyHybrid, basicPct: props.accuracyBasicPct, hybridPct: props.accuracyHybridPct, deltaPp: props.accuracyDeltaPp },
  { label: 'Faithfulness', basic: props.faithfulnessBasic, hybrid: props.faithfulnessHybrid, basicPct: props.faithfulnessBasicPct, hybridPct: props.faithfulnessHybridPct, deltaPp: props.faithfulnessDeltaPp },
]
</script>

<template>
  <figure class="score-chart" :aria-label="`0 到 4 分量表，${basicLabel} 与 ${hybridLabel} 对比，有效配对 n=${sampleSize}`">
    <figcaption>
      <span>满分归一化百分比 · 0–4 Judge 量表</span>
      <span>n={{ sampleSize }} · {{ judgeProtocol }}</span>
    </figcaption>
    <div v-for="metric in rows({ accuracyBasic, accuracyHybrid, faithfulnessBasic, faithfulnessHybrid, accuracyBasicPct, accuracyHybridPct, accuracyDeltaPp, faithfulnessBasicPct, faithfulnessHybridPct, faithfulnessDeltaPp })" :key="metric.label" class="score-chart-metric">
      <strong>{{ metric.label }}</strong>
      <div class="score-chart-rows">
        <div class="score-chart-row">
          <span class="score-chart-series">{{ basicLabel }}</span>
          <div class="score-chart-track" role="img" :aria-label="`${metric.label} ${basicLabel}: ${metric.basic.toFixed(3)} 分（满分 4）`">
            <span class="score-chart-bar score-chart-bar-basic" :style="{ width: `${Math.max(0, Math.min(100, metric.basic / 4 * 100))}%` }"></span>
          </div>
          <span class="score-chart-value">{{ metric.basicPct.toFixed(1) }}%（LLM Judge {{ metric.basic.toFixed(3) }}/4分）</span>
        </div>
        <div class="score-chart-row">
          <span class="score-chart-series">{{ hybridLabel }}</span>
          <div class="score-chart-track" role="img" :aria-label="`${metric.label} ${hybridLabel}: ${metric.hybrid.toFixed(3)} 分（满分 4）`">
            <span class="score-chart-bar score-chart-bar-hybrid" :style="{ width: `${Math.max(0, Math.min(100, metric.hybrid / 4 * 100))}%` }"></span>
          </div>
          <span class="score-chart-value">{{ metric.hybridPct.toFixed(1) }}%（LLM Judge {{ metric.hybrid.toFixed(3) }}/4分）</span>
        </div>
      </div>
      <small class="score-chart-delta">差值：{{ metric.deltaPp >= 0 ? '+' : '' }}{{ metric.deltaPp.toFixed(1) }} 个百分点</small>
    </div>
    <div class="score-chart-axis" aria-hidden="true"><span>0</span><span>2</span><span>4</span></div>
  </figure>
</template>
