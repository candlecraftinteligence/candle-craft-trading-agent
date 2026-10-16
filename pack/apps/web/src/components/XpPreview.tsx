import type { XpLine } from "../domain/xp";

type XpPreviewProps = {
  lines: XpLine[];
  total: number;
};

export function XpPreview({ lines, total }: XpPreviewProps) {
  return (
    <section className="panel" aria-label="Pack XP preview" data-testid="xp-preview">
      <div className="panel-head">
        <p className="kicker">Cosmetic Pack XP</p>
        <p className="readout xp-total">{total}</p>
      </div>
      <ul className="rank-list">
        {lines.length === 0 ? (
          <li className="fine">No marks on this device yet.</li>
        ) : (
          lines.map((line) => (
            <li key={line.id} className="rank-item">
              <span>{line.label}</span>
              <span>+{line.amount}</span>
            </li>
          ))
        )}
      </ul>
      <p className="fine">A cosmetic climb. No cash value. This is not a ledger, and it is not profit.</p>
    </section>
  );
}
