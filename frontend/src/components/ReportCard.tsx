/**
 * 把 RCA（根因分析）报告渲染成卡片：根因假设按置信度排序，每个假设下列出证据和出处。
 *
 * 设计要点：每条证据都显示 reference（commit sha、日志原文、runbook 编号），
 * 这样工程师可以自己去核对，而不是只能相信模型的转述。这是整个项目"证据优先"的体现。
 */
import type { Evidence, Hypothesis, RCAReport } from "@/lib/types";

export function ReportCard({ report }: { report: RCAReport }) {
  return (
    <section className="report">
      <header className="report-header">
        <h3>Root cause analysis · {report.incident_id}</h3>
        <p>{report.summary}</p>
      </header>
      {report.hypotheses.map((hypothesis, index) => (
        <HypothesisView key={index} hypothesis={hypothesis} rank={index + 1} />
      ))}
    </section>
  );
}

function HypothesisView({ hypothesis, rank }: { hypothesis: Hypothesis; rank: number }) {
  const percent = Math.round(hypothesis.confidence * 100);
  return (
    <article className="hypothesis">
      <div className="hypothesis-title">
        <span className="rank">#{rank}</span>
        <strong>{hypothesis.root_cause}</strong>
      </div>
      <div className="confidence" title={`Confidence ${percent}%`}>
        {/* 用一个宽度等于百分比的色块画进度条。style 里写的是内联 CSS，宽度是运行时算出来的。 */}
        <div className="confidence-bar" style={{ width: `${percent}%` }} />
        <span>{percent}% confidence</span>
      </div>

      <h4>Evidence</h4>
      <ul className="evidence">
        {hypothesis.evidence.map((item, index) => (
          <EvidenceView key={index} evidence={item} />
        ))}
      </ul>

      {hypothesis.suggested_next_steps.length > 0 && (
        <>
          <h4>Suggested next steps</h4>
          <ul>
            {hypothesis.suggested_next_steps.map((step, index) => (
              <li key={index}>{step}</li>
            ))}
          </ul>
        </>
      )}
    </article>
  );
}

function EvidenceView({ evidence }: { evidence: Evidence }) {
  return (
    <li>
      <span className={`badge badge-${evidence.source}`}>{evidence.source}</span> {evidence.summary}
      <div className="reference">
        <code>{evidence.reference}</code>
        {evidence.observed_at && <time> · {new Date(evidence.observed_at).toISOString().replace("T", " ").slice(0, 19)} UTC</time>}
      </div>
    </li>
  );
}
