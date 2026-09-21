import { Link } from "react-router-dom";
import type { DecisionId } from "../decisions/localDecisions";
import { todayKey, todaysQuests, type QuestTemplate } from "../domain/quests";
import type { JournalRecord } from "../storage/journals";
import type { ReplayAttempt } from "../storage/replays";

type TodaysQuestsProps = {
  decisions: Record<string, DecisionId>;
  journals: Record<string, JournalRecord>;
  replays: Record<string, ReplayAttempt>;
  evidenceReads: string[];
};

export function TodaysQuests({ decisions, journals, replays, evidenceReads }: TodaysQuestsProps) {
  const quests = todaysQuests(todayKey());
  return (
    <section className="panel" aria-label="Today's drills">
      <div className="panel-head">
        <p className="kicker">Today's drills</p>
        <p className="fine">3 on the board</p>
      </div>
      <ul className="quest-list">
        {quests.map((quest) => {
          const done = isQuestDone(quest, { decisions, journals, replays, evidenceReads });
          return (
            <li key={quest.id} className="quest-row" data-done={done ? "true" : "false"}>
              <div>
                <p className="section-title">{quest.title}</p>
                <p className="fine">{quest.detail}</p>
              </div>
              <Link className="btn" to={quest.href}>
                {done ? "Logged" : "Go"}
              </Link>
            </li>
          );
        })}
      </ul>
    </section>
  );
}

function isQuestDone(
  quest: QuestTemplate,
  input: TodaysQuestsProps,
): boolean {
  const decisions = Object.values(input.decisions);
  switch (quest.id) {
    case "read-lock":
      return decisions.length > 0;
    case "no-trade":
      return decisions.includes("NO_TRADE");
    case "journal":
      return Object.keys(input.journals).length > 0;
    case "replay":
      return Object.keys(input.replays).length > 0;
    case "evidence":
      return input.evidenceReads.length > 0;
  }
}
