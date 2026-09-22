import { Crest } from "./Crest";

export function Wordmark() {
  return (
    <div className="wordmark">
      <span className="wordmark-cci">
        C
        <Crest size={22} />
        CI
      </span>
      <span className="wordmark-pack">The Pack</span>
    </div>
  );
}
