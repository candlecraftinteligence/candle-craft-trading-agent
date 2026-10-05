import type { LifecycleEvent } from "../api/types";
import { toneForState } from "../presentation";

type LifecycleTimelineProps = {
  events: LifecycleEvent[];
};

export function LifecycleTimeline({ events }: LifecycleTimelineProps) {
  if (events.length === 0) {
    return <p className="status-line">No lifecycle events were included in this fixture.</p>;
  }

  return (
    <ol className="timeline">
      {events.map((event, index) => (
        <li
          key={event.cci_event_id}
          className={index === events.length - 1 ? "timeline-item is-latest" : "timeline-item"}
        >
          <div className="event-type">{event.event_type}</div>
          <p className="timeline-state">
            <span className="state-chip" data-tone={toneForState(event.state)}>
              {event.state}
            </span>
          </p>
          <p className="fine">{event.occurred_at}</p>
          {event.outcome_code ? <p className="fine">Outcome code {event.outcome_code}</p> : null}
          <p className="fine event-id">{event.cci_event_id}</p>
        </li>
      ))}
    </ol>
  );
}
