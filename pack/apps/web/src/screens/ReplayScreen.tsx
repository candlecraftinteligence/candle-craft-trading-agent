import { REPLAY_DISCLAIMER } from "../copy";

export function ReplayScreen() {
  return (
    <div className="stack">
      <header>
        <p className="kicker">Training</p>
        <h1 className="display">Replay Vault</h1>
      </header>
      <section className="panel">
        <p className="kicker">Closed setups</p>
        <p className="quiet-copy">The drill opens in a later slice.</p>
        <p className="body-copy">{REPLAY_DISCLAIMER}</p>
      </section>
    </div>
  );
}
