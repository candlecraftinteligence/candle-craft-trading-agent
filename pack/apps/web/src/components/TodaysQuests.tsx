import { Link } from "react-router-dom";
import { useServerQuests, type ServerQuest } from "../api/quests";

export function TodaysQuests() {
  const board = useServerQuests();
  const daily = board?.daily ?? [];
  const weekly = board?.weekly ?? [];
  return (
    <section className="panel" aria-label="Today's drills">
      <div className="panel-head">
        <p className="kicker">Today's drills</p>
        <p className="fine">{board ? String(daily.length) : "N/A"}</p>
      </div>
      {board ? (
        <ul className="quest-list">
          {daily.map((quest) => (
            <QuestRow key={quest.code} quest={quest} />
          ))}
        </ul>
      ) : (
        <p className="fine">Drills N/A until the den answers.</p>
      )}
      {weekly.length > 0 ? (
        <>
          <p className="kicker">This week</p>
          <ul className="quest-list">
            {weekly.map((quest) => (
              <QuestRow key={quest.code} quest={quest} />
            ))}
          </ul>
        </>
      ) : null}
    </section>
  );
}

function QuestRow({ quest }: { quest: ServerQuest }) {
  return (
    <li className="quest-row" data-done={quest.completed ? "true" : "false"}>
      <div>
        <p className="section-title">{quest.title}</p>
        <p className="fine">
          {quest.detail} {quest.progress}/{quest.target}
        </p>
      </div>
      <Link className="btn" to={quest.href}>
        {quest.completed ? "Logged" : "Go"}
      </Link>
    </li>
  );
}
